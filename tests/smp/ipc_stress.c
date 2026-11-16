/* ipc_stress.c — stress Mach IPC between threads on different CPUs.
 *
 * Spawns one thread per online CPU.  Each thread owns a Mach receive port
 * and sends a message to the next thread's port in a ring.  A token
 * circulates around the ring for several laps, proving Mach IPC delivers
 * across threads that may be executing on different CPUs concurrently.
 *
 * Exits non-zero if any message is lost or any thread fails to run.
 *
 * The Mach traps are forward-declared instead of including <mach/message.h>
 * because the guest's system headers may not match the host toolchain that
 * clangd runs under (same approach as tests/smp/pset_stress.c).
 */

#include <mach/mach_types.h>
#include <hurd.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include "smp_stress.h"

#define MAX_CPUS 16
#define SEND_TIMEOUT_MS 5000

typedef natural_t *processor_name_array_t;

typedef struct {
    mach_msg_header_t header;
    int               pad[(256 - sizeof(mach_msg_header_t)) / sizeof(int)];
} msg_t;

static int ncpus;
static mach_port_t ports[MAX_CPUS];
static int thread_seen[MAX_CPUS];
static pthread_barrier_t start_barrier;
static pthread_barrier_t done_barrier;
static volatile int failures;

struct thread_ctx {
    int idx;
    int laps;
};

static void *
thread_fn(void *arg)
{
    struct thread_ctx *ctx = arg;
    int idx = ctx->idx;
    int laps = ctx->laps;
    int next = (idx + 1) % ncpus;
    msg_t msg;
    kern_return_t kr;

    /* Mark this thread as having run. */
    thread_seen[idx] = 1;

    /* Synchronize start so all threads are waiting on receive together. */
    pthread_barrier_wait(&start_barrier);

    /* Prime: thread 0 injects the first token into the ring. */
    if (idx == 0) {
        memset(&msg, 0, sizeof(msg));
        msg.header.msgh_bits = MACH_MSGH_BITS(MACH_MSG_TYPE_COPY_SEND, 0);
        msg.header.msgh_size = (mach_msg_size_t)sizeof(msg);
        msg.header.msgh_remote_port = ports[next];
        msg.header.msgh_local_port = MACH_PORT_NULL;
        msg.header.msgh_id = 1000;
        kr = mach_msg(&msg.header, MACH_SEND_MSG | MACH_SEND_TIMEOUT,
                      (mach_msg_size_t)sizeof(msg), 0,
                      MACH_PORT_NULL, SEND_TIMEOUT_MS, MACH_PORT_NULL);
        if (kr != KERN_SUCCESS) {
            fprintf(stderr, "thread %d: initial send failed: %d\n", idx, kr);
            __sync_add_and_fetch(&failures, 1);
        }
    }

    for (int lap = 0; lap < laps; lap++) {
        /* Wait for a message on our port. */
        msg_t reply;
        memset(&reply, 0, sizeof(reply));
        reply.header.msgh_local_port = ports[idx];
        reply.header.msgh_size = (mach_msg_size_t)sizeof(reply);

        kr = mach_msg(&reply.header, MACH_RCV_MSG | MACH_RCV_TIMEOUT, 0,
                      (mach_msg_size_t)sizeof(reply), ports[idx], 5000,
                      MACH_PORT_NULL);
        if (kr != KERN_SUCCESS) {
            fprintf(stderr, "thread %d lap %d: receive failed: %d\n",
                    idx, lap, kr);
            __sync_add_and_fetch(&failures, 1);
            break;
        }

        /* Forward to the next thread. */
        msg_t forward;
        memset(&forward, 0, sizeof(forward));
        forward.header.msgh_bits = MACH_MSGH_BITS(MACH_MSG_TYPE_COPY_SEND, 0);
        forward.header.msgh_size = (mach_msg_size_t)sizeof(forward);
        forward.header.msgh_remote_port = ports[next];
        forward.header.msgh_local_port = MACH_PORT_NULL;
        forward.header.msgh_id = reply.header.msgh_id + 1;

        kr = mach_msg(&forward.header, MACH_SEND_MSG | MACH_SEND_TIMEOUT,
                      (mach_msg_size_t)sizeof(forward), 0,
                      MACH_PORT_NULL, SEND_TIMEOUT_MS, MACH_PORT_NULL);
        if (kr != KERN_SUCCESS) {
            fprintf(stderr, "thread %d lap %d: send failed: %d\n",
                    idx, lap, kr);
            __sync_add_and_fetch(&failures, 1);
            break;
        }
    }

    pthread_barrier_wait(&done_barrier);
    return NULL;
}

int
main(int argc, char **argv)
{
    pthread_t threads[MAX_CPUS];
    struct thread_ctx ctxs[MAX_CPUS];
    void *work_args[MAX_CPUS];
    int laps = smp_parse_iterations(argc, argv, 10);
    int i;

    ncpus = smp_check_ncpus();
    if (ncpus < 0)
        return 1;

    /* Create one receive port per thread. */
    for (i = 0; i < ncpus; i++) {
        kern_return_t kr = mach_port_allocate(mach_task_self(),
                                              MACH_PORT_RIGHT_RECEIVE,
                                              &ports[i]);
        if (kr != KERN_SUCCESS) {
            fprintf(stderr, "mach_port_allocate(%d) failed: %d\n", i, kr);
            return 1;
        }
        kr = mach_port_insert_right(mach_task_self(), ports[i], ports[i],
                                    MACH_MSG_TYPE_MAKE_SEND);
        if (kr != KERN_SUCCESS) {
            fprintf(stderr, "mach_port_insert_right(%d) failed: %d\n", i, kr);
            return 1;
        }
    }

    if (pthread_barrier_init(&start_barrier, NULL, ncpus) != 0 ||
        pthread_barrier_init(&done_barrier, NULL, ncpus) != 0) {
        fprintf(stderr, "pthread_barrier_init failed\n");
        return 1;
    }

    for (i = 0; i < ncpus; i++) {
        ctxs[i].idx = i;
        ctxs[i].laps = laps;
        work_args[i] = &ctxs[i];
    }

    double elapsed = smp_run_threads(ncpus, thread_fn, work_args, NULL);
    if (elapsed < 0.0) {
        return 1;
    }

    /* Each thread does `laps` receives + `laps` sends; thread 0 also sends
       the initial prime message.  Total messages = ncpus * laps * 2 + 1. */
    long total_msgs = (long)ncpus * laps * 2 + 1;
    double msgs_per_sec = elapsed > 0.0 ? (double)total_msgs / elapsed : 0.0;

    for (i = 0; i < ncpus; i++) {
        if (!thread_seen[i]) {
            fprintf(stderr, "thread %d never ran\n", i);
            failures = 1;
        }
    }

    /* Destroy ports. */
    for (i = 0; i < ncpus; i++)
        mach_port_deallocate(mach_task_self(), ports[i]);

    smp_print_results("ipc_stress", failures == 0,
                      elapsed * 1000.0, msgs_per_sec, "msgs/sec");

    return failures;
}
