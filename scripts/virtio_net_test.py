#!/usr/bin/env python3
"""Drive the virtio-net test matrix in the guest.

scripts/virtio_net_test.sh boots the guest with QEMU's virtio-net device
attached to a user-mode network backend; this script installs the driver
and runs the network matrix, printing one PASS/FAIL line per assertion.

Configuration comes from the environment:

    VIRTIO_NET_BIN   host path of the virtio-net binary

    (SSH_HOST / SSH_PORT / SSH_KEY / QEMU_LOG / GDB are the shared
    harness configuration documented in scripts/virtio_harness.py.)

Modes (argv[1]):

    main     the full matrix, with the device attached
    nodev    the no-device negative test, without the device attached
    stress   repeated selftest cycles to churn bring-up/teardown

Exit codes: 0 every assertion passed, 1 at least one failed.
"""

import os
import re
import sys

import virtio_harness as vh

VIRTIO_NET_BIN = os.environ.get("VIRTIO_NET_BIN", "")
VIRTIO_NET_CLIENT_BIN = os.environ.get("VIRTIO_NET_CLIENT_BIN", "")

_MODE = sys.argv[1] if len(sys.argv) > 1 else "main"

DAEMON_LOG = "/tmp/virtio-net.log"
SERVE_NODE = "/dev/vnet0"


def guest_responsive(session, label):
    status, out = session.run("ls / >/dev/null && echo alive", timeout=60)
    vh.record(f"{label}: guest responsive", status == 0 and "alive" in out,
              f"rc={status}, {out.strip()[-200:]}")


def install_binary(session):
    vh.install(VIRTIO_NET_BIN, "/tmp/virtio-net")
    session.run("install -m 755 /tmp/virtio-net /hurd/virtio-net", timeout=60)

def install_client(session):
    vh.install(VIRTIO_NET_CLIENT_BIN, "/tmp/vnet-client")
    session.run("install -m 755 /tmp/vnet-client /hurd/vnet-client", timeout=60)


def phase_status(session):
    status, out = session.run("/hurd/virtio-net --status", timeout=120)
    vh.record("phase1 status: device usable", status == 0,
              f"rc={status}, {out.strip()[-300:]}")
    if status == 0:
        vh.record("phase1 status: MAC reported", "MAC" in out, out.strip()[-200:])
    return status == 0


def phase_selftest(session):
    status, out = session.run("/hurd/virtio-net --selftest", timeout=300)
    gate = "selftest ok" in out
    vh.record("phase2 selftest gate", status == 0 and gate,
              f"rc={status}, {out.strip().splitlines()[-1] if out.strip() else ''}")
    if status == 0:
        vh.record("phase2 selftest: NET_RCV_MSG round trip reported",
                  "NET_RCV_MSG round trip" in out, "")
    return status == 0 and gate


def phase_traffic(session):
    status, out = session.run(
        "/hurd/virtio-net --selftest --selftest-traffic", timeout=300)
    gate = "selftest ok" in out
    vh.record("phase3 selftest+traffic gate", status == 0 and gate,
              f"rc={status}, {out.strip().splitlines()[-1] if out.strip() else ''}")
    # Traffic arrival is backend-dependent; report, don't fail.
    if "raw RX observed" in out:
        vh.record("phase3 raw RX observed", True, "")
    else:
        vh.record("phase3 raw RX observed (backend-dependent)",
                  True, "no backend traffic observed (not a failure)")
    return status == 0 and gate


def phase_daemon(session):
    session.run(
        "sh -c 'setsid /hurd/virtio-net >%s 2>&1 </dev/null &'" % DAEMON_LOG,
        timeout=30)
    import time
    deadline = time.time() + 60
    while time.time() < deadline:
        pids = vh.guest_pids(session, r"hurd\/virtio-net")
        _, log = session.run(f"cat {DAEMON_LOG}", timeout=30)
        if pids:
            vh.record("phase4 daemon running", True, f"pids {', '.join(pids)}")
            return
        if log.strip():
            vh.record("phase4 daemon running", False,
                      f"exited; log: {log.strip()[-300:]}")
            return
        time.sleep(2)
    vh.record("phase4 daemon running", False, "did not start within 60s")


def stop_daemon(session):
    pids = vh.guest_pids(session, r"hurd\/virtio-net")
    if not pids:
        return
    # Exclude the --serve translator if any; stop only plain daemon.
    session.run("pkill -f 'hurd/virtio-net$' 2>/dev/null || true", timeout=30)
    import time
    deadline = time.time() + 60
    while time.time() < deadline:
        if not vh.guest_pids(session, r"hurd\/virtio-net$"):
            return
        time.sleep(2)


def phase_serve(session):
    # Clean any stale node first.
    session.run(f"settrans -g {SERVE_NODE} 2>/dev/null || true", timeout=30)
    status, out = session.run(
        f"settrans -c {SERVE_NODE} /hurd/virtio-net --serve", timeout=120)
    vh.record("phase5 serve: translator started", status == 0,
              f"rc={status}, {out.strip()[-300:]}")
    status, out = session.run(f"ls -l {SERVE_NODE}", timeout=60)
    vh.record("phase5 serve: node published", status == 0 and "vnet0" in out,
              f"rc={status}, {out.strip()[-200:]}")
    pids = vh.guest_pids(session, r"virtio-net.*--serve")
    vh.record("phase5 serve: process running", bool(pids),
              "no matching process" if not pids else ", ".join(pids))
    # Leave the translator running for subsequent phases; teardown happens
    # in phase_serve_teardown().


def phase_raw_client(session):
    install_client(session)
    status, out = session.run("/hurd/vnet-client /dev/vnet0 test", timeout=120)
    vh.record("phase6 raw client TX/RX", status == 0 and "PASS" in out,
              f"rc={status}, {out.strip()[-300:]}")

def phase_pfinet_attach(session):
    status, out = session.run(
        "settrans -c /tmp/pfinet-vnet0 /hurd/pfinet -i /dev/vnet0 "
        "-a 10.0.2.20 -m 255.255.255.0", timeout=120)
    vh.record("phase7 pfinet attach", status == 0,
              f"rc={status}, {out.strip()[-200:]}")
    import time
    time.sleep(2)
    pids = vh.guest_pids(session, r"hurd\/pfinet")
    vh.record("phase7 pfinet running", bool(pids),
              "no pfinet process" if not pids else ", ".join(pids))

def phase_serve_teardown(session):
    session.run(f"settrans -g {SERVE_NODE} 2>/dev/null || true", timeout=60)
    pids = vh.guest_pids(session, r"virtio-net.*--serve")
    if pids:
        session.run("kill " + " ".join(pids), timeout=30)

def nodev(session):
    status, out = session.run("/hurd/virtio-net --selftest", timeout=120)
    vh.record("negative: no device, selftest clean exit", status != 0,
              f"rc={status}, "
              f"{out.strip().splitlines()[-1] if out.strip() else ''}")
    status, out = session.run("/hurd/virtio-net --status", timeout=60)
    vh.record("negative: no device, status clean exit", status != 0,
              f"rc={status}, "
              f"{out.strip().splitlines()[-1] if out.strip() else ''}")


def stress(session):
    cycles = int(os.environ.get("CYCLES", "8"))
    for i in range(cycles):
        status, out = session.run("/hurd/virtio-net --selftest", timeout=300)
        ok = status == 0 and "selftest ok" in out
        vh.record(f"stress cycle {i + 1} selftest", ok,
                  f"rc={status}")
    guest_responsive(session, "stress post")


def dump_guest_state(session):
    vh.dump_commands(session, (
        ("driver status", "/hurd/virtio-net --status || true"),
        ("processes", "ps -ef | grep -i virtio-net | grep -v grep || true"),
    ))
    if vh.WANT_GDB:
        vh.gdb_backtraces(
            session, r"hurd\/virtio-net",
            "gdb /hurd/virtio-net -p {pid} -batch "
            "-ex 'thread apply all bt' 2>&1")


def main():
    mode = _MODE

    if mode == "nodev":
        pass
    else:
        if not VIRTIO_NET_BIN:
            sys.exit("VIRTIO_NET_BIN is not set (the boot script exports it)")

    session = None
    failed_early = False
    try:
        session = vh.GuestSession()
        if mode == "nodev":
            nodev(session)
        elif mode == "stress":
            install_binary(session)
            stress(session)
        else:
            install_binary(session)
            if not phase_status(session):
                failed_early = True
            else:
                if phase_selftest(session):
                    phase_traffic(session)
            phase_daemon(session)
            stop_daemon(session)
            phase_serve(session)
            phase_raw_client(session)
            phase_pfinet_attach(session)
            phase_serve_teardown(session)
    except vh.GuestError as exc:
        vh.record("harness", False, str(exc))
        failed_early = True
    if failed_early:
        try:
            dump_guest_state(session)
        except Exception as exc:  # noqa: BLE001
            print(f"--- guest state dump failed: {exc} ---", flush=True)
        print("--- QEMU serial log tail ---", flush=True)
        print(vh.qemu_log_tail(), flush=True)

    return vh.finish(session)


if __name__ == "__main__":
    sys.exit(main())
