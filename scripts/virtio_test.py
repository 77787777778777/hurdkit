#!/usr/bin/env python3
"""Drive the virtio-9p test matrix in the guest.

scripts/virtio_test.sh boots the guest with a host directory shared through
QEMU's virtio-9p device; this script only needs the guest over SSH. It
installs the two translators, runs the Phase 1 selftest, mounts the share
with the unmodified 9pfs front end, and runs the Phase 2/3 assertions,
printing one PASS/FAIL line per assertion.

Configuration comes from the environment:

    SSH_HOST          host the guest's SSH port is forwarded to
    SSH_PORT          the forwarded SSH port
    SSH_KEY           private key of the guest's root account
    VIRTIO_9P_BIN     host path of the virtio-9p translator
    P9FS_BIN          host path of the unmodified 9pfs translator
    SHARE             host path of the shared directory
    QEMU_LOG          path of the QEMU serial log (dumped on failure)
    GDB               "1" installs gdb in the guest and attaches it to the
                      stuck processes when the matrix fails

Modes (argv[1]):

    main     the full matrix, with the device attached
    nodev    the no-device negative test, without the device attached
    stress   add/exercise/remove the driver in a loop to churn the
             registration/teardown path

Every command runs with a bounded timeout, so a stuck translator turns into
a FAIL line instead of a hang. On failure the guest is left alone for
inspection: scripts/virtio_test.sh keeps QEMU running and points at the
log and the SSH command.

Exit codes: 0 every assertion passed, 1 at least one failed.
"""

import os
import re
import subprocess
import sys

try:
    import pexpect
except ImportError:
    sys.exit("pexpect is required (pip install pexpect)")

SSH_HOST = os.environ.get("SSH_HOST", "127.0.0.1")
SSH_PORT = int(os.environ.get("SSH_PORT", "2222"))
SSH_KEY = os.environ.get("SSH_KEY", "img/keys/root_ed25519")
VIRTIO_9P_BIN = os.environ["VIRTIO_9P_BIN"]
P9FS_BIN = os.environ["P9FS_BIN"]
SHARE = os.environ["SHARE"]
MOUNT_TAG = os.environ.get("MOUNT_TAG", "hostshare")
QEMU_LOG = os.environ.get("QEMU_LOG", "")
WANT_GDB = os.environ.get("GDB", "0") == "1"

LOGIN_TIMEOUT = 120
# sshd comes up well after QEMU starts accepting connections, and pexpect
# dies on the first reset instead of waiting, so the login itself is
# retried: each attempt gets a fresh ssh process and a short budget.
LOGIN_ATTEMPTS = 5
LOGIN_ATTEMPT_TIMEOUT = 30
# The default per-command budget: long enough for anything legitimate,
# short enough that a stuck translator shows up as a failure in minutes
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


def record(name, ok, detail=""):
    results.append((name, ok))
    line = f"{'PASS' if ok else 'FAIL'}: {name}"
    if detail:
        line += f" -- {detail}"
    print(line, flush=True)


def install(session, local, remote):
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


def qemu_log_tail():
    if not QEMU_LOG or not os.path.exists(QEMU_LOG):
        return ""
    with open(QEMU_LOG, errors="replace") as f:
        return "".join(f.readlines()[-40:])


def dump_guest_state(session):
    """Print what the guest looks like after a failure, for the log."""
    print("--- guest state after failure ---", flush=True)
    for name, command in (
        ("ps", "ps"),
        ("device node", "settrans -g /dev/virtio-9p0 || true"),
        ("mount", "settrans -g /host || true"),
        ("fsysopts", "fsysopts /dev/virtio-9p0 /host || true"),
        ("selftest", "/hurd/virtio-9p --selftest || true"),
    ):
        try:
            status, out = session.run(command, timeout=30)
            print(f"--- {name} (rc={status}) ---\n{out}", flush=True)
        except GuestError as exc:
            print(f"--- {name}: {exc} ---", flush=True)
    if WANT_GDB:
        gdb_dump(session)


def gdb_dump(session):
    """Attach gdb to the stuck translators and print their stacks.

    The Hurd runs every translator as an ordinary process, so an in-guest
    gdb sees the userspace stacks directly; that is the shortest path from
    "the command hung" to "this RPC is stuck here".
    """
    session.run("command -v gdb >/dev/null || apt-get install -y gdb",
                timeout=600)
    status, out = session.run(
        "ps | awk '/hurd\\/(virtio-9p|9pfs)/ && !/awk/ {print $1}'")
    for pid in out.split():
        try:
            _, bt = session.run(
                f"gdb -p {pid} -batch -ex 'thread apply all bt' 2>&1",
                timeout=120)
            print(f"--- gdb bt of pid {pid} ---\n{bt}", flush=True)
        except GuestError as exc:
            print(f"--- gdb of pid {pid} failed: {exc} ---", flush=True)


def phase1(session):
    status, out = session.run("/hurd/virtio-9p --selftest")
    gate = all(
        line in out
        for line in (
            "mount tag feature negotiated",
            "Rversion ok",
            "msize=4186112",
            "version=9P2000.L",
        )
    )
    record(
        "phase1 selftest gate",
        status == 0 and gate,
        f"rc={status}, output: {out.strip()[:2000]}",
    )
    if not (status == 0 and gate):
        # Everything below mounts through this device; without it the
        # assertions would only measure the hang.
        print("phase1 failed, skipping the mount phases", flush=True)
        return False

    status, _ = session.run("/hurd/virtio-9p --selftest --mount-tag=wrong")
    record("phase1 wrong mount tag rejected", status != 0, f"rc={status}")
    return True


def phase2(session):
    # Idempotent start: a reused guest may still hold translators from an
    # earlier run; removing them first (the empty-translator form prints an
    # ERANGE complaint but does remove the record and goaway the active
    # translator) makes the fresh settrans below deterministic.
    session.run("settrans -c /host '' || true")
    session.run("settrans -c /dev/virtio-9p0 '' || true")
    session.run("mkdir -p /host")
    status, out = session.run(
        "settrans -c /dev/virtio-9p0 /hurd/virtio-9p --mount-tag=" + MOUNT_TAG
    )
    record("phase2 device node settrans", status == 0,
           f"rc={status}, {out.strip()[-500:]}")
    status, out = session.run("settrans -c /host /hurd/9pfs /dev/virtio-9p0")
    record("phase2 9pfs mount", status == 0,
           f"rc={status}, {out.strip()[-500:]}")

    status, _ = session.run("echo hello > /host/greeting.txt")
    record("phase2 guest write", status == 0, f"rc={status}")
    try:
        with open(os.path.join(SHARE, "greeting.txt"), encoding="utf-8") as f:
            content = f.read()
    except OSError as exc:
        content = f"<{exc}>"
    record("phase2 guest write visible on host", content.strip() == "hello",
           content)
    status, out = session.run("cat /host/greeting.txt")
    record("phase2 read back", status == 0 and out.strip() == "hello",
           out.strip())

    status, out = session.run("cat /host/host-seed.txt")
    record(
        "phase2 host file visible in guest",
        status == 0 and out.strip() == "from host",
        out.strip(),
    )


def phase3(session):
    # creation both ways, nested directories
    status, _ = session.run("mkdir -p /host/a/b/c && echo x > /host/a/b/c/f")
    record("phase3 nested creation", status == 0, f"rc={status}")
    record(
        "phase3 nested creation on host",
        os.path.exists(os.path.join(SHARE, "a", "b", "c", "f")),
    )

    # content integrity, both directions, small and large
    status, _ = session.run("dd if=/dev/urandom of=/host/big1m bs=1M count=1",
                            timeout=LONG_TIMEOUT)
    ok = status == 0 and host_md5(os.path.join(SHARE, "big1m")) == \
        session.run("md5sum /host/big1m")[1].split()[0]
    record("phase3 1 MiB guest->host", ok, f"rc={status}")

    status, _ = session.run("dd if=/dev/urandom of=/host/big64m bs=1M count=64",
                            timeout=LONG_TIMEOUT)
    ok = status == 0 and host_md5(os.path.join(SHARE, "big64m")) == \
        session.run("md5sum /host/big64m")[1].split()[0]
    record("phase3 64 MiB guest->host", ok, f"rc={status}")

    subprocess.run(
        ["dd", "if=/dev/urandom", "of=" + os.path.join(SHARE, "h1m"),
         "bs=1M", "count=1", "status=none"],
        check=True,
    )
    ok = session.run("md5sum /host/h1m")[1].split()[0] == \
        host_md5(os.path.join(SHARE, "h1m"))
    record("phase3 1 MiB host->guest", ok)

    # metadata
    status, out = session.run("stat -c %s /host/big1m")
    record("phase3 stat size", status == 0 and out.strip() == "1048576",
           out.strip())
    session.run("chmod 600 /host/greeting.txt")
    status, out = session.run("stat -c %a /host/greeting.txt")
    record("phase3 chmod", status == 0 and out.strip() == "600", out.strip())
    session.run("touch /host/greeting.txt")

    # symlinks: the unmodified 9pfs front end implements no symlink RPCs,
    # so creation must be refused cleanly (EOPNOTSUPP) and the driver must
    # keep serving afterwards.
    session.run("ln -s greeting.txt /host/link")
    status, out = session.run("readlink /host/link")
    record("phase3 symlink refused by front end", status != 0, f"rc={status}")
    status, _ = session.run("echo after-symlink > /host/ok")
    record("phase3 translator alive after refused symlink", status == 0,
           f"rc={status}")
    session.run("rm -f /host/ok")
    session.run("mv /host/link /host/link2")
    session.run("rm /host/link2")
    session.run("mkdir /host/dir && rmdir /host/dir")
    status, _ = session.run("test ! -e /host/link2 && test ! -e /host/dir")
    record("phase3 rename/unlink/rmdir", status == 0, f"rc={status}")

    # readdir of >1000 entries (ring wrap)
    session.run("mkdir -p /host/many")
    session.run("for i in $(seq 1 1001); do : > /host/many/f$i; done",
                timeout=LONG_TIMEOUT)
    status, out = session.run("ls /host/many | wc -l")
    record("phase3 readdir 1001 entries",
           status == 0 and out.strip() == "1001", out.strip())

    # concurrent readers/writers
    session.run(
        "(dd if=/dev/urandom of=/host/conc1 bs=1M count=4 & "
        "dd if=/dev/urandom of=/host/conc2 bs=1M count=4 & wait)",
        timeout=LONG_TIMEOUT,
    )
    ok = all(
        session.run(f"md5sum /host/conc{n}")[1].split()[0]
        == host_md5(os.path.join(SHARE, f"conc{n}"))
        for n in (1, 2)
    )
    record("phase3 two concurrent writers", ok)

    # single-open discipline: the mounted node refuses a second open
    status, _ = session.run("exec 3<>/dev/virtio-9p0")
    record("phase3 second open refused", status != 0, f"rc={status}")

    # translator restart: unmount, remove, re-set, re-mount.  The removal
    # form is `settrans -c NODE ''` (empty translator), which prints an
    # ERANGE complaint but does remove the record and goaway the active
    # translator.
    session.run("settrans -c /host ''")
    session.run("settrans -c /dev/virtio-9p0 ''")
    status, out = session.run(
        "ps | awk '/hurd\\/(virtio-9p|9pfs)/ && !/awk/ {print $1}'")
    record("phase3 restart: no translator processes left",
           status == 0 and not out.strip(), out.strip())
    status, out = session.run("settrans -g /dev/virtio-9p0 || true")
    record("phase3 restart: node has no active translator",
           status == 0 and not out.strip(), out.strip())
    session.run("settrans -c /dev/virtio-9p0 /hurd/virtio-9p --mount-tag="
                + MOUNT_TAG)
    session.run("settrans -c /host /hurd/9pfs /dev/virtio-9p0")
    status, out = session.run("cat /host/greeting.txt")
    record("phase3 restart: mount works again",
           status == 0 and out.strip() == "hello", out.strip())


def netdde_cpu_seconds (session):
    """netdde's total CPU time, the bill for any shared-line storm."""
    status, out = session.run(
        "ps -A -o pid=,time=,comm= | awk '$3 == \"netdde\" {print $2}'",
        timeout=30)
    m = re.search(r"([0-9]+):([0-9.]+)", out)
    if not m:
        return None
    return int(m.group(1)) * 60 + float(m.group(2))


def phase_stress (session, cycles):
    """Add, exercise and remove the driver repeatedly on the live line.

    The main matrix mounts once and removes the driver once, so the
    registration/teardown path (ACPI IRQ walk, entry insert, dead-port
    compensation) runs at most twice per boot.  This mode hammers it:
    every cycle installs the translator, forces one real 9P exchange each
    way through the unmodified 9pfs front end, and removes both
    translators again.  With the default VIRTIO_ADDR the device sits on
    its own IRQ line; pass VIRTIO_ADDR= to the harness to put it back on
    the e1000's line, where gnumach intermittently deadlocks the shared
    line under this churn (IOAPIC pin masked with IRR latched; both
    devices starve).  That shared-line wedge is an open gnumach/qemu
    issue, not a driver bug, so the ping flood only matters in the shared
    configuration.  netdde CPU staying flat across the run is the storm
    detector.
    """
    session.run("mkdir -p /host")
    session.run("settrans -c /host '' || true")
    session.run("settrans -c /dev/virtio-9p0 '' || true")

    before = netdde_cpu_seconds (session)
    record("stress netdde baseline", before is not None,
           "netdde not running" if before is None else f"{before:.2f}s")
    if before is None:
        return
    if session.run("command -v ping", timeout=30)[0] == 0:
        # The & has to live inside the nested sh: this harness appends
        # ` ; }` to every command, and `& ;` is a bash syntax error that
        # swallows the __RC__ marker and hangs the session.
        session.run(f"sh -c 'setsid ping -c {cycles * 4} 10.0.2.2 "
                    ">/dev/null 2>&1 </dev/null &'", timeout=30)

    for i in range(cycles):
        session.run("settrans -c /dev/virtio-9p0 /hurd/virtio-9p "
                    "--mount-tag=" + MOUNT_TAG, timeout=60)
        session.run("settrans -c /host /hurd/9pfs /dev/virtio-9p0",
                    timeout=60)
        # ls + dd force the active start and one real PDU exchange each way.
        session.run("ls /host >/dev/null && dd if=/dev/urandom of=/host/s "
                    "bs=1024 count=1", timeout=60)
        session.run("settrans -c /host '' || true", timeout=60)
        session.run("settrans -c /dev/virtio-9p0 '' || true", timeout=60)
        status, out = session.run(
            "ps | awk '/hurd\\/(virtio-9p|9pfs)/ && !/awk/ {print $1}'",
            timeout=30)
        status2, out2 = session.run("settrans -g /dev/virtio-9p0 || true",
                                    timeout=30)
        record(f"stress cycle {i + 1} add/exchange/remove",
               status == 0 and not out.strip()
               and status2 == 0 and not out2.strip(),
               f"processes: {out.strip()!r}, node: {out2.strip()!r}")

    after = netdde_cpu_seconds (session)
    record("stress netdde cpu flat",
           after is not None and after - before < 3.0,
           f"netdde {before:.2f}s -> {after:.2f}s over {cycles} cycles")


def nodev(session):
    status, out = session.run("/hurd/virtio-9p --selftest")
    record("negative: no device, clean exit", status != 0,
           f"rc={status}, {out.strip().splitlines()[0] if out.strip() else ''}")


def stress(session):
    install(session, VIRTIO_9P_BIN, "/tmp/virtio-9p")
    install(session, P9FS_BIN, "/tmp/9pfs")
    session.run("install -m 755 /tmp/virtio-9p /hurd/virtio-9p && "
                "install -m 755 /tmp/9pfs /hurd/9pfs")
    if not phase1(session):
        return
    phase_stress(session, int(os.environ.get("CYCLES", "15")))


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "main"
    session = None
    failed_early = False
    try:
        session = GuestSession()
        if mode == "nodev":
            nodev(session)
        elif mode == "stress":
            stress(session)
        else:
            install(session, VIRTIO_9P_BIN, "/tmp/virtio-9p")
            install(session, P9FS_BIN, "/tmp/9pfs")
            session.run("install -m 755 /tmp/virtio-9p /hurd/virtio-9p && "
                        "install -m 755 /tmp/9pfs /hurd/9pfs")
            if not phase1(session):
                failed_early = True
            else:
                phase2(session)
                phase3(session)
    except GuestError as exc:
        record("harness", False, str(exc))
        failed_early = True
    if failed_early:
        try:
            dump_guest_state(session)
        except Exception as exc:  # noqa: BLE001 - the dump must not mask the
            print(f"--- guest state dump failed: {exc} ---", flush=True)
        print("--- QEMU serial log tail ---", flush=True)
        print(qemu_log_tail(), flush=True)

    failed = [name for name, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} assertions passed",
          flush=True)
    if failed:
        print("failed: " + ", ".join(failed), flush=True)
        # Leave the guest running for inspection: the bash script reports
        # the pid, the serial log and the REUSE=1 rerun command.
        return 1
    if session is not None:
        try:
            session.poweroff()
        except Exception:  # noqa: BLE001 - poweroff is best effort
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
