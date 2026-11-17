/* sem_stress.c — stress POSIX semaphores across CPUs.
 *
 * One producer thread (thread 0) posts a semaphore; every other thread
 * waits on it and counts successful waits.  The producer posts
 * (ncpus-1) * rounds times so that every consumer can wait exactly
 * `rounds` times — total posts equal total waits, so no thread starves
 * or deadlocks.  All threads start together on a barrier so consumers
 * are waiting when posts arrive.
 *
 * This exercises semaphore block/wakeup across threads that may park and
 * unpark on different CPUs — distinct from mutexes, rwlocks, and cond
 * variables.
 *
 * Exits non-zero if any semaphore call fails, not all threads wake, or
 * the total number of consumed posts does not match the posts made.
 */

#include <semaphore.h>
#include <pthread.h>
#include <stdio.h>
#include <unistd.h>

#include "smp_stress.h"

#define MAX_CPUS 16
#define ROUNDS 1000

static sem_t sem;
static pthread_barrier_t barrier;
static long consumed[MAX_CPUS];
static int thread_seen[MAX_CPUS];
static volatile int failures;
static int ncpus;

struct thread_ctx {
    int idx;
    int rounds;
    int is_producer;
};

static void *
producer_fn(void *arg)
{
    struct thread_ctx *ctx = arg;
    long total_post = (long)ctx->rounds * (ncpus - 1);
    long i;

    thread_seen[ctx->idx] = 1;

    /* Synchronize start so consumers are waiting when posts arrive. */
    pthread_barrier_wait(&barrier);

    for (i = 0; i < total_post; i++) {
        if (sem_post(&sem) != 0) {
            fprintf(stderr, "producer: sem_post failed\n");
            __sync_add_and_fetch(&failures, 1);
            return NULL;
        }
    }

    return NULL;
}

static void *
consumer_fn(void *arg)
{
    struct thread_ctx *ctx = arg;
    int rounds = ctx->rounds;
    int my = 0;
    int i;

    thread_seen[ctx->idx] = 1;

    /* Synchronize start so consumers are waiting when posts arrive. */
    pthread_barrier_wait(&barrier);

    for (i = 0; i < rounds; i++) {
        if (sem_wait(&sem) != 0) {
            fprintf(stderr, "consumer %d: sem_wait failed\n", ctx->idx);
            __sync_add_and_fetch(&failures, 1);
            return NULL;
        }
        my++;
    }

    consumed[ctx->idx] = my;
    return NULL;
}

int
main(int argc, char **argv)
{
    pthread_t threads[MAX_CPUS];
    struct thread_ctx ctxs[MAX_CPUS];
    void *work_args[MAX_CPUS];
    int rounds = smp_parse_iterations(argc, argv, ROUNDS);
    long total_consumed = 0;
    long total_post;
    double start, elapsed;
    int i;

    /*
     * total_post is (rounds * (ncpus - 1)), so it can only be computed
     * after smp_check_ncpus() fills in the file-scope count.  Deriving
     * it at declaration time read ncpus == 0 and made the final balance
     * check expect -rounds posts, even though producer and consumers
     * themselves always stayed in balance.  producer_fn() recomputes
     * the same value per-thread, which is safe because threads are only
     * created below, after ncpus is known.
     */
    ncpus = smp_check_ncpus();
    if (ncpus < 0)
        return 1;
    if (ncpus < 2) {
        fprintf(stderr, "need >= 2 CPUs for sem_stress\n");
        return 1;
    }
    total_post = (long)rounds * (ncpus - 1);

    if (sem_init(&sem, 0, 0) != 0) {
        fprintf(stderr, "sem_init failed\n");
        return 1;
    }
    if (pthread_barrier_init(&barrier, NULL, ncpus) != 0) {
        fprintf(stderr, "pthread_barrier_init failed\n");
        return 1;
    }

    /* Thread 0 produces; every other thread consumes. */
    ctxs[0].idx = 0;
    ctxs[0].rounds = rounds;
    ctxs[0].is_producer = 1;
    work_args[0] = &ctxs[0];
    if (pthread_create(&threads[0], NULL, producer_fn, work_args[0]) != 0) {
        fprintf(stderr, "failed to create producer thread\n");
        return 1;
    }

    for (i = 1; i < ncpus; i++) {
        ctxs[i].idx = i;
        ctxs[i].rounds = rounds;
        ctxs[i].is_producer = 0;
        work_args[i] = &ctxs[i];
        if (pthread_create(&threads[i], NULL, consumer_fn, work_args[i]) != 0) {
            fprintf(stderr, "failed to create consumer thread %d\n", i);
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
        total_consumed += consumed[i];
    }

    if (total_consumed != total_post) {
        fprintf(stderr, "consumed %ld posts, expected %ld\n",
                total_consumed, total_post);
        failures = 1;
    }

    double posts_per_sec = elapsed > 0.0 ? (double)total_post / elapsed : 0.0;
    smp_print_results("sem_stress", failures == 0,
                      elapsed * 1000.0, posts_per_sec, "posts/sec");

    sem_destroy(&sem);
    pthread_barrier_destroy(&barrier);
    return failures;
}
