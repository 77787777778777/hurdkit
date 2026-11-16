/* sock_stress.c — stress Unix domain socket I/O across CPUs.
 *
 * Thread 0 (client) and thread 1 (server) are connected via socketpair().
 * The client sends ROUNDS messages; the server reads each one and echoes
 * it back; the client verifies the echo.  Threads 2+ idle on a barrier.
 * This exercises socket-buffer and credential passing — a different IPC
 * mechanism than Mach msg_send (ipc_stress) or Mach RPC (rpc_stress) —
 * across threads potentially executing on different CPUs.
 *
 * Exits non-zero if any socket call fails or any echoed message is
 * corrupted.
 */

#include <sys/socket.h>
#include <sys/uio.h>
#include <pthread.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

#include "smp_stress.h"

#define MAX_CPUS 16
#define ROUNDS 1000
#define MSG_SIZE 64

static int sv[2]; /* socketpair */
static pthread_barrier_t barrier;
static long verify_failures;
static int thread_seen[MAX_CPUS];
static volatile int failures;

struct thread_ctx {
    int idx;
    int rounds;
};

static void *
client_fn(void *arg)
{
    struct thread_ctx *ctx = arg;
    int rounds = ctx->rounds;
    char send_buf[MSG_SIZE];
    char recv_buf[MSG_SIZE];
    int i;

    thread_seen[0] = 1;

    /* Synchronize start so the server is ready to receive. */
    pthread_barrier_wait(&barrier);

    for (i = 0; i < rounds; i++) {
        memset(send_buf, (char)('A' + i % 26), MSG_SIZE);
        send_buf[MSG_SIZE - 1] = '\0';

        if (write(sv[0], send_buf, MSG_SIZE) != MSG_SIZE) {
            fprintf(stderr, "client round %d: write failed\n", i);
            __sync_add_and_fetch(&failures, 1);
            return NULL;
        }

        memset(recv_buf, 0, sizeof(recv_buf));
        if (read(sv[0], recv_buf, MSG_SIZE) != MSG_SIZE) {
            fprintf(stderr, "client round %d: read failed\n", i);
            __sync_add_and_fetch(&failures, 1);
            return NULL;
        }

        if (memcmp(recv_buf, send_buf, MSG_SIZE) != 0) {
            fprintf(stderr, "client round %d: echo mismatch\n", i);
            __sync_add_and_fetch(&failures, 1);
            __sync_add_and_fetch(&verify_failures, 1);
        }
    }

    return NULL;
}

static void *
server_fn(void *arg)
{
    struct thread_ctx *ctx = arg;
    int rounds = ctx->rounds;
    char buf[MSG_SIZE];
    int i;

    thread_seen[1] = 1;

    /* Synchronize start so the server is ready to receive. */
    pthread_barrier_wait(&barrier);

    for (i = 0; i < rounds; i++) {
        memset(buf, 0, sizeof(buf));
        if (read(sv[1], buf, MSG_SIZE) != MSG_SIZE) {
            fprintf(stderr, "server round %d: read failed\n", i);
            __sync_add_and_fetch(&failures, 1);
            return NULL;
        }

        if (write(sv[1], buf, MSG_SIZE) != MSG_SIZE) {
            fprintf(stderr, "server round %d: write (echo) failed\n", i);
            __sync_add_and_fetch(&failures, 1);
            return NULL;
        }
    }

    return NULL;
}

static void *
idle_fn(void *arg)
{
    struct thread_ctx *ctx = arg;

    thread_seen[ctx->idx] = 1;

    /* Synchronize start with the client and server. */
    pthread_barrier_wait(&barrier);

    /* No socket work for non-client/non-server threads. */
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

    ncpus = smp_check_ncpus();
    if (ncpus < 0)
        return 1;
    if (ncpus < 2) {
        fprintf(stderr, "need >= 2 CPUs for sock_stress\n");
        return 1;
    }

    if (socketpair(AF_UNIX, SOCK_STREAM, 0, sv) != 0) {
        fprintf(stderr, "socketpair failed\n");
        return 1;
    }

    if (pthread_barrier_init(&barrier, NULL, ncpus) != 0) {
        fprintf(stderr, "pthread_barrier_init failed\n");
        close(sv[0]);
        close(sv[1]);
        return 1;
    }

    /* Thread 0 = client, thread 1 = server, rest idle. */
    ctxs[0].idx = 0;
    ctxs[0].rounds = rounds;
    work_args[0] = &ctxs[0];
    if (pthread_create(&threads[0], NULL, client_fn, work_args[0]) != 0) {
        fprintf(stderr, "failed to create client thread\n");
        return 1;
    }

    ctxs[1].idx = 1;
    ctxs[1].rounds = rounds;
    work_args[1] = &ctxs[1];
    if (pthread_create(&threads[1], NULL, server_fn, work_args[1]) != 0) {
        fprintf(stderr, "failed to create server thread\n");
        return 1;
    }

    for (i = 2; i < ncpus; i++) {
        ctxs[i].idx = i;
        ctxs[i].rounds = rounds;
        work_args[i] = &ctxs[i];
        if (pthread_create(&threads[i], NULL, idle_fn, work_args[i]) != 0) {
            fprintf(stderr, "failed to create idle thread %d\n", i);
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
        fprintf(stderr, "%ld echo mismatches\n", verify_failures);
        failures = 1;
    }

    close(sv[0]);
    close(sv[1]);
    pthread_barrier_destroy(&barrier);

    long total_msgs = (long)rounds * 2; /* send + echo */
    double msgs_per_sec = elapsed > 0.0 ? (double)total_msgs / elapsed : 0.0;
    smp_print_results("sock_stress", failures == 0,
                      elapsed * 1000.0, msgs_per_sec, "msgs/sec");

    return failures;
}
