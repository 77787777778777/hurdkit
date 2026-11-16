#!/usr/bin/env bash
# Boot the Hurd guest with QEMU's virtio-balloon device and run the
# balloon test matrix (scripts/virtio_balloon_test.py): the driver
# selftest, a host-driven inflate/deflate round trip through the QEMU
# monitor, churn, and the driver restart/teardown path.
#
# The guest boots from a throw-away overlay of the base image, so the base
# stays pristine.  The balloon device is pinned to its own PCI slot, away
# from the e1000's IRQ line (see VBALLOON_ADDR below): gnumach
# intermittently deadlocks a shared level line under add/remove churn, and
# the balloon driver, unlike virtio-9p, lives on no path that needs that
# sharing, so the default never shares.
#
# The virtio_balloon* QEMU trace events are enabled for every boot: they
# show what the device model saw (config writes from the guest, every PFN
# batch handled), which is the host-side evidence for the accounting
# assertions beyond the guest's own counters.
#
# On failure QEMU is left running and the serial log is kept, so the guest
# can be inspected by hand (see the printed SSH command; GDB=1 makes the
# harness attach gdb to the stuck daemon first).
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
#   BASE_IMG    base image for the overlay (default img/hurd.base.qcow2)
#   RAM_MB      guest memory in MiB (default 2048); the matrix's balloon
#               targets are expressed against it
#   VBALLOON_ADDR
#               PCI slot of the balloon device.  Slots ≡ 1 (mod 4) route
#               through PIRQ A, which this guest assigns to IRQ 10, so the
#               default 0x5 keeps the balloon off the e1000's line.  An
#               empty override restores the shared-line configuration.
#   E1000_ADDR  PCI slot of the e1000 (default: QEMU's next free slot)
#   QEMU_EXTRA  extra arguments passed to the QEMU invocation
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
OVERLAY=""
QEMU_LOG="${TMP_DIR}/virtio-balloon-test-qemu.log"
QEMU_PID=""
FAILED=0
REUSE="${REUSE:-0}"
PIDFILE="${TMP_DIR}/virtio-balloon-test-qemu.pid"

# One test guest at a time: a previous run's QEMU would collide on the
# forwarded port, and the collision otherwise surfaces as a confusing
# login failure minutes into the next run.  The pidfile names the owner,
# so leftover sessions are detected instead of guessed at with pgrep.
if [[ -f "$PIDFILE" ]]; then
    OLD_PID="$(cat "$PIDFILE" 2>/dev/null || true)"
    if [[ -n "$OLD_PID" ]] && kill -0 "$OLD_PID" 2>/dev/null \
        && grep -q qemu-system "/proc/$OLD_PID/comm" 2>/dev/null; then
        if [[ "$REUSE" != "1" ]]; then
            echo "virtio-balloon-test: previous test QEMU (pid $OLD_PID) still running;" >&2
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
            echo "virtio-balloon-test: FAILED; QEMU left running (pid $QEMU_PID)"
        else
            echo "virtio-balloon-test: FAILED; the reused guest is still running"
        fi
        echo "  serial log: $QEMU_LOG"
        echo "  inspect with: ssh -i $SSH_KEY -p $SSH_PORT root@$SSH_HOST"
        echo "  rerun the matrix against it with: REUSE=1 MODE=$MODE make virtio-balloon-test"
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
    # "nodev" boots without the virtio-balloon device: the negative test
    # needs a guest where the driver finds nothing on the bus.
    local dev_args=()
    if [[ "${1:-}" != "nodev" ]]; then
        dev_args=(-device "virtio-balloon-pci,disable-legacy=off${VBALLOON_ADDR:+,addr=$VBALLOON_ADDR}")
    fi
    # The virtio_balloon_* traces show whether the device model ever saw a
    # config write or a PFN batch, which separates driver bugs from
    # front-end bugs and backs the host-side accounting assertions.
    : > "$QEMU_LOG"
    MONITOR_SOCK="$(dirname "$QEMU_LOG")/virtio-balloon-test-monitor.sock"
    rm -f "$MONITOR_SOCK"
    qemu-system-x86_64 \
        "${QEMU_ACCEL[@]}" \
        -smp 4 \
        -m "${RAM_MB}M" \
        -drive file="$OVERLAY",if=ide,cache=writeback \
        -netdev user,id=net0,hostfwd=tcp:"$SSH_HOST":"$SSH_PORT"-:22 \
        -device e1000,netdev=net0${E1000_ADDR:+,addr="$E1000_ADDR"} \
        "${dev_args[@]}" \
        -trace enable='virtio_balloon*' \
        ${QTRACE:+-trace enable="$QTRACE" -D "$TMP_DIR/qtrace.log"} \
        ${QEMU_EXTRA:+$QEMU_EXTRA} \
        -monitor unix:"$MONITOR_SOCK",server,nowait \
        -nographic > "$QEMU_LOG" 2>&1 &
    QEMU_PID=$!
    echo "$QEMU_PID" > "$PIDFILE"

    # With SSH_PORT=0 QEMU binds an ephemeral port; discover which one it
    # got so the SSH probe below and the python matrix can connect.
    if [[ "$SSH_PORT" == "0" || "$SSH_PORT" == "auto" ]]; then
        SSH_PORT=$(ss -ltnp 2>/dev/null | awk -v pid="$QEMU_PID" '
            $0 ~ "pid=" pid "," { n = split($4, a, ":"); print a[n]; exit }')
        if [[ -z "$SSH_PORT" ]]; then
            echo "virtio-balloon-test: could not discover the forwarded SSH port" >&2
            return 1
        fi
        export SSH_PORT
        echo "virtio-balloon-test: QEMU picked SSH port $SSH_PORT"
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
            echo "virtio-balloon-test: QEMU exited during boot" >&2
            cat "$QEMU_LOG" >&2
            return 1
        fi
        sleep 2
    done
    echo "virtio-balloon-test: timed out waiting for SSH on port $SSH_PORT" >&2
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
    echo "virtio-balloon-test: the guest did not power off, terminating QEMU" >&2
    kill "$QEMU_PID" 2>/dev/null || true
    return 1
}

export SSH_HOST SSH_PORT SSH_KEY VIRTIO_BALLOON_BIN QEMU_LOG RAM_MB
export GDB="${GDB:-0}"

if [[ "$REUSE" == "1" ]]; then
    # Trace assertions grep this log; a reused guest has the traces of
    # every earlier run in it, so restart the evidence from zero.  The
    # QEMU process keeps its open fd, so truncation does not disturb it.
    : > "$QEMU_LOG"
    if ! ssh -i "$SSH_KEY" -p "$SSH_PORT" -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null -o BatchMode=yes -o LogLevel=ERROR \
        -o ConnectTimeout=5 root@"$SSH_HOST" true 2>/dev/null; then
        echo "virtio-balloon-test: REUSE=1 but no guest answers on port $SSH_PORT" >&2
        echo "  boot one first (without REUSE) or leave a failed run's QEMU running" >&2
        exit 1
    fi
    echo "virtio-balloon-test: reusing the guest already running on port $SSH_PORT"
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

    echo "virtio-balloon-test: booting with the virtio-balloon device (RAM ${RAM_MB} MiB)..."
    boot_qemu
fi

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
    if [[ -n "$QEMU_PID" ]]; then
        wait_poweroff || FAILED=1
    fi
    if [[ "$FAILED" -eq 1 ]]; then
        exit 1
    fi

    echo "virtio-balloon-test: rebooting without the device for the negative test..."
    boot_qemu nodev
    python3 "$SCRIPT_DIR/virtio_balloon_test.py" nodev || FAILED=1
fi

wait_poweroff
echo "virtio-balloon-test: done."
