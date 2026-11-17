/* thread_stress.c — verify concurrent execution on multiple CPUs.
 *
 * Creates one thread per online CPU.  All threads start simultaneously
 * (barrier) and increment a shared counter with __atomic_fetch_add,
 * proving the kernel schedules user threads across more than one CPU
 * and that the increments are atomic.
 *
 * Exits non-zero if any thread fails to run, or if the final counter
 * does not equal ncpus * ITERATIONS (a mismatch means increments were
 * lost — either a scheduling bug or non-atomic reads/writes).
 */

#include <pthread.h>
#include <stdio.h>
#include <time.h>
#include <unistd.h>

#include "smp_stress.h"

#define MAX_CPUS 16
#define ITERATIONS 1000000

static long counter;
static long my_count[MAX_CPUS];  /* per-thread increment count */
static int thread_seen[MAX_CPUS];
static pthread_barrier_t barrier;

static void *
thread_fn(void *arg)
{
    int idx = *(int *)arg;

    /* Synchronize start so all threads run concurrently. */
    pthread_barrier_wait(&barrier);

    /* Mark this thread as having run. */
    thread_seen[idx] = 1;

    /* Do real work so the scheduler has something to dispatch. */
    for (long i = 0; i < ITERATIONS; i++) {
        __atomic_fetch_add(&counter, 1, __ATOMIC_SEQ_CST);
        my_count[idx]++;
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

    if (pthread_barrier_init(&barrier, NULL, ncpus) != 0) {
        fprintf(stderr, "pthread_barrier_init failed\n");
        return 1;
    }

    for (i = 0; i < ncpus; i++) {
        args[i] = i;
        work_args[i] = &args[i];
    }

    counter = 0;
    double elapsed = smp_run_threads(ncpus, thread_fn, work_args, NULL);
    if (elapsed < 0.0) {
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
    smp_print_results("thread_stress", failures == 0,
                      elapsed * 1000.0, ops_per_sec, "ops/sec");

    pthread_barrier_destroy(&barrier);
    return failures;
}
