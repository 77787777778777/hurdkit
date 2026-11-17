/* fork_exec_stress.c — stress process creation across CPUs.
 *
 * Spawns one thread per online CPU.  Each thread repeatedly forks; the
 * child execs /bin/true and exits, while the parent waits for the child.
 * This exercises process creation, address-space setup, page-table
 * construction, and program loading on secondary CPUs — a kernel path
 * distinct from thread creation (thread_churn), which shares the parent
 * address space, and from all synchronization-path tests.
 *
 * Exits non-zero if any fork fails, any child exits non-zero, or any
 * thread fails to run.
 */

#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/wait.h>
#include <unistd.h>

#include "smp_stress.h"

#define MAX_CPUS 16
#define ITERATIONS 100

static int ncpus;
static long my_forks[MAX_CPUS];   /* successful forks per thread */
static int thread_seen[MAX_CPUS];
static pthread_barrier_t barrier;
static volatile int failures;

static void *
thread_fn(void *arg)
{
    int idx = *(int *)arg;
    int iterations = ITERATIONS;
    int i;

    /* Mark this thread as having run. */
    thread_seen[idx] = 1;

    /* Synchronize start so all threads fork concurrently. */
    pthread_barrier_wait(&barrier);

    for (i = 0; i < iterations; i++) {
        pid_t pid = fork();
        if (pid < 0) {
            fprintf(stderr, "thread %d iter %d: fork failed\n", idx, i);
            __sync_add_and_fetch(&failures, 1);
            break;
        }
        if (pid == 0) {
            /* Child: exec /bin/true and exit with its status. */
            execl("/bin/true", "true", (char *)NULL);
            _exit(127);
        }

        /* Parent: wait for the child. */
        int status;
        if (waitpid(pid, &status, 0) < 0) {
            fprintf(stderr, "thread %d iter %d: waitpid failed\n", idx, i);
            __sync_add_and_fetch(&failures, 1);
            break;
        }
        if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
            fprintf(stderr, "thread %d iter %d: child exited abnormally\n",
                    idx, i);
            __sync_add_and_fetch(&failures, 1);
            break;
        }

        my_forks[idx]++;
    }

    return NULL;
}

int
main(int argc, char **argv)
{
    pthread_t threads[MAX_CPUS];
    int args[MAX_CPUS];
    void *work_args[MAX_CPUS];
    int iterations = smp_parse_iterations(argc, argv, ITERATIONS);
    long total_forks = 0;
    double elapsed, forks_per_sec;
    int i;

    ncpus = smp_check_ncpus();
    if (ncpus < 0)
        return 1;

    if (pthread_barrier_init(&barrier, NULL, ncpus) != 0) {
        fprintf(stderr, "pthread_barrier_init failed\n");
        return 1;
    }

    for (i = 0; i < ncpus; i++) {
        my_forks[i] = 0;
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
        total_forks += my_forks[i];
    }

    forks_per_sec = elapsed > 0.0 ? (double)total_forks / elapsed : 0.0;
    smp_print_results("fork_exec_stress", failures == 0,
                      elapsed * 1000.0, forks_per_sec, "forks/sec");

    pthread_barrier_destroy(&barrier);
    return failures;
}
