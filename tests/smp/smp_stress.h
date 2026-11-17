#ifndef SMP_STRESS_H
#define SMP_STRESS_H

#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define SMP_MAX_CPUS 16

/* Prototype for a per-thread work function.  The argument is a
   test-supplied pointer (typically a struct containing the thread index
   and any per-thread state). */
typedef void *(*smp_work_fn)(void *arg);

/* Monotonic time in seconds. */
static inline double
smp_now_sec(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

/* Print elapsed time: milliseconds when under one second, seconds
   otherwise. */
static inline void
smp_print_elapsed(double elapsed)
{
    if (elapsed < 1.0)
        printf("%.1f ms", elapsed * 1e3);
    else
        printf("%.3f s", elapsed);
}

/* Parse optional iteration count from argv[1].  Falls back to
   default_iterations when no argument is given. */
static inline int
smp_parse_iterations(int argc, char **argv, int default_iterations)
{
    if (argc > 1)
        return atoi(argv[1]);
    return default_iterations;
}

/* Validate that at least 2 CPUs are online, unless SMP_UP=1 (uniprocessor
   baseline mode).  Returns the online CPU count, or -1 on failure. */
static inline int
smp_check_ncpus(void)
{
    int ncpus = (int)sysconf(_SC_NPROCESSORS_ONLN);
    if (ncpus < 2 && !getenv("SMP_UP")) {
        fprintf(stderr, "only %d CPU(s) online, need >= 2 for SMP test\n",
                ncpus);
        return -1;
    }
    if (ncpus > SMP_MAX_CPUS)
        ncpus = SMP_MAX_CPUS;
    return ncpus;
}

/* Create ncpus threads running work_fn(work_args[i]) on each, then join
   them all.  Times the join section and returns elapsed seconds.  If
   out_threads is non-NULL, the pthread_t handles are stored there so
   callers can reference other threads (e.g. for pthread_kill). */
static inline double
smp_run_threads(int ncpus, smp_work_fn work_fn, void *work_args[],
                pthread_t out_threads[])
{
    pthread_t threads[SMP_MAX_CPUS];
    double start, end;
    int i;

    for (i = 0; i < ncpus; i++)
        if (pthread_create(&threads[i], NULL,
                           (void *(*)(void *))work_fn, work_args[i]) != 0) {
            fprintf(stderr, "failed to create thread %d\n", i);
            return -1.0;
        }
        else if (out_threads)
            out_threads[i] = threads[i];

    start = smp_now_sec();
    for (i = 0; i < ncpus; i++)
        pthread_join(threads[i], NULL);
    end = smp_now_sec();

    return end - start;
}

/* Escape a C string for safe inclusion inside a JSON string value.
   Writes to 'out' (must be at least 2*in_len+1 bytes). Returns the
   number of bytes written (not counting the NUL terminator). */
static inline size_t
smp_json_escape(char *out, const char *in, size_t out_size)
{
    size_t i, j = 0;
    for (i = 0; in[i] != '\0' && j + 1 < out_size; i++) {
        unsigned char c = (unsigned char)in[i];
        switch (c) {
        case '"':  out[j++] = '\\'; out[j++] = '"'; break;
        case '\\': out[j++] = '\\'; out[j++] = '\\'; break;
        case '\n': out[j++] = '\\'; out[j++] = 'n'; break;
        case '\r': out[j++] = '\\'; out[j++] = 'r'; break;
        case '\t': out[j++] = '\\'; out[j++] = 't'; break;
        default:
            if (c < 0x20) {
                if (j + 6 < out_size) {
                    j += snprintf(out + j, out_size - j, "\\u%04x", c);
                } else {
                    goto done;
                }
            } else {
                out[j++] = (char)c;
            }
            break;
        }
    }
done:
    out[j] = '\0';
    return j;
}

/* Print pass/fail result with timing and rate.
   JSON when SMP_JSON=1, human-readable otherwise. */
static inline void
smp_print_results(const char *phase, int passed,
                  double elapsed_ms, double rate, const char *unit)
{
    if (getenv("SMP_JSON")) {
        char phase_esc[512];
        char unit_esc[128];
        smp_json_escape(phase_esc, phase, sizeof(phase_esc));
        smp_json_escape(unit_esc, unit, sizeof(unit_esc));
        printf("{\"phase\":\"%s\",\"passed\":%s,\"elapsed_ms\":%.3f,"
               "\"rate\":%.0f,\"unit\":\"%s\"}\n",
               phase_esc, passed ? "true" : "false",
               elapsed_ms, rate, unit_esc);
    } else if (passed) {
        printf("%s: OK, ", phase);
        smp_print_elapsed(elapsed_ms / 1000.0);
        printf(", %.0f %s\n", rate, unit);
    } else {
        printf("%s: FAILED, ", phase);
        smp_print_elapsed(elapsed_ms / 1000.0);
        printf(", %.0f %s\n", rate, unit);
    }
}

/* Return 1 when the SMP_JSON environment variable is set (non-empty). */
static inline int
smp_json_mode(void)
{
    return getenv("SMP_JSON") != NULL;
}

/* Return 1 when the SMP_UP environment variable is set (non-empty),
   meaning the test is running on a uniprocessor kernel. */
static inline int
smp_up_mode(void)
{
    return getenv("SMP_UP") != NULL;
}

#endif /* SMP_STRESS_H */
