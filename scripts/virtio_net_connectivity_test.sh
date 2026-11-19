#!/usr/bin/env bash
# Boot the virtio-net-only guest image and verify end-to-end connectivity
# over virtio-net: SSH login, gateway ping, and external ping.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BASE_IMG="${BASE_IMG:-${REPO_ROOT}/img/hurd.vnet.qcow2}"
SSH_PORT="${SSH_PORT:-2222}"
SSH_HOST="${SSH_HOST:-127.0.0.1}"
SSH_KEY_DIR="${SSH_KEY_DIR:-${REPO_ROOT}/img/keys}"
SSH_KEY="${SSH_KEY_DIR}/root_ed25519"

for f in "$BASE_IMG" "$SSH_KEY"; do
    if [[ ! -f "$f" ]]; then
        echo "virtio-net-connectivity-test: missing $f" >&2
        exit 1
    fi
done

TMP_DIR="${REPO_ROOT}/img/tmp"
mkdir -p "$TMP_DIR"
FAILED=0

# Create overlay
OVERLAY="${TMP_DIR}/conn-overlay.qcow2"
rm -f "$OVERLAY"
qemu-img create -f qcow2 -b "$(readlink -f "$BASE_IMG")" -F qcow2 "$OVERLAY" >/dev/null

QEMU_LOG="${TMP_DIR}/virtio-net-connectivity-test-qemu.log"
QEMU_PIDFILE="${TMP_DIR}/virtio-net-connectivity-test-qemu.pid"

# Boot with only virtio-net
qemu-system-x86_64 -enable-kvm -cpu host -smp 4 -m 2048M \
  -drive file="$OVERLAY",if=ide,cache=writeback \
  -netdev user,id=vnet0,hostfwd=tcp:${SSH_HOST}:${SSH_PORT}-:22 \
  -device virtio-net-pci,netdev=vnet0,disable-legacy=off,addr=0x6 \
  -nographic > "$QEMU_LOG" 2>&1 &
QEMU_PID=$!
echo "$QEMU_PID" > "$QEMU_PIDFILE"

cleanup() {
    if [[ "$FAILED" -eq 1 ]]; then
        echo "virtio-net-connectivity-test: FAILED; QEMU left running (pid $QEMU_PID)"
        echo "  serial log: $QEMU_LOG"
    else
        /bin/kill "$QEMU_PID" 2>/dev/null || true
        wait "$QEMU_PID" 2>/dev/null || true
        rm -f "$OVERLAY" "$QEMU_PIDFILE"
    fi
}
trap cleanup EXIT

# Wait for SSH
for i in $(seq 1 90); do
    if ssh -i "$SSH_KEY" -p "$SSH_PORT" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o BatchMode=yes -o ConnectTimeout=3 -o LogLevel=ERROR root@"$SSH_HOST" true 2>/dev/null; then
        echo "SSH UP"
        break
    fi
    if ! kill -0 "$QEMU_PID" 2>/dev/null; then
        echo "virtio-net-connectivity-test: QEMU exited during boot"
        cat "$QEMU_LOG" >&2
        FAILED=1
        exit 1
    fi
    sleep 2
done

echo "virtio-net-connectivity-test: checking gateway ping..."
if ! ssh -i "$SSH_KEY" -p "$SSH_PORT" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o BatchMode=yes -o LogLevel=ERROR root@"$SSH_HOST" 'ping -c 3 -W 2 10.0.2.2'; then
    FAILED=1
fi

echo "virtio-net-connectivity-test: checking external ping..."
if ! ssh -i "$SSH_KEY" -p "$SSH_PORT" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o BatchMode=yes -o LogLevel=ERROR root@"$SSH_HOST" 'ping -c 2 -W 3 1.1.1.1'; then
    FAILED=1
fi

if [[ "$FAILED" -eq 0 ]]; then
    echo "virtio-net-connectivity-test: PASS"
fi

echo "virtio-net-connectivity-test: done."
