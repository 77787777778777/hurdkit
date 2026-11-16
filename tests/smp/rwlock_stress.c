/* rwlock_stress.c — stress pthread readers-writer locks across CPUs.
 *
 * Multiple reader threads and writer threads contend on a single
 * pthread_rwlock_t protecting a shared integer.  Writers acquire the
 * exclusive (write) lock and increment the value; readers acquire the
 * shared (read) lock and verify the value is non-decreasing.  This
 * exercises the kernel's shared/exclusive locking path across CPUs —
 * a kernel path distinct from plain mutexes, condition variables,
 * atomics, Mach IPC, VM map operations, signals, pipes, timers, and
 * file I/O.
 *
 * Exits non-zero if data corruption is detected or any thread fails
 * to complete its iterations.
 */

#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>

#include "smp_stress.h"

#define MAX_CPUS 16
#define ITERATIONS 1000

static int ncpus;
static int shared_data;
static long reader_ops[MAX_CPUS];   /* successful reads per reader */
static long writer_ops[MAX_CPUS];   /* successful writes per writer */
static int thread_seen[MAX_CPUS];
static pthread_barrier_t barrier;
static pthread_rwlock_t rwlock;
static volatile int failures;

static void *
reader_fn(void *arg)
{
    int idx = *(int *)arg;
    int iterations = ITERATIONS;
    int last_seen = 0;
    int i;

    /* Mark this thread as having run. */
    thread_seen[idx] = 1;

    /* Synchronize start so all threads begin together. */
    pthread_barrier_wait(&barrier);

    for (i = 0; i < iterations; i++) {
        if (pthread_rwlock_rdlock(&rwlock) != 0) {
            fprintf(stderr, "reader %d iter %d: rdlock failed\n", idx, i);
            __sync_add_and_fetch(&failures, 1);
            break;
        }
        int val = shared_data;
        if (val < last_seen) {
            fprintf(stderr, "reader %d iter %d: data went backwards: "
                    "%d -> %d\n", idx, i, last_seen, val);
            __sync_add_and_fetch(&failures, 1);
        }
        last_seen = val;
        if (pthread_rwlock_unlock(&rwlock) != 0) {
            fprintf(stderr, "reader %d iter %d: unlock failed\n", idx, i);
            __sync_add_and_fetch(&failures, 1);
            break;
        }
        reader_ops[idx]++;
    }

    return NULL;
}

static void *
writer_fn(void *arg)
{
    int idx = *(int *)arg;
    int iterations = ITERATIONS;
    int i;

    /* Mark this thread as having run. */
    thread_seen[idx] = 1;

    /* Synchronize start so all threads begin together. */
    pthread_barrier_wait(&barrier);

    for (i = 0; i < iterations; i++) {
        if (pthread_rwlock_wrlock(&rwlock) != 0) {
            fprintf(stderr, "writer %d iter %d: wrlock failed\n", idx, i);
            __sync_add_and_fetch(&failures, 1);
            break;
        }
        shared_data++;
        if (pthread_rwlock_unlock(&rwlock) != 0) {
            fprintf(stderr, "writer %d iter %d: unlock failed\n", idx, i);
            __sync_add_and_fetch(&failures, 1);
            break;
        }
        writer_ops[idx]++;
    }

    return NULL;
}

int
main(int argc, char **argv)
{
    pthread_t threads[MAX_CPUS];
    void *work_args[MAX_CPUS];
    int args[MAX_CPUS];
    int iterations = smp_parse_iterations(argc, argv, ITERATIONS);
    int nreaders, nwriters;
    long total_ops = 0;
    double elapsed, ops_per_sec;
    int i;

    ncpus = smp_check_ncpus();
    if (ncpus < 0)
        return 1;

    /* Split CPUs between readers and writers: at least one of each. */
    if (ncpus >= 2) {
        nreaders = ncpus - 1;
        nwriters = 1;
    } else {
        nreaders = 1;
        nwriters = 0;
    }

    if (pthread_rwlock_init(&rwlock, NULL) != 0) {
        fprintf(stderr, "pthread_rwlock_init failed\n");
        return 1;
    }
    if (pthread_barrier_init(&barrier, NULL, ncpus) != 0) {
        fprintf(stderr, "pthread_barrier_init failed\n");
        pthread_rwlock_destroy(&rwlock);
        return 1;
    }

    shared_data = 0;
    for (i = 0; i < ncpus; i++) {
        reader_ops[i] = 0;
        writer_ops[i] = 0;
        thread_seen[i] = 0;
    }

    for (i = 0; i < ncpus; i++) {
        args[i] = i;
        work_args[i] = &args[i];
    }

    /* Launch readers first, then writers, all synchronized on the
       barrier so they start concurrently. */
    for (i = 0; i < nreaders; i++) {
        if (pthread_create(&threads[i], NULL, reader_fn, work_args[i]) != 0) {
            fprintf(stderr, "failed to create reader %d\n", i);
            failures = 1;
            break;
        }
    }
    for (i = 0; i < nwriters && !failures; i++) {
        int wi = nreaders + i;
        if (pthread_create(&threads[wi], NULL, writer_fn, work_args[wi]) != 0) {
            fprintf(stderr, "failed to create writer %d\n", i);
            failures = 1;
        }
    }

    if (!failures) {
        double start = smp_now_sec();
        for (i = 0; i < ncpus; i++)
            pthread_join(threads[i], NULL);
        double end = smp_now_sec();
        elapsed = end - start;
    } else {
        elapsed = 0.0;
    }

    for (i = 0; i < ncpus; i++) {
        if (!thread_seen[i]) {
            fprintf(stderr, "thread %d never ran\n", i);
            failures = 1;
        }
        total_ops += reader_ops[i] + writer_ops[i];
    }

    if (shared_data != nwriters * iterations) {
        fprintf(stderr, "shared_data is %d, expected %d (lost writes)\n",
                shared_data, nwriters * iterations);
        failures = 1;
    }

    ops_per_sec = elapsed > 0.0 ? (double)total_ops / elapsed : 0.0;
    smp_print_results("rwlock_stress", failures == 0,
                      elapsed * 1000.0, ops_per_sec, "ops/sec");

    pthread_rwlock_destroy(&rwlock);
    pthread_barrier_destroy(&barrier);
    return failures;
}
