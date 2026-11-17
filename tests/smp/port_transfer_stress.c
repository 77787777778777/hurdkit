/* port_transfer_stress.c — stress Mach port right transfer across CPUs.
 *
 * Thread 0 creates a port P and inserts a send right.  Thread 0 then
 * sends P's send right to thread 1 using MACH_MSG_TYPE_MOVE_SEND (the
 * right moves: thread 0 loses it, thread 1 gains it).  Thread 1 sends
 * it back the same way.  This round-trip repeats ROUNDS times.
 *
 * This exercises Mach port right reference counting and deallocation
 * across threads on different CPUs — a different path from ipc_stress
 * / rpc_stress, which only copy-send rights (the right is duplicated,
 * not moved).
 *
 * Exits non-zero if any mach_msg send/receive fails during the transfer.
 */

#include <mach/mach_types.h>
#include <hurd.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "smp_stress.h"

#define MAX_CPUS 16
#define ROUNDS 1000
#define SEND_TIMEOUT_MS 5000

/* Forward-declare Mach trap the guest headers may not expose. */
kern_return_t mach_port_deallocate(mach_port_t, mach_port_t);

/* A Mach port descriptor embedded in a message body.  The guest headers
   may not expose mach_msg_port_descriptor_t, so we declare it here. */
typedef struct {
    mach_port_t name;
    unsigned int pad;
    unsigned int type;
    unsigned int disposition;
} port_descriptor_t;

typedef struct {
    mach_msg_header_t header;
    /* mach_msg_body_t is implicit between header and descriptors. */
    port_descriptor_t port;
    int pad[(256 - sizeof(mach_msg_header_t) - sizeof(port_descriptor_t)) / sizeof(int)];
} port_msg_t;

static mach_port_t port_p;   /* the port whose send right is transferred */
static mach_port_t port_r0;  /* thread 0's receive port */
static mach_port_t port_r1;  /* thread 1's receive port */
static int thread_seen[MAX_CPUS];
static volatile int failures;
static int ncpus;

struct thread_ctx {
    int idx;
    int rounds;
};

static void *
sender_fn(void *arg)
{
    struct thread_ctx *ctx = arg;
    int rounds = ctx->rounds;
    int i;

    thread_seen[0] = 1;

    for (i = 0; i < rounds; i++) {
        /* Send port_p's send right to thread 1 (MOVE_SEND). */
        port_msg_t msg;
        memset(&msg, 0, sizeof(msg));
        msg.header.msgh_bits = MACH_MSGH_BITS(MACH_MSG_TYPE_COPY_SEND,
                                              MACH_MSG_TYPE_MAKE_SEND);
        msg.header.msgh_size = (mach_msg_size_t)sizeof(msg);
        msg.header.msgh_remote_port = port_r1;
        msg.header.msgh_local_port = port_r0;
        msg.header.msgh_id = 2000;
        msg.port.name = port_p;
        msg.port.type = MACH_MSG_TYPE_MOVE_SEND;
        msg.port.disposition = MACH_MSG_TYPE_MOVE_SEND;

        kern_return_t kr = mach_msg(&msg.header, MACH_SEND_MSG | MACH_SEND_TIMEOUT,
                                    (mach_msg_size_t)sizeof(msg), 0,
                                    MACH_PORT_NULL, SEND_TIMEOUT_MS, MACH_PORT_NULL);
        if (kr != KERN_SUCCESS) {
            fprintf(stderr, "sender round %d: move-send failed: %d\n", i, kr);
            __sync_add_and_fetch(&failures, 1);
            break;
        }

        /* Receive port_p's send right back from thread 1. */
        memset(&msg, 0, sizeof(msg));
        msg.header.msgh_local_port = port_r0;
        msg.header.msgh_size = (mach_msg_size_t)sizeof(msg);

        kr = mach_msg(&msg.header, MACH_RCV_MSG | MACH_RCV_TIMEOUT, 0,
                      (mach_msg_size_t)sizeof(msg), port_r0,
                      SEND_TIMEOUT_MS, MACH_PORT_NULL);
        if (kr != KERN_SUCCESS) {
            fprintf(stderr, "sender round %d: move-receive failed: %d\n", i, kr);
            __sync_add_and_fetch(&failures, 1);
            break;
        }
        if (msg.header.msgh_id != 2001) {
            fprintf(stderr, "sender round %d: unexpected msg id %d\n",
                    i, msg.header.msgh_id);
            __sync_add_and_fetch(&failures, 1);
            break;
        }
    }

    return NULL;
}

static void *
receiver_fn(void *arg)
{
    struct thread_ctx *ctx = arg;
    int rounds = ctx->rounds;
    int i;

    thread_seen[1] = 1;

    for (i = 0; i < rounds; i++) {
        /* Receive port_p's send right from thread 0. */
        port_msg_t msg;
        memset(&msg, 0, sizeof(msg));
        msg.header.msgh_local_port = port_r1;
        msg.header.msgh_size = (mach_msg_size_t)sizeof(msg);

        kern_return_t kr = mach_msg(&msg.header, MACH_RCV_MSG | MACH_RCV_TIMEOUT, 0,
                                    (mach_msg_size_t)sizeof(msg), port_r1,
                                    SEND_TIMEOUT_MS, MACH_PORT_NULL);
        if (kr != KERN_SUCCESS) {
            fprintf(stderr, "receiver round %d: move-receive failed: %d\n", i, kr);
            __sync_add_and_fetch(&failures, 1);
            break;
        }
        if (msg.header.msgh_id != 2000) {
            fprintf(stderr, "receiver round %d: unexpected msg id %d\n",
                    i, msg.header.msgh_id);
            __sync_add_and_fetch(&failures, 1);
            break;
        }

        /* Send port_p's send right back to thread 0 (MOVE_SEND). */
        memset(&msg, 0, sizeof(msg));
        msg.header.msgh_bits = MACH_MSGH_BITS(MACH_MSG_TYPE_COPY_SEND,
                                              MACH_MSG_TYPE_MAKE_SEND);
        msg.header.msgh_size = (mach_msg_size_t)sizeof(msg);
        msg.header.msgh_remote_port = port_r0;
        msg.header.msgh_local_port = port_r1;
        msg.header.msgh_id = 2001;
        msg.port.name = port_p;
        msg.port.type = MACH_MSG_TYPE_MOVE_SEND;
        msg.port.disposition = MACH_MSG_TYPE_MOVE_SEND;

        kr = mach_msg(&msg.header, MACH_SEND_MSG | MACH_SEND_TIMEOUT,
                      (mach_msg_size_t)sizeof(msg), 0,
                      MACH_PORT_NULL, SEND_TIMEOUT_MS, MACH_PORT_NULL);
        if (kr != KERN_SUCCESS) {
            fprintf(stderr, "receiver round %d: move-send failed: %d\n", i, kr);
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

    thread_seen[ctx->idx] = 1;

    /* No port work for non-participant threads. */
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
    kern_return_t kr;

    ncpus = smp_check_ncpus();
    if (ncpus < 0)
        return 1;
    if (ncpus < 2) {
        fprintf(stderr, "need >= 2 CPUs for port_transfer_stress\n");
        return 1;
    }

    /* Create port P (receive right + send right). */
    kr = mach_port_allocate(mach_task_self(), MACH_PORT_RIGHT_RECEIVE,
                            &port_p);
    if (kr != KERN_SUCCESS) {
        fprintf(stderr, "mach_port_allocate(P) failed: %d\n", kr);
        return 1;
    }
    kr = mach_port_insert_right(mach_task_self(), port_p, port_p,
                                MACH_MSG_TYPE_MAKE_SEND);
    if (kr != KERN_SUCCESS) {
        fprintf(stderr, "mach_port_insert_right(P) failed: %d\n", kr);
        return 1;
    }

    /* Create receive ports R0 and R1 for the two threads. */
    for (i = 0; i < 2; i++) {
        mach_port_t *rp = (i == 0) ? &port_r0 : &port_r1;
        kr = mach_port_allocate(mach_task_self(), MACH_PORT_RIGHT_RECEIVE, rp);
        if (kr != KERN_SUCCESS) {
            fprintf(stderr, "mach_port_allocate(R%d) failed: %d\n", i, kr);
            return 1;
        }
        kr = mach_port_insert_right(mach_task_self(), *rp, *rp,
                                    MACH_MSG_TYPE_MAKE_SEND);
        if (kr != KERN_SUCCESS) {
            fprintf(stderr, "mach_port_insert_right(R%d) failed: %d\n", i, kr);
            return 1;
        }
    }

    /* Thread 0 = sender, thread 1 = receiver, rest idle. */
    ctxs[0].idx = 0;
    ctxs[0].rounds = rounds;
    work_args[0] = &ctxs[0];
    if (pthread_create(&threads[0], NULL, sender_fn, work_args[0]) != 0) {
        fprintf(stderr, "failed to create sender thread\n");
        return 1;
    }

    ctxs[1].idx = 1;
    ctxs[1].rounds = rounds;
    work_args[1] = &ctxs[1];
    if (pthread_create(&threads[1], NULL, receiver_fn, work_args[1]) != 0) {
        fprintf(stderr, "failed to create receiver thread\n");
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

    double transfers_per_sec =
        elapsed > 0.0 ? (double)(rounds * 2) / elapsed : 0.0;
    smp_print_results("port_transfer_stress", failures == 0,
                      elapsed * 1000.0, transfers_per_sec, "transfers/sec");

    /* Clean up ports. */
    mach_port_deallocate(mach_task_self(), port_p);
    mach_port_deallocate(mach_task_self(), port_r0);
    mach_port_deallocate(mach_task_self(), port_r1);
    return failures;
}
