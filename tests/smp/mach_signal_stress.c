/* mach_signal_stress.c — stress signal delivery to a thread blocked in
 * a mach_msg receive.
 *
 * Thread 0 (receiver) blocks in a raw mach_msg(MACH_RCV_MSG) receive on
 * its port.  Thread 1 (sender) sends SIGUSR1 to the receiver via
 * pthread_kill and verifies the handler runs promptly, while the
 * receive is still blocked, then unblocks the receive by sending a
 * real message to the port.
 *
 * Platform semantics (from source, verified in guest): pthread_kill()
 * reaches the receiver through _hurd_raise_signal() (3p/glibc/sysdeps/
 * hurd/htl/pt-kill.c calls it, and 3p/glibc/hurd/hurd-raise.c posts
 * the signal poll to _hurd_msgport).  The signal thread delivers the
 * signal promptly, but the trap itself is not aborted:
 * _hurdsig_abort_rpcs() in 3p/glibc/hurd/hurdsig.c only aborts RPCs
 * that published an intr_port, which only the interruptible-RPC
 * wrapper _hurd_intr_rpc_mach_msg() in 3p/glibc/hurd/intr-msg.c (used
 * by MiG stubs) does; a raw mach_msg trap never does.  The sigreturn
 * trampoline (3p/glibc/sysdeps/mach/hurd/x86/trampoline.c) has a
 * special rpc_wait case only for those wrapper frames; for a plain
 * trap it restores user state with the instruction pointer at the trap
 * instruction, so the receive re-executes from the start with a fresh
 * timeout (the kernel wait restarted via ipc_mqueue_receive() and
 * mach_msg_receive_continue in 3p/gnumach/ipc/mach_msg.c).  A raw
 * trap therefore can never return KERN_ABORTED in response to
 * pthread_kill; the property it does provide, and the one this test
 * verifies, is prompt handler delivery to a thread parked inside the
 * Mach IPC trap.
 *
 * Synchronization per round: the receiver writes a "ready" byte to a
 * pipe before blocking; the sender reads it, kills, waits up to
 * HANDLER_WAIT_SEC for the handler to run (polling a counter), then
 * always sends a real message so the round completes.  If handler
 * delivery were deferred until the trap completed, the round would
 * fail on the deadline instead of deadlocking.
 *
 * Exits non-zero if a handler does not run on the receiver thread, a
 * handler runs only after its round's receive returned, mach_msg does
 * not return the round's message, or any thread fails to run.
 */

#include <mach/mach_types.h>
#include <hurd.h>
#include <signal.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include "smp_stress.h"

#define MAX_CPUS 16
#define ROUNDS 100
#define HANDLER_WAIT_SEC 1.0
#define RCV_TIMEOUT_SEC 30000 /* safety net only; rounds are unblocked via msg */
#define TEST_TIMEOUT_SEC 90

static mach_port_t recv_port;
static pthread_t receiver_thread;
static pthread_barrier_t barrier;
static int ready_pipe[2];
static int ncpus;
static int thread_seen[MAX_CPUS];
static volatile int failures;

/* Handler-side bookkeeping.  times[] and end_times[] hold the handler
   and trap-return timestamps per round so pass/fail can require the
   handler to have run before its round's receive returned. */
#define PROBE_ROUNDS 1024
static volatile int sig_count;
static volatile int handler_on_receiver;
static double handler_times[PROBE_ROUNDS];
static double trap_end_times[PROBE_ROUNDS];

struct thread_ctx {
    int idx;
    int rounds;
};

static double
now_sec(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

static void
sigusr1_handler(int sig)
{
    (void)sig;
    handler_times[sig_count] = now_sec();
    handler_on_receiver += pthread_equal(pthread_self(), receiver_thread);
    sig_count++;
}

static void
sigalrm_handler(int sig)
{
    (void)sig;
    fprintf(stderr, "mach_signal_stress: timed out after %d seconds\n",
            TEST_TIMEOUT_SEC);
    _exit(1);
}

static void *
receiver_fn(void *arg)
{
    struct thread_ctx *ctx = arg;
    int rounds = ctx->rounds;
    int i;
    char ready = 1;

    /* Handler runs on this thread; record each delivery. */
    signal(SIGUSR1, sigusr1_handler);

    thread_seen[ctx->idx] = 1;
    pthread_barrier_wait(&barrier);

    for (i = 0; i < rounds; i++) {
        mach_msg_header_t msg;
        kern_return_t kr;

        memset(&msg, 0, sizeof(msg));
        msg.msgh_local_port = recv_port;
        msg.msgh_size = (mach_msg_size_t)sizeof(msg);

        if (write(ready_pipe[1], &ready, 1) != 1) {
            fprintf(stderr, "receiver round %d: ready pipe write failed\n", i);
            __sync_add_and_fetch(&failures, 1);
            return NULL;
        }

        kr = mach_msg(&msg, MACH_RCV_MSG | MACH_RCV_TIMEOUT, 0,
                      (mach_msg_size_t)sizeof(msg), recv_port,
                      RCV_TIMEOUT_SEC, MACH_PORT_NULL);
        trap_end_times[i] = now_sec();

        if (kr != MACH_MSG_SUCCESS) {
            fprintf(stderr, "receiver round %d: mach_msg failed: %d\n",
                    i, kr);
            __sync_add_and_fetch(&failures, 1);
            return NULL;
        }
    }

    return NULL;
}

static void *
sender_fn(void *arg)
{
    struct thread_ctx *ctx = arg;
    int rounds = ctx->rounds;
    int i;
    char ready;

    thread_seen[ctx->idx] = 1;
    pthread_barrier_wait(&barrier);

    for (i = 0; i < rounds; i++) {
        double t_kill;
        int seen = i;

        if (read(ready_pipe[0], &ready, 1) != 1) {
            fprintf(stderr, "sender round %d: ready pipe read failed\n", i);
            __sync_add_and_fetch(&failures, 1);
            return NULL;
        }

        t_kill = now_sec();
        if (pthread_kill(receiver_thread, SIGUSR1) != 0) {
            fprintf(stderr, "sender round %d: pthread_kill failed\n", i);
            __sync_add_and_fetch(&failures, 1);
        }

        /* Wait for the handler to run on the receiver while its
           receive is still parked in the trap. */
        while (sig_count < seen + 1 &&
               now_sec() - t_kill < HANDLER_WAIT_SEC)
            ; /* poll: the handler must preempt, not queue behind us */

        if (sig_count < seen + 1) {
            fprintf(stderr, "round %d: SIGUSR1 not delivered within %.1fs\n",
                    i, HANDLER_WAIT_SEC);
            __sync_add_and_fetch(&failures, 1);
        }

        /* The handler ran (or the deadline passed): unblock the
           receiver's trap with a real message so the round ends. */
        {
            mach_msg_header_t m;
            kern_return_t kr;

            memset(&m, 0, sizeof(m));
            m.msgh_bits = MACH_MSGH_BITS(MACH_MSG_TYPE_COPY_SEND, 0);
            m.msgh_remote_port = recv_port;
            m.msgh_size = (mach_msg_size_t)sizeof(m);
            kr = mach_msg(&m, MACH_SEND_MSG, (mach_msg_size_t)sizeof(m), 0,
                          MACH_PORT_NULL, MACH_MSG_TIMEOUT_NONE,
                          MACH_PORT_NULL);
            if (kr != MACH_MSG_SUCCESS) {
                fprintf(stderr, "sender round %d: unblock send failed: %d\n",
                        i, kr);
                __sync_add_and_fetch(&failures, 1);
                return NULL;
            }
        }
    }

    return NULL;
}

int
main(int argc, char **argv)
{
    pthread_t threads[2];
    struct thread_ctx ctxs[2];
    void *work_args[2];
    int rounds = smp_parse_iterations(argc, argv, ROUNDS);
    double start, elapsed;
    kern_return_t kr;
    int i;

    if (rounds > PROBE_ROUNDS)
        rounds = PROBE_ROUNDS;

    ncpus = smp_check_ncpus();
    if (ncpus < 0)
        return 1;
    if (ncpus < 2) {
        fprintf(stderr, "need >= 2 CPUs for mach_signal_stress\n");
        return 1;
    }

    signal(SIGALRM, sigalrm_handler);
    alarm(TEST_TIMEOUT_SEC);

    kr = mach_port_allocate(mach_task_self(), MACH_PORT_RIGHT_RECEIVE,
                            &recv_port);
    if (kr != KERN_SUCCESS) {
        fprintf(stderr, "mach_port_allocate failed: %d\n", kr);
        return 1;
    }
    kr = mach_port_insert_right(mach_task_self(), recv_port, recv_port,
                                MACH_MSG_TYPE_MAKE_SEND);
    if (kr != KERN_SUCCESS) {
        fprintf(stderr, "mach_port_insert_right failed: %d\n", kr);
        return 1;
    }

    if (pipe(ready_pipe) != 0) {
        fprintf(stderr, "pipe failed\n");
        return 1;
    }

    if (pthread_barrier_init(&barrier, NULL, 2) != 0) {
        fprintf(stderr, "pthread_barrier_init failed\n");
        return 1;
    }

    ctxs[0].idx = 0;
    ctxs[0].rounds = rounds;
    work_args[0] = &ctxs[0];
    if (pthread_create(&threads[0], NULL, receiver_fn, work_args[0]) != 0) {
        fprintf(stderr, "failed to create receiver thread\n");
        return 1;
    }
    receiver_thread = threads[0];

    ctxs[1].idx = 1;
    ctxs[1].rounds = rounds;
    work_args[1] = &ctxs[1];
    if (pthread_create(&threads[1], NULL, sender_fn, work_args[1]) != 0) {
        fprintf(stderr, "failed to create sender thread\n");
        return 1;
    }

    start = smp_now_sec();
    for (i = 0; i < 2; i++)
        if (pthread_join(threads[i], NULL) != 0) {
            fprintf(stderr, "failed to join thread %d\n", i);
            return 1;
        }
    elapsed = smp_now_sec() - start;

    for (i = 0; i < 2; i++) {
        if (!thread_seen[i]) {
            fprintf(stderr, "thread %d never ran\n", i);
            failures = 1;
        }
    }

    if (sig_count != rounds) {
        fprintf(stderr, "handler ran %d times, expected %d\n",
                sig_count, rounds);
        failures = 1;
    }
    if (!handler_on_receiver) {
        fprintf(stderr, "SIGUSR1 never ran on the receiver thread\n");
        failures = 1;
    }

    /* The handler must have run before the round's receive returned:
       delivery deferred until the trap completes is the failure mode
       this test exists to catch. */
    for (i = 0; i < rounds; i++) {
        if (handler_times[i] == 0.0 || trap_end_times[i] == 0.0)
            continue; /* already reported above */
        if (handler_times[i] > trap_end_times[i]) {
            fprintf(stderr, "round %d: handler ran after its receive "
                            "returned\n", i);
            failures = 1;
            break;
        }
    }

    mach_port_deallocate(mach_task_self(), recv_port);
    close(ready_pipe[0]);
    close(ready_pipe[1]);
    pthread_barrier_destroy(&barrier);

    smp_print_results("mach_signal_stress", failures == 0,
                      elapsed * 1000.0,
                      elapsed > 0.0 ? (double)rounds / elapsed : 0.0,
                      "interrupts/sec");
    return failures;
}
