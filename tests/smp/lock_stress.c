/* lock_stress.c — stress pthread mutex contention across CPUs.
 *
 * Spawns one thread per online CPU.  All threads hammer a single
 * pthread_mutex protecting a shared counter.  The final counter must
 * equal ncpus * ITERATIONS; anything less means a lock was bypassed or
 * an increment was lost — a SMP locking bug.
 *
 * Exits non-zero if any thread fails to run, or the counter is wrong.
 */

#include <pthread.h>
#include <stdio.h>
#include <time.h>
#include <unistd.h>

#include "smp_stress.h"

#define MAX_CPUS 16
#define ITERATIONS 200000

static long counter;
static long my_count[MAX_CPUS];  /* per-thread increment count */
static int thread_seen[MAX_CPUS];
static pthread_barrier_t barrier;
static pthread_mutex_t lock;

static void *
thread_fn(void *arg)
{
    int idx = *(int *)arg;

    /* Synchronize start so all threads contend simultaneously. */
    pthread_barrier_wait(&barrier);

    /* Mark this thread as having run. */
    pthread_mutex_lock(&lock);
    thread_seen[idx] = 1;
    pthread_mutex_unlock(&lock);

    /* Contend on the lock with real work inside the critical section. */
    for (long i = 0; i < ITERATIONS; i++) {
        pthread_mutex_lock(&lock);
        counter++;
        my_count[idx]++;
        pthread_mutex_unlock(&lock);
    }

    return NULL;
}

int
main(int argc, char **argv)
{
    int iterations = smp_parse_iterations(argc, argv, ITERATIONS);
    int ncpus = smp_check_ncpus();
    int args[MAX_CPUS];
    void *work_args[MAX_CPUS];
    int failures = 0;
    long expected;
    int i;

    if (ncpus < 0)
        return 1;

    if (pthread_mutex_init(&lock, NULL) != 0) {
        fprintf(stderr, "pthread_mutex_init failed\n");
        return 1;
    }
    if (pthread_barrier_init(&barrier, NULL, ncpus) != 0) {
        fprintf(stderr, "pthread_barrier_init failed\n");
        pthread_mutex_destroy(&lock);
        return 1;
    }

    for (i = 0; i < ncpus; i++) {
        args[i] = i;
        work_args[i] = &args[i];
    }

    counter = 0;
    double elapsed = smp_run_threads(ncpus, thread_fn, work_args, NULL);
    if (elapsed < 0.0) {
        pthread_mutex_destroy(&lock);
        pthread_barrier_destroy(&barrier);
        return 1;
    }

    expected = (long)ncpus * iterations;
    for (i = 0; i < ncpus; i++) {
        if (!thread_seen[i]) {
            fprintf(stderr, "thread %d never ran\n", i);
            failures = 1;
        }
        if (my_count[i] != iterations) {
            fprintf(stderr, "thread %d did %ld increments, expected %d\n",
                    i, my_count[i], iterations);
            failures = 1;
        }
    }

    if (counter != expected) {
        fprintf(stderr, "counter is %ld, expected %ld (lost increments)\n",
                counter, expected);
        failures = 1;
    }

    long total_ops = counter;
    double ops_per_sec = elapsed > 0.0 ? (double)total_ops / elapsed : 0.0;
    smp_print_results("lock_stress", failures == 0,
                      elapsed * 1000.0, ops_per_sec, "ops/sec");

    pthread_mutex_destroy(&lock);
    pthread_barrier_destroy(&barrier);
    return failures;
}
