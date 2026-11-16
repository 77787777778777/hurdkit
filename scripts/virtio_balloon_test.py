#!/usr/bin/env python3
"""Drive the virtio-balloon test matrix in the guest.

scripts/virtio_balloon_test.sh boots the guest with QEMU's virtio-balloon
device and hands over the SSH session plus the QEMU monitor socket; this
script installs the driver, runs the Phase 1 selftest, starts the daemon,
and drives inflate/deflate from the host side through the monitor
(`balloon` / `info balloon`), printing one PASS/FAIL line per assertion.

The assertions are deliberately two-sided: the guest's own view
(`/hurd/virtio-balloon --status`) and the device model's view (the QEMU
monitor and the virtio_balloon* trace lines in the serial log) must agree,
because `actual` in the config space alone does not prove the host saw the
same accounting.

Configuration comes from the environment:

    VIRTIO_BALLOON_BIN   host path of the virtio-balloon daemon
    RAM_MB               guest memory in MiB; balloon targets are derived
                         from it (default 2048)

    (SSH_HOST / SSH_PORT / SSH_KEY / QEMU_LOG / GDB are the shared
    harness configuration documented in scripts/virtio_harness.py.)

Modes (argv[1]):

    main     the full matrix, with the device attached
    nodev    the no-device negative test, without the device attached
    stress   start/converge/stop the daemon in a loop to churn the
             registration/teardown path

Every command runs with a bounded timeout, so a stuck daemon turns into a
FAIL line instead of a hang. On failure the guest is left alone for
inspection: scripts/virtio_balloon_test.sh keeps QEMU running and points
at the log and the SSH command.

Exit codes: 0 every assertion passed, 1 at least one failed.
"""

import os
import re
import sys
import time

import virtio_harness as vh

VIRTIO_BALLOON_BIN = os.environ["VIRTIO_BALLOON_BIN"]
RAM_MB = int(os.environ.get("RAM_MB", "2048"))
# Balloon size for the main inflate/deflate round trip; the churn phase
# uses fractions of it.  Capped at an eighth of the RAM so a small guest
# never has most of its memory withheld from Mach.
BALLOON_STEP_MB = int(os.environ.get("BALLOON_STEP_MB", str(min(256, RAM_MB // 8))))

# Convergence of a 256 MiB balloon is ~1k queue round trips plus the
# chunk allocations behind them; give it room, but bound it.
CONVERGE_TIMEOUT = 600
MONITOR_PATH = os.environ.get("VIRTIO_MONITOR_SOCKET") or os.path.join(
    os.path.dirname(vh.QEMU_LOG) or ".", "virtio-balloon-test-monitor.sock")
DAEMON_LOG = "/tmp/virtio-balloon.log"


def daemon_pids(session):
    return vh.guest_pids(session, r"hurd\/virtio-balloon")


def driver_status(session):
    """(target, actual) in pages, or None if --status fails."""
    status, out = session.run("/hurd/virtio-balloon --status", timeout=60)
    if status != 0:
        return None
    m = re.search(r"target=(\d+) pages .*actual=(\d+) pages", out)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def start_daemon(session):
    """Start the balloon daemon detached; the nested sh keeps the &
    away from this harness's ` ; }` wrapper (a bash syntax error
    otherwise swallows the __RC__ marker and hangs the session)."""
    session.run(
        f"sh -c 'setsid /hurd/virtio-balloon >{DAEMON_LOG} 2>&1 </dev/null &'",
        timeout=30)
    deadline = time.time() + 60
    while time.time() < deadline:
        pids = daemon_pids(session)
        _, log = session.run(f"cat {DAEMON_LOG}", timeout=30)
        if pids and "initial target" in log:
            return
        if not pids and log.strip():
            raise vh.GuestError(
                f"daemon exited during bring-up; log: {log.strip()[-500:]}")
        time.sleep(2)
    raise vh.GuestError("daemon did not reach DRIVER_OK within 60s")


def stop_daemon(session, timeout=180):
    """SIGTERM the daemon and wait for it to exit (the shutdown path
    deflates every held page before it exits)."""
    pids = daemon_pids(session)
    if not pids:
        return
    session.run(f"kill {' '.join(pids)}", timeout=30)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not daemon_pids(session):
            return
        time.sleep(3)
    raise vh.GuestError(
        f"daemon did not exit within {timeout}s of SIGTERM "
        f"(pids {', '.join(pids)})")


def wait_balloon(mon, session, want_mb, timeout=CONVERGE_TIMEOUT):
    """Poll the monitor until the host sees want_mb, or fail."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = mon.balloon_mb()
        if last == want_mb:
            return last
        if not daemon_pids(session):
            raise vh.GuestError("daemon died during convergence")
        time.sleep(3)
    raise vh.GuestError(
        f"balloon did not converge to {want_mb} MiB within {timeout}s "
        f"(last: {last} MiB)")


def guest_responsive(session, label):
    status, out = session.run("ls / >/dev/null && echo alive", timeout=60)
    vh.record(f"{label}: guest responsive", status == 0 and "alive" in out,
              f"rc={status}, {out.strip()[-200:]}")


def trace_grep(pattern, want, name):
    """Host-side evidence from the device model's trace lines."""
    hits = vh.qemu_log_grep(pattern)
    vh.record(name, want in hits,
              f"{hits.count(chr(10)) + (1 if hits else 0)} trace lines; "
              f"looking for {want!r}")


def phase1(session):
    status, out = session.run("/hurd/virtio-balloon --selftest")
    gate = all(
        line in out
        for line in (
            "inflateq/deflateq round trip ok",
            "actual tracked 0 -> 1 -> 0",
            "selftest ok",
        )
    )
    vh.record(
        "phase1 selftest gate",
        status == 0 and gate,
        f"rc={status}, output: {out.strip()[:2000]}",
    )
    if not (status == 0 and gate):
        print("phase1 failed, skipping the daemon phases", flush=True)
        return False

    status, out = session.run(
        "/hurd/virtio-balloon --selftest --device=0000:00:1f.0")
    vh.record("phase1 wrong device slot rejected", status != 0,
              f"rc={status}, {out.strip().splitlines()[-1] if out.strip() else ''}")
    return True


def phase2(session):
    session.run(f"rm -f {DAEMON_LOG}")
    start_daemon(session)
    vh.record("phase2 daemon started and reached DRIVER_OK", True)

    st = driver_status(session)
    vh.record("phase2 fresh status is target 0, actual 0",
              st == (0, 0), f"status: {st}")


def phase3(session):
    mon = vh.Hmp(MONITOR_PATH)
    step_pages = BALLOON_STEP_MB * 1024 * 1024 // 4096

    vh.record("phase3 baseline: host sees full RAM", mon.balloon_mb() == RAM_MB,
              f"info balloon: {mon.balloon_mb()} MiB, expected {RAM_MB}")
    guest_responsive(session, "phase3 baseline")

    # Inflate: host asks for a smaller guest, the driver holds
    # BALLOON_STEP_MB in the balloon.
    inflate_mb = RAM_MB - BALLOON_STEP_MB
    mon.cmd(f"balloon {inflate_mb}")
    got = wait_balloon(mon, session, inflate_mb)
    vh.record("phase3 inflate converged on host", got == inflate_mb,
              f"info balloon: {got} MiB, expected {inflate_mb}")
    st = driver_status(session)
    vh.record("phase3 inflate status is target==actual==step",
              st == (step_pages, step_pages), f"status: {st}")
    trace_grep(r"virtio_balloon_set_config",
               f"actual: {step_pages}",
               "phase3 inflate seen by device model")

    # The guest must still work with BALLOON_STEP_MB withheld.
    guest_responsive(session, "phase3 inflated")
    status, out = session.run(
        "dd if=/dev/urandom of=/tmp/bt bs=1M count=4 2>/dev/null "
        "&& md5sum /tmp/bt", timeout=120)
    vh.record("phase3 guest memory still usable", status == 0 and
              re.search(r"[0-9a-f]{32}", out) is not None,
              f"rc={status}, {out.strip()[-100:]}")

    # Deflate: everything goes back to Mach, the host sees full RAM.
    mon.cmd(f"balloon {RAM_MB}")
    got = wait_balloon(mon, session, RAM_MB)
    vh.record("phase3 deflate converged on host", got == RAM_MB,
              f"info balloon: {got} MiB, expected {RAM_MB}")
    st = driver_status(session)
    vh.record("phase3 deflate status is target 0, actual 0",
              st == (0, 0), f"status: {st}")

    # Churn: rapid target changes, bounded convergence each time.
    for i, mb in enumerate((RAM_MB - BALLOON_STEP_MB // 2,
                            RAM_MB - BALLOON_STEP_MB * 3 // 2, RAM_MB)):
        mon.cmd(f"balloon {mb}")
        got = wait_balloon(mon, session, mb, timeout=CONVERGE_TIMEOUT)
        vh.record(f"phase3 churn {i + 1} converged to {mb}", got == mb,
                  f"info balloon: {got} MiB")
    guest_responsive(session, "phase3 churn")

    # Teardown: SIGTERM deflates every held page before the daemon exits,
    # so the host always ends up with the full RAM visible.
    stop_daemon(session)
    vh.record("phase3 shutdown: daemon exited", not daemon_pids(session))
    vh.record("phase3 shutdown: host sees full RAM",
              mon.balloon_mb() == RAM_MB,
              f"info balloon: {mon.balloon_mb()} MiB, expected {RAM_MB}")
    st = driver_status(session)
    vh.record("phase3 shutdown: device config back to 0", st == (0, 0),
              f"status: {st}")

    # One restart on the same device: fresh handshake, queues, IRQ.
    session.run(f"rm -f {DAEMON_LOG}")
    start_daemon(session)
    st = driver_status(session)
    vh.record("phase3 restart: daemon up again with clean status",
              st == (0, 0), f"status: {st}")
    stop_daemon(session, timeout=120)
    vh.record("phase3 restart: daemon stopped again",
              not daemon_pids(session))


def dump_guest_state(session):
    vh.dump_commands(session, (
        ("ps", "ps"),
        ("daemon status", "/hurd/virtio-balloon --status || true"),
        ("daemon log", f"cat {DAEMON_LOG} 2>/dev/null | tail -20 || true"),
    ))
    if vh.WANT_GDB:
        vh.gdb_backtraces(
            session, r"hurd\/virtio-balloon",
            f"{vh.GDB_COMMAND or 'gdb'} /hurd/virtio-balloon -p {{pid}} -batch "
            "-ex 'thread apply all bt' 2>&1")


def stress(session):
    mon = vh.Hmp(MONITOR_PATH)
    churn_mb = RAM_MB - min(64, BALLOON_STEP_MB // 4)

    before = vh.netdde_cpu_seconds(session)
    vh.record("stress netdde baseline", before is not None,
              "netdde not running" if before is None else f"{before:.2f}s")
    if before is None:
        return

    for i in range(int(os.environ.get("CYCLES", "8"))):
        start_daemon(session)
        mon.cmd(f"balloon {churn_mb}")
        got = wait_balloon(mon, session, churn_mb,
                           timeout=CONVERGE_TIMEOUT)
        stop_daemon(session)
        gone = not daemon_pids(session)
        host_ok = mon.balloon_mb() == RAM_MB
        vh.record(f"stress cycle {i + 1} start/converge/stop",
                  got == churn_mb and gone and host_ok,
                  f"converged: {got}, daemon gone: {gone}, "
                  f"host: {mon.balloon_mb()} MiB")

    after = vh.netdde_cpu_seconds(session)
    vh.record("stress netdde cpu flat",
              after is not None and after - before < 3.0,
              f"netdde {before:.2f}s -> {after:.2f}s over "
              f"{os.environ.get('CYCLES', '8')} cycles")


def nodev(session):
    status, out = session.run("/hurd/virtio-balloon --selftest")
    vh.record("negative: no device, selftest clean exit", status != 0,
              f"rc={status}, "
              f"{out.strip().splitlines()[-1] if out.strip() else ''}")
    status, out = session.run("/hurd/virtio-balloon --status")
    vh.record("negative: no device, status clean exit", status != 0,
              f"rc={status}, "
              f"{out.strip().splitlines()[-1] if out.strip() else ''}")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "main"
    session = None
    failed_early = False
    try:
        session = vh.GuestSession()
        if mode == "nodev":
            nodev(session)
        elif mode == "stress":
            vh.install(VIRTIO_BALLOON_BIN, "/tmp/virtio-balloon")
            session.run("install -m 755 /tmp/virtio-balloon "
                        "/hurd/virtio-balloon")
            stress(session)
        else:
            vh.install(VIRTIO_BALLOON_BIN, "/tmp/virtio-balloon")
            session.run("install -m 755 /tmp/virtio-balloon "
                        "/hurd/virtio-balloon")
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
