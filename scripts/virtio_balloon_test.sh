#!/usr/bin/env bash
# Boot the Hurd guest with QEMU's virtio-balloon device and run the
# balloon test matrix (scripts/virtio_balloon_test.py): the driver
# selftest, a host-driven inflate/deflate round trip through the QEMU
# monitor, churn, and the teardown path.
#
# The guest boots from a throw-away overlay of the staged image, so the
# image stays pristine.  The balloon device is pinned to its own PCI
# slot, away from the e1000's IRQ line (see VBALLOON_ADDR below): gnumach
# intermittently deadlocks a shared level line under add/remove churn,
# and the balloon driver, unlike virtio-9p, lives on no path that needs
# that sharing, so the default never shares.
#
# The virtio_balloon* QEMU trace events are enabled for every boot: they
# show what the device model saw (config writes from the guest, every PFN
# batch handled), which is the host-side evidence for the accounting
# assertions beyond the guest's own counters.
#
# On failure QEMU is left running and the serial log is kept, so the
# guest can be inspected by hand (see the printed SSH command; GDB=1
# makes the harness attach gdb to the stuck daemon first).
#
# The boot scaffolding (overlay, pidfile discipline, SSH probe, monitor
# socket, poweroff wait) lives in scripts/virtio_boot_lib.sh, shared with
# the other virtio harnesses.
#
# Environment:
#   VIRTIO_DIR  source directory of the virtio drivers (default
#               ~/src/virtio; a workbranch overrides this)
#   VIRTIO_BALLOON_BIN
#               balloon binary to install (default $VIRTIO_DIR/virtio-balloon)
#   SSH_HOST / SSH_PORT
#               address QEMU forwards the guest SSH port to (default
#               127.0.0.1 / 2222; "0" lets QEMU pick a free port)
#   REUSE       "1" run the matrix against the guest already listening on
#               SSH_PORT instead of booting a fresh overlay (the state left
#               behind by a failed run, for inspection and re-runs)
#   MODE        which matrix to run: main (default), nodev or stress
#   GDB         "1" installs gdb in the guest and dumps stacks on failure
#   BASE_IMG    base image for the overlay (default img/hurd.packages.qcow2)
#   RAM_MB      guest memory in MiB (default 2048); the matrix's balloon
#               targets are expressed against it
#   VBALLOON_ADDR
#               PCI slot of the balloon device.  Slots ≡ 1 (mod 4) route
#               through PIRQ A, which this guest assigns to IRQ 10, so the
#               default 0x5 keeps the balloon off the e1000's line.  An
#               empty override restores the shared-line configuration.
#   E1000_ADDR  PCI slot of the e1000 (default: QEMU's next free slot)
#   QEMU_EXTRA  extra arguments passed to QEMU
#   SSH_PROBE_ATTEMPTS
#               boot patience: number of 2-5 s SSH probes before the boot
#               is declared timed out (default 120)
#   CYCLES      churn cycles in stress mode (default 8, python side)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# The packages variant of the image pipeline: it carries the account SSH
# keys (accounts stage) and the guest packages, unlike the raw base, so
# the harness can log in.  make virtio-balloon-test builds the stage
# chain (base -> accounts -> packages) automatically when it is missing
# or stale; standalone runs can point BASE_IMG anywhere.
BASE_IMG="${BASE_IMG:-${REPO_ROOT}/img/hurd.packages.qcow2}"
VIRTIO_DIR="${VIRTIO_DIR:-${HOME}/src/virtio}"
VIRTIO_BALLOON_BIN="${VIRTIO_BALLOON_BIN:-${VIRTIO_DIR}/virtio-balloon}"
SSH_PORT="${SSH_PORT:-2222}"
SSH_HOST="${SSH_HOST:-127.0.0.1}"
MODE="${MODE:-main}"
# Keep the balloon off the e1000's IRQ line by default (see VBALLOON_ADDR
# above).  An empty override restores the shared-line configuration.
VBALLOON_ADDR="${VBALLOON_ADDR-0x5}"
E1000_ADDR="${E1000_ADDR-}"
RAM_MB="${RAM_MB:-2048}"
SSH_KEY_DIR="${SSH_KEY_DIR:-${REPO_ROOT}/img/keys}"
SSH_KEY="${SSH_KEY_DIR}/root_ed25519"

for f in "$BASE_IMG" "$SSH_KEY" "$VIRTIO_BALLOON_BIN"; do
    if [[ ! -f "$f" ]]; then
        echo "virtio-balloon-test: missing $f" >&2
        echo "  (for $BASE_IMG: make stage STAGE=packages VARIANT=img/hurd.packages.qcow2)" >&2
        exit 1
    fi
done

TMP_DIR="${REPO_ROOT}/img/tmp"
mkdir -p "$TMP_DIR"
FAILED=0
REUSE="${REUSE:-0}"

VT_NAME="virtio-balloon-test"
VT_TMP_DIR="$TMP_DIR"
VT_QEMU_LOG="${TMP_DIR}/virtio-balloon-test-qemu.log"
VT_PIDFILE="${TMP_DIR}/virtio-balloon-test-qemu.pid"
VT_SSH_HOST="$SSH_HOST"
VT_SSH_PORT="$SSH_PORT"
VT_SSH_KEY="$SSH_KEY"
VT_BASE_IMG="$BASE_IMG"
VT_MEM_MB="$RAM_MB"
VT_E1000_ADDR="$E1000_ADDR"
VT_TRACE="virtio_balloon*"
VT_QTRACE="${QTRACE:-}"
VT_QEMU_EXTRA="${QEMU_EXTRA:-}"
VT_RERUN_HINT="rerun the matrix against it with: REUSE=1 MODE=$MODE make virtio-balloon-test"
source "$SCRIPT_DIR/virtio_boot_lib.sh"

vt_check_pidfile "$REUSE"
trap 'vt_cleanup "$FAILED" "$REUSE"' EXIT

if [[ "$REUSE" == "1" ]]; then
    # Trace assertions grep this log; a reused guest has the traces of
    # every earlier run in it, so restart the evidence from zero.  The
    # QEMU process keeps its open fd, so truncation does not disturb it.
    : > "$VT_QEMU_LOG"
    if ! vt_ssh_alive; then
        echo "virtio-balloon-test: REUSE=1 but no guest answers on port $VT_SSH_PORT" >&2
        echo "  boot one first (without REUSE) or leave a failed run's QEMU running" >&2
        exit 1
    fi
    echo "virtio-balloon-test: reusing the guest already running on port $VT_SSH_PORT"
else
    vt_make_overlay
    echo "virtio-balloon-test: booting with the virtio-balloon device (RAM ${RAM_MB} MiB)..."
    # The balloon device lives on its own PCI slot by default; an empty
    # VBALLOON_ADDR restores the shared-line configuration.
    vt_boot -device "virtio-balloon-pci,disable-legacy=off${VBALLOON_ADDR:+,addr=$VBALLOON_ADDR}"
fi
SSH_PORT="$VT_SSH_PORT"

export SSH_HOST SSH_PORT SSH_KEY VIRTIO_BALLOON_BIN QEMU_LOG="$VT_QEMU_LOG"
export RAM_MB REUSE GDB="${GDB:-0}"

python3 "$SCRIPT_DIR/virtio_balloon_test.py" "$MODE" || FAILED=1

if [[ "$REUSE" == "1" ]]; then
    # The reused guest keeps running for further inspection or another
    # REUSE run; only the python matrix decides pass/fail here.
    if [[ "$FAILED" -eq 1 ]]; then
        exit 1
    fi
    echo "virtio-balloon-test: REUSE=1; guest left running"
    exit 0
fi

if [[ "$FAILED" -eq 1 ]]; then
    exit 1
fi

if [[ "$MODE" == "main" ]]; then
    # The main matrix's python powers the guest off on success; wait for
    # that QEMU to exit and release the forwarded port before the
    # no-device boot binds the same port, or the new QEMU dies on the bind
    # race.
    if [[ -n "$VT_QEMU_PID" ]]; then
        vt_wait_poweroff || FAILED=1
    fi
    if [[ "$FAILED" -eq 1 ]]; then
        exit 1
    fi

    echo "virtio-balloon-test: rebooting without the device for the negative test..."
    # "nodev" boots without the virtio-balloon device: the negative test
    # needs a guest where the driver finds nothing on the bus.
    vt_boot
    python3 "$SCRIPT_DIR/virtio_balloon_test.py" nodev || FAILED=1
fi

vt_wait_poweroff
echo "virtio-balloon-test: done."
