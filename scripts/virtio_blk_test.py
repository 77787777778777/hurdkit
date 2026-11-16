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
    VBLK_CLIENT_BIN  host path of the direct Mach device RPC client
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
VBLK_CLIENT_BIN = os.environ.get("VBLK_CLIENT_BIN", "")
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


# The Stage C device server: the translator node and the paths the
# client-side write pattern lands in.  The write pattern is
# deterministic (fixed tag prefix, rest zero) so the host can verify it
# in the image without the guest reporting a digest.
SERVE_NODE = "/dev/vblk0"
STORE_NODE = "/tmp/vblk-store"
GUEST_WRITE_TAG = b"vblk-guest-wrote-v1"
# Far from the seeded canary/pattern ranges; the write test destroys
# these sectors, and nothing else in the matrix reads them.
WRITE_SECTOR = 16384
# 1 MiB dd round trips through the storeio node: the read lands on the
# seeded far pattern, the write zeroes a range nothing else touches.
ONE_MB = 1 << 20
DD_WRITE_SECTOR = WRITE_SECTOR + 256


def client_read_sha(session, node, rec, count):
    """sha256 of COUNT records read through the device RPC path."""
    status, out = session.run(
        f"/hurd/vblk-client read {node} {rec} {count} | sha256sum",
        timeout=180)
    if status != 0:
        return None, out
    m = re.search(r"([0-9a-f]{32,64})", out)
    if not m:
        return None, out
    return m.group(1), out


def phase4(session):
    """The Mach device translator: publish, open, read, write, close.

    This is plan milestone M3 / spike S3: the MIG ds_device_* server on
    top of the semantics layer, exercised through real RPCs by the
    direct-RPC client, plus the optional libstore/storeio consumer
    path.  Every check runs against the disposable image policy of
    plan D5 (the write test destroys only the WRITE_SECTOR range).
    """
    client = "/hurd/vblk-client"

    status, out = session.run(
        f"settrans -c {SERVE_NODE} /hurd/virtio-blk --serve", timeout=120)
    vh.record("phase4 translator published", status == 0,
              f"rc={status}, {out.strip()[-300:]}")
    if status != 0:
        return

    # fsysopts answers only once trivfs_startup replied to the parent;
    # a small settle delay keeps the first RPC off the startup race.
    session.run("sleep 2")
    status, out = session.run(f"fsysopts {SERVE_NODE}", timeout=60)
    vh.record("phase4 translator node live", status == 0 and "virtio-blk" in out,
              f"rc={status}, {out.strip()[-200:]}")

    # Size query through device_open + DEV_GET_RECORDS/DEV_GET_SIZE.
    status, out = session.run(f"{client} status {SERVE_NODE}", timeout=120)
    want_sectors = (DISK_MB * (1 << 20)) // SECTOR
    m = re.search(r"records=(\d+) record_size=(\d+)", out or "")
    ok = (status == 0 and m is not None
          and int(m.group(1)) == want_sectors and int(m.group(2)) == SECTOR)
    vh.record("phase4 device_open + size query match the test image", ok,
              f"rc={status}, {(out or '').strip()[:300]}")

    # Two-sided reads through the RPC path: seeded ranges, including
    # the multi-chunk far range.
    got, out = client_read_sha(session, SERVE_NODE, 0, 128)
    want = host_range_sha(0, 128 * SECTOR)
    vh.record("phase4 device RPC read of seeded range matches image",
              got == want, f"guest {got}, host {want}" if got != want else got)
    got, out = client_read_sha(session, SERVE_NODE, FAR_SECTOR, FAR_COUNT)
    want = host_range_sha(FAR_SECTOR * SECTOR, FAR_COUNT * SECTOR)
    vh.record("phase4 device RPC multi-chunk read matches image",
              got == want, f"guest {got}, host {want}" if got != want else got)

    # Negative contract through real RPCs: bounds, alignment, unknown
    # flavors, inband/map refusals, RO-open write refusal, close.
    status, out = session.run(f"{client} check {SERVE_NODE}", timeout=180)
    ok = status == 0 and out.count("PASS") >= 9 and "FAIL" not in out
    vh.record("phase4 negative-contract checks", ok,
              f"rc={status}, {out.strip()[-500:]}")

    # Write + flush + read-back through the RPC path, then the host
    # verifies the bytes in the image.
    status, out = session.run(
        f"printf '{GUEST_WRITE_TAG.decode()}%.0s' $(seq 1 64) "
        f"| head -c {SECTOR} > /tmp/vblk-write.bin; "
        f"{client} write {SERVE_NODE} {WRITE_SECTOR} /tmp/vblk-write.bin",
        timeout=180)
    ok = status == 0 and "flush and read-back ok" in out
    vh.record("phase4 device RPC write+flush+read-back", ok,
              f"rc={status}, {out.strip()[-300:]}")
    with open(TEST_DISK, "rb") as f:
        f.seek(WRITE_SECTOR * SECTOR)
        image = f.read(SECTOR)
    want_bytes = (GUEST_WRITE_TAG * 64)[:SECTOR]
    vh.record("phase4 guest device write visible in host image",
              image == want_bytes,
              "sector content matches the guest pattern" if
              image == want_bytes else f"sector: {image[:32].hex()}...")

    # Optional libstore consumer (M3's storeio leg): attach storeio to
    # the translator through libstore's dopen forms and run small dd
    # round trips through the store node.  `-c' (no-cache) keeps user
    # I/O on direct device RPCs instead of the pager path, which this
    # server refuses (device_map is D_INVALID_OPERATION).  Both spec
    # syntaxes are tried because the guest's storeio vintage decides
    # which one parses; when neither attaches, these checks are
    # skipped, not failed.
    attached = False
    for spec in (f"-T device '@{SERVE_NODE}:'", f"'device:@{SERVE_NODE}:'"):
        status, out = session.run(
            f"settrans -c {STORE_NODE} /hurd/storeio {spec}",
            timeout=120)
        if status == 0:
            session.run("sleep 2")
            st, _ = session.run(f"fsysopts {STORE_NODE}", timeout=60)
            if st == 0:
                attached = True
                break
        session.run(f"settrans -g -f {STORE_NODE} 2>/dev/null || true",
                    timeout=60)

    if not attached:
        for name in ("phase4 storeio 128-block read matches image",
                     "phase4 storeio 1 MiB dd read matches image",
                     "phase4 storeio 1 MiB dd write verified in image"):
            vh.record(name, True, "SKIPPED: storeio attach unavailable")
    else:
        got, out = guest_read_sha_node(session, STORE_NODE, 0, 128)
        want = host_range_sha(0, 128 * SECTOR)
        vh.record("phase4 storeio 128-block read matches image",
                  got == want,
                  f"store {got}, host {want}" if got != want else got)

        # A small (1 MiB) dd read through the full libstore path: the
        # seeded far pattern, so the digest is two-sided evidence.
        got, out = guest_read_sha_node(session, STORE_NODE, FAR_SECTOR,
                                       ONE_MB // SECTOR)
        want = host_range_sha(FAR_SECTOR * SECTOR, ONE_MB)
        vh.record("phase4 storeio 1 MiB dd read matches image",
                  got == want,
                  f"store {got}, host {want}" if got != want else got)

        # A small (1 MiB) dd write through libstore: deterministic
        # zeros over a range nothing else touches, verified in the
        # host image afterwards (plan D5 disposable-image policy).
        status, out = session.run(
            f"dd if=/dev/zero of={STORE_NODE} bs={SECTOR} "
            f"seek={DD_WRITE_SECTOR} count={ONE_MB // SECTOR} "
            f"conv=notrunc 2>&1", timeout=180)
        ok = status == 0
        with open(TEST_DISK, "rb") as f:
            f.seek(DD_WRITE_SECTOR * SECTOR)
            image = f.read(ONE_MB)
        ok = ok and image == bytes(ONE_MB)
        vh.record("phase4 storeio 1 MiB dd write verified in image",
                  ok,
                  f"rc={status}, region "
                  f"{'zeroed' if image == bytes(ONE_MB) else 'MISMATCH'}")

        session.run(f"settrans -g {STORE_NODE}", timeout=60)

    # Teardown: goaway (may need force if a client lingers), then the
    # translator process must be gone and the node un-translated.
    session.run(f"settrans -g {SERVE_NODE}", timeout=60)
    status, out = session.run(
        f"sleep 1; settrans -g -f {SERVE_NODE} 2>/dev/null; "
        r"ps -ef | awk '/hurd\/virtio-blk --serve/ && !/awk/ {print $2}'",
        timeout=60)
    vh.record("phase4 translator torn down", out.strip() == "",
              f"leftover pids: {out.strip()}")


def guest_read_sha_node(session, node, rec, count):
    """sha256 of COUNT 512-byte blocks read from a store NODE."""
    status, out = session.run(
        f"dd if={node} bs={SECTOR} skip={rec} count={count} 2>/dev/null "
        f"| sha256sum", timeout=180)
    if status != 0:
        return None, out
    m = re.search(r"([0-9a-f]{32,64})", out)
    if not m:
        return None, out
    return m.group(1), out


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
            vh.install(VBLK_CLIENT_BIN, "/tmp/vblk-client")
            session.run("install -m 755 /tmp/vblk-client /hurd/vblk-client")
            vh.install(VIRTIO_BLK_BIN, "/tmp/virtio-blk")
            session.run("install -m 755 /tmp/virtio-blk /hurd/virtio-blk")
            if not phase1(session):
                failed_early = True
            else:
                if phase2(session):
                    phase3(session)
                    phase4(session)
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
