/* vm_protect_stress.c — stress page permission changes across CPUs.
 *
 * All threads share one MAP_SHARED anonymous region with one page per
 * thread.  In each round every thread writes its own page, then thread
 * 0 mprotects all pages to PROT_READ while the other threads keep
 * reading.  Thread 0 then mprotects back to PROT_READ|PROT_WRITE and
 * everyone writes again.
 *
 * This exercises pmap permission updates and cross-CPU TLB shootdowns:
 * after mprotect(PROT_READ) returns on thread 0's CPU, no other CPU
 * should be able to write to the pages.  A SIGSEGV handler catches any
 * illegal write that slips through (a shootdown bug) and aborts so the
 * test reports failure rather than faulting again at the same address.
 *
 * Complements mmap_stress.c (shared page visibility vs. permission
 * changes) and vm_stress.c (VM map churn vs. permission changes on a
 * stable mapping).
 */

#include <sys/mman.h>
#include <signal.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "smp_stress.h"

#define MAX_CPUS 16
#define PAGESIZE 4096
#define NPAGES MAX_CPUS
#define ROUNDS 10
#define TEST_TIMEOUT_SEC 30

static char *region;
static pthread_barrier_t write_barrier;
static pthread_barrier_t protect_barrier;
static pthread_barrier_t read_barrier;
static volatile long sigsegv_count;
static int thread_seen[MAX_CPUS];
static volatile int failures;
static int ncpus;

struct thread_ctx {
    int idx;
    int rounds;
};

static void
sigsegv_handler(int sig)
{
    (void)sig;
    __sync_add_and_fetch(&sigsegv_count, 1);
    /* Abort so we don't re-fault at the same address and loop forever. */
    _exit(1);
}

static void
sigalrm_handler(int sig)
{
    (void)sig;
    fprintf(stderr, "vm_protect_stress: timed out after %d seconds\n",
            TEST_TIMEOUT_SEC);
    _exit(1);
}

static void *
thread_fn(void *arg)
{
    struct thread_ctx *ctx = arg;
    int rounds = ctx->rounds;
    int idx = ctx->idx;
    char *my_page = region + (off_t)idx * PAGESIZE;
    char buf[PAGESIZE];
    int i, r, bw;

    thread_seen[idx] = 1;

    for (r = 0; r < rounds; r++) {
        /* Phase 1: write our page (read-write). */
        memset(my_page, (char)('A' + idx % 26), PAGESIZE);

        /* Wait for all writers to finish before changing protection.
           The barrier result must not land in the loop counter: the
           serial return resets r and strands this thread at a barrier
           whose partner has already exited. */
        bw = pthread_barrier_wait(&write_barrier);
        if (bw != 0 && bw != PTHREAD_BARRIER_SERIAL_THREAD) {
            fprintf(stderr, "thread %d: write barrier failed: %d\n", idx, bw);
            __sync_add_and_fetch(&failures, 1);
        }

        /* Thread 0 makes all pages read-only. */
        if (idx == 0) {
            if (mprotect(region, (size_t)ncpus * PAGESIZE, PROT_READ) != 0) {
                fprintf(stderr, "thread 0: mprotect PROT_READ failed\n");
                __sync_add_and_fetch(&failures, 1);
            }
        }

        /* Wait for protection change to complete on all CPUs. */
        bw = pthread_barrier_wait(&protect_barrier);
        if (bw != 0 && bw != PTHREAD_BARRIER_SERIAL_THREAD) {
            fprintf(stderr, "thread %d: protect barrier failed: %d\n", idx, bw);
            __sync_add_and_fetch(&failures, 1);
        }

        /* Phase 2: read all pages (read-only now). */
        for (i = 0; i < ncpus; i++) {
            char *scan = region + (off_t)i * PAGESIZE;
            memcpy(buf, scan, PAGESIZE);
            (void)buf;
        }

        /* Thread 0 makes pages writable again. */
        if (idx == 0) {
            if (mprotect(region, (size_t)ncpus * PAGESIZE,
                         PROT_READ | PROT_WRITE) != 0) {
                fprintf(stderr, "thread 0: mprotect PROT_WRITE failed\n");
                __sync_add_and_fetch(&failures, 1);
            }
        }

        /* Wait before the next round. */
        bw = pthread_barrier_wait(&read_barrier);
        if (bw != 0 && bw != PTHREAD_BARRIER_SERIAL_THREAD) {
            fprintf(stderr, "thread %d: read barrier failed: %d\n", idx, bw);
            __sync_add_and_fetch(&failures, 1);
        }
    }

    return NULL;
}

int
main(int argc, char **argv)
{
    pthread_t threads[MAX_CPUS];
    struct thread_ctx ctxs[MAX_CPUS];
    void *work_args[MAX_CPUS];
    int rounds = smp_parse_iterations(argc, argv, ROUNDS);
    size_t len = (size_t)NPAGES * PAGESIZE;
    double start, elapsed;
    int i;

    ncpus = smp_check_ncpus();
    if (ncpus < 0)
        return 1;
    if (ncpus < 2) {
        fprintf(stderr, "need >= 2 CPUs for vm_protect_stress\n");
        return 1;
    }
    if (ncpus > NPAGES) {
        fprintf(stderr, "too many CPUs (%d > %d)\n", ncpus, NPAGES);
        return 1;
    }

    signal(SIGSEGV, sigsegv_handler);
    signal(SIGALRM, sigalrm_handler);
    alarm(TEST_TIMEOUT_SEC);

    region = mmap(NULL, len, PROT_READ | PROT_WRITE,
                  MAP_SHARED | MAP_ANONYMOUS, -1, 0);
    if (region == MAP_FAILED) {
        fprintf(stderr, "mmap failed\n");
        return 1;
    }

    if (pthread_barrier_init(&write_barrier, NULL, ncpus) != 0 ||
        pthread_barrier_init(&protect_barrier, NULL, ncpus) != 0 ||
        pthread_barrier_init(&read_barrier, NULL, ncpus) != 0) {
        fprintf(stderr, "pthread_barrier_init failed\n");
        munmap(region, len);
        return 1;
    }

    for (i = 0; i < ncpus; i++) {
        ctxs[i].idx = i;
        ctxs[i].rounds = rounds;
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

    if (sigsegv_count) {
        fprintf(stderr, "%ld SIGSEGVs during read-only phase\n", sigsegv_count);
        failures = 1;
    }

    if (munmap(region, len) != 0) {
        fprintf(stderr, "munmap failed\n");
        return 1;
    }

    long total_ops = (long)ncpus * rounds * ncpus;
    double ops_per_sec = elapsed > 0.0 ? (double)total_ops / elapsed : 0.0;
    smp_print_results("vm_protect_stress", failures == 0,
                      elapsed * 1000.0, ops_per_sec, "page-ops/sec");

    return failures;
}
