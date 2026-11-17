/* pipe_stress.c — stress POSIX pipe I/O across CPUs.
 *
 * Spawns one thread per online CPU.  Threads form a ring using pipes:
 * thread i writes one byte to pipe[i], and thread (i+1)%ncpus reads it.
 * Thread 0 primes the ring with the first byte.
 *
 * This exercises file-descriptor and pipe-buffer locking across CPUs —
 * a kernel path distinct from atomics, Mach IPC, VM map operations, and
 * signal delivery.
 *
 * Exits non-zero if any pipe operation fails, any thread fails to run,
 * or any thread completes the wrong number of iterations.
 */

#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>

#include "smp_stress.h"

#define MAX_CPUS 16
#define ITERATIONS 5000

typedef struct {
    int idx;
    int iterations;
} thread_ctx_t;

static int ncpus;
static int pipes[MAX_CPUS][2];   /* pipe[i]: thread i writes, thread (i+1)%ncpus reads */
static pthread_t threads[MAX_CPUS];
static long my_count[MAX_CPUS];   /* completed read+write cycles per thread */
static int thread_seen[MAX_CPUS];
static pthread_barrier_t barrier;
static volatile int failures;

static void *
thread_fn(void *arg)
{
    thread_ctx_t *ctx = arg;
    int idx = ctx->idx;
    int target = (idx + 1) % ncpus;
    int src = (idx + ncpus - 1) % ncpus;
    int iterations = ctx->iterations;
    char buf = 'X';

    /* Mark this thread as having run. */
    thread_seen[idx] = 1;

    /* Synchronize start so all threads are ready together. */
    pthread_barrier_wait(&barrier);

    /* Thread 0 primes the ring with the first byte. */
    if (idx == 0) {
        if (write(pipes[idx][1], &buf, 1) != 1) {
            fprintf(stderr, "thread %d: prime write failed\n", idx);
            __sync_add_and_fetch(&failures, 1);
        }
    }

    for (int i = 0; i < iterations; i++) {
        /* Read one byte from the previous thread's pipe. */
        ssize_t n = read(pipes[src][0], &buf, 1);
        if (n != 1) {
            fprintf(stderr, "thread %d iter %d: read failed: %zd\n",
                    idx, i, n);
            __sync_add_and_fetch(&failures, 1);
            break;
        }

        /* Forward one byte to the next thread's pipe. */
        n = write(pipes[idx][1], &buf, 1);
        if (n != 1) {
            fprintf(stderr, "thread %d iter %d: write failed: %zd\n",
                    idx, i, n);
            __sync_add_and_fetch(&failures, 1);
            break;
        }

        my_count[idx]++;
    }

    return NULL;
}

int
main(int argc, char **argv)
{
    thread_ctx_t ctxs[MAX_CPUS];
    void *work_args[MAX_CPUS];
    int iterations = smp_parse_iterations(argc, argv, ITERATIONS);
    long total_ops = 0;
    double elapsed, ops_per_sec;
    int i;

    ncpus = smp_check_ncpus();
    if (ncpus < 0)
        return 1;

    /* Create one pipe per thread: pipe[i] carries bytes from thread i
       to thread (i+1)%ncpus. */
    for (i = 0; i < ncpus; i++) {
        if (pipe(pipes[i]) != 0) {
            fprintf(stderr, "pipe(%d) failed\n", i);
            return 1;
        }
    }

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
        ctxs[i].iterations = iterations;
        work_args[i] = &ctxs[i];
    }

    /* smp_run_threads creates the threads, joins them, and returns the
       elapsed seconds of the join window. */
    elapsed = smp_run_threads(ncpus, thread_fn, work_args, NULL);
    if (elapsed < 0.0) {
        pthread_barrier_destroy(&barrier);
        return 1;
    }

    for (i = 0; i < ncpus; i++)
        total_ops += my_count[i];
    ops_per_sec = elapsed > 0.0 ? (double)total_ops / elapsed : 0.0;

    for (i = 0; i < ncpus; i++) {
        if (!thread_seen[i]) {
            fprintf(stderr, "thread %d never ran\n", i);
            failures = 1;
        }
        if (my_count[i] != iterations) {
            fprintf(stderr, "thread %d did %ld cycles, expected %d\n",
                    i, my_count[i], iterations);
            failures = 1;
        }
    }

    /* Close pipes. */
    for (i = 0; i < ncpus; i++) {
        close(pipes[i][0]);
        close(pipes[i][1]);
    }
    pthread_barrier_destroy(&barrier);

    smp_print_results("pipe_stress", failures == 0,
                      elapsed * 1000.0, ops_per_sec, "ops/sec");

    return failures;
}
