/* signal_stress.c — stress cross-CPU signal delivery.
 *
 * Spawns one thread per online CPU.  Threads form a ring: each thread
 * waits for SIGUSR1 via sigwait(), then forwards it to the next thread
 * via pthread_kill().  Thread 0 primes the ring with the first signal.
 *
 * This exercises the cross-CPU AST/IPI interrupt path: a signal targeted
 * at a thread running on a different CPU must be delivered through the
 * kernel's signal-delivery machinery, distinct from plain atomics,
 * Mach IPC, VM map operations, and pipes.
 *
 * Exits non-zero if any signal is lost, any thread fails to run, or any
 * thread receives the wrong number of signals.
 */

#include <signal.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>

#include "smp_stress.h"

#define MAX_CPUS 16
#define LAPS 1000

typedef struct {
    int idx;
    int laps;
} thread_ctx_t;

static int ncpus;
static pthread_t threads[MAX_CPUS];
static long my_count[MAX_CPUS];   /* signals received per thread */
static int thread_seen[MAX_CPUS];
static pthread_barrier_t barrier;
static volatile int failures;

static void *
thread_fn(void *arg)
{
    thread_ctx_t *ctx = arg;
    int idx = ctx->idx;
    int target = (idx + 1) % ncpus;
    int laps = ctx->laps;
    sigset_t set;

    /* Block SIGUSR1 so sigwait() can receive it. */
    sigemptyset(&set);
    sigaddset(&set, SIGUSR1);
    if (pthread_sigmask(SIG_BLOCK, &set, NULL) != 0) {
        fprintf(stderr, "thread %d: pthread_sigmask failed\n", idx);
        __sync_add_and_fetch(&failures, 1);
        return NULL;
    }

    /* Mark this thread as having run. */
    thread_seen[idx] = 1;

    /* Synchronize start so all threads are waiting together. */
    pthread_barrier_wait(&barrier);

    /* Thread 0 primes the ring with the first signal. */
    if (idx == 0) {
        if (pthread_kill(threads[target], SIGUSR1) != 0) {
            fprintf(stderr, "thread %d: prime pthread_kill to %d failed\n",
                    idx, target);
            __sync_add_and_fetch(&failures, 1);
        }
    }

    for (int lap = 0; lap < laps; lap++) {
        int sig;
        int ret = sigwait(&set, &sig);
        if (ret != 0 || sig != SIGUSR1) {
            fprintf(stderr, "thread %d lap %d: sigwait failed: %d\n",
                    idx, lap, ret);
            __sync_add_and_fetch(&failures, 1);
            break;
        }
        my_count[idx]++;

        /* Forward to the next thread in the ring. */
        if (pthread_kill(threads[target], SIGUSR1) != 0) {
            fprintf(stderr, "thread %d lap %d: pthread_kill to %d failed\n",
                    idx, lap, target);
            __sync_add_and_fetch(&failures, 1);
            break;
        }
    }

    return NULL;
}

int
main(int argc, char **argv)
{
    thread_ctx_t ctxs[MAX_CPUS];
    void *work_args[MAX_CPUS];
    int laps = smp_parse_iterations(argc, argv, LAPS);
    long total_signals = 0;
    double elapsed, sigs_per_sec;
    int i;

    ncpus = smp_check_ncpus();
    if (ncpus < 0)
        return 1;

    if (pthread_barrier_init(&barrier, NULL, ncpus) != 0) {
        fprintf(stderr, "pthread_barrier_init failed\n");
        return 1;
    }

    for (i = 0; i < ncpus; i++) {
        my_count[i] = 0;
        thread_seen[i] = 0;
    }

    for (i = 0; i < ncpus; i++) {
        ctxs[i].idx = i;
        ctxs[i].laps = laps;
        work_args[i] = &ctxs[i];
    }

    /* smp_run_threads creates the threads, joins them, and returns the
       elapsed seconds of the join window. */
    elapsed = smp_run_threads(ncpus, thread_fn, work_args, threads);
    if (elapsed < 0.0) {
        pthread_barrier_destroy(&barrier);
        return 1;
    }

    for (i = 0; i < ncpus; i++)
        total_signals += my_count[i];
    sigs_per_sec = elapsed > 0.0 ? (double)total_signals / elapsed : 0.0;

    for (i = 0; i < ncpus; i++) {
        if (!thread_seen[i]) {
            fprintf(stderr, "thread %d never ran\n", i);
            failures = 1;
        }
        if (my_count[i] != laps) {
            fprintf(stderr, "thread %d got %ld signals, expected %d\n",
                    i, my_count[i], laps);
            failures = 1;
        }
    }

    pthread_barrier_destroy(&barrier);

    smp_print_results("signal_stress", failures == 0,
                      elapsed * 1000.0, sigs_per_sec, "sigs/sec");

    return failures;
}
