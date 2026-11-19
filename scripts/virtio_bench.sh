#!/usr/bin/env bash
# Boot the Hurd guest and run the virtio benchmark matrix
# (scripts/virtio_bench.py): raw block I/O (B1), tar create/extract (B2),
# filesystem dd (B3), and network driver throughput/latency (N1).
#
# The guest boots from a throwaway overlay of the staged image, so the
# staged image stays pristine. Disposable benchmark images/shares are
# created under img/tmp/ and are never the boot disk.
#
# Environment:
#   MODE        which matrix to run: block, fs-tar, fs-dd, net, all (default);
#               net runs two sequential legs (net-e1000, net-vnet), one guest
#               boot per network backend so only one NIC ever forwards SSH
#   BENCH_RUNS  timed iterations per case (default 3)
#   BENCH_JSON  "1" emits one JSON object per case/run (smp-style)
#   REUSE       "1" reuse a running guest (single-leg modes only)
#   BENCH_E1000_IMG / BENCH_VNET_IMG  images for the two network legs
#   BASE_IMG    base image (default img/hurd.bench.qcow2)
#   SSH_HOST / SSH_PORT
#   RAM_MB      guest memory (default 2048)
#   VIRTIO_DIR / VIRTIO_NET_DIR
#   VIRTIO_BLK_BIN / VBLK_CLIENT_BIN / VIRTIO_NET_BIN / VIRTIO_NET_CLIENT_BIN
#   B1_* / B3_* / N1_* size knobs

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BASE_IMG="${BASE_IMG:-${REPO_ROOT}/img/hurd.bench.qcow2}"
if [[ ! -f "$BASE_IMG" ]]; then BASE_IMG="${REPO_ROOT}/img/hurd.packages.qcow2"; fi
# One network backend per boot: e1000 (stock control path) and virtio-net
# legs boot separate guests.  Attaching both SSH-forwarding NICs to one
# guest puts two connected routes for 10.0.2.0/24 in its routing table, so
# the sshd replies race between slirp instances and the control login dies.
BENCH_E1000_IMG="${BENCH_E1000_IMG:-$BASE_IMG}"
BENCH_VNET_IMG="${BENCH_VNET_IMG:-${REPO_ROOT}/img/hurd.bench-vnet.qcow2}"
VIRTIO_DIR="${VIRTIO_DIR:-${HOME}/src/virtio}"
VIRTIO_NET_DIR="${VIRTIO_NET_DIR:-${HOME}/src/virtio}"
VIRTIO_BLK_BIN="${VIRTIO_BLK_BIN:-${VIRTIO_DIR}/virtio-blk}"
VBLK_CLIENT_BIN="${VBLK_CLIENT_BIN:-${VIRTIO_DIR}/vblk-client}"
VIRTIO_NET_BIN="${VIRTIO_NET_BIN:-${VIRTIO_NET_DIR}/virtio-net}"
VIRTIO_NET_CLIENT_BIN="${VIRTIO_NET_CLIENT_BIN:-${VIRTIO_NET_DIR}/vnet-client}"
VIRTIO_9P_BIN="${VIRTIO_9P_BIN:-${VIRTIO_DIR}/virtio-9p}"
NINEPFS_BIN="${NINEPFS_BIN:-${REPO_ROOT}/3p/9pfs/9pfs}"
SSH_PORT="${SSH_PORT:-2222}"
SSH_HOST="${SSH_HOST:-127.0.0.1}"
MODE="${MODE:-all}"
case "$MODE" in
    all|block|fs-tar|fs-dd|net|net-e1000|net-vnet) ;;
    *)
        echo "virtio-bench: unknown MODE '$MODE'" >&2
        echo "  valid modes: all, block, fs-tar, fs-dd, net, net-e1000, net-vnet" >&2
        exit 2 ;;
esac
RAM_MB="${RAM_MB:-2048}"
BENCH_RUNS="${BENCH_RUNS:-3}"
BENCH_JSON="${BENCH_JSON:-0}"
REUSE="${REUSE:-0}"
SSH_KEY_DIR="${SSH_KEY_DIR:-${REPO_ROOT}/img/keys}"
SSH_KEY="${SSH_KEY_DIR}/root_ed25519"

for f in "$BENCH_E1000_IMG" "$SSH_KEY"; do
    if [[ ! -f "$f" ]]; then
        echo "virtio-bench: missing $f" >&2
        exit 1
    fi
done
if [[ "$MODE" == net* && ! -f "$BENCH_VNET_IMG" ]]; then
    echo "virtio-bench: missing $BENCH_VNET_IMG (built by make virtio-bench-vnet-image)" >&2
    exit 1
fi

TMP_DIR="${REPO_ROOT}/img/tmp"
mkdir -p "$TMP_DIR"
RESULTS_DIR="${TMP_DIR}/results"
mkdir -p "$RESULTS_DIR"
FAILED=0

# Disposable images / share.
BENCH_VBLK="${TMP_DIR}/bench-vblk.img"
BENCH_ORDINARY="${TMP_DIR}/bench-ordinary.img"
BENCH_EXT2="${TMP_DIR}/bench-ext2.img"
BENCH_SHARE="${TMP_DIR}/bench-share"
if [[ "$REUSE" != "1" ]]; then
    rm -f "$BENCH_VBLK" "$BENCH_ORDINARY" "$BENCH_EXT2"
    # 128 MiB raw images.
    for img in "$BENCH_VBLK" "$BENCH_ORDINARY" "$BENCH_EXT2"; do
        python3 - "$img" <<'PY'
import sys
p=sys.argv[1]
with open(p,'wb') as f:
    f.truncate(128*1024*1024)
PY
    done
    rm -rf "$BENCH_SHARE"; mkdir -p "$BENCH_SHARE"
    # host-format ext2 if available
    if command -v mkfs.ext2 >/dev/null 2>&1; then
        mkfs.ext2 -F -b 4096 "$BENCH_EXT2" >/dev/null 2>&1 || true
    fi
fi

VT_NAME="virtio-bench"
VT_TMP_DIR="$TMP_DIR"
VT_QEMU_LOG="${TMP_DIR}/virtio-bench-qemu.log"
VT_PIDFILE="${TMP_DIR}/virtio-bench-qemu.pid"
VT_SSH_HOST="$SSH_HOST"
VT_SSH_PORT="$SSH_PORT"
VT_SSH_KEY="$SSH_KEY"
VT_MEM_MB="$RAM_MB"
VT_TRACE=""
VT_QTRACE=""
VT_QEMU_EXTRA="${QEMU_EXTRA:-}"
VT_RERUN_HINT="rerun against the running guest with: REUSE=1 MODE=$MODE make virtio-bench"
source "$SCRIPT_DIR/virtio_boot_lib.sh"

vt_check_pidfile "$REUSE"
trap 'vt_cleanup "$FAILED" "$REUSE"' EXIT

run_py() {
    SSH_PORT="$VT_SSH_PORT"
    export SSH_HOST SSH_PORT SSH_KEY
    export QEMU_LOG="$VT_QEMU_LOG"
    export MODE="$1" BENCH_RUNS BENCH_JSON REUSE GDB="${GDB:-0}"
    export BENCH_VBLK BENCH_ORDINARY BENCH_EXT2 BENCH_SHARE
    export VIRTIO_BLK_BIN VBLK_CLIENT_BIN VIRTIO_NET_BIN VIRTIO_NET_CLIENT_BIN VIRTIO_9P_BIN NINEPFS_BIN
    export VIRTIO_DIR VIRTIO_NET_DIR
    export TMP_DIR RESULTS_DIR
    echo "virtio-bench: applying $1 tests..." >&2
    python3 "$SCRIPT_DIR/virtio_bench.py" "$1" || FAILED=1
}

if [[ "$REUSE" == "1" ]]; then
    : > "$VT_QEMU_LOG"
    if ! vt_ssh_alive; then
        echo "virtio-bench: REUSE=1 but no guest answers on port $VT_SSH_PORT" >&2
        exit 1
    fi
    echo "virtio-bench: reusing the guest already running on port $VT_SSH_PORT"
    if [[ "$MODE" == "net" ]]; then
        echo "virtio-bench: REUSE needs a single leg; use MODE=net-e1000 or MODE=net-vnet" >&2
        FAILED=1
    else
        run_py "$MODE"
    fi
else
    case "$MODE" in
        net)
            # One boot per backend: the e1000 leg boots the bench image with
            # the stock NIC; the virtio-net leg boots the bench-vnet image
            # whose only NIC is virtio-net (SSH forward included).
            for leg in e1000 vnet; do
                if [[ "$leg" == "e1000" ]]; then
                    VT_BASE_IMG="$BENCH_E1000_IMG"
                    VT_NET_BACKEND=e1000
                else
                    VT_BASE_IMG="$BENCH_VNET_IMG"
                    VT_NET_BACKEND=virtio-net
                fi
                vt_make_overlay
                echo "virtio-bench: booting net leg $leg ($VT_NET_BACKEND, $VT_BASE_IMG)..."
                if ! vt_boot; then
                    FAILED=1
                    break
                fi
                run_py "net-$leg"
                # A failed run leaves a wedged guest behind; waiting for a
                # poweroff that will never come just stalls the operator.
                # vt_cleanup keeps QEMU alive for inspection either way.
                if [[ "$FAILED" -eq 0 ]] && ! vt_wait_poweroff; then
                    FAILED=1
                    break
                fi
                if [[ "$FAILED" -eq 1 ]]; then break; fi
            done
            ;;
        net-vnet)
            VT_BASE_IMG="$BENCH_VNET_IMG"
            VT_NET_BACKEND=virtio-net
            vt_make_overlay
            echo "virtio-bench: booting net-vnet leg ($VT_NET_BACKEND, $VT_BASE_IMG)..."
            if ! vt_boot; then
                FAILED=1
            else
                run_py "net-vnet"
                if [[ "$FAILED" -eq 0 ]] && ! vt_wait_poweroff; then
                    FAILED=1
                fi
            fi
            ;;
        *)
            VT_BASE_IMG="$BENCH_E1000_IMG"
            VT_NET_BACKEND=e1000
            vt_make_overlay
            echo "virtio-bench: booting... ($MODE)"
            # Block/9p devices plus e1000 control NIC; no second NIC here.
            vt_boot \
                -drive "if=none,id=vd0,file=$BENCH_VBLK,format=raw,cache=writeback" \
                -device "virtio-blk-pci,drive=vd0,disable-legacy=off,addr=0x5" \
                -drive "file=$BENCH_ORDINARY,format=raw,if=ide,cache=writeback,index=2" \
                -drive "if=none,id=vd1,file=$BENCH_EXT2,format=raw,cache=writeback" \
                -device "virtio-blk-pci,drive=vd1,disable-legacy=off,addr=0x9" \
                -fsdev "local,id=benchfs,path=$BENCH_SHARE,security_model=none" \
                -device "virtio-9p-pci,fsdev=benchfs,mount_tag=benchfs,disable-legacy=off,addr=0xd"
            run_py "$MODE"
            if [[ "$FAILED" -eq 0 ]] && ! vt_wait_poweroff; then
                FAILED=1
            fi
            if [[ "$MODE" == all && "$FAILED" -eq 0 ]]; then
                # virtio-net leg on its own boot (bench-vnet image, NIC = virtio-net).
                VT_BASE_IMG="$BENCH_VNET_IMG"
                VT_NET_BACKEND=virtio-net
                vt_make_overlay
                echo "virtio-bench: booting net-vnet leg..."
                if vt_boot; then
                    run_py "net-vnet"
                    if [[ "$FAILED" -eq 0 ]] && ! vt_wait_poweroff; then
                        FAILED=1
                    fi
                else
                    FAILED=1
                fi
            fi
            # Merge the per-leg summaries so MODE=all leaves one aggregate
            # containing every workload (B1, B2, B3, both N1 legs).
            if [[ "$MODE" == all ]]; then
                python3 - "$REPO_ROOT" <<'PY'
import glob, json, os, sys
root = sys.argv[1]
tmp = os.path.join(root, "img", "tmp")
cases, seen = [], set()
for f in sorted(glob.glob(os.path.join(tmp, "virtio-bench-summary-*.json"))):
    if f.endswith("virtio-bench-summary.json"):
        continue
    try:
        d = json.load(open(f))
    except Exception:
        continue
    for c in d.get("cases", []):
        key = (c.get("case"), c.get("run"), c.get("warmup"))
        if key in seen:
            continue
        seen.add(key)
        cases.append(c)
payload = {"meta": {"mode": "all"}, "cases": cases}
for d in (os.path.join(tmp, "results"), tmp):
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "virtio-bench-summary.json"), "w") as fh:
        json.dump(payload, fh, indent=2)
print("merged MODE=all summary:", len(cases), "cases")
PY
            fi
            ;;
    esac
fi

if [[ "$REUSE" == "1" ]]; then
    if [[ "$FAILED" -eq 1 ]]; then exit 1; fi
    echo "virtio-bench: REUSE=1; guest left running"
    exit 0
fi
if [[ "$FAILED" -eq 1 ]]; then exit 1; fi
# The net legs already waited for their own poweroffs inside the loop.
if [[ "$MODE" != net* ]]; then vt_wait_poweroff; fi
echo "virtio-bench: done."
