/* timer_stress.c — stress per-CPU timer delivery with many concurrent
 * sleepers.
 *
 * Spawns THREADS_PER_CPU threads per online CPU (oversubscribing the
 * machine) and has every thread nanosleep() in a loop with a short,
 * varying timeout until a wall-clock budget is exhausted.  A single
 * sleeper per CPU would only produce a couple of wakeups per second and
 * leave the CPUs idle; oversubscribing makes every timer tick expire
 * several outstanding timers at once, so each tick does real work in
 * the timeout wheel and the wakeup/schedule/re-sleep path, and threads
 * compete for CPUs so timers are armed and expired on and across both
 * CPUs.
 *
 * What one iteration actually exercises: on the Hurd, nanosleep() is a
 * timed mach_msg receive on a throwaway reply port
 * (nanosleep_call() in 3p/glibc/sysdeps/mach/clock_nanosleep.c creates
 * the port, waits with MACH_RCV_TIMEOUT | MACH_RCV_INTERRUPT, then
 * destroys it), so every iteration also churns a port allocation and a
 * timed IPC receive, the same path the kernel uses for its own IPC
 * timeouts.
 *
 * Timeout granularity: the kernel converts the millisecond timeout to
 * ticks with convert_ipc_timeout_to_ticks() in
 * 3p/gnumach/kern/sched_prim.h, which rounds UP so sub-tick requests
 * wait at least one tick, and arming is quantized to tick boundaries
 * (HZ=100 nominally gives a 10 ms tick, see
 * 3p/gnumach/i386/i386/mach_param.h, driven by tick() in
 * 3p/gnumach/kern/mach_clock.c).  Measured on this kernel, every
 * request below one tick wakes on the next tick boundary for an
 * effective step of ~20 ms, so the 1..MAX_SLEEP_MS requests here all
 * collapse onto the same bucket; on a finer-grained kernel the varying
 * requests would spread deadlines across buckets instead.  Either way
 * the per-tick burst of concurrent expiries is what gets stressed:
 * measured with 32 sleepers, this kernel sustains ~1600 wakeups/s with
 * per-sleep latency still pinned at the tick step and no drift.
 *
 * Iterations are bounded by a wall-clock budget instead of a fixed
 * count: with tick granularity, a "1 ms" sleep really waits a full
 * tick, so the previous fixed 5000-iteration loop ran ~100 s wall on
 * this kernel while doing almost no work.  A budget keeps the phase
 * short and makes the reported sleeps/sec comparable across kernels
 * with different tick rates.
 *
 * Exits non-zero if any sleep fails with an unexpected error, any
 * thread wakes up before its requested time has elapsed (an early
 * timer expiry is a kernel bug; a small slack is allowed for clock
 * source skew), or any thread did not run.
 */

#include <errno.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>

#include "smp_stress.h"

#define THREADS_PER_CPU 16
#define MAX_TIMER_THREADS 64
#define DEFAULT_BUDGET_SEC 2
#define MAX_SLEEP_MS 8
#define TEST_TIMEOUT_SEC 60

/* An early wake smaller than this is treated as clock skew between the
   sleep clock and CLOCK_MONOTONIC rather than a timer bug. */
#define UNDERSHOOT_SLACK_MS 1.0

struct timer_ctx {
    int idx;
    int budget_sec;
};

static int nthreads;
static long t_sleeps[MAX_TIMER_THREADS];
static double t_over_sum[MAX_TIMER_THREADS];
static double t_over_max[MAX_TIMER_THREADS];
static double t_min_actual[MAX_TIMER_THREADS];
static int t_undershoots[MAX_TIMER_THREADS];
static int t_seen[MAX_TIMER_THREADS];
static pthread_barrier_t barrier;
static volatile int failures;

/* Requested sleep for iteration i of thread idx: cycles 1..MAX_SLEEP_MS
   so consecutive sleeps land on different tick buckets. */
static int
req_ms_for(int idx, long i)
{
    return 1 + (int)((idx + i) % MAX_SLEEP_MS);
}

static void *
timer_fn(void *arg)
{
    struct timer_ctx *ctx = arg;
    double deadline = smp_now_sec() + ctx->budget_sec;
    long i;

    t_seen[ctx->idx] = 1;
    t_min_actual[ctx->idx] = 1e9;

    /* Synchronize start so all sleepers arm their first timer together. */
    pthread_barrier_wait(&barrier);

    for (i = 0; smp_now_sec() < deadline; i++) {
        int req_ms = req_ms_for(ctx->idx, i);
        struct timespec req = {0, req_ms * 1000000L};
        struct timespec rem = req;
        double actual;

        double iter_start = smp_now_sec();
        for (;;) {
            if (nanosleep(&rem, &rem) == 0)
                break;
            if (errno != EINTR) {
                fprintf(stderr,
                        "timer thread %d: nanosleep failed: %d\n",
                        ctx->idx, errno);
                __sync_add_and_fetch(&failures, 1);
                return NULL;
            }
            /* EINTR: sleep the remainder (no signals are expected in
               this test, so this path is just safety). */
        }
        actual = (smp_now_sec() - iter_start) * 1000.0;

        t_sleeps[ctx->idx]++;
        if (actual < t_min_actual[ctx->idx])
            t_min_actual[ctx->idx] = actual;
        if (actual >= (double)req_ms) {
            double over = actual - req_ms;
            t_over_sum[ctx->idx] += over;
            if (over > t_over_max[ctx->idx])
                t_over_max[ctx->idx] = over;
        } else if (actual < (double)req_ms - UNDERSHOOT_SLACK_MS) {
            /* Woke up before the request elapsed: the timeout wheel
               fired early, which nanosleep() must never allow. */
            if (t_undershoots[ctx->idx] == 0)
                fprintf(stderr,
                        "timer thread %d: slept %.2f ms, requested %d ms\n",
                        ctx->idx, actual, req_ms);
            t_undershoots[ctx->idx]++;
            __sync_add_and_fetch(&failures, 1);
        }
    }

    return NULL;
}

int
main(int argc, char **argv)
{
    struct timer_ctx ctxs[MAX_TIMER_THREADS];
    void *work_args[MAX_TIMER_THREADS];
    int budget_sec = smp_parse_iterations(argc, argv, DEFAULT_BUDGET_SEC);
    int ncpus;
    long total_sleeps = 0, total_undershoots = 0;
    double over_sum = 0.0, over_max = 0.0, min_actual = 1e9;
    double elapsed, rate;
    int i;

    alarm(TEST_TIMEOUT_SEC);

    ncpus = smp_check_ncpus();
    if (ncpus < 0)
        return 1;

    nthreads = ncpus * THREADS_PER_CPU;
    if (nthreads > MAX_TIMER_THREADS)
        nthreads = MAX_TIMER_THREADS;

    if (pthread_barrier_init(&barrier, NULL, nthreads) != 0) {
        fprintf(stderr, "pthread_barrier_init failed\n");
        return 1;
    }

    for (i = 0; i < nthreads; i++) {
        ctxs[i].idx = i;
        ctxs[i].budget_sec = budget_sec;
        work_args[i] = &ctxs[i];
    }

    /*
     * Create and join the sleepers directly instead of via smp_run_threads:
     * smp_run_threads keeps a fixed pthread_t[SMP_MAX_CPUS] array, so it
     * cannot safely hold more than 16 threads, and this test deliberately
     * oversubscribes (THREADS_PER_CPU per CPU) to burst the timer wheel.
     */
    pthread_t sleeper_threads[MAX_TIMER_THREADS];
    double start = smp_now_sec();
    for (i = 0; i < nthreads; i++)
        if (pthread_create(&sleeper_threads[i], NULL, timer_fn, work_args[i]) != 0) {
            fprintf(stderr, "failed to create timer thread %d\n", i);
            __sync_add_and_fetch(&failures, 1);
            nthreads = i; /* only join the ones we created */
            goto joined;
        }
joined:
    for (i = 0; i < nthreads; i++)
        pthread_join(sleeper_threads[i], NULL);
    elapsed = smp_now_sec() - start;
    if (failures)
        goto done;

    for (i = 0; i < nthreads; i++) {
        if (!t_seen[i]) {
            fprintf(stderr, "thread %d never ran\n", i);
            failures = 1;
            continue;
        }
        if (t_sleeps[i] == 0) {
            fprintf(stderr, "thread %d completed no sleeps\n", i);
            failures = 1;
        }
        if (t_undershoots[i] > 0)
            fprintf(stderr, "thread %d: %d early wakes\n",
                    i, t_undershoots[i]);
        total_sleeps += t_sleeps[i];
        total_undershoots += t_undershoots[i];
        over_sum += t_over_sum[i];
        if (t_over_max[i] > over_max)
            over_max = t_over_max[i];
        if (t_min_actual[i] < min_actual)
            min_actual = t_min_actual[i];
    }

    rate = elapsed > 0.0 ? (double)total_sleeps / elapsed : 0.0;

    printf("timer_stress: %d threads, %ld sleeps in %.1fs, "
           "overshoot mean/max %.2f/%.2f ms, fastest sleep %.2f ms, "
           "early wakes %ld\n",
           nthreads, total_sleeps, elapsed,
           total_sleeps > 0 ? over_sum / (double)total_sleeps : 0.0,
           over_max, min_actual, total_undershoots);

    smp_print_results("timer_stress", failures == 0,
                      elapsed * 1000.0, rate, "sleeps/sec");

done:
    pthread_barrier_destroy(&barrier);
    return failures;
}
