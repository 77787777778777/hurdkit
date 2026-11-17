/* file_stress.c — stress concurrent file I/O across CPUs.
 *
 * Spawns one thread per online CPU.  A shared file is pre-allocated with
 * one 8-byte record per thread.  Each thread repeatedly writes its thread
 * index to its own record, then reads it back and verifies.  Concurrent
 * reads/writes to the same file from threads on different CPUs exercise
 * VFS and file-descriptor locking — a kernel path distinct from atomics,
 * Mach IPC, VM map operations, signals, pipes, and timers.
 *
 * Exits non-zero if any read/write fails, data is corrupted, or any
 * thread fails to run.
 */

#include <fcntl.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#include "smp_stress.h"

#define MAX_CPUS 16
#define ITERATIONS 1000
#define RECORD_SIZE 8

typedef struct {
    int idx;
    int iterations;
    int fd;
} thread_ctx_t;

static int ncpus;
static long my_ops[MAX_CPUS];    /* completed write+read cycles per thread */
static int thread_seen[MAX_CPUS];
static pthread_barrier_t barrier;
static volatile int failures;

static void *
thread_fn(void *arg)
{
    thread_ctx_t *ctx = arg;
    int idx = ctx->idx;
    int fd = ctx->fd;
    int iterations = ctx->iterations;
    long long val = idx;
    long long readback;
    off_t offset = (off_t)idx * RECORD_SIZE;

    /* Mark this thread as having run. */
    thread_seen[idx] = 1;

    /* Synchronize start so all threads contend on the file together. */
    pthread_barrier_wait(&barrier);

    for (int i = 0; i < iterations; i++) {
        /* Write our index to our record. */
        if (pwrite(fd, &val, sizeof(val), offset) != sizeof(val)) {
            fprintf(stderr, "thread %d iter %d: pwrite failed\n", idx, i);
            __sync_add_and_fetch(&failures, 1);
            break;
        }

        /* Read it back and verify. */
        if (pread(fd, &readback, sizeof(readback), offset) != sizeof(readback)) {
            fprintf(stderr, "thread %d iter %d: pread failed\n", idx, i);
            __sync_add_and_fetch(&failures, 1);
            break;
        }

        if (readback != val) {
            fprintf(stderr, "thread %d iter %d: data corruption: "
                    "wrote %lld read %lld\n", idx, i, val, readback);
            __sync_add_and_fetch(&failures, 1);
            break;
        }

        my_ops[idx]++;
    }

    return NULL;
}

int
main(int argc, char **argv)
{
    pthread_t threads[MAX_CPUS];
    thread_ctx_t ctxs[MAX_CPUS];
    void *work_args[MAX_CPUS];
    int iterations = smp_parse_iterations(argc, argv, ITERATIONS);
    long total_ops = 0;
    double elapsed, ops_per_sec;
    int fd;
    int i;

    ncpus = smp_check_ncpus();
    if (ncpus < 0)
        return 1;

    /* Pre-allocate a shared file with one record per thread. */
    fd = open("/tmp/file_stress.dat",
              O_CREAT | O_TRUNC | O_RDWR, 0644);
    if (fd < 0) {
        fprintf(stderr, "open /tmp/file_stress.dat failed\n");
        return 1;
    }
    if (ftruncate(fd, (off_t)ncpus * RECORD_SIZE) != 0) {
        fprintf(stderr, "ftruncate failed\n");
        close(fd);
        return 1;
    }

    if (pthread_barrier_init(&barrier, NULL, ncpus) != 0) {
        fprintf(stderr, "pthread_barrier_init failed\n");
        close(fd);
        return 1;
    }

    for (i = 0; i < ncpus; i++) {
        my_ops[i] = 0;
        thread_seen[i] = 0;
    }

    for (i = 0; i < ncpus; i++) {
        ctxs[i].idx = i;
        ctxs[i].iterations = iterations;
        ctxs[i].fd = fd;
        work_args[i] = &ctxs[i];
    }

    /* smp_run_threads creates the threads, joins them, and returns the
       elapsed seconds of the join window. */
    elapsed = smp_run_threads(ncpus, thread_fn, work_args, NULL);
    if (elapsed < 0.0) {
        close(fd);
        pthread_barrier_destroy(&barrier);
        return 1;
    }

    for (i = 0; i < ncpus; i++)
        total_ops += my_ops[i];
    ops_per_sec = elapsed > 0.0 ? (double)total_ops / elapsed : 0.0;

    for (i = 0; i < ncpus; i++) {
        if (!thread_seen[i]) {
            fprintf(stderr, "thread %d never ran\n", i);
            failures = 1;
        }
        if (my_ops[i] != iterations) {
            fprintf(stderr, "thread %d did %ld ops, expected %d\n",
                    i, my_ops[i], iterations);
            failures = 1;
        }
    }

    close(fd);
    unlink("/tmp/file_stress.dat");
    pthread_barrier_destroy(&barrier);

    smp_print_results("file_stress", failures == 0,
                      elapsed * 1000.0, ops_per_sec, "ops/sec");

    return failures;
}
