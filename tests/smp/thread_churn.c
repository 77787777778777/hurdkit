/* thread_churn.c — stress thread create/join churn across CPUs.
 *
 * Repeatedly creates one thread per online CPU, lets each do a small
 * amount of work (increment a shared counter), then joins them all.
 * Repeating this for many batches exercises thread lifecycle and
 * scheduler runqueue insertion/removal on secondary CPUs.
 *
 * Exits non-zero if any thread fails to create/join or the shared
 * counter doesn't equal ncpus * batches.
 */

#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>

#include "smp_stress.h"

#define MAX_CPUS 16
#define BATCHES 500

typedef struct {
    int idx;
} thread_arg_t;

static int ncpus;
static long counter;
static long my_work[MAX_CPUS];   /* work items done per thread index */
static int thread_seen[MAX_CPUS];
static pthread_barrier_t barrier;
static volatile int failures;

static void *
thread_fn(void *arg)
{
    thread_arg_t *ta = arg;
    int idx = ta->idx;

    /* Mark this thread as having run. */
    thread_seen[idx] = 1;

    /* Synchronize start so all threads in the batch start together. */
    pthread_barrier_wait(&barrier);

    __atomic_fetch_add(&counter, 1, __ATOMIC_SEQ_CST);
    my_work[idx]++;

    return NULL;
}

int
main(int argc, char **argv)
{
    pthread_t threads[MAX_CPUS];
    int batch_count = smp_parse_iterations(argc, argv, BATCHES);
    long expected;
    double elapsed, ops_per_sec;
    thread_arg_t args[MAX_CPUS];
    void *work_args[MAX_CPUS];
    int i, b;

    ncpus = smp_check_ncpus();
    if (ncpus < 0)
        return 1;

    if (pthread_barrier_init(&barrier, NULL, ncpus) != 0) {
        fprintf(stderr, "pthread_barrier_init failed\n");
        return 1;
    }

    counter = 0;
    for (i = 0; i < ncpus; i++) {
        my_work[i] = 0;
        thread_seen[i] = 0;
    }

    for (i = 0; i < ncpus; i++) {
        args[i].idx = i;
        work_args[i] = &args[i];
    }

    elapsed = 0.0;
    for (b = 0; b < batch_count; b++) {
        double batch_elapsed = smp_run_threads(ncpus, thread_fn, work_args, NULL);
        if (batch_elapsed < 0.0) {
            failures = 1;
            break;
        }
        elapsed += batch_elapsed;
    }

    expected = (long)ncpus * batch_count;
    ops_per_sec = elapsed > 0.0 ? (double)counter / elapsed : 0.0;

    for (i = 0; i < ncpus; i++) {
        if (!thread_seen[i]) {
            fprintf(stderr, "thread %d never ran\n", i);
            failures = 1;
        }
        if (my_work[i] != batch_count) {
            fprintf(stderr, "thread %d did %ld batches, expected %d\n",
                    i, my_work[i], batch_count);
            failures = 1;
        }
    }

    if (counter != expected) {
        fprintf(stderr, "counter is %ld, expected %ld (lost increments)\n",
                counter, expected);
        failures = 1;
    }

    pthread_barrier_destroy(&barrier);

    smp_print_results("thread_churn", failures == 0,
                      elapsed * 1000.0, ops_per_sec, "ops/sec");

    return failures;
}
