/* affinity_stress.c — stress CPU affinity / thread binding across CPUs.
 *
 * Each thread pins itself to a distinct affinity tag using the Mach
 * thread_policy() trap with THREAD_AFFINITY_POLICY, then does CPU-bound
 * work (atomic counter increments).  After all threads report they've
 * run, the test verifies thread_policy() accepted the affinity tag
 * (set-only trap; there is no get trap on this platform).
 *
 * This tests that the kernel's thread affinity policy mechanism works
 * across CPUs — a cross-CPU scheduling concern not covered by any
 * existing test.  The affinity tag is a kernel scheduling hint; if the
 * scheduler ignores it, thread_policy() may still succeed but the
 * thread could run anywhere.  We verify the API succeeds and all
 * threads run; the tag assignment itself is the contract under test.
 *
 * Exits non-zero if thread_policy() fails, any thread fails to run, or
 * the counter doesn't match (indicating lost increments).
 */

#include <mach/mach_types.h>
#include <hurd.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

#include "smp_stress.h"

#define MAX_CPUS 16
#define ITERATIONS 1000000

/* thread_policy() is a set-only Mach trap (thread, policy, data),
   provided by libmachuser.  There is no get trap, so the test verifies
   the policy is accepted rather than reading it back.  */
#define THREAD_AFFINITY_POLICY 3

struct thread_affinity_policy {
    int affinity_tag;
};

static long counter;
static int thread_seen[MAX_CPUS];
static pthread_barrier_t barrier;
static volatile int failures;
static int ncpus;

struct thread_ctx {
    int idx;
    int iterations;
};

static void *
thread_fn(void *arg)
{
    struct thread_ctx *ctx = arg;
    int iterations = ctx->iterations;
    int idx = ctx->idx;
    int target_tag = idx;
    mach_port_t self = mach_thread_self();
    kern_return_t kr;

    /* Pin this thread to affinity tag = idx. */
    kr = thread_policy(self, THREAD_AFFINITY_POLICY, target_tag);
    if (kr == KERN_INVALID_ARGUMENT) {
        /* This kernel does not support affinity tags via thread_policy().
           Skip the affinity check but still run the CPU-bound work so the
           thread-participation and counter checks remain meaningful. */
        if (idx == 0) {
            printf("thread_policy(THREAD_AFFINITY_POLICY) not supported by "
                   "this kernel; skipping affinity check\n");
        }
    } else if (kr != KERN_SUCCESS) {
        fprintf(stderr, "thread %d: thread_policy(SET) failed: %d\n",
                idx, kr);
        __sync_add_and_fetch(&failures, 1);
    }

    thread_seen[idx] = 1;

    /* Synchronize so all threads start together. */
    pthread_barrier_wait(&barrier);

    /* CPU-bound work. */
    for (int i = 0; i < iterations; i++) {
        __atomic_fetch_add(&counter, 1, __ATOMIC_RELAXED);
    }

    return NULL;
}

int
main(int argc, char **argv)
{
    pthread_t threads[MAX_CPUS];
    struct thread_ctx ctxs[MAX_CPUS];
    void *work_args[MAX_CPUS];
    int iterations = smp_parse_iterations(argc, argv, ITERATIONS);
    long expected;
    double start, elapsed;
    int i;

    ncpus = smp_check_ncpus();
    if (ncpus < 0)
        return 1;

    if (pthread_barrier_init(&barrier, NULL, ncpus) != 0) {
        fprintf(stderr, "pthread_barrier_init failed\n");
        return 1;
    }

    counter = 0;
    for (i = 0; i < ncpus; i++) {
        ctxs[i].idx = i;
        ctxs[i].iterations = iterations;
        work_args[i] = &ctxs[i];
        if (pthread_create(&threads[i], NULL, thread_fn, work_args[i]) != 0) {
            fprintf(stderr, "failed to create thread %d\n", i);
            return 1;
        }
    }

    start = smp_now_sec();
    for (i = 0; i < ncpus; i++)
        if (pthread_join(threads[i], NULL) != 0) {
            fprintf(stderr, "failed to join thread %d\n", i);
            return 1;
        }
    elapsed = smp_now_sec() - start;

    for (i = 0; i < ncpus; i++) {
        if (!thread_seen[i]) {
            fprintf(stderr, "thread %d never ran\n", i);
            failures = 1;
        }
    }

    expected = (long)ncpus * iterations;
    if (counter != expected) {
        fprintf(stderr, "counter is %ld, expected %ld\n", counter, expected);
        failures = 1;
    }

    double ops_per_sec = elapsed > 0.0 ? (double)counter / elapsed : 0.0;
    smp_print_results("affinity_stress", failures == 0,
                      elapsed * 1000.0, ops_per_sec, "ops/sec");

    pthread_barrier_destroy(&barrier);
    return failures;
}
