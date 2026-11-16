/* mmap_stress.c — stress shared anonymous mmap visibility across CPUs.
 *
 * All threads mmap() the same MAP_SHARED | MAP_ANONYMOUS region.  In
 * phase 1 each thread writes a page-specific pattern to its own page;
 * after a barrier, phase 2 has every thread scan every page and verify
 * each page holds the uniform pattern char of its owning thread.  This
 * exercises shared-page mapping and cross-CPU visibility of stores from
 * the OS side (TLB shootdown / cache coherency maintained by the
 * kernel), distinct from vm_stress's VM-map churn.
 *
 * The two-phase barrier separates writers from readers so the
 * verification is deterministic rather than racy.
 *
 * Exits non-zero if mmap/munmap/mprotect/barrier fails or any page does
 * not match its owner's expected pattern.
 */

#include <sys/mman.h>
#include <pthread.h>
#include <stdio.h>
#include <unistd.h>

#include "smp_stress.h"

#define MAX_CPUS 16
#define PAGESIZE 4096
#define NPAGES MAX_CPUS
#define ROUNDS 100

static char *region;
static pthread_barrier_t write_barrier;
static pthread_barrier_t read_barrier;
static long verify_failures;
static int thread_seen[MAX_CPUS];
static volatile int failures;
static int ncpus;

struct thread_ctx {
    int idx;
    int rounds;
};

static void *
thread_fn(void *arg)
{
    struct thread_ctx *ctx = arg;
    int rounds = ctx->rounds;
    int idx = ctx->idx;
    char *my_page = region + (off_t)idx * PAGESIZE;
    char expected = (char)('A' + idx % 26);
    char buf[PAGESIZE];
    int i, j, r, bw;

    thread_seen[idx] = 1;

    for (r = 0; r < rounds; r++) {
        /* Phase 1: write our page with a uniform pattern. */
        memset(my_page, expected, PAGESIZE);

        /* Wait for all writers to finish before anyone reads.  The
           barrier result must not land in the loop counter: the serial
           return (PTHREAD_BARRIER_SERIAL_THREAD) would reset r and
           leave this thread looping forever after its partner exits. */
        bw = pthread_barrier_wait(&write_barrier);
        if (bw != 0 && bw != PTHREAD_BARRIER_SERIAL_THREAD) {
            fprintf(stderr, "thread %d: write barrier failed: %d\n", idx, bw);
            __sync_add_and_fetch(&failures, 1);
        }

        /* Phase 2: scan every page and verify it matches its owner's
           pattern.  Every page should be uniform: all bytes equal the
           owner's expected char. */
        for (j = 0; j < ncpus; j++) {
            char *scan = region + (off_t)j * PAGESIZE;
            char page_expected = (char)('A' + j % 26);
            int uniform = 1;

            memcpy(buf, scan, PAGESIZE);
            if (buf[0] != page_expected)
                uniform = 0;
            else {
                for (i = 1; i < PAGESIZE; i++)
                    if (buf[i] != page_expected) {
                        uniform = 0;
                        break;
                    }
            }

            if (!uniform) {
                fprintf(stderr, "thread %d round %d: page %d not uniform "
                                "(expected %c)\n", idx, r, j, page_expected);
                __sync_add_and_fetch(&failures, 1);
                __sync_add_and_fetch(&verify_failures, 1);
            }
        }

        /* Wait for all readers to finish before the next round
           overwrites the pages. */
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
    long total_page_checks;
    int i;

    ncpus = smp_check_ncpus();
    if (ncpus < 0)
        return 1;
    if (ncpus > NPAGES) {
        fprintf(stderr, "too many CPUs (%d > %d)\n", ncpus, NPAGES);
        return 1;
    }

    region = mmap(NULL, len, PROT_READ | PROT_WRITE,
                  MAP_SHARED | MAP_ANONYMOUS, -1, 0);
    if (region == MAP_FAILED) {
        fprintf(stderr, "mmap failed\n");
        return 1;
    }

    if (pthread_barrier_init(&write_barrier, NULL, ncpus) != 0 ||
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

    if (verify_failures) {
        fprintf(stderr, "%ld page mismatches\n", verify_failures);
        failures = 1;
    }

    if (munmap(region, len) != 0) {
        fprintf(stderr, "munmap failed\n");
        return 1;
    }

    /* Each thread checks ncpus pages per round. */
    total_page_checks = (long)ncpus * ncpus * rounds;
    double checks_per_sec = elapsed > 0.0 ? (double)total_page_checks / elapsed : 0.0;
    smp_print_results("mmap_stress", failures == 0,
                      elapsed * 1000.0, checks_per_sec, "page-checks/sec");

    return failures;
}
