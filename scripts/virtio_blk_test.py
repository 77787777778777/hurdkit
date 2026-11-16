#!/usr/bin/env python3
"""Drive the virtio-blk test matrix in the guest.

scripts/virtio_blk_test.sh boots the guest with QEMU's virtio-blk device
backed by a disposable raw disk image; this script installs the driver
and runs the disk matrix, printing one PASS/FAIL line per assertion.

The assertions are deliberately two-sided.  The `seed` mode (run by the
boot script on the host, before the guest boots) writes known patterns
into the disposable image; the matrix has the guest read those ranges
back through the driver's --read-range and compares sha256 digests, then
lets --selftest --write-test overwrite sector 0 and verifies on the host
that the driver's write pattern landed in the image.  The QEMU
virtio_blk* trace lines supply the device model's view.

Configuration comes from the environment:

    VIRTIO_BLK_BIN   host path of the virtio-blk binary
    TEST_DISK        host path of the disposable test image
    DISK_MB          its size in MiB

    (SSH_HOST / SSH_PORT / SSH_KEY / QEMU_LOG / GDB are the shared
    harness configuration documented in scripts/virtio_harness.py.)

Modes (argv[1]):

    seed     host-side: (re)create the test image with the known patterns
    main     the full matrix, with the device attached
    nodev    the no-device negative test, without the device attached
    stress   repeated selftest/write-test cycles; every selftest is a
             full bring-up plus teardown of the device and its IRQ, so
             the registration/teardown path is churned (plan T8)

Every command runs with a bounded timeout, so a stuck driver turns into
a FAIL line instead of a hang. On failure the guest is left alone for
inspection: scripts/virtio_blk_test.sh keeps QEMU running and points at
the log and the SSH command.

Exit codes: 0 every assertion passed, 1 at least one failed.
"""

import hashlib
import os
import re
import sys

import virtio_harness as vh

# Guest-mode env: the boot script exports these, but the host-side seed
# mode must import cleanly without them, so every lookup tolerates a
# missing variable and main() validates the guest modes' needs.
VIRTIO_BLK_BIN = os.environ.get("VIRTIO_BLK_BIN", "")
TEST_DISK = os.environ.get("TEST_DISK", "")
DISK_MB = int(os.environ.get("DISK_MB", "64"))

# Mode is needed at import time (the boot script calls us as
# `seed DISK DISK_MB` before any guest-mode env exists), but
# VIRTIO_BLK_BIN is only meaningful for the guest modes and is
# therefore validated lazily in main().
_MODE = sys.argv[1] if len(sys.argv) > 1 else "main"

SECTOR = 512
# Sector 0 carries the canary the guest must read back before the write
# test overwrites it; the far range is read through more than one DMA
# chunk (the driver's max_transfer is 256 KiB), so the digest also covers
# the multi-chunk assembly.  Keep these in step with the driver's own
# write pattern below, which is fixed code in virtio-blk.c.
CANARY_TAG = b"virtio-blk-canary-v1"
FAR_SECTOR = 8192
FAR_COUNT = 2048



def sector0_pattern():
    return CANARY_TAG + bytes((i * 11 + 5) & 0xFF for i in range(20, SECTOR))


def far_pattern():
    return bytes((k * 31 + 17) & 0xFF for k in range(FAR_COUNT * SECTOR))


def driver_write_pattern():
    """What --selftest --write-test leaves in sector 0 (virtio-blk.c)."""
    return bytes((i * 7 + 1) & 0xFF for i in range(SECTOR))


def host_range_sha(offset, length):
    h = hashlib.sha256()
    with open(TEST_DISK, "rb") as f:
        f.seek(offset)
        remaining = length
        while remaining > 0:
            chunk = f.read(min(1 << 20, remaining))
            if not chunk:
                break
            h.update(chunk)
            remaining -= len(chunk)
    return h.hexdigest()


def seed(disk_path, disk_mb):
    """Create the disposable image and write the known patterns."""
    with open(disk_path, "wb") as f:
        f.truncate(disk_mb << 20)
        f.seek(0)
        f.write(sector0_pattern())
        f.seek(FAR_SECTOR * SECTOR)
        f.write(far_pattern())
    print(f"seeded {disk_path} ({disk_mb} MiB): canary at sector 0, "
          f"pattern at sectors {FAR_SECTOR}..{FAR_SECTOR + FAR_COUNT - 1}",
          flush=True)


def driver_status(session):
    """/hurd/virtio-blk --status output, or None on failure."""
    status, out = session.run("/hurd/virtio-blk --status", timeout=60)
    if status != 0:
        return None
    return out


def guest_read_sha(session, sector, count):
    """sha256 of COUNT sectors read by the guest at SECTOR."""
    status, out = session.run(
        f"/hurd/virtio-blk --read-range={sector}:{count} | sha256sum",
        timeout=180)
    if status != 0:
        return None, out
    m = re.search(r"([0-9a-f]{32,64})", out)
    if not m:
        return None, out
    return m.group(1), out


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


def check_read_range(session, label, sector, count):
    """Guest read vs host image content over one range."""
    got, out = guest_read_sha(session, sector, count)
    want = host_range_sha(sector * SECTOR, count * SECTOR)
    vh.record(f"{label}: guest read of sectors {sector}+{count} matches image",
              got == want,
              f"guest {got}, host {want}" if got != want else got)


def phase1(session):
    """Probe-only checks: --status never touches the virtio status."""
    out = driver_status(session)
    ok = out is not None
    m = re.search(r"capacity=(\d+) sectors", out or "")
    want_sectors = (DISK_MB * (1 << 20)) // SECTOR
    vh.record("phase1 status: capacity matches the test image",
           ok and m is not None and int(m.group(1)) == want_sectors,
           f"{(out or '').strip().splitlines()[0] if out else 'status failed'}")
    if not ok:
        print("phase1 failed, skipping the datapath phases", flush=True)
        return False

    status, out = session.run("/hurd/virtio-blk --status --device=0000:00:1f.0")
    vh.record("phase1 wrong device slot rejected", status != 0,
           f"rc={status}, {out.strip().splitlines()[-1] if out.strip() else ''}")
    return True


def phase2(session):
    """The read-only selftest: datapath bring-up plus semantics layer."""
    status, out = session.run("/hurd/virtio-blk --selftest", timeout=300)
    gate = all(
        line in out
        for line in (
            "raw read of sector 0 ok, read past capacity refused",
            "Mach semantics: size/records consistent",
            "selftest ok",
        )
    )
    vh.record("phase2 selftest gate", status == 0 and gate,
           f"rc={status}, output: {out.strip()[:2000]}")
    return status == 0 and gate


def phase3(session):
    """Two-sided data checks, then the write/flush round trip."""
    guest_responsive(session, "phase3 pre-I/O")

    # Read path (plan T3/T6): the guest must see exactly what the host
    # seeded, including a multi-chunk range.
    check_read_range(session, "phase3 read path", 0, 128)
    check_read_range(session, "phase3 read path, multi-chunk",
                     FAR_SECTOR, FAR_COUNT)
    trace_grep(r"virtio_blk_handle_read",
               f"sector {FAR_SECTOR} ",
               "phase3 far read seen by device model")

    # Write path (plan T4): the selftest's write/flush/read-back, then
    # the host verifies the driver's pattern landed in the image.
    status, out = session.run("/hurd/virtio-blk --selftest --write-test",
                              timeout=300)
    ok = status == 0 and "write/flush/read-back of sector 0 ok" in out
    vh.record("phase3 write/flush/read-back round trip", ok,
           f"rc={status}, {out.strip().splitlines()[-1] if out.strip() else ''}")
    with open(TEST_DISK, "rb") as f:
        f.seek(0)
        image_sector0 = f.read(SECTOR)
    vh.record("phase3 guest write visible in host image",
           image_sector0 == driver_write_pattern(),
           "sector 0 matches the driver pattern" if
           image_sector0 == driver_write_pattern()
           else f"sector 0: {image_sector0[:32].hex()}...")
    trace_grep(r"virtio_blk_handle_write", "sector 0 ",
               "phase3 write seen by device model")
    trace_grep(r"virtio_blk_rw_complete", "ret 0",
               "phase3 completions successful on device model")

    guest_responsive(session, "phase3 post-I/O")


def dump_guest_state(session):
    vh.dump_commands(session, (
        ("ps -ef", "ps -ef | head -40"),
        ("driver status", "/hurd/virtio-blk --status || true"),
        ("disk file", f"ls -la {TEST_DISK} 2>/dev/null || true"),
    ))
    if vh.WANT_GDB:
        vh.gdb_backtraces(
            session, r"hurd\/virtio-blk",
            "gdb /hurd/virtio-blk -p {pid} -batch "
            "-ex 'thread apply all bt' 2>&1")


def stress(session):
    """Churn the bring-up/teardown path: every selftest is one full cycle."""
    before = vh.netdde_cpu_seconds(session)
    vh.record("stress netdde baseline", before is not None,
           "netdde not running" if before is None else f"{before:.2f}s")
    if before is None:
        return

    cycles = int(os.environ.get("CYCLES", "8"))
    for i in range(cycles):
        status, out = session.run("/hurd/virtio-blk --selftest --write-test",
                                  timeout=300)
        ok = status == 0 and "selftest ok" in out
        with open(TEST_DISK, "rb") as f:
            f.seek(0)
            image_ok = f.read(SECTOR) == driver_write_pattern()
        vh.record(f"stress cycle {i + 1} selftest/write/verify",
               ok and image_ok,
               f"rc={status}, image sector 0 "
               f"{'ok' if image_ok else 'MISMATCH'}")

    after = vh.netdde_cpu_seconds(session)
    vh.record("stress netdde cpu flat",
           after is not None and after - before < 3.0,
           f"netdde {before:.2f}s -> {after:.2f}s over {cycles} cycles")


def nodev(session):
    status, out = session.run("/hurd/virtio-blk --selftest")
    vh.record("negative: no device, selftest clean exit", status != 0,
           f"rc={status}, "
           f"{out.strip().splitlines()[-1] if out.strip() else ''}")
    status, out = session.run("/hurd/virtio-blk --status")
    vh.record("negative: no device, status clean exit", status != 0,
           f"rc={status}, "
           f"{out.strip().splitlines()[-1] if out.strip() else ''}")


def main():
    mode = _MODE

    if mode == "seed":
        if len(sys.argv) != 4:
            sys.exit("usage: virtio_blk_test.py seed DISK_PATH DISK_MB")
        seed(sys.argv[2], int(sys.argv[3]))
        return 0

    if not VIRTIO_BLK_BIN:
        sys.exit("VIRTIO_BLK_BIN is not set (the boot script exports it)")

    session = None
    failed_early = False
    try:
        session = vh.GuestSession()
        if mode == "nodev":
            nodev(session)
        elif mode == "stress":
            vh.install(VIRTIO_BLK_BIN, "/tmp/virtio-blk")
            session.run("install -m 755 /tmp/virtio-blk /hurd/virtio-blk")
            stress(session)
        else:
            vh.install(VIRTIO_BLK_BIN, "/tmp/virtio-blk")
            session.run("install -m 755 /tmp/virtio-blk /hurd/virtio-blk")
            if not phase1(session):
                failed_early = True
            else:
                if phase2(session):
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
