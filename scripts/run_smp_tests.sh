#!/usr/bin/env bash
# Boot the Hurd guest with an SMP gnumach kernel (-smp 2) and run the
# SMP test matrix: the userland `smp` command, the in-guest pset stress
# binary, a multi-threaded execution stress test, a lock contention stress
# test, a readers-writer lock test, a condition-variable test, a POSIX
# semaphore test, a Mach exception delivery test, a fork/exec test, a Mach
# IPC token ring, a Mach RPC request/reply test, a Unix domain socket echo
# test, a directory VFS test, a VM map stress test exercising cross-CPU
# pmap_update IPIs, a shared mmap visibility test, a cross-CPU signal
# delivery test, a signal-interrupting-mach_msg test, a POSIX pipe I/O
# stress test, a per-CPU timer stress test, a concurrent file I/O stress
# test, a shared file-descriptor I/O test, a thread create/join churn test,
# a page permission change test (vm_protect), a Mach port right transfer
# test (MOVE_SEND), and a CPU affinity / thread binding test.
#
# The guest boots from a throwaway overlay of the SMP kernel image, so the
# staged image stays pristine. The overlay is created fresh each run; use
# REUSE=1 to reattach to a running guest (e.g. after a failed run).
#
# On failure QEMU is left running and the serial log is kept, so the guest
# can be inspected by hand (see the printed SSH command).
#
# Environment:
#   SMP_IMG        SMP kernel image to boot (default img/hurd.kernel.smp.qcow2)
#   SSH_HOST       address QEMU forwards the guest SSH port to (default 127.0.0.1)
#   SSH_PORT       forwarded SSH port (default 2222; "0" lets QEMU pick)
#   GDB            "1" installs gdb in the guest and dumps stacks on failure
#   REUSE          "1" run against the guest already listening on SSH_PORT
#   SMP_CPU_COUNT  vCPUs exposed to the guest (default 2; must equal
#                  GNUMACH_NCPUS or gnumach's ACPI parse aborts)
#   SSH_PROBE_ATTEMPTS  boot patience: number of 2-5 s SSH probes (default 120)
#   XCC            path to the x86_64-gnu cross compiler
#                  (default $HOME/opt/x86_64-gnu/bin/x86_64-gnu-gcc)
#
# The SMP tests are Hurd user-space programs (Mach traps + pthreads). They are
# cross-compiled on the host with x86_64-gnu-gcc and copied into the guest;
# they are never built with the host's Linux gcc, which produces binaries
# that cannot run Mach/Hurd code in the guest.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

SMP_UP="${SMP_UP:-0}"
if [[ "$SMP_UP" == "1" ]]; then
    SMP_IMG="${SMP_IMG:-${REPO_ROOT}/img/hurd.kernel.up.qcow2}"
    SMP_CPU_COUNT="${SMP_CPU_COUNT:-1}"
else
    SMP_IMG="${SMP_IMG:-${REPO_ROOT}/img/hurd.kernel.smp.qcow2}"
    SMP_CPU_COUNT="${SMP_CPU_COUNT:-2}"
fi
SSH_HOST="${SSH_HOST:-127.0.0.1}"
SSH_PORT="${SSH_PORT:-2222}"
GDB="${GDB:-0}"
REUSE="${REUSE:-0}"
SMP_CPU_COUNT="${SMP_CPU_COUNT:-2}"
SSH_KEY_DIR="${SSH_KEY_DIR:-${REPO_ROOT}/img/keys}"
SSH_KEY="${SSH_KEY_DIR}/root_ed25519"
XCC="${XCC:-$HOME/opt/x86_64-gnu/bin/x86_64-gnu-gcc}"
# Per-phase timeout (seconds): a guest phase that hangs (e.g. a cross-CPU
# TLB shootdown deadlock) is killed and reported as failed instead of
# blocking the whole matrix forever.
PHASE_TIMEOUT="${PHASE_TIMEOUT:-120}"

if [[ ! -f "$SMP_IMG" ]]; then
    echo "run-smp-tests: missing $SMP_IMG" >&2
    if [[ "$SMP_UP" == "1" ]]; then
        echo "  build one: make up-kernel &&" >&2
        echo "    make stage STAGE=kernel VARIANT=$SMP_IMG" >&2
    else
        echo "  build one: make smp-kernel GNUMACH_NCPUS=$SMP_CPU_COUNT &&" >&2
    fi
    echo "        CUSTOM_KERNEL=\$(PREFIX)/bin/x86_64-gnu-build/gnumach-kernel/gnumach" >&2
    echo "        STAGE_INPUT=img/hurd.packages.qcow2" >&2
    exit 1
fi
if [[ ! -f "$SSH_KEY" ]]; then
    echo "run-smp-tests: missing $SSH_KEY" >&2
    echo "  (the packages stage provisions the root SSH key)" >&2
    exit 1
fi
if [[ ! -x "$XCC" ]]; then
    echo "run-smp-tests: cross compiler not found: $XCC" >&2
    echo "  build it: make -C toolchain gcc1 gcc2 glibc" >&2
    exit 1
fi

TMP_DIR="${REPO_ROOT}/img/tmp"
mkdir -p "$TMP_DIR"
FAILED=0

# --- timestamped benchmark results log ---
RESULTS_DIR="${REPO_ROOT}/img/tmp/results"
mkdir -p "$RESULTS_DIR"
RESULTS_FILE="${RESULTS_DIR}/smp-$(date +%Y%m%d-%H%M%S).log"
{
    echo "SMP test run: $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    echo "  image: $SMP_IMG"
    echo "  qemu flags: -smp $SMP_CPU_COUNT"
    echo "  host: $SSH_HOST:$SSH_PORT"
    echo
} | tee "$RESULTS_FILE"

# Run a guest phase, capturing output to stdout and the results log.
# When the guest binary emits JSON (SMP_JSON=1), parse it with jq to
# extract pass/fail, elapsed_ms, rate and unit.  Otherwise fall back to
# exit-code-only pass/fail (e.g. smp /bin/true).  Sets FAILED on
# non-zero exit. Skipped if a prior phase already failed.
run_phase() {
    local phase="$1"; shift
    local out rc summary passed elapsed_ms rate unit
    echo "run-smp-tests: running $phase..."
    # Run every phase even if a prior phase failed/hung (no short-circuit);
    # overall pass/fail is tracked in FAILED for the summary and cleanup.
    if true; then
        out=$(guest_run "$@" 2>&1) || rc=$?
        echo "$out" | tee -a "$RESULTS_FILE"

        # Defaults
        elapsed_ms=0
        rate=0
        unit="n/a"
        passed=true

        # Check if output is JSON (test binaries emit one JSON line when SMP_JSON=1)
        if echo "$out" | grep -q '^{\s*"phase"'; then
            summary=$(echo "$out" | grep '^{\s*"phase"' | tail -1)
            passed=$(echo "$summary" | jq -r 'if has("passed") then .passed else true end')
            elapsed_ms=$(echo "$summary" | jq -r '.elapsed_ms // 0')
            rate=$(echo "$summary" | jq -r '.rate // 0')
            unit=$(echo "$summary" | jq -r '.unit // "n/a"')
            if [[ "$passed" != "true" ]]; then
                FAILED=1
            fi
        elif [[ -n "${rc:-}" && "$rc" -ne 0 ]]; then
            passed=false
            FAILED=1
        fi

        # Save the raw JSON line as the per-phase JSON file (sanitize phase
        # name for use as a filename — "smp /bin/true" -> "smp_bin")
        safe_phase=$(echo "$phase" | tr ' /' '__')
        if echo "$out" | grep -q '^{\s*"phase"'; then
            echo "$out" | grep '^{\s*"phase"' | tail -1 > "${RESULTS_DIR}/${safe_phase}.json" 2>/dev/null || true
        else
            # No JSON emitted (e.g. smp /bin/true) — write a synthetic entry
            # so the phase still appears in summary.json.
            local synth_passed
            if [[ -n "${rc:-}" && "$rc" -ne 0 ]]; then
                synth_passed="false"
            else
                synth_passed="true"
            fi
            cat > "${RESULTS_DIR}/${safe_phase}.json" <<JSONEOF
{"phase":"${phase}","passed":${synth_passed},"elapsed_ms":0,"rate":0,"unit":"n/a"}
JSONEOF
        fi

        echo "${phase}: passed=${passed} elapsed_ms=${elapsed_ms} rate=${rate} ${unit}" | tee -a "$RESULTS_FILE"

        if [[ -n "${rc:-}" && "$passed" != "true" && -z "${summary:-}" ]]; then
            FAILED=1
            echo "run-smp-tests: $phase FAILED (exit $rc)" | tee -a "$RESULTS_FILE"
        fi
    fi
}

VT_NAME="run-smp-tests"
VT_TMP_DIR="$TMP_DIR"
VT_QEMU_LOG="${TMP_DIR}/run-smp-tests-qemu.log"
VT_PIDFILE="${TMP_DIR}/run-smp-tests-qemu.pid"
VT_SSH_HOST="$SSH_HOST"
VT_SSH_PORT="$SSH_PORT"
VT_SSH_KEY="$SSH_KEY"
VT_BASE_IMG="$SMP_IMG"
VT_RERUN_HINT="rerun against the running guest with: REUSE=1 $0"
source "$SCRIPT_DIR/virtio_boot_lib.sh"

# --- build the SMP test binaries on the host with the cross toolchain ---
# Fail fast here (before booting) if the cross toolchain can't build them.
BUILD_DIR="$TMP_DIR/smp-build"
mkdir -p "$BUILD_DIR"

build_one() {
    local src="$1" out="$2"; shift 2
    "$XCC" -o "$BUILD_DIR/$out" "$REPO_ROOT/tests/smp/$src" "$@" || {
        echo "run-smp-tests: failed to cross-compile $src" >&2
        return 1
    }
    file "$BUILD_DIR/$out" | grep -q 'for GNU/Hurd' || {
        echo "run-smp-tests: $out is not a GNU/Hurd ELF (wrong toolchain?)" >&2
        return 1
    }
}

build_one pset_stress.c   pset_stress   -lmachuser || FAILED=1
build_one thread_stress.c thread_stress -lpthread  || FAILED=1
build_one lock_stress.c    lock_stress    -lpthread  || FAILED=1
build_one rwlock_stress.c  rwlock_stress  -lpthread  || FAILED=1
build_one fork_exec_stress.c fork_exec_stress -lpthread  || FAILED=1
build_one cond_stress.c     cond_stress    -lpthread  || FAILED=1
build_one sem_stress.c      sem_stress     -lpthread  || FAILED=1
build_one exception_stress.c exception_stress -lpthread -lmachuser  || FAILED=1
build_one mach_signal_stress.c mach_signal_stress -lpthread -lmachuser  || FAILED=1
build_one fd_stress.c       fd_stress      -lpthread  || FAILED=1
build_one mmap_stress.c     mmap_stress    -lpthread  || FAILED=1
build_one sock_stress.c     sock_stress    -lpthread  || FAILED=1
build_one rpc_stress.c     rpc_stress    -lpthread -lmachuser || FAILED=1
build_one dir_stress.c     dir_stress    -lpthread  || FAILED=1
build_one ipc_stress.c     ipc_stress    -lpthread -lmachuser || FAILED=1
build_one vm_stress.c      vm_stress     -lpthread -lmachuser || FAILED=1
build_one signal_stress.c  signal_stress -lpthread  || FAILED=1
build_one pipe_stress.c    pipe_stress   -lpthread  || FAILED=1
build_one timer_stress.c   timer_stress -lpthread  || FAILED=1
build_one file_stress.c    file_stress  -lpthread  || FAILED=1
build_one vm_protect_stress.c vm_protect_stress -lpthread  || FAILED=1
build_one port_transfer_stress.c port_transfer_stress -lpthread -lmachuser  || FAILED=1
build_one affinity_stress.c affinity_stress -lpthread -lmachuser  || FAILED=1

if [[ "$FAILED" -ne 0 ]]; then
    exit 1
fi

vt_check_pidfile "$REUSE"
trap 'vt_cleanup "$FAILED" "$REUSE"' EXIT

echo "run-smp-tests: booting SMP kernel ($SMP_IMG) with -smp $SMP_CPU_COUNT..."
if [[ "$REUSE" == "1" ]]; then
    if ! vt_ssh_alive; then
        echo "run-smp-tests: REUSE=1 but no guest answers on port $VT_SSH_PORT" >&2
        exit 1
    fi
    echo "run-smp-tests: reusing the guest already running on port $VT_SSH_PORT"
else
    vt_make_overlay
    vt_boot \
        -smp "$SMP_CPU_COUNT"
fi
SSH_PORT="$VT_SSH_PORT"

export SSH_HOST SSH_PORT SSH_KEY GDB SMP_JSON=1 SMP_UP

# --- copy the pre-built binaries into the guest ---
put_guest() {
    scp -P "$SSH_PORT" -i "$SSH_KEY" -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null -o BatchMode=yes -o LogLevel=ERROR \
        -o ConnectTimeout=10 "$BUILD_DIR/$1" root@"$SSH_HOST:/tmp/$1" || FAILED=1
}

# SSH options shared by every guest command
SSH_OPTS=(-i "$SSH_KEY" -p "$SSH_PORT" -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null -o BatchMode=yes -o LogLevel=ERROR \
    -o ConnectTimeout=10)

# Copy the pre-built binaries into the guest (always, even if a prior phase
# failed — binaries are already built and independent of phase results).
put_guest pset_stress
put_guest thread_stress
put_guest lock_stress
put_guest rwlock_stress
put_guest fork_exec_stress
put_guest cond_stress
put_guest sem_stress
put_guest exception_stress
put_guest rpc_stress
put_guest sock_stress
put_guest dir_stress
put_guest ipc_stress
put_guest vm_stress
put_guest mmap_stress
put_guest signal_stress
put_guest mach_signal_stress
put_guest pipe_stress
put_guest timer_stress
put_guest file_stress
put_guest fd_stress
put_guest vm_protect_stress
put_guest port_transfer_stress
put_guest affinity_stress
put_guest thread_churn

guest_run() {
    timeout "$PHASE_TIMEOUT" ssh "${SSH_OPTS[@]}" root@"$SSH_HOST" "SMP_JSON=1 SMP_UP=$SMP_UP $*"
}

# --- Phase 1: the `smp` userland command ---
# `smp /bin/true` requires host_processor_sets() to return >= 2 psets and
# task_assign() to move the calling task onto the slave pset. Exit 0 proves
# both CPU 0 and CPU 1 are up and schedulable.  Skipped on UP (1 CPU) since
# there is only one pset.
echo "run-smp-tests: running 'smp /bin/true'..."
if [[ "$SMP_UP" != "1" ]]; then
    run_phase "smp /bin/true" "smp" "/bin/true"
elif [[ "$SMP_UP" == "1" ]]; then
    echo "run-smp-tests: skipping smp /bin/true (UP mode: only 1 pset)"
fi

# --- Phase 2: the pset stress binary ---
# Compiled on the host with the cross toolchain (see above) and copied in;
# run the task_assign ping-pong between default and slave pset.  Skipped on
# UP since host_processor_sets() returns only 1 pset.
if [[ "$SMP_UP" != "1" ]]; then
    run_phase "pset_stress" "/tmp/pset_stress" "10"
elif [[ "$SMP_UP" == "1" ]]; then
    echo "run-smp-tests: skipping pset_stress (UP mode: only 1 pset)"
fi

# --- Phase 3: multi-threaded concurrent execution stress ---
# Create one thread per online CPU, synchronize them on a barrier, and
# have each increment a shared counter.  Proves the kernel actually
# schedules threads across more than one CPU.
run_phase "thread_stress" "/tmp/thread_stress"

# --- Phase 4: lock contention across CPUs ---
# All threads hammer a single pthread_mutex protecting a shared counter.
# Proves the lock serializes correctly across threads on different CPUs
# (no lost increments, no deadlock under contention).
run_phase "lock_stress" "/tmp/lock_stress"

# --- Phase 5: readers-writer lock across CPUs ---
# Readers acquire a shared (read) lock and verify shared data is
# non-decreasing; one writer takes the exclusive (write) lock and
# increments the data.  Exercises shared/exclusive locking across CPUs.
run_phase "rwlock_stress" "/tmp/rwlock_stress"

# --- Phase 6: condition variable wait/wake across CPUs ---
# One producer thread broadcasts a condition variable; consumer threads
# wait on it and consume produced items.  Exercises cond park/unpark
# across threads that may run on different CPUs.  Needs >= 2 CPUs, so
# skipped on UP.
if [[ "$SMP_UP" != "1" ]]; then
    run_phase "cond_stress" "/tmp/cond_stress"
elif [[ "$SMP_UP" == "1" ]]; then
    echo "run-smp-tests: skipping cond_stress (UP mode: needs >= 2 CPUs)"
fi

# --- Phase 7: POSIX semaphore wait/post across CPUs ---
# One producer thread posts a semaphore; consumer threads block on
# sem_wait and count posts.  Exercises semaphore block/wakeup across
# CPUs, distinct from mutexes and cond variables.  Needs >= 2 CPUs, so
# skipped on UP.
if [[ "$SMP_UP" != "1" ]]; then
    run_phase "sem_stress" "/tmp/sem_stress"
elif [[ "$SMP_UP" == "1" ]]; then
    echo "run-smp-tests: skipping sem_stress (UP mode: needs >= 2 CPUs)"
fi

# --- Phase 8: Mach exception delivery across CPUs ---
# Thread 0 installs a Mach exception port as the task exception port and
# dereferences a PROT_NONE page, triggering EXC_BAD_ACCESS.  Thread 1
# receives the kernel's exception message on a different CPU, mprotects
# the page readable, and replies KERN_SUCCESS, which makes the kernel
# resume thread 0 at the faulting instruction (now successful).  A
# KERN_FAILURE reply would instead terminate the task (the kernel has no
# in-kernel signal translation; glibc's displaced exception server does
# that).  Exercises task exception port setup, kernel exception message
# generation/delivery, and cross-CPU exception receipt.
# Needs >= 2 CPUs, so skipped on UP.
if [[ "$SMP_UP" != "1" ]]; then
    run_phase "exception_stress" "/tmp/exception_stress"
elif [[ "$SMP_UP" == "1" ]]; then
    echo "run-smp-tests: skipping exception_stress (UP mode: needs >= 2 CPUs)"
fi

# --- Phase 9: process creation (fork/exec) across CPUs ---
# Each thread repeatedly forks; the child execs /bin/true and exits,
# while the parent waits.  Exercises process creation, address-space
# setup, page-table construction, and program loading on secondary CPUs.
run_phase "fork_exec_stress" "/tmp/fork_exec_stress"

# --- Phase 10: Mach RPC request/reply across CPUs ---
# Thread 0 (client) sends requests with its own port as the reply port to
# thread 1 (server), which replies; the remaining threads idle.  Exercises
# the full Mach RPC path across threads on different CPUs.  Needs >= 2
# CPUs, so skipped on UP.
if [[ "$SMP_UP" != "1" ]]; then
    run_phase "rpc_stress" "/tmp/rpc_stress"
elif [[ "$SMP_UP" == "1" ]]; then
    echo "run-smp-tests: skipping rpc_stress (UP mode: needs >= 2 CPUs)"
fi

# --- Phase 11: Unix domain socket I/O across CPUs ---
# Thread 0 (client) sends messages to thread 1 (server) over a
# socketpair; the server echoes each message back and the client verifies
# the echo.  Exercises socket-buffer and credential passing, a different
# IPC mechanism than Mach msg_send / rpc_stress, across CPUs.  Needs >= 2
# CPUs, so skipped on UP.
if [[ "$SMP_UP" != "1" ]]; then
    run_phase "sock_stress" "/tmp/sock_stress"
elif [[ "$SMP_UP" == "1" ]]; then
    echo "run-smp-tests: skipping sock_stress (UP mode: needs >= 2 CPUs)"
fi

# --- Phase 12: directory VFS operations across CPUs ---
# Each thread repeatedly mkdirs, creates a file, looks it up via
# opendir/readdir, unlinks, and rmdirs — using unique names per thread
# but contending on the parent directory lock.  Exercises directory
# entry hashing, name lookup, and VFS inode serialization across CPUs.
run_phase "dir_stress" "/tmp/dir_stress"

# --- Phase 13: Mach IPC stress between threads on different CPUs ---
# Threads on each CPU pass a token around a ring via mach_msg, proving
# IPC delivery works across threads executing on different CPUs.
run_phase "ipc_stress" "/tmp/ipc_stress" "10"

# --- Phase 14: VM map stress across CPUs ---
# All threads allocate and deallocate pages concurrently.  VM map
# operations on one CPU trigger pmap_update IPIs to the other CPUs,
# exercising the cross-CPU TLB shootdown path.
run_phase "vm_stress" "/tmp/vm_stress"

# --- Phase 15: shared mmap visibility across CPUs ---
# All threads mmap() the same MAP_SHARED anonymous region, write a
# page-specific pattern, then scan and verify every page.  Exercises
# shared-page mapping, cross-CPU TLB shootdown, and cross-CPU visibility
# of stores from the OS side.  Complements vm_stress (map churn vs.
# shared visibility).  Needs >= 2 CPUs, so skipped on UP.
if [[ "$SMP_UP" != "1" ]]; then
    run_phase "mmap_stress" "/tmp/mmap_stress"
elif [[ "$SMP_UP" == "1" ]]; then
    echo "run-smp-tests: skipping mmap_stress (UP mode: needs >= 2 CPUs)"
fi

# --- Phase 16: cross-CPU signal delivery ---
# Threads form a ring: each waits for SIGUSR1 via sigwait(), then forwards
# it to the next thread via pthread_kill().  Proves signals are delivered
# across threads executing on different CPUs.
run_phase "signal_stress" "/tmp/signal_stress"

# --- Phase 17: signal delivered to thread blocked in mach_msg ---
# Thread 0 blocks in mach_msg(MACH_RCV_MSG); thread 1 sends SIGUSR1 via
# pthread_kill.  The signal should interrupt the blocking mach_msg receive,
# causing it to return KERN_ABORTED/MACH_RCV_INTERRUPTED rather than timing
# out.  Exercises cross-CPU signal delivery to a thread blocked in a Mach
# IPC trap (the AST path).  Needs >= 2 CPUs, so skipped on UP.
if [[ "$SMP_UP" != "1" ]]; then
    run_phase "mach_signal_stress" "/tmp/mach_signal_stress"
elif [[ "$SMP_UP" == "1" ]]; then
    echo "run-smp-tests: skipping mach_signal_stress (UP mode: needs >= 2 CPUs)"
fi

# --- Phase 18: POSIX pipe I/O across CPUs ---
# Threads pass a byte around a pipe ring, exercising pipe-buffer and
# file-descriptor locking across threads on different CPUs.
run_phase "pipe_stress" "/tmp/pipe_stress"

# --- Phase 19: per-CPU timer stress ---
# Each thread repeatedly nanosleep()s, exercising per-CPU timer interrupt
# delivery (local APIC timer / AST check) on each CPU concurrently.
run_phase "timer_stress" "/tmp/timer_stress"

# --- Phase 20: concurrent file I/O stress ---
# All threads write/read their own record in a shared file via pread/
# pwrite, exercising VFS and file-descriptor locking across CPUs.
run_phase "file_stress" "/tmp/file_stress"

# --- Phase 21: shared file-descriptor I/O across CPUs ---
# All threads share one file descriptor and repeatedly pwrite()/pread()
# a thread-specific pattern at a thread-specific offset, verifying the
# readback.  Exercises VFS inode and buffer-cache locking on the shared
# file from threads on different CPUs.  Complements file_stress (own fd
# per thread vs. shared fd).
run_phase "fd_stress" "/tmp/fd_stress"

# --- Phase 22: thread create/join churn ---
# Repeatedly creates one thread per CPU, each does work and exits,
# exercising thread lifecycle and scheduler runqueue on secondary CPUs.
run_phase "thread_churn" "/tmp/thread_churn"

# --- Phase 23: page permission changes across CPUs ---
# All threads share one MAP_SHARED region; each writes its own page,
# then thread 0 mprotects all pages to PROT_READ while others read,
# then back to PROT_READ|PROT_WRITE.  Exercises pmap permission updates
# and cross-CPU TLB shootdowns from the OS side.  Needs >= 2 CPUs, so
# skipped on UP.
if [[ "$SMP_UP" != "1" ]]; then
    run_phase "vm_protect_stress" "/tmp/vm_protect_stress"
elif [[ "$SMP_UP" == "1" ]]; then
    echo "run-smp-tests: skipping vm_protect_stress (UP mode: needs >= 2 CPUs)"
fi

# --- Phase 24: Mach port right transfer across CPUs ---
# Thread 0 sends a port's send right to thread 1 via MACH_MSG_TYPE_MOVE_SEND;
# thread 1 sends it back.  Each transfer moves the right (sender loses it,
# receiver gains it).  Exercises Mach port right reference counting and
# deallocation across CPUs, distinct from ipc_stress / rpc_stress which
# only copy-send rights.  Needs >= 2 CPUs, so skipped on UP.
if [[ "$SMP_UP" != "1" ]]; then
    run_phase "port_transfer_stress" "/tmp/port_transfer_stress"
elif [[ "$SMP_UP" == "1" ]]; then
    echo "run-smp-tests: skipping port_transfer_stress (UP mode: needs >= 2 CPUs)"
fi

# --- Phase 25: CPU affinity / thread binding ---
# Each thread pins itself to a distinct CPU via sched_setaffinity and
# verifies it actually ran on that CPU via sched_getcpu().  Proves the
# scheduler honors CPU affinity across CPUs.  Needs >= 2 CPUs, so
# skipped on UP.
if [[ "$SMP_UP" != "1" ]]; then
    run_phase "affinity_stress" "/tmp/affinity_stress"
elif [[ "$SMP_UP" == "1" ]]; then
    echo "run-smp-tests: skipping affinity_stress (UP mode: needs >= 2 CPUs)"
fi

# --- Phase 26: serial-log sanity ---
# Grep the serial log for panics, fatal AP start failures, or stranded IRQs.
echo "run-smp-tests: scanning serial log for SMP anomalies..."
if grep -qiE 'panic|fatal|failed to start|stranded|wedge|deadlock' "$VT_QEMU_LOG" 2>/dev/null; then
    echo "run-smp-tests: anomalies found in serial log" >&2
    grep -iE 'panic|fatal|failed to start|stranded|wedge|deadlock' "$VT_QEMU_LOG" >&2
    FAILED=1
fi

if [[ "$FAILED" -eq 1 ]]; then
    echo "run-smp-tests: some SMP checks failed."
else
    echo "run-smp-tests: all SMP checks passed."
fi

# --- write summary JSON ---
local_overall=$([[ "$FAILED" -eq 0 ]] && echo true || echo false)

# Collect per-phase JSON in order, rename "smp__bin_true" -> "smp /bin/true",
# then slurp into a single phases array with jq --slurp.
PHASES_FILE="${RESULTS_DIR}/phases.json"
{
    for phase in smp__bin_true pset_stress thread_stress lock_stress rwlock_stress cond_stress sem_stress exception_stress fork_exec_stress rpc_stress sock_stress dir_stress ipc_stress vm_stress mmap_stress signal_stress mach_signal_stress pipe_stress timer_stress file_stress fd_stress thread_churn vm_protect_stress port_transfer_stress affinity_stress; do
        pj="${RESULTS_DIR}/${phase}.json"
        if [[ -f "$pj" ]]; then
            pname="$phase"
            [[ "$phase" == "smp__bin_true" ]] && pname="smp /bin/true"
            jq --arg pname "$pname" '.phase = $pname' "$pj"
        fi
    done
} | jq --slurp '.' > "$PHASES_FILE"

PHASES_JSON="$(cat "$PHASES_FILE")"
rm -f "$PHASES_FILE"

jq -n \
    --arg run "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
    --arg image "${SMP_IMG}" \
    --arg qemu_flags "-smp ${SMP_CPU_COUNT}" \
    --arg host "${SSH_HOST}:${SSH_PORT}" \
    --argjson overall_passed "$local_overall" \
    --argjson phases "$PHASES_JSON" \
    '{
        run: $run,
        image: $image,
        qemu_flags: $qemu_flags,
        host: $host,
        overall_passed: $overall_passed,
        phases: $phases
    }' > "${RESULTS_DIR}/summary.json"

echo "run-smp-tests: results written to ${RESULTS_FILE} and ${RESULTS_DIR}/summary.json"

# Ask the guest to power itself off so vt_wait_poweroff can release the
# forwarded port.  If poweroff isn't available or the guest ignores it,
# vt_cleanup will terminate QEMU anyway.
guest_run "poweroff" 2>/dev/null || true

vt_wait_poweroff || true
