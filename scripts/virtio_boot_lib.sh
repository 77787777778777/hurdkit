#!/usr/bin/env bash
# Shared boot scaffolding for the virtio guest test harnesses
# (scripts/virtio_*_test.sh).  Every harness boots the same shape of
# guest -- a throwaway qcow2 overlay, e1000 user-net with a forwarded SSH
# port, its own virtio device on its own PCI slot, QEMU trace events for
# the device family, and a monitor socket for forensics -- and shares the
# pidfile/one-guest-at-a-time discipline, the SSH boot probe and the
# poweroff wait.  A harness sources this file, sets the VT_* variables
# below, and calls the vt_* functions; only its device arguments and its
# mode dispatch stay local.
#
# Variables the harness sets before sourcing:
#   VT_NAME        message prefix, e.g. "virtio-balloon-test"
#   VT_TMP_DIR     scratch directory (img/tmp)
#   VT_QEMU_LOG    QEMU serial log path (the harness's trace assertions
#                  grep this file)
#   VT_PIDFILE     pidfile path naming the harness's QEMU
#   VT_SSH_HOST / VT_SSH_PORT / VT_SSH_KEY
#   VT_BASE_IMG    image the overlay backs (only needed to boot fresh)
# Optional:
#   VT_MEM_MB      guest memory in MiB (default 2048)
#   VT_TRACE       default -trace pattern, e.g. 'virtio_balloon*'
#                  (empty disables the family trace)
#   VT_QTRACE      extra -trace pattern logged to $VT_TMP_DIR/qtrace.log
#                  (IRQ forensics; needs kernel_irqchip=split to see
#                  anything)
#   VT_QEMU_EXTRA  extra QEMU arguments (e.g. -machine kernel_irqchip=split)
#   VT_E1000_ADDR  PCI slot of the e1000 (default: QEMU's next free slot)
#   VT_RERUN_HINT  full "rerun ..." line printed on failure
#
# After vt_boot, the harness talks to the guest through VT_SSH_PORT and
# the monitor socket printed here; VT_QEMU_PID and VT_OVERLAY are set.

# One test guest at a time: a previous run's QEMU would collide on the
# forwarded port, and the collision otherwise surfaces as a confusing
# login failure minutes into the next run.  The pidfile names the owner,
# so leftover sessions are detected instead of guessed at with pgrep.
vt_check_pidfile() {
    local reuse="$1" old_pid
    if [[ -f "$VT_PIDFILE" ]]; then
        old_pid="$(cat "$VT_PIDFILE" 2>/dev/null || true)"
        if [[ -n "$old_pid" ]] && kill -0 "$old_pid" 2>/dev/null \
            && grep -q qemu-system "/proc/$old_pid/comm" 2>/dev/null; then
            if [[ "$reuse" != "1" ]]; then
                echo "$VT_NAME: previous test QEMU (pid $old_pid) still running;" >&2
                echo "  rerun with REUSE=1 to use it, or /bin/kill -9 $old_pid first" >&2
                exit 1
            fi
            # REUSE keeps the pidfile: it still identifies the live guest.
        else
            rm -f "$VT_PIDFILE"
        fi
    fi
}

vt_ssh_alive() {
    ssh -i "$VT_SSH_KEY" -p "$VT_SSH_PORT" -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null -o BatchMode=yes -o LogLevel=ERROR \
        -o ConnectTimeout="${1:-5}" root@"$VT_SSH_HOST" true 2>/dev/null
}

# Create the throwaway overlay the guest boots from; called once per run,
# before the first boot (the device-less negative boot reuses it, so
# whatever the first boot installed survives).
vt_make_overlay() {
    VT_OVERLAY="$(mktemp -u "$VT_TMP_DIR/XXXXXX.qcow2")"
    qemu-img create -f qcow2 -b "$(readlink -f "$VT_BASE_IMG")" -F qcow2 \
        "$VT_OVERLAY" >/dev/null
}

# Boot the guest.  The arguments are the extra QEMU arguments -- the
# harness's virtio device and its backend; no arguments means the
# device-less negative boot.  KVM is used when it is available.
vt_boot() {
    local trace_args=()
    if [[ -n "${VT_TRACE:-}" ]]; then
        trace_args=(-trace "enable=$VT_TRACE")
    fi
    VT_QEMU_ACCEL=()
    if [[ -r /dev/kvm && -w /dev/kvm ]]; then
        VT_QEMU_ACCEL=(-enable-kvm -cpu host)
    else
        VT_QEMU_ACCEL=(-cpu max)
    fi

    # The trace events show whether the device model ever saw a request,
    # which separates driver bugs from front-end bugs and backs the
    # host-side assertions.
    : > "$VT_QEMU_LOG"
    VT_MONITOR_SOCK="$(dirname "$VT_QEMU_LOG")/${VT_NAME}-monitor.sock"
    rm -f "$VT_MONITOR_SOCK"
    qemu-system-x86_64 \
        "${VT_QEMU_ACCEL[@]}" \
        -smp 4 \
        -m "${VT_MEM_MB:-2048}M" \
        -drive file="$VT_OVERLAY",if=ide,cache=writeback \
        -netdev user,id=net0,hostfwd=tcp:"$VT_SSH_HOST":"$VT_SSH_PORT"-:22 \
        -device e1000,netdev=net0${VT_E1000_ADDR:+,addr="$VT_E1000_ADDR"} \
        "${trace_args[@]}" \
        ${VT_QTRACE:+-trace enable="$VT_QTRACE" -D "$VT_TMP_DIR/qtrace.log"} \
        ${VT_QEMU_EXTRA:+$VT_QEMU_EXTRA} \
        -monitor unix:"$VT_MONITOR_SOCK",server,nowait \
        "$@" \
        -nographic > "$VT_QEMU_LOG" 2>&1 &
    VT_QEMU_PID=$!
    echo "$VT_QEMU_PID" > "$VT_PIDFILE"

    # With SSH_PORT=0 QEMU binds an ephemeral port; discover which one it
    # got so the SSH probe below and the python matrix can connect.
    if [[ "$VT_SSH_PORT" == "0" || "$VT_SSH_PORT" == "auto" ]]; then
        VT_SSH_PORT=$(ss -ltnp 2>/dev/null | awk -v pid="$VT_QEMU_PID" '
            $0 ~ "pid=" pid "," { n = split($4, a, ":"); print a[n]; exit }')
        if [[ -z "$VT_SSH_PORT" ]]; then
            echo "$VT_NAME: could not discover the forwarded SSH port" >&2
            return 1
        fi
        export VT_SSH_PORT
        echo "$VT_NAME: QEMU picked SSH port $VT_SSH_PORT"
    fi

    # Probe with a real SSH login: QEMU binds the hostfwd port long before
    # the guest's sshd answers, so a TCP connect alone reports success
    # while the login would still race and die on a reset.
    local attempt
    for attempt in $(seq 1 "${VT_SSH_PROBE_ATTEMPTS:-120}"); do
        if vt_ssh_alive 3; then
            return 0
        fi
        if ! kill -0 "$VT_QEMU_PID" 2>/dev/null; then
            echo "$VT_NAME: QEMU exited during boot" >&2
            cat "$VT_QEMU_LOG" >&2
            return 1
        fi
        sleep 2
    done
    echo "$VT_NAME: timed out waiting for SSH on port $VT_SSH_PORT" >&2
    cat "$VT_QEMU_LOG" >&2
    return 1
}

# Wait for a guest that powered itself off (the python matrix's success
# path), so the next boot can bind the same forwarded port.
vt_wait_poweroff() {
    # When reusing a guest we didn't start, VT_QEMU_PID is unset;
    # there's nothing to wait for or clean up.
    [[ -z "${VT_QEMU_PID:-}" ]] && return 0

    local attempt
    for attempt in $(seq 1 60); do
        if ! kill -0 "$VT_QEMU_PID" 2>/dev/null; then
            wait "$VT_QEMU_PID" 2>/dev/null || true
            VT_QEMU_PID=""
            return 0
        fi
        sleep 5
    done
    echo "$VT_NAME: the guest did not power off, terminating QEMU" >&2
    kill "$VT_QEMU_PID" 2>/dev/null || true
    return 1
}

# EXIT-trap body: on failure leave the scene for inspection, on success
# clean up the overlay and the pidfile.  FAILED/REUSE come from the
# caller's state.
vt_cleanup() {
    local failed="$1" reuse="$2"
    if [[ "$failed" -eq 1 ]]; then
        if [[ -n "${VT_QEMU_PID:-}" ]]; then
            echo "$VT_NAME: FAILED; QEMU left running (pid $VT_QEMU_PID)"
        else
            echo "$VT_NAME: FAILED; the reused guest is still running"
        fi
        echo "  serial log: $VT_QEMU_LOG"
        echo "  inspect with: ssh -i $VT_SSH_KEY -p $VT_SSH_PORT root@$VT_SSH_HOST"
        if [[ -n "${VT_RERUN_HINT:-}" ]]; then
            echo "  $VT_RERUN_HINT"
        fi
        exit 1
    fi
    if [[ -n "${VT_QEMU_PID:-}" ]] && kill -0 "$VT_QEMU_PID" 2>/dev/null; then
        kill "$VT_QEMU_PID" 2>/dev/null || true
        wait "$VT_QEMU_PID" 2>/dev/null || true
    fi
    if [[ -n "${VT_OVERLAY:-}" ]]; then
        rm -f "$VT_OVERLAY"
    fi
    if [[ "$reuse" != "1" ]]; then
        rm -f "$VT_PIDFILE"
    fi
}
