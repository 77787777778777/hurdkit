/* rpc_stress.c — stress Mach RPC (request/reply) across CPUs.
 *
 * Thread 0 acts as the client: it sends request messages to thread 1
 * (the server) with its own port as the reply port, then waits for the
 * reply.  Thread 1 acts as the server: it receives requests on its port
 * and sends replies back to the client.  This exercises the full Mach
 * RPC path (msg_send with a reply port, msg_receive on a reply port,
 * and port right reference counting) across threads potentially
 * executing on different CPUs.  Distinct from ipc_stress's one-way
 * token ring, which never uses reply ports.
 *
 * Received-header port convention: after the kernel copies a message
 * out to a receiver, the ports are reversed relative to the send-side
 * layout.  ipc_kmsg_copyout_header() in 3p/gnumach/ipc/ipc_kmsg.c
 * stores the reply-port right the sender carried in msgh_remote_port
 * and the port the message was received on in msgh_local_port; this
 * mirrors the user-space MiG convention in 3p/mig/server.c, where
 * msgh_reply_port is defined as msgh_remote_port and msgh_request_port
 * as msgh_local_port.  A server must therefore reply to
 * req.header.msgh_remote_port.  Replying to msgh_local_port instead
 * sends the reply to the server's own receive port: the client never
 * sees a reply and fails with MACH_RCV_TIMED_OUT, while the server
 * reads its own reply back as a corrupt request on the next round.
 *
 * The reply re-uses the received reply-port right with the disposition
 * the kernel reported for it: MACH_MSGH_BITS_REMOTE() of the received
 * header yields MACH_MSG_TYPE_PORT_SEND (the client carries the reply
 * port as MAKE_SEND, which ipc_right_copyin() in
 * 3p/gnumach/ipc/ipc_right.c turns into a real send right) or
 * MACH_MSG_TYPE_PORT_SEND_ONCE.  Moving the right consumes it, the
 * same way the generated MiG server code in 3p/mig/server.c emits
 * replies (OutP->Head.msgh_remote_port = InP->msgh_reply_port with
 * MACH_MSGH_BITS_REPLY, which equals MACH_MSGH_BITS_REMOTE), so no
 * extra mach_port_deallocate is needed.
 *
 * For ncpus > 2, only threads 0 and 1 participate in the RPC exchange;
 * the remaining threads synchronize on the barrier but do not send or
 * receive messages.
 *
 * Exits non-zero if any message send/receive fails or any thread fails
 * to run.
 */

#include <mach/mach_types.h>
#include <hurd.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include "smp_stress.h"

#define MAX_CPUS 16
#define ROUNDS 1000
#define SEND_TIMEOUT_MS 5000

typedef natural_t *processor_name_array_t;

typedef struct {
    mach_msg_header_t header;
    int               seq;
    int               pad[(256 - sizeof(mach_msg_header_t) - 2 * sizeof(int)) / sizeof(int)];
} req_msg_t;

typedef struct {
    mach_msg_header_t header;
    int               seq;
    int               result;
    int               pad[(256 - sizeof(mach_msg_header_t) - 3 * sizeof(int)) / sizeof(int)];
} rep_msg_t;

static int ncpus;
static mach_port_t ports[MAX_CPUS];
static int thread_seen[MAX_CPUS];
static pthread_barrier_t barrier;
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
    int round;

    /* Mark this thread as having run. */
    thread_seen[0] = 1;

    /* Synchronize start so the server is ready to receive. */
    pthread_barrier_wait(&barrier);

    for (round = 0; round < rounds; round++) {
        /*
         * Request to the server: remote is ports[1] via the send right
         * pre-inserted at setup; local is ports[0] carried as MAKE_SEND,
         * so the kernel mints a fresh send right for the reply port on
         * every request (ipc_right_copyin() in
         * 3p/gnumach/ipc/ipc_right.c).  After the server's copyout that
         * right appears in the received header's msgh_remote_port.
         */
        req_msg_t req;
        memset(&req, 0, sizeof(req));
        req.header.msgh_bits = MACH_MSGH_BITS(MACH_MSG_TYPE_COPY_SEND, MACH_MSG_TYPE_MAKE_SEND);
        req.header.msgh_size = (mach_msg_size_t)sizeof(req);
        req.header.msgh_remote_port = ports[1];
        req.header.msgh_local_port = ports[0];
        req.header.msgh_id = 1000;
        req.seq = round;

        kern_return_t kr = mach_msg(&req.header, MACH_SEND_MSG | MACH_SEND_TIMEOUT,
                                    (mach_msg_size_t)sizeof(req), 0,
                                    MACH_PORT_NULL, SEND_TIMEOUT_MS, MACH_PORT_NULL);
        if (kr != KERN_SUCCESS) {
            fprintf(stderr, "client round %d: request send failed: %d\n", round, kr);
            __sync_add_and_fetch(&failures, 1);
            break;
        }

        /* Wait for the reply on our own port. */
        rep_msg_t rep;
        memset(&rep, 0, sizeof(rep));
        rep.header.msgh_local_port = ports[0];
        rep.header.msgh_size = (mach_msg_size_t)sizeof(rep);

        kr = mach_msg(&rep.header, MACH_RCV_MSG | MACH_RCV_TIMEOUT, 0,
                      (mach_msg_size_t)sizeof(rep), ports[0],
                      SEND_TIMEOUT_MS, MACH_PORT_NULL);
        if (kr != KERN_SUCCESS) {
            fprintf(stderr, "client round %d: reply receive failed: %d\n", round, kr);
            __sync_add_and_fetch(&failures, 1);
            break;
        }

        if (rep.header.msgh_id != 1001 || rep.seq != round) {
            fprintf(stderr, "client round %d: bad reply: id=%d seq=%d\n",
                    round, rep.header.msgh_id, rep.seq);
            __sync_add_and_fetch(&failures, 1);
            break;
        }
    }

    return NULL;
}

static void *
server_fn(void *arg)
{
    struct thread_ctx *ctx = arg;
    int rounds = ctx->rounds;
    int round;

    /* Mark this thread as having run. */
    thread_seen[1] = 1;

    /* Synchronize start so the client is ready to send. */
    pthread_barrier_wait(&barrier);

    for (round = 0; round < rounds; round++) {
        /* Receive request from client (thread 0). */
        req_msg_t req;
        memset(&req, 0, sizeof(req));
        req.header.msgh_local_port = ports[1];
        req.header.msgh_size = (mach_msg_size_t)sizeof(req);

        kern_return_t kr = mach_msg(&req.header, MACH_RCV_MSG | MACH_RCV_TIMEOUT, 0,
                                    (mach_msg_size_t)sizeof(req), ports[1],
                                    SEND_TIMEOUT_MS, MACH_PORT_NULL);
        if (kr != KERN_SUCCESS) {
            fprintf(stderr, "server round %d: request receive failed: %d\n", round, kr);
            __sync_add_and_fetch(&failures, 1);
            break;
        }

        if (req.header.msgh_id != 1000 || req.seq != round) {
            fprintf(stderr, "server round %d: bad request: id=%d seq=%d\n",
                    round, req.header.msgh_id, req.seq);
            __sync_add_and_fetch(&failures, 1);
            break;
        }

        /*
         * Reply to the client.  The reply right arrived in
         * req.header.msgh_remote_port (received-header convention, see
         * the file comment and ipc_kmsg_copyout_header() in
         * 3p/gnumach/ipc/ipc_kmsg.c), so target that port and move the
         * right back with the disposition the kernel reported for it,
         * the way the generated MiG server code does (3p/mig/server.c).
         */
        rep_msg_t rep;
        memset(&rep, 0, sizeof(rep));
        rep.header.msgh_bits =
            MACH_MSGH_BITS(MACH_MSGH_BITS_REMOTE(req.header.msgh_bits), 0);
        rep.header.msgh_size = (mach_msg_size_t)sizeof(rep);
        rep.header.msgh_remote_port = req.header.msgh_remote_port;
        rep.header.msgh_local_port = MACH_PORT_NULL;
        rep.header.msgh_id = 1001;
        rep.seq = round;
        rep.result = round + 1;

        kr = mach_msg(&rep.header, MACH_SEND_MSG | MACH_SEND_TIMEOUT,
                      (mach_msg_size_t)sizeof(rep), 0,
                      MACH_PORT_NULL, SEND_TIMEOUT_MS, MACH_PORT_NULL);
        if (kr != KERN_SUCCESS) {
            fprintf(stderr, "server round %d: reply send failed: %d\n", round, kr);
            __sync_add_and_fetch(&failures, 1);
            break;
        }
    }

    return NULL;
}

static void *
idle_fn(void *arg)
{
    struct thread_ctx *ctx = arg;

    /* Mark this thread as having run. */
    thread_seen[ctx->idx] = 1;

    /* Synchronize start with the other threads. */
    pthread_barrier_wait(&barrier);

    /* No RPC work for non-client/non-server threads. */
    return NULL;
}

int
main(int argc, char **argv)
{
    pthread_t threads[MAX_CPUS];
    struct thread_ctx ctxs[MAX_CPUS];
    void *work_args[MAX_CPUS];
    int rounds = smp_parse_iterations(argc, argv, ROUNDS);
    int i;

    ncpus = smp_check_ncpus();
    if (ncpus < 0)
        return 1;
    if (ncpus < 2) {
        fprintf(stderr, "need >= 2 CPUs for rpc_stress\n");
        return 1;
    }

    /* Create one receive port per thread (client and server each need one). */
    for (i = 0; i < 2; i++) {
        kern_return_t kr = mach_port_allocate(mach_task_self(),
                                              MACH_PORT_RIGHT_RECEIVE,
                                              &ports[i]);
        if (kr != KERN_SUCCESS) {
            fprintf(stderr, "mach_port_allocate(%d) failed: %d\n", i, kr);
            return 1;
        }
        kr = mach_port_insert_right(mach_task_self(), ports[i], ports[i],
                                    MACH_MSG_TYPE_MAKE_SEND);
        if (kr != KERN_SUCCESS) {
            fprintf(stderr, "mach_port_insert_right(%d) failed: %d\n", i, kr);
            return 1;
        }
    }

    if (pthread_barrier_init(&barrier, NULL, ncpus) != 0) {
        fprintf(stderr, "pthread_barrier_init failed\n");
        return 1;
    }

    for (i = 0; i < ncpus; i++) {
        ctxs[i].idx = i;
        ctxs[i].rounds = rounds;
        work_args[i] = &ctxs[i];
    }

    /* Thread 0 = client, thread 1 = server, rest idle. */
    if (pthread_create(&threads[0], NULL, client_fn, work_args[0]) != 0) {
        fprintf(stderr, "failed to create client thread\n");
        return 1;
    }
    if (pthread_create(&threads[1], NULL, server_fn, work_args[1]) != 0) {
        fprintf(stderr, "failed to create server thread\n");
        return 1;
    }
    for (i = 2; i < ncpus; i++) {
        if (pthread_create(&threads[i], NULL, idle_fn, work_args[i]) != 0) {
            fprintf(stderr, "failed to create idle thread %d\n", i);
            return 1;
        }
    }

    /* smp_run_threads uses one work_fn for every thread; this test needs
       client_fn on thread 0, server_fn on thread 1, and idle_fn on the
       rest, so create and join the threads directly and time the join
       window ourselves. */
    double start = smp_now_sec();
    for (i = 0; i < ncpus; i++)
        if (pthread_join(threads[i], NULL) != 0) {
            fprintf(stderr, "failed to join thread %d\n", i);
            return 1;
        }
    double elapsed = smp_now_sec() - start;

    long total_rpcs = (long)rounds;
    double rpcs_per_sec = elapsed > 0.0 ? (double)total_rpcs / elapsed : 0.0;
    smp_print_results("rpc_stress", failures == 0,
                      elapsed * 1000.0, rpcs_per_sec, "rpcs/sec");

    /* Destroy ports. */
    for (i = 0; i < 2; i++)
        mach_port_deallocate(mach_task_self(), ports[i]);

    return failures;
}
