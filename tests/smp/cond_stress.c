/* cond_stress.c — stress pthread condition variables across CPUs.
 *
 * One producer thread (thread 0) increments a shared counter and
 * broadcasts the condition variable; consumer threads wait on the
 * condition, decrement the counter, and count items consumed.  This
 * exercises cond wait/wake across threads that may park and unpark on
 * different CPUs, distinct from signal_stress (signals) and
 * lock_stress (mutex-only contention).
 *
 * The producer produces (ncpus-1) * rounds items so that every consumer
 * can consume exactly `rounds` items — total produced equals total
 * consumed, so no thread starves or deadlocks.
 *
 * Exits non-zero if any cond/mutex call fails, not all threads run, or
 * the item count does not balance (produced == consumed, none left).
 */

#include <pthread.h>
#include <stdio.h>
#include <unistd.h>

#include "smp_stress.h"

#define MAX_CPUS 16
#define ROUNDS 1000

static pthread_mutex_t mutex;
static pthread_cond_t cond;
static long items;
static long produced;
static long consumed[MAX_CPUS];
static int ncpus;
static int thread_seen[MAX_CPUS];
static volatile int failures;

struct thread_ctx {
    int idx;
    int rounds;
    int is_producer;
};

static void *
producer_fn(void *arg)
{
    struct thread_ctx *ctx = arg;
    long total_produce = (long)ctx->rounds * (ncpus - 1);
    long i;

    thread_seen[ctx->idx] = 1;

    for (i = 0; i < total_produce; i++) {
        if (pthread_mutex_lock(&mutex) != 0) {
            fprintf(stderr, "producer: mutex_lock failed\n");
            __sync_add_and_fetch(&failures, 1);
            return NULL;
        }
        items++;
        produced++;
        if (pthread_cond_broadcast(&cond) != 0) {
            fprintf(stderr, "producer: cond_broadcast failed\n");
            pthread_mutex_unlock(&mutex);
            __sync_add_and_fetch(&failures, 1);
            return NULL;
        }
        if (pthread_mutex_unlock(&mutex) != 0) {
            fprintf(stderr, "producer: mutex_unlock failed\n");
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

    for (i = 0; i < rounds; i++) {
        if (pthread_mutex_lock(&mutex) != 0) {
            fprintf(stderr, "consumer %d: mutex_lock failed\n", ctx->idx);
            __sync_add_and_fetch(&failures, 1);
            return NULL;
        }
        while (items == 0) {
            if (pthread_cond_wait(&cond, &mutex) != 0) {
                fprintf(stderr, "consumer %d: cond_wait failed\n", ctx->idx);
                pthread_mutex_unlock(&mutex);
                __sync_add_and_fetch(&failures, 1);
                return NULL;
            }
        }
        items--;
        my++;
        if (pthread_mutex_unlock(&mutex) != 0) {
            fprintf(stderr, "consumer %d: mutex_unlock failed\n", ctx->idx);
            __sync_add_and_fetch(&failures, 1);
            return NULL;
        }
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
    long total_produce;
    double start, elapsed;
    int i;

    /*
     * total_produce is (rounds * (ncpus - 1)), so it can only be computed
     * after smp_check_ncpus() fills in the file-scope count.  Deriving it
     * at declaration time read ncpus == 0 and made the final balance
     * checks expect -rounds items, even though the producer and consumers
     * themselves always stayed in balance.  producer_fn() recomputes the
     * same value per-thread, which is safe because threads are only
     * created below, after ncpus is known.
     */
    ncpus = smp_check_ncpus();
    if (ncpus < 0)
        return 1;
    if (ncpus < 2) {
        fprintf(stderr, "need >= 2 CPUs for cond_stress\n");
        return 1;
    }
    total_produce = (long)rounds * (ncpus - 1);

    if (pthread_mutex_init(&mutex, NULL) != 0 ||
        pthread_cond_init(&cond, NULL) != 0) {
        fprintf(stderr, "cond/mutex init failed\n");
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

    if (produced != total_produce) {
        fprintf(stderr, "produced %ld, expected %ld\n", produced, total_produce);
        failures = 1;
    }
    if (total_consumed != total_produce) {
        fprintf(stderr, "consumed %ld, expected %ld\n", total_consumed,
                total_produce);
        failures = 1;
    }
    if (items != 0) {
        fprintf(stderr, "items left: %ld, expected 0\n", items);
        failures = 1;
    }

    double items_per_sec = elapsed > 0.0 ? (double)total_produce / elapsed : 0.0;
    smp_print_results("cond_stress", failures == 0,
                      elapsed * 1000.0, items_per_sec, "items/sec");

    pthread_mutex_destroy(&mutex);
    pthread_cond_destroy(&cond);
    return failures;
}
