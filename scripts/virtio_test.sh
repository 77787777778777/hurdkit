#!/usr/bin/env bash
# Boot the Hurd guest with a host directory shared through QEMU's virtio-9p
# device and run the translator test matrix (scripts/virtio_test.py).
#
# The guest boots from a throw-away overlay of the staged image, so the
# image stays pristine. The same overlay is reused for the no-device
# negative test at the end, which is why the translators installed into it
# survive the first poweroff.
#
# On failure QEMU is left running and the serial log is kept, so the guest
# can be inspected by hand (see the printed SSH command; GDB=1 makes the
# driver attach gdb to the stuck translators first).
#
# The boot scaffolding (overlay, pidfile discipline, SSH probe, monitor
# socket, poweroff wait) lives in scripts/virtio_boot_lib.sh, shared with
# the other virtio harnesses.
#
# Environment:
#   SHARE     host directory to share (default img/share)
#   MOUNT_TAG mount tag of the fsdev (default hostshare)
#   SSH_HOST  address QEMU forwards the guest SSH port to (default 127.0.0.1)
#   SSH_PORT  forwarded SSH port (default 2222; "0" lets QEMU pick a free
#             port, which the script then discovers from the QEMU process)
#   GDB       "1" installs gdb in the guest and dumps stacks on failure
#   REUSE     "1" run the matrix against the guest already listening on
#             SSH_PORT instead of booting a fresh overlay (the state left
#             behind by a failed run, for inspection and re-runs); the
#             no-device negative test is skipped in this mode
#   MODE      which matrix to run: main (default), nodev or stress
#   VIRTIO_ADDR / E1000_ADDR
#             PCI slot of the virtio-9p / e1000 device.  At the default
#             slots both land on IRQ 11, and gnumach intermittently
#             deadlocks that shared line under add/remove churn (IOAPIC
#             pin masked with IRR latched; both devices starve; open
#             issue, not a driver bug).  Slots ≡ 1 (mod 4) route through
#             PIRQ A, which this guest assigns to IRQ 10, so virtio-9p
#             defaults to its own line.  Pass VIRTIO_ADDR= (empty) to
#             reproduce the shared-line wedge.
#   QTRACE    QEMU trace pattern (e.g. "ioapic_*") logged to
#             img/tmp/qtrace.log for IRQ-delivery forensics.  Only
#             meaningful with kernel_irqchip=split (QEMU_EXTRA below);
#             with the default in-kernel irqchip the guest IOAPIC is
#             emulated by KVM and QEMU sees none of its traffic.
#   QEMU_EXTRA
#             Extra command-line arguments passed to QEMU
#             (e.g. -machine kernel_irqchip=split).
#   SSH_PROBE_ATTEMPTS
#             Boot patience: number of 2-5 s SSH probes before the boot
#             is declared timed out (default 120; split-irqchip boots
#             need several hundred).
#   BASE_IMG  Base image for the overlay (default img/hurd.packages.qcow2).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# The packages variant of the image pipeline: it carries the account SSH
# keys (accounts stage) and the guest packages, unlike the raw base, so
# the harness can log in.  make virtio-test builds the stage chain
# (base -> accounts -> packages) automatically when it is missing or
# stale; standalone runs can point BASE_IMG anywhere.
BASE_IMG="${BASE_IMG:-${REPO_ROOT}/img/hurd.packages.qcow2}"
SHARE="${SHARE:-${REPO_ROOT}/img/share}"
MOUNT_TAG="${MOUNT_TAG:-hostshare}"
SSH_PORT="${SSH_PORT:-2222}"
SSH_HOST="${SSH_HOST:-127.0.0.1}"
MODE="${MODE:-main}"
# Keep virtio-9p off the e1000's IRQ line by default (see VIRTIO_ADDR
# above).  An empty override restores the shared-line configuration.
VIRTIO_ADDR="${VIRTIO_ADDR-0x5}"
E1000_ADDR="${E1000_ADDR-}"
SSH_KEY_DIR="${SSH_KEY_DIR:-${REPO_ROOT}/img/keys}"
SSH_KEY="${SSH_KEY_DIR}/root_ed25519"
VIRTIO_DIR="${VIRTIO_DIR:-${HOME}/src/virtio}"
VIRTIO_9P_BIN="${VIRTIO_9P_BIN:-${VIRTIO_DIR}/virtio-9p}"
P9FS_BIN="${P9FS_BIN:-${REPO_ROOT}/3p/9pfs/9pfs}"

for f in "$BASE_IMG" "$SSH_KEY" "$VIRTIO_9P_BIN" "$P9FS_BIN"; do
    if [[ ! -f "$f" ]]; then
        echo "virtio-test: missing $f" >&2
        echo "  (for $BASE_IMG: make stage STAGE=packages VARIANT=img/hurd.packages.qcow2)" >&2
        exit 1
    fi
done

mkdir -p "$SHARE"
printf 'from host\n' > "$SHARE/host-seed.txt"

TMP_DIR="${REPO_ROOT}/img/tmp"
mkdir -p "$TMP_DIR"
FAILED=0
REUSE="${REUSE:-0}"

VT_NAME="virtio-test"
VT_TMP_DIR="$TMP_DIR"
VT_QEMU_LOG="${TMP_DIR}/virtio-test-qemu.log"
VT_PIDFILE="${TMP_DIR}/virtio-test-qemu.pid"
VT_SSH_HOST="$SSH_HOST"
VT_SSH_PORT="$SSH_PORT"
VT_SSH_KEY="$SSH_KEY"
VT_BASE_IMG="$BASE_IMG"
VT_E1000_ADDR="$E1000_ADDR"
VT_TRACE="v9fs_*"
VT_QTRACE="${QTRACE:-}"
VT_QEMU_EXTRA="${QEMU_EXTRA:-}"
VT_RERUN_HINT="rerun the matrix against it with: REUSE=1 make virtio-test"
source "$SCRIPT_DIR/virtio_boot_lib.sh"

vt_check_pidfile "$REUSE"
trap 'vt_cleanup "$FAILED" "$REUSE"' EXIT

echo "virtio-test: booting with the virtio-9p device (tag $MOUNT_TAG)..."
if [[ "$REUSE" == "1" ]]; then
    if ! vt_ssh_alive; then
        echo "virtio-test: REUSE=1 but no guest answers on port $VT_SSH_PORT" >&2
        echo "  boot one first (without REUSE) or leave a failed run's QEMU running" >&2
        exit 1
    fi
    echo "virtio-test: reusing the guest already running on port $VT_SSH_PORT"
else
    vt_make_overlay
    # The fsdev/device pair carries the share; the v9fs_* traces are
    # enabled by the lib's family trace.  The no-device negative boot
    # passes no device arguments at all.
    vt_boot \
        -fsdev local,id=virtfs0,path="$SHARE",security_model=none \
        -device "virtio-9p-pci,fsdev=virtfs0,mount_tag=$MOUNT_TAG,disable-legacy=off${VIRTIO_ADDR:+,addr=$VIRTIO_ADDR}"
fi
SSH_PORT="$VT_SSH_PORT"

export SSH_HOST SSH_PORT SSH_KEY VIRTIO_9P_BIN P9FS_BIN SHARE MOUNT_TAG
export REUSE QEMU_LOG="$VT_QEMU_LOG" GDB="${GDB:-0}"

python3 "$SCRIPT_DIR/virtio_test.py" "$MODE" || FAILED=1

if [[ "$REUSE" == "1" ]]; then
    # The reused guest still has the device attached, so the no-device
    # negative test does not apply; leave the guest running for further
    # inspection or another REUSE run.
    if [[ "$FAILED" -eq 1 ]]; then
        exit 1
    fi
    echo "virtio-test: REUSE=1; guest left running, negative test skipped"
    exit 0
fi

if [[ "$FAILED" -eq 1 ]]; then
    exit 1
fi

if [[ "$MODE" == "main" ]]; then
    # The main matrix's python powers the guest off on success; wait for that
    # QEMU to exit and release the forwarded port before the no-device boot
    # binds the same port, or the new QEMU dies on the bind race.
    if [[ -n "$VT_QEMU_PID" ]]; then
        vt_wait_poweroff || FAILED=1
    fi
    if [[ "$FAILED" -eq 1 ]]; then
        exit 1
    fi

    echo "virtio-test: rebooting without the device for the negative test..."
    vt_boot
    python3 "$SCRIPT_DIR/virtio_test.py" nodev || FAILED=1
fi

vt_wait_poweroff
echo "virtio-test: done."
