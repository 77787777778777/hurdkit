#!/usr/bin/env bash
# Boot the Hurd guest with a host directory shared through QEMU's virtio-9p
# device and run the translator test matrix (scripts/virtio_test.py).
#
# The guest boots from a throw-away overlay of the base image, so the base
# stays pristine. The same overlay is reused for the no-device negative test
# at the end, which is why the translators installed into it survive the
# first poweroff.
#
# On failure QEMU is left running and the serial log is kept, so the guest
# can be inspected by hand (see the printed SSH command; GDB=1 makes the
# driver attach gdb to the stuck translators first).
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
#             Extra command-line arguments passed to boot_qemu's QEMU
#             invocation (e.g. -machine kernel_irqchip=split).
#   SSH_PROBE_ATTEMPTS
#             Boot patience: number of 2-5 s SSH probes before the boot
#             is declared timed out (default 120; split-irqchip boots
#             need several hundred).
#   BASE_IMG  Base image for the overlay (default img/hurd.base.qcow2).

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
# Which matrix to run in the guest: main (default), nodev or stress
# (scripts/virtio_test.py documents each).
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
OVERLAY=""
QEMU_LOG="${TMP_DIR}/virtio-test-qemu.log"
QEMU_PID=""
FAILED=0
REUSE="${REUSE:-0}"
PIDFILE="${TMP_DIR}/virtio-test-qemu.pid"

# One test guest at a time: a previous run's QEMU would collide on the
# forwarded port, and the collision otherwise surfaces as a confusing
# login failure minutes into the next run.  The pidfile names the owner,
# so leftover sessions are detected instead of guessed at with pgrep.
if [[ -f "$PIDFILE" ]]; then
    OLD_PID="$(cat "$PIDFILE" 2>/dev/null || true)"
    if [[ -n "$OLD_PID" ]] && kill -0 "$OLD_PID" 2>/dev/null \
        && grep -q qemu-system "/proc/$OLD_PID/comm" 2>/dev/null; then
        if [[ "$REUSE" != "1" ]]; then
            echo "virtio-test: previous test QEMU (pid $OLD_PID) still running;" >&2
            echo "  rerun with REUSE=1 to use it, or /bin/kill -9 $OLD_PID first" >&2
            exit 1
        fi
        # REUSE keeps the pidfile: it still identifies the live guest.
    else
        rm -f "$PIDFILE"
    fi
fi

cleanup() {
    if [[ "$FAILED" -eq 1 ]]; then
        if [[ -n "$QEMU_PID" ]]; then
            echo "virtio-test: FAILED; QEMU left running (pid $QEMU_PID), "
        else
            echo "virtio-test: FAILED; the reused guest is still running"
        fi
        echo "  serial log: $QEMU_LOG"
        echo "  inspect with: ssh -i $SSH_KEY -p $SSH_PORT root@$SSH_HOST"
        echo "  rerun the matrix against it with: REUSE=1 make virtio-test"
        exit 1
    fi
    if [[ -n "$QEMU_PID" ]] && kill -0 "$QEMU_PID" 2>/dev/null; then
        kill "$QEMU_PID" 2>/dev/null || true
        wait "$QEMU_PID" 2>/dev/null || true
    fi
    if [[ -n "$OVERLAY" ]]; then
        rm -f "$OVERLAY"
    fi
    if [[ "$REUSE" != "1" ]]; then
        rm -f "$PIDFILE"
    fi
}
trap cleanup EXIT

boot_qemu() {
    # The extra arguments are the fsdev/device pair; the no-device boot
    # passes none, which is the negative test's whole point. The v9fs_*
    # trace events show whether the device model ever sees a request, which
    # separates driver bugs from front-end bugs.
    : > "$QEMU_LOG"
    # Monitor socket: lets a wedged run be dissected live (info pic/ioapic,
    # registers, guest-physical memory) without SSH or QEMU symbols.
    MONITOR_SOCK="$(dirname "$QEMU_LOG")/virtio-test-monitor.sock"
    rm -f "$MONITOR_SOCK"
    qemu-system-x86_64 \
        "${QEMU_ACCEL[@]}" \
        -smp 4 \
        -m 2G \
        -drive file="$OVERLAY",if=ide,cache=writeback \
        -netdev user,id=net0,hostfwd=tcp:"$SSH_HOST":"$SSH_PORT"-:22 \
        -device e1000,netdev=net0${E1000_ADDR:+,addr="$E1000_ADDR"} \
        -trace enable='v9fs_*' \
        ${QTRACE:+-trace enable="$QTRACE" -D "$TMP_DIR/qtrace.log"} \
        ${QEMU_EXTRA:+$QEMU_EXTRA} \
        -monitor unix:"$MONITOR_SOCK",server,nowait \
        "$@" \
        -nographic > "$QEMU_LOG" 2>&1 &
    QEMU_PID=$!
    echo "$QEMU_PID" > "$PIDFILE"

    # With SSH_PORT=0 QEMU binds an ephemeral port; discover which one it
    # got so the SSH probe below and scripts/virtio_test.py can connect.
    if [[ "$SSH_PORT" == "0" || "$SSH_PORT" == "auto" ]]; then
        SSH_PORT=$(ss -ltnp 2>/dev/null | awk -v pid="$QEMU_PID" '
            $0 ~ "pid=" pid "," { n = split($4, a, ":"); print a[n]; exit }')
        if [[ -z "$SSH_PORT" ]]; then
            echo "virtio-test: could not discover the forwarded SSH port" >&2
            return 1
        fi
        export SSH_PORT
        echo "virtio-test: QEMU picked SSH port $SSH_PORT"
    fi

    # Probe with a real SSH login: QEMU binds the hostfwd port long before
    # the guest's sshd answers, so a TCP connect alone reports success
    # while the login would still race and die on a reset.
    for _ in $(seq 1 "${SSH_PROBE_ATTEMPTS:-120}"); do
        if ssh -i "$SSH_KEY" -p "$SSH_PORT" -o StrictHostKeyChecking=no \
            -o UserKnownHostsFile=/dev/null -o BatchMode=yes \
            -o LogLevel=ERROR -o ConnectTimeout=3 \
            root@"$SSH_HOST" true 2>/dev/null; then
            return 0
        fi
        if ! kill -0 "$QEMU_PID" 2>/dev/null; then
            echo "virtio-test: QEMU exited during boot" >&2
            cat "$QEMU_LOG" >&2
            return 1
        fi
        sleep 2
    done
    echo "virtio-test: timed out waiting for SSH on port $SSH_PORT" >&2
    cat "$QEMU_LOG" >&2
    return 1
}

wait_poweroff() {
    for _ in $(seq 1 60); do
        if ! kill -0 "$QEMU_PID" 2>/dev/null; then
            wait "$QEMU_PID" 2>/dev/null || true
            QEMU_PID=""
            return 0
        fi
        sleep 5
    done
    echo "virtio-test: the guest did not power off, terminating QEMU" >&2
    kill "$QEMU_PID" 2>/dev/null || true
    return 1
}

export SSH_HOST SSH_PORT SSH_KEY VIRTIO_9P_BIN P9FS_BIN SHARE MOUNT_TAG
export QEMU_LOG GDB="${GDB:-0}"

echo "virtio-test: booting with the virtio-9p device (tag $MOUNT_TAG)..."
if [[ "$REUSE" == "1" ]]; then
    if ! ssh -i "$SSH_KEY" -p "$SSH_PORT" -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null -o BatchMode=yes -o LogLevel=ERROR \
        -o ConnectTimeout=5 root@"$SSH_HOST" true 2>/dev/null; then
        echo "virtio-test: REUSE=1 but no guest answers on port $SSH_PORT" >&2
        echo "  boot one first (without REUSE) or leave a failed run's QEMU running" >&2
        exit 1
    fi
    echo "virtio-test: reusing the guest already running on port $SSH_PORT"
else
    OVERLAY="$(mktemp -u "$TMP_DIR/XXXXXX.qcow2")"
    qemu-img create -f qcow2 -b "$(readlink -f "$BASE_IMG")" -F qcow2 \
        "$OVERLAY" >/dev/null

    QEMU_ACCEL=()
    if [[ -r /dev/kvm && -w /dev/kvm ]]; then
        QEMU_ACCEL=(-enable-kvm -cpu host)
    else
        QEMU_ACCEL=(-cpu max)
    fi

    boot_qemu \
        -fsdev local,id=virtfs0,path="$SHARE",security_model=none \
        -device virtio-9p-pci,fsdev=virtfs0,mount_tag="$MOUNT_TAG",disable-legacy=off${VIRTIO_ADDR:+,addr="$VIRTIO_ADDR"}
fi

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
    if [[ -n "$QEMU_PID" ]]; then
        wait_poweroff || FAILED=1
    fi
    if [[ "$FAILED" -eq 1 ]]; then
        exit 1
    fi

    echo "virtio-test: rebooting without the device for the negative test..."
    boot_qemu
    python3 "$SCRIPT_DIR/virtio_test.py" nodev || FAILED=1
fi

wait_poweroff
echo "virtio-test: done."
