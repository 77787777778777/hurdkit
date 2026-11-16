#!/usr/bin/env python3
"""Shared machinery for the virtio guest test harnesses.

The harnesses for the virtio devices (scripts/virtio_test.py for the 9p
translator, virtio_balloon_test.py, virtio_blk_test.py) all drive the same
guest over one SSH session, compare the guest's view against host-side
device-model evidence, and print one PASS/FAIL line per assertion.  This
module holds that machinery; a harness imports it and adds only its
device-specific matrix:

    import virtio_harness as vh
    ...
    vh.record("phase1 gate", ok, detail)

Configuration comes from the environment (the boot scripts export it):

    SSH_HOST / SSH_PORT  forwarded guest SSH endpoint
    SSH_KEY              private key of the guest's root account
    QEMU_LOG             path of the QEMU serial log (traces land here; it
                         is dumped on failure)
    GDB                  "1" installs gdb in the guest and dumps stacks
                         of the processes matching the harness's pattern
                         when the matrix fails

Guest-side process checks use `ps -ef`, never plain `ps`: on this guest
plain `ps` silently prints nothing (and exits 1) for processes whose TTY
column is "-", which is every detached daemon.

Every command runs with a bounded timeout, so a stuck guest turns into a
FAIL line instead of a hang.  On failure the guest is left alone for
inspection: the boot script keeps QEMU running and points at the log and
the SSH command.

Exit codes: 0 every assertion passed, 1 at least one failed.
"""

import os
import re
import socket
import subprocess
import sys
import time

try:
    import pexpect
except ImportError:
    sys.exit("pexpect is required (pip install pexpect)")

SSH_HOST = os.environ.get("SSH_HOST", "127.0.0.1")
SSH_PORT = int(os.environ.get("SSH_PORT", "2222"))
SSH_KEY = os.environ.get("SSH_KEY", "img/keys/root_ed25519")
QEMU_LOG = os.environ.get("QEMU_LOG", "")
WANT_GDB = os.environ.get("GDB", "0") == "1"

LOGIN_TIMEOUT = 120
# sshd comes up well after QEMU starts accepting connections, and pexpect
# dies on the first reset instead of waiting, so the login itself is
# retried: each attempt gets a fresh ssh process and a short budget.
LOGIN_ATTEMPTS = 5
LOGIN_ATTEMPT_TIMEOUT = 30
# The default per-command budget: long enough for anything legitimate,
# short enough that a stuck guest shows up as a failure in minutes
# instead of an hour.
COMMAND_TIMEOUT = 180
# Big copies and directory stress get their own, longer budget.
LONG_TIMEOUT = 1800

PROMPT = re.compile(r"# $")
EXIT_STATUS = re.compile(r"__RC__(-?[0-9]+)__")
# The guest's interactive bash wraps output in bracketed-paste and other
# terminal escape sequences; strip them before comparing anything.
ANSI = re.compile(
    r"\x1b\][^\x07]*\x07"          # window title
    r"|\x1b\[[0-9;?]*[a-zA-Z]"     # CSI sequences
    r"|\x1b[()][A-Z0-9]"           # charset selection
    r"|[\x00-\x08\x0b-\x1f\x7f]"   # other control chars but \t \n \r
)


def clean(text):
    return ANSI.sub("", text)


results = []


class GuestError(Exception):
    pass


class GuestSession:
    """A key-authenticated SSH session with the guest."""

    def __init__(self):
        last = "<no output>"
        for _ in range(LOGIN_ATTEMPTS):
            self.child = pexpect.spawn(
                "ssh",
                [
                    "-o", "StrictHostKeyChecking=no",
                    "-o", "UserKnownHostsFile=/dev/null",
                    "-o", "BatchMode=yes",
                    "-i", SSH_KEY,
                    "-p", str(SSH_PORT),
                    f"root@{SSH_HOST}",
                ],
                encoding="utf-8",
                timeout=LOGIN_ATTEMPT_TIMEOUT,
            )
            try:
                index = self.child.expect(
                    [PROMPT, pexpect.EOF, pexpect.TIMEOUT]
                )
            except pexpect.TIMEOUT:
                index = 2
            if index == 0:
                # Echo off, so the command line cannot be mistaken for
                # output.
                self.child.send("stty -echo\r")
                self.child.expect(PROMPT)
                return
            last = (self.child.before or "")[-200:]
            try:
                self.child.close(force=True)
            except Exception:  # noqa: BLE001 - closing a dead spawn is best effort
                pass
        raise GuestError(
            f"Could not log into the guest with the key ({LOGIN_ATTEMPTS} "
            f"attempts); last output: {last.strip()[-200:]}"
        )

    def run(self, command, timeout=COMMAND_TIMEOUT):
        """Run a command in the guest; return (status, output).

        The command runs in a group so the redirection covers it, and its
        exit status is echoed as a marker the session can match on. A
        timeout or a lost connection raises GuestError with what the guest
        printed, so the harness can report it instead of hanging.
        """
        command = command.rstrip().rstrip(";").rstrip()
        self.child.send(f"{{ {command} ; }} 2>&1; echo \"__RC__$?__\"\r")
        index = self.child.expect(
            [EXIT_STATUS, pexpect.EOF, pexpect.TIMEOUT], timeout=timeout
        )
        if index == 1:
            raise GuestError(f"Connection lost while running: {command}")
        if index == 2:
            raise GuestError(
                f"Timed out after {timeout}s while running: {command}\n"
                f"last output: {self.child.before[-2000:]}"
            )
        status = int(self.child.match.group(1))
        output = clean(self.child.before)
        self.child.expect(PROMPT)
        return status, output

    def poweroff(self):
        self.child.send("poweroff\r")
        try:
            self.child.expect(pexpect.EOF, timeout=120)
        except pexpect.TIMEOUT:
            pass


class Hmp:
    """A HMP session over the harness's monitor socket."""

    def __init__(self, path, timeout=60):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(path)
        self.buf = b""
        self._read_prompt()

    def _read_prompt(self):
        while b"(qemu)" not in self.buf:
            data = self.sock.recv(4096)
            if not data:
                raise GuestError("monitor closed")
            self.buf += data
        out, self.buf = self.buf.split(b"(qemu)", 1)
        return clean(out.decode(errors="replace"))

    def cmd(self, command):
        self.buf = b""
        self.sock.sendall((command + "\n").encode())
        return self._read_prompt()

    def balloon_mb(self):
        """Current host view of the balloon: guest RAM the host still sees."""
        out = self.cmd("info balloon")
        m = re.search(r"balloon: actual=(\d+)", out)
        if not m:
            raise GuestError(f"unparsable info balloon output: {out!r}")
        return int(m.group(1))


def record(name, ok, detail=""):
    results.append((name, ok))
    line = f"{'PASS' if ok else 'FAIL'}: {name}"
    if detail:
        line += f" -- {detail}"
    print(line, flush=True)


def install(local, remote):
    subprocess.run(
        [
            "scp", "-i", SSH_KEY, "-P", str(SSH_PORT),
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            local, f"root@{SSH_HOST}:{remote}",
        ],
        check=True,
        timeout=120,
    )


def host_md5(path):
    out = subprocess.run(
        ["md5sum", path], capture_output=True, text=True, check=True
    ).stdout
    return out.split()[0]


def qemu_log_grep(pattern):
    if not QEMU_LOG or not os.path.exists(QEMU_LOG):
        return ""
    with open(QEMU_LOG, errors="replace") as f:
        return "\n".join(
            line for line in f if re.search(pattern, line))


def qemu_log_tail():
    if not QEMU_LOG or not os.path.exists(QEMU_LOG):
        return ""
    with open(QEMU_LOG, errors="replace") as f:
        return "".join(f.readlines()[-40:])


def guest_pids(session, pattern, timeout=30):
    """PIDs of the guest processes whose command line matches pattern.

    Plain `ps` on this guest silently prints nothing for detached
    daemons; `ps -ef` lists them, with the PID in column 2.
    """
    _, out = session.run(
        f"ps -ef | awk '/{pattern}/ && !/awk/ {{print $2}}'", timeout=timeout)
    return out.split()


def netdde_cpu_seconds(session):
    """netdde's total CPU time, the bill for any shared-line storm."""
    status, out = session.run(
        "ps -A -o pid=,time=,comm= | awk '$3 == \"netdde\" {print $2}'",
        timeout=30)
    m = re.search(r"([0-9]+):([0-9.]+)", out)
    if not m:
        return None
    return int(m.group(1)) * 60 + float(m.group(2))


def dump_commands(session, checks):
    """Print what the guest looks like after a failure, for the log.

    CHECKS is a list of (name, command) pairs the harness wants in the
    failure log.
    """
    print("--- guest state after failure ---", flush=True)
    for name, command in checks:
        try:
            status, out = session.run(command, timeout=30)
            print(f"--- {name} (rc={status}) ---\n{out}", flush=True)
        except GuestError as exc:
            print(f"--- {name}: {exc} ---", flush=True)


def gdb_backtraces(session, ps_pattern, command_template):
    """Attach gdb to the processes matching ps_pattern and print stacks.

    The Hurd runs every driver as an ordinary process, so an in-guest gdb
    sees the userspace stacks directly; that is the shortest path from
    "the command hung" to "this RPC is stuck here".  COMMAND_TEMPLATE is
    formatted with the pid; pass the binary when the unwinding needs it.
    """
    session.run("command -v gdb >/dev/null || apt-get install -y gdb",
                timeout=600)
    for pid in guest_pids(session, ps_pattern):
        try:
            _, bt = session.run(
                command_template.format(pid=pid), timeout=120)
            print(f"--- gdb bt of pid {pid} ---\n{bt}", flush=True)
        except GuestError as exc:
            print(f"--- gdb of pid {pid} failed: {exc} ---", flush=True)


def finish(session):
    """Print the assertion summary; return the process exit code.

    A failed matrix leaves the guest running for inspection (the boot
    script reports the pid, the serial log and the REUSE=1 rerun
    command); a clean one powers it off -- except in a REUSE=1 run,
    whose contract is that the reused guest stays up for further
    inspection or another REUSE run.
    """
    failed = [name for name, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} assertions passed",
          flush=True)
    if failed:
        print("failed: " + ", ".join(failed), flush=True)
        return 1
    if session is not None and os.environ.get("REUSE") != "1":
        try:
            session.poweroff()
        except Exception:  # noqa: BLE001 - poweroff is best effort
            pass
    return 0
