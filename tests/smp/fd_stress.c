/* fd_stress.c — stress concurrent file I/O on a shared file descriptor.
 *
 * All threads share one file descriptor opened on a temporary file and
 * repeatedly pwrite() a thread-specific pattern at a thread-specific
 * offset, then pread() it back and verify the bytes.  Because pread/
 * pwrite take explicit offsets (they do not touch the shared file
 * offset), concurrent access is safe; what this exercises is the VFS
 * inode and buffer-cache locking that serializes access to the shared
 * underlying file from threads running on different CPUs.
 *
 * This complements file_stress.c, which uses each thread's own fd with
 * pread/pwrite on distinct records — here the fd itself is shared.
 *
 * Exits non-zero if any file call fails or any readback mismatches.
 */

#include <fcntl.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include "smp_stress.h"

#define MAX_CPUS 16
#define ROUNDS 100
#define FILE_SIZE (MAX_CPUS * 4096)

static int shared_fd;
static char pattern[MAX_CPUS][4096];
static long verify_failures;
static int thread_seen[MAX_CPUS];
static volatile int failures;

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
    off_t offset = (off_t)idx * 4096;
    char buf[4096];
    int i;

    thread_seen[idx] = 1;

    for (i = 0; i < rounds; i++) {
        /* Write our pattern at our offset. */
        if (pwrite(shared_fd, pattern[idx], 4096, offset) != 4096) {
            fprintf(stderr, "thread %d round %d: pwrite failed\n", idx, i);
            __sync_add_and_fetch(&failures, 1);
            return NULL;
        }

        /* Read it back and verify. */
        memset(buf, 0, sizeof(buf));
        if (pread(shared_fd, buf, 4096, offset) != 4096) {
            fprintf(stderr, "thread %d round %d: pread failed\n", idx, i);
            __sync_add_and_fetch(&failures, 1);
            return NULL;
        }
        if (memcmp(buf, pattern[idx], 4096) != 0) {
            fprintf(stderr, "thread %d round %d: readback mismatch\n", idx, i);
            __sync_add_and_fetch(&failures, 1);
            __sync_add_and_fetch(&verify_failures, 1);
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
    double start, elapsed;
    int ncpus;
    int i;
    char tmpl[] = "/tmp/fd_stress.XXXXXX";

    ncpus = smp_check_ncpus();
    if (ncpus < 0)
        return 1;

    /* Build a unique pattern per thread. */
    for (i = 0; i < ncpus; i++) {
        memset(pattern[i], (char)('A' + i % 26), 4096);
    }

    shared_fd = mkstemp(tmpl);
    if (shared_fd < 0) {
        fprintf(stderr, "mkstemp failed\n");
        return 1;
    }
    unlink(tmpl); /* remove from namespace immediately */

    if (ftruncate(shared_fd, FILE_SIZE) != 0) {
        fprintf(stderr, "ftruncate failed\n");
        close(shared_fd);
        return 1;
    }

    for (i = 0; i < ncpus; i++) {
        ctxs[i].idx = i;
        ctxs[i].rounds = rounds;
        work_args[i] = &ctxs[i];
        if (pthread_create(&threads[i], NULL, thread_fn, work_args[i]) != 0) {
            fprintf(stderr, "failed to create thread %d\n", i);
            close(shared_fd);
            return 1;
        }
    }

    start = smp_now_sec();
    for (i = 0; i < ncpus; i++)
        if (pthread_join(threads[i], NULL) != 0) {
            fprintf(stderr, "failed to join thread %d\n", i);
            close(shared_fd);
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
        fprintf(stderr, "%ld readback mismatches\n", verify_failures);
        failures = 1;
    }

    close(shared_fd);

    long total_ops = (long)ncpus * rounds * 2; /* pwrite + pread */
    double ops_per_sec = elapsed > 0.0 ? (double)total_ops / elapsed : 0.0;
    smp_print_results("fd_stress", failures == 0,
                      elapsed * 1000.0, ops_per_sec, "ops/sec");

    return failures;
}
