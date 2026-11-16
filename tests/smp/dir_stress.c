/* dir_stress.c — stress directory VFS operations across CPUs.
 *
 * Spawns one thread per online CPU.  Each thread repeatedly creates a
 * subdirectory, creates a file inside it, looks it up, removes the file,
 * and removes the directory — using thread-unique names so they don't
 * conflict with each other, but contending on the parent directory lock
 * for every operation.  This exercises directory entry hashing, name
 * lookup, and VFS inode serialization across CPUs — a kernel path
 * distinct from file_stress's pread/pwrite on an existing file, and
 * particularly relevant for Hurd where filesystem translators run in
 * userspace and every directory op crosses the Mach IPC boundary.
 *
 * Exits non-zero if any directory operation fails or any thread fails
 * to run.
 */

#include <dirent.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include "smp_stress.h"

#define MAX_CPUS 16
#define ITERATIONS 500

static int ncpus;
static long my_ops[MAX_CPUS];   /* completed mkdir/create/lookup/unlink/rmdir cycles per thread */
static int thread_seen[MAX_CPUS];
static pthread_barrier_t barrier;
static volatile int failures;

static void *
thread_fn(void *arg)
{
    int idx = *(int *)arg;
    int iterations = ITERATIONS;
    char base[256];
    char dir[256];
    char file[256];
    int i;

    /* Mark this thread as having run. */
    thread_seen[idx] = 1;

    /* Synchronize start so all threads contend on the directory together. */
    pthread_barrier_wait(&barrier);

    snprintf(base, sizeof(base), "/tmp/dir_stress_%d", idx);

    /* Create the per-thread base directory before the loop. */
    if (mkdir(base, 0755) != 0) {
        fprintf(stderr, "thread %d: mkdir %s failed\n", idx, base);
        __sync_add_and_fetch(&failures, 1);
        return NULL;
    }

    for (i = 0; i < iterations; i++) {
        /* Create a thread-unique subdirectory. */
        snprintf(dir, sizeof(dir), "%s/dir_%d", base, i);
        if (mkdir(dir, 0755) != 0) {
            fprintf(stderr, "thread %d iter %d: mkdir %s failed\n", idx, i, dir);
            __sync_add_and_fetch(&failures, 1);
            break;
        }

        /* Create a file inside it. */
        snprintf(file, sizeof(file), "%s/data", dir);
        if (creat(file, 0644) < 0) {
            fprintf(stderr, "thread %d iter %d: creat %s failed\n", idx, i, file);
            rmdir(dir);
            __sync_add_and_fetch(&failures, 1);
            break;
        }

        /* Look it up via opendir/readdir to exercise name resolution. */
        DIR *dp = opendir(dir);
        if (dp == NULL) {
            fprintf(stderr, "thread %d iter %d: opendir %s failed\n", idx, i, dir);
            unlink(file);
            rmdir(dir);
            __sync_add_and_fetch(&failures, 1);
            break;
        }
        struct dirent *de;
        int found = 0;
        while ((de = readdir(dp)) != NULL) {
            if (strcmp(de->d_name, "data") == 0) {
                found = 1;
                break;
            }
        }
        closedir(dp);
        if (!found) {
            fprintf(stderr, "thread %d iter %d: data not found in %s\n", idx, i, dir);
            unlink(file);
            rmdir(dir);
            __sync_add_and_fetch(&failures, 1);
            break;
        }

        /* Remove the file, then the directory. */
        if (unlink(file) != 0) {
            fprintf(stderr, "thread %d iter %d: unlink %s failed\n", idx, i, file);
            rmdir(dir);
            __sync_add_and_fetch(&failures, 1);
            break;
        }
        if (rmdir(dir) != 0) {
            fprintf(stderr, "thread %d iter %d: rmdir %s failed\n", idx, i, dir);
            __sync_add_and_fetch(&failures, 1);
            break;
        }

        my_ops[idx]++;
    }

    /* Clean up any leftover base directory. */
    snprintf(dir, sizeof(dir), "%s", base);
    rmdir(dir);

    return NULL;
}

int
main(int argc, char **argv)
{
    pthread_t threads[MAX_CPUS];
    int args[MAX_CPUS];
    void *work_args[MAX_CPUS];
    int iterations = smp_parse_iterations(argc, argv, ITERATIONS);
    long total_ops = 0;
    double elapsed, ops_per_sec;
    int i;

    ncpus = smp_check_ncpus();
    if (ncpus < 0)
        return 1;

    /* Create the shared base directory. */
    if (mkdir("/tmp/dir_stress_root", 0755) != 0) {
        /* Ignore EEXIST — a previous run may have left it. */
    }

    if (pthread_barrier_init(&barrier, NULL, ncpus) != 0) {
        fprintf(stderr, "pthread_barrier_init failed\n");
        return 1;
    }

    for (i = 0; i < ncpus; i++) {
        my_ops[i] = 0;
        thread_seen[i] = 0;
    }

    for (i = 0; i < ncpus; i++) {
        args[i] = i;
        work_args[i] = &args[i];
    }

    /* smp_run_threads creates the threads, joins them, and returns the
       elapsed seconds of the join window. */
    elapsed = smp_run_threads(ncpus, thread_fn, work_args, NULL);
    if (elapsed < 0.0) {
        pthread_barrier_destroy(&barrier);
        return 1;
    }

    for (i = 0; i < ncpus; i++) {
        if (!thread_seen[i]) {
            fprintf(stderr, "thread %d never ran\n", i);
            failures = 1;
        }
        total_ops += my_ops[i];
    }

    /* Each cycle is 5 ops: mkdir + creat + lookup + unlink + rmdir. */
    total_ops *= 5;
    ops_per_sec = elapsed > 0.0 ? (double)total_ops / elapsed : 0.0;
    smp_print_results("dir_stress", failures == 0,
                      elapsed * 1000.0, ops_per_sec, "ops/sec");

    pthread_barrier_destroy(&barrier);
    rmdir("/tmp/dir_stress_root");
    return failures;
}
