#!/usr/bin/env python3
"""Drive the virtio-9p test matrix in the guest.

scripts/virtio_test.sh boots the guest with a host directory shared through
QEMU's virtio-9p device; this script only needs the guest over SSH. It
installs the two translators, runs the Phase 1 selftest, mounts the share
with the unmodified 9pfs front end, and runs the Phase 2/3 assertions,
printing one PASS/FAIL line per assertion.

Configuration comes from the environment:

    VIRTIO_9P_BIN     host path of the virtio-9p translator
    P9FS_BIN          host path of the unmodified 9pfs translator
    SHARE             host path of the shared directory
    MOUNT_TAG         mount tag of the fsdev (default hostshare)

    (SSH_HOST / SSH_PORT / SSH_KEY / QEMU_LOG / GDB are the shared
    harness configuration documented in scripts/virtio_harness.py.)

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
import subprocess
import sys

import virtio_harness as vh

VIRTIO_9P_BIN = os.environ["VIRTIO_9P_BIN"]
P9FS_BIN = os.environ["P9FS_BIN"]
SHARE = os.environ["SHARE"]
MOUNT_TAG = os.environ.get("MOUNT_TAG", "hostshare")

# Both translators are detached daemons; this pattern finds them in the
# guest's process table (plain `ps` on this guest would print nothing).
TRANSLATOR_PS = r"hurd\/(virtio-9p|9pfs)"


def host_md5(path):
    out = subprocess.run(
        ["md5sum", path], capture_output=True, text=True, check=True
    ).stdout
    return out.split()[0]


def translator_pids(session):
    return vh.guest_pids(session, TRANSLATOR_PS)


def remove_translators(session):
    """Detach translators from their nodes and kill the orphaned daemons.

    `settrans -c NODE ''` removes the node registration (and goaways the
    active translator) but does not kill the standalone daemon process;
    the daemon lingers as an orphan.  Kill whatever is left so the next
    `settrans -c ... /hurd/virtio-9p ...` starts from a clean process
    table rather than accumulating strays across cycles.
    """
    session.run("settrans -c /host '' || true")
    session.run("settrans -c /dev/virtio-9p0 '' || true")
    for pid in translator_pids(session):
        session.run(f"kill -TERM {pid}")


def dump_guest_state(session):
    """Print what the guest looks like after a failure, for the log."""
    vh.dump_commands(session, (
        ("ps", "ps"),
        ("device node", "settrans -g /dev/virtio-9p0 || true"),
        ("mount", "settrans -g /host || true"),
        ("fsysopts", "fsysopts /dev/virtio-9p0 /host || true"),
        ("selftest", "/hurd/virtio-9p --selftest || true"),
    ))
    if vh.WANT_GDB:
        vh.gdb_backtraces(
            session, TRANSLATOR_PS,
            f"{vh.GDB_COMMAND or 'gdb'} -p {{pid}} -batch "
            "-ex 'thread apply all bt' 2>&1")


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
    vh.record(
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
    vh.record("phase1 wrong mount tag rejected", status != 0, f"rc={status}")
    return True


def phase2(session):
    # Idempotent start: a reused guest may still hold translators from an
    # earlier run; removing them first (and reaping orphaned daemons that
    # `settrans -c NODE ''` leaves behind) makes the fresh settrans below
    # deterministic.
    remove_translators(session)
    session.run("mkdir -p /host")
    status, out = session.run(
        "settrans -c /dev/virtio-9p0 /hurd/virtio-9p --mount-tag=" + MOUNT_TAG
    )
    vh.record("phase2 device node settrans", status == 0,
              f"rc={status}, {out.strip()[-500:]}")
    status, out = session.run("settrans -c /host /hurd/9pfs /dev/virtio-9p0")
    vh.record("phase2 9pfs mount", status == 0,
              f"rc={status}, {out.strip()[-500:]}")

    status, _ = session.run("echo hello > /host/greeting.txt")
    vh.record("phase2 guest write", status == 0, f"rc={status}")
    try:
        with open(os.path.join(SHARE, "greeting.txt"), encoding="utf-8") as f:
            content = f.read()
    except OSError as exc:
        content = f"<{exc}>"
    vh.record("phase2 guest write visible on host", content.strip() == "hello",
              content)
    status, out = session.run("cat /host/greeting.txt")
    vh.record("phase2 read back", status == 0 and out.strip() == "hello",
              out.strip())

    status, out = session.run("cat /host/host-seed.txt")
    vh.record(
        "phase2 host file visible in guest",
        status == 0 and out.strip() == "from host",
        out.strip(),
    )


def phase3(session):
    # creation both ways, nested directories
    status, _ = session.run("mkdir -p /host/a/b/c && echo x > /host/a/b/c/f")
    vh.record("phase3 nested creation", status == 0, f"rc={status}")
    vh.record(
        "phase3 nested creation on host",
        os.path.exists(os.path.join(SHARE, "a", "b", "c", "f")),
    )

    # content integrity, both directions, small and large
    status, _ = session.run("dd if=/dev/urandom of=/host/big1m bs=1M count=1",
                            timeout=vh.LONG_TIMEOUT)
    ok = status == 0 and host_md5(os.path.join(SHARE, "big1m")) == \
        session.run("md5sum /host/big1m")[1].split()[0]
    vh.record("phase3 1 MiB guest->host", ok, f"rc={status}")

    status, _ = session.run("dd if=/dev/urandom of=/host/big64m bs=1M count=64",
                            timeout=vh.LONG_TIMEOUT)
    ok = status == 0 and host_md5(os.path.join(SHARE, "big64m")) == \
        session.run("md5sum /host/big64m")[1].split()[0]
    vh.record("phase3 64 MiB guest->host", ok, f"rc={status}")

    subprocess.run(
        ["dd", "if=/dev/urandom", "of=" + os.path.join(SHARE, "h1m"),
         "bs=1M", "count=1", "status=none"],
        check=True,
    )
    ok = session.run("md5sum /host/h1m")[1].split()[0] == \
        host_md5(os.path.join(SHARE, "h1m"))
    vh.record("phase3 1 MiB host->guest", ok)

    # metadata
    status, out = session.run("stat -c %s /host/big1m")
    vh.record("phase3 stat size", status == 0 and out.strip() == "1048576",
              out.strip())
    session.run("chmod 600 /host/greeting.txt")
    status, out = session.run("stat -c %a /host/greeting.txt")
    vh.record("phase3 chmod", status == 0 and out.strip() == "600", out.strip())
    session.run("touch /host/greeting.txt")

    # symlinks: the unmodified 9pfs front end implements no symlink RPCs,
    # so creation must be refused cleanly (EOPNOTSUPP) and the driver must
    # keep serving afterwards.
    session.run("ln -s greeting.txt /host/link")
    status, out = session.run("readlink /host/link")
    vh.record("phase3 symlink refused by front end", status != 0, f"rc={status}")
    status, _ = session.run("echo after-symlink > /host/ok")
    vh.record("phase3 translator alive after refused symlink", status == 0,
              f"rc={status}")
    session.run("rm -f /host/ok")
    session.run("mv /host/link /host/link2")
    session.run("rm /host/link2")
    session.run("mkdir /host/dir && rmdir /host/dir")
    status, _ = session.run("test ! -e /host/link2 && test ! -e /host/dir")
    vh.record("phase3 rename/unlink/rmdir", status == 0, f"rc={status}")

    # readdir of >1000 entries (ring wrap)
    session.run("mkdir -p /host/many")
    session.run("for i in $(seq 1 1001); do : > /host/many/f$i; done",
                timeout=vh.LONG_TIMEOUT)
    status, out = session.run("ls /host/many | wc -l")
    vh.record("phase3 readdir 1001 entries",
              status == 0 and out.strip() == "1001", out.strip())

    # concurrent readers/writers
    session.run(
        "(dd if=/dev/urandom of=/host/conc1 bs=1M count=4 & "
        "dd if=/dev/urandom of=/host/conc2 bs=1M count=4 & wait)",
        timeout=vh.LONG_TIMEOUT,
    )
    ok = all(
        session.run(f"md5sum /host/conc{n}")[1].split()[0]
        == host_md5(os.path.join(SHARE, f"conc{n}"))
        for n in (1, 2)
    )
    vh.record("phase3 two concurrent writers", ok)

    # single-open discipline: the mounted node refuses a second open
    status, _ = session.run("exec 3<>/dev/virtio-9p0")
    vh.record("phase3 second open refused", status != 0, f"rc={status}")

    # translator restart: unmount, remove, re-set, re-mount.  The removal
    # form is `settrans -c NODE ''` (empty translator), which removes the
    # node record and goaways the active translator but does not kill the
    # standalone daemon, so we reap the orphaned pids explicitly.
    remove_translators(session)
    pids = translator_pids(session)
    vh.record("phase3 restart: no translator processes left", not pids,
              f"pids: {pids}")
    status, out = session.run("settrans -g /dev/virtio-9p0 || true")
    vh.record("phase3 restart: node has no active translator",
              status == 0 and not out.strip(), out.strip())
    session.run("settrans -c /dev/virtio-9p0 /hurd/virtio-9p --mount-tag="
                + MOUNT_TAG)
    session.run("settrans -c /host /hurd/9pfs /dev/virtio-9p0")
    status, out = session.run("cat /host/greeting.txt")
    vh.record("phase3 restart: mount works again",
              status == 0 and out.strip() == "hello", out.strip())


def phase_stress(session, cycles):
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

    before = vh.netdde_cpu_seconds(session)
    vh.record("stress netdde baseline", before is not None,
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
        remove_translators(session)
        pids = translator_pids(session)
        status, out = session.run("settrans -g /dev/virtio-9p0 || true",
                                  timeout=30)
        vh.record(f"stress cycle {i + 1} add/exchange/remove",
                  not pids and status == 0 and not out.strip(),
                  f"pids: {pids}, node: {out.strip()!r}")

    after = vh.netdde_cpu_seconds(session)
    vh.record("stress netdde cpu flat",
              after is not None and after - before < 3.0,
              f"netdde {before:.2f}s -> {after:.2f}s over {cycles} cycles")


def nodev(session):
    status, out = session.run("/hurd/virtio-9p --selftest")
    vh.record("negative: no device, clean exit", status != 0,
              f"rc={status}, {out.strip().splitlines()[0] if out.strip() else ''}")


def stress(session):
    vh.install(VIRTIO_9P_BIN, "/tmp/virtio-9p")
    vh.install(P9FS_BIN, "/tmp/9pfs")
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
        session = vh.GuestSession()
        if mode == "nodev":
            nodev(session)
        elif mode == "stress":
            stress(session)
        else:
            vh.install(VIRTIO_9P_BIN, "/tmp/virtio-9p")
            vh.install(P9FS_BIN, "/tmp/9pfs")
            session.run("install -m 755 /tmp/virtio-9p /hurd/virtio-9p && "
                        "install -m 755 /tmp/9pfs /hurd/9pfs")
            if not phase1(session):
                failed_early = True
            else:
                phase2(session)
                phase3(session)
    except vh.GuestError as exc:
        vh.record("harness", False, str(exc))
        failed_early = True
    if failed_early:
        try:
            dump_guest_state(session)
        except Exception as exc:  # noqa: BLE001 - the dump must not mask the
            print(f"--- guest state dump failed: {exc} ---", flush=True)
        print("--- QEMU serial log tail ---", flush=True)
        print(vh.qemu_log_tail(), flush=True)

    return vh.finish(session)


if __name__ == "__main__":
    sys.exit(main())
