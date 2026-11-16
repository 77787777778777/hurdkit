/* exception_stress.c — stress cross-CPU Mach exception delivery.
 *
 * Thread 0 (the fault thread) installs a Mach exception receive port as
 * the task-wide exception port via task_set_exception_port(), then
 * dereferences a PROT_NONE page.  The page fault raises EXC_BAD_ACCESS
 * and the kernel suspends thread 0 inside the exception up-call while
 * sending a struct mach_exception message (msgh_id 2400) to the port.
 * Thread 1 (the handler, potentially scheduled on a different CPU)
 * receives the exception, makes the page readable with mprotect(), and
 * replies KERN_SUCCESS in the exact mig_reply_header_t shape the
 * kernel's exception_parse_reply() accepts.  The kernel then resumes
 * thread 0 at the faulting instruction, which now succeeds, proving the
 * full exception round-trip worked.
 *
 * Why the reply must be KERN_SUCCESS: the return code is consumed by
 * exception_raise_continue_slow() in 3p/gnumach/kern/exception.c.
 * KERN_SUCCESS calls thread_exception_return(), which resumes the
 * faulting thread at the faulting instruction.  Any other code falls
 * through to exception_try_task(); since this test set the task
 * exception port, the thread-level fallback has nothing to try (glibc
 * installs thread exception ports only on its own message-port thread,
 * see hurdfault.c, while _hurdsig_init() in 3p/glibc/hurd/hurdsig.c
 * points the task exception port at _hurd_msgport, a port this test
 * deliberately displaces), so the kernel ends in
 * exception_no_server(), which terminates the whole task.  In
 * particular, replying KERN_FAILURE does NOT deliver a SIGSEGV:
 * translating exceptions into Unix signals is done by glibc's
 * in-process exception server on the Hurd, not by the kernel.
 *
 * Message shapes (3p/gnumach/kern/exception.c): the kernel sends
 * struct mach_exception with msgh_id 2400 (MACH_EXCEPTION_ID) and
 * msgh_size sizeof(struct mach_exception), about 64 bytes on LP64
 * because the subcode is a full rpc_long_integer_t.  A hand-rolled
 * request struct smaller than that fails the receive with
 * MACH_RCV_TOO_LARGE before any handler code runs, so the receive
 * buffer below is generously sized.  After ipc_kmsg_copyout_header()
 * in 3p/gnumach/ipc/ipc_kmsg.c runs, the reply-port right the kernel
 * created (a send-once right on the faulting thread's RPC reply port)
 * sits in msgh_remote_port and the name of the port the message
 * arrived on sits in msgh_local_port; this is the same received-header
 * reversal that the MiG user-space convention encodes in
 * 3p/mig/server.c (msgh_reply_port == msgh_remote_port).  The handler
 * must therefore reply to req.head.msgh_remote_port.
 *
 * Reply shape: exception_parse_reply() is strict, and a malformed
 * reply comes back as MIG_REPLY_MISMATCH and kills the task.  It
 * accepts exactly mig_reply_header_t (3p/gnumach/include/mach/
 * mig_errors.h) with msgh_id 2500 (MACH_EXCEPTION_REPLY_ID, i.e.
 * request id + 100), msgh_size sizeof(mig_reply_header_t), msgh_bits
 * MACH_MSGH_BITS(MACH_MSG_TYPE_PORT_SEND_ONCE, 0), which is what
 * moving the received send-once right with MACH_MSG_TYPE_MOVE_SEND_ONCE
 * produces, and a RetCodeType descriptor matching exc_RetCode_proto
 * (MACH_MSG_TYPE_INTEGER_32, 32 bits, one element, inline).  The reply
 * is not COMPLEX and carries no body.
 *
 * This exercises the full cross-CPU exception path: task exception port
 * setup, kernel exception message generation and delivery, exception
 * receipt on another CPU, and the exception reply that restarts the
 * faulting thread.  Distinct from ipc_stress / rpc_stress, which only
 * exercise ordinary msg_send / msg_receive, not kernel-generated
 * exception messages.
 *
 * Exits non-zero if the exception port can't be set, the exception
 * message isn't received, the fault thread isn't resumed through the
 * faulting instruction, or more than one exception is delivered.
 */

#include <mach/mach_types.h>
#include <mach/mig_errors.h>
#include <hurd.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/mman.h>

#include "smp_stress.h"

/* msgh_id of the kernel's exception request and of the reply the kernel
   accepts (3p/gnumach/kern/exception.c: MACH_EXCEPTION_ID and
   MACH_EXCEPTION_REPLY_ID). */
#define EXC_MSG_ID 2400
#define EXC_REPLY_ID (EXC_MSG_ID + 100)

/* The kernel's exception message is about 64 bytes on LP64 (struct
   mach_exception in 3p/gnumach/kern/exception.c); receive into a
   generous buffer so MACH_RCV_TOO_LARGE cannot happen. */
#define RCV_BUF_SIZE 512

/* Exception receive and reply timeouts, milliseconds. */
#define EXC_TIMEOUT_MS 10000

/* Backstop: if the handler never replies, the fault thread stays
   suspended inside the kernel exception up-call forever.  SIGALRM's
   default action terminates the process, so a stuck round-trip fails
   the test here instead of hanging until the suite timeout. */
#define TEST_TIMEOUT_SEC 30

static mach_port_t exc_port;
static volatile char *fault_page;
static size_t page_size;
static volatile int exceptions_seen;
static volatile int recovered;
static pthread_barrier_t barrier;
static volatile int failures;

/* The reply return-code descriptor the kernel typechecks against
   exc_RetCode_proto in exception_parse_reply()
   (3p/gnumach/kern/exception.c). */
static const mach_msg_type_t exc_retcode_proto = {
    .msgt_name = MACH_MSG_TYPE_INTEGER_32,
    .msgt_size = 32,
    .msgt_number = 1,
    .msgt_inline = 1,
    .msgt_longform = 0,
    .msgt_deallocate = 0,
    .msgt_unused = 0
};

static void *
handler_fn(void *arg)
{
    (void)arg;
    char rcvbuf[RCV_BUF_SIZE];
    mach_msg_header_t *req = (mach_msg_header_t *)rcvbuf;
    mig_reply_header_t rep;
    kern_return_t kr;
    int seen;

    /* Wait for the faulting thread to install the exception port. */
    pthread_barrier_wait(&barrier);

    /*
     * Receive the kernel's exception message.  It is a COMPLEX message
     * carrying send rights for the faulting thread and its task in the
     * body; after copyout those rights exist in this task's port
     * namespace, but since the task exits right after the test we
     * deliberately do not bother deallocating them.
     */
    memset(rcvbuf, 0, sizeof(rcvbuf));
    req->msgh_local_port = exc_port;
    req->msgh_size = (mach_msg_size_t)sizeof(rcvbuf);

    kr = mach_msg(req, MACH_RCV_MSG | MACH_RCV_TIMEOUT, 0,
                  (mach_msg_size_t)sizeof(rcvbuf), exc_port,
                  EXC_TIMEOUT_MS, MACH_PORT_NULL);
    if (kr != KERN_SUCCESS) {
        fprintf(stderr, "handler: exception receive failed: %d\n", kr);
        __sync_add_and_fetch(&failures, 1);
        return NULL;
    }

    seen = __sync_add_and_fetch(&exceptions_seen, 1);
    if (seen != 1) {
        /*
         * A second exception means the faulting instruction re-faulted
         * after the reply instead of succeeding, i.e. the page fixup
         * was not in effect when the kernel resumed the thread.
         */
        fprintf(stderr, "handler: got %d exceptions, expected 1\n", seen);
        __sync_add_and_fetch(&failures, 1);
        return NULL;
    }

    if (req->msgh_id != EXC_MSG_ID) {
        fprintf(stderr, "handler: exception msgh_id %d, expected %d\n",
                req->msgh_id, EXC_MSG_ID);
        __sync_add_and_fetch(&failures, 1);
        return NULL;
    }

    /*
     * Fix up the fault before resuming the faulting thread: make the
     * page readable.  The kernel keeps thread 0 suspended until this
     * thread sends the reply, so the mprotect is guaranteed to be
     * visible when the faulting instruction re-executes.
     */
    if (mprotect((void *)fault_page, page_size, PROT_READ) != 0) {
        fprintf(stderr, "handler: mprotect failed\n");
        __sync_add_and_fetch(&failures, 1);
        return NULL;
    }

    /*
     * Reply KERN_SUCCESS in the exact mig_reply_header_t shape
     * exception_parse_reply() accepts (see the file comment).  The
     * kernel's send-once reply right was delivered in
     * req->msgh_remote_port (ipc_kmsg_copyout_header() in
     * 3p/gnumach/ipc/ipc_kmsg.c), so move that right back.
     */
    memset(&rep, 0, sizeof(rep));
    rep.Head.msgh_bits = MACH_MSGH_BITS(MACH_MSG_TYPE_MOVE_SEND_ONCE, 0);
    rep.Head.msgh_size = (mach_msg_size_t)sizeof(rep);
    rep.Head.msgh_remote_port = req->msgh_remote_port;
    rep.Head.msgh_local_port = MACH_PORT_NULL;
    rep.Head.msgh_id = EXC_REPLY_ID;
    rep.RetCodeType = exc_retcode_proto;
    rep.RetCode = KERN_SUCCESS;

    kr = mach_msg(&rep.Head, MACH_SEND_MSG | MACH_SEND_TIMEOUT,
                  (mach_msg_size_t)sizeof(rep), 0,
                  MACH_PORT_NULL, EXC_TIMEOUT_MS, MACH_PORT_NULL);
    if (kr != KERN_SUCCESS) {
        fprintf(stderr, "handler: exception reply failed: %d\n", kr);
        __sync_add_and_fetch(&failures, 1);
        return NULL;
    }

    return NULL;
}

static void *
fault_fn(void *arg)
{
    (void)arg;
    volatile char c;

    /* Wait for the handler thread to be ready to receive. */
    pthread_barrier_wait(&barrier);

    /*
     * Deliberately fault: read from the PROT_NONE page.  The kernel
     * raises EXC_BAD_ACCESS, suspends this thread, and sends the
     * exception message; this thread only runs again once the handler
     * has replied, at which point the page is readable and the same
     * load succeeds.
     */
    c = *fault_page;

    /*
     * Reaching this line proves the exception was received, the handler
     * made the page readable, replied KERN_SUCCESS, and the kernel
     * resumed this thread at the faulting instruction
     * (thread_exception_return() in 3p/gnumach/kern/exception.c).
     */
    recovered = 1;
    (void)c;
    return NULL;
}

int
main(void)
{
    pthread_t threads[2];
    kern_return_t kr;
    double start, elapsed;
    int pass;
    int ncpus;
    int i;

    ncpus = smp_check_ncpus();
    if (ncpus < 0)
        return 1;
    if (ncpus < 2) {
        fprintf(stderr, "need >= 2 CPUs for exception_stress\n");
        return 1;
    }

    alarm(TEST_TIMEOUT_SEC);

    /* Create the exception receive port. */
    kr = mach_port_allocate(mach_task_self(), MACH_PORT_RIGHT_RECEIVE,
                            &exc_port);
    if (kr != KERN_SUCCESS) {
        fprintf(stderr, "mach_port_allocate failed: %d\n", kr);
        return 1;
    }
    kr = mach_port_insert_right(mach_task_self(), exc_port, exc_port,
                                MACH_MSG_TYPE_MAKE_SEND);
    if (kr != KERN_SUCCESS) {
        fprintf(stderr, "mach_port_insert_right failed: %d\n", kr);
        return 1;
    }

    /*
     * Make this the task-wide exception port.  This displaces the port
     * glibc installed (_hurdsig_init() in 3p/glibc/hurd/hurdsig.c
     * points the task exception ports at _hurd_msgport), so for the
     * duration of the test the task has no in-process exception-to-
     * signal translation: the raw kernel protocol above is what the
     * handler thread must speak.
     */
    kr = task_set_exception_port(mach_task_self(), exc_port);
    if (kr != KERN_SUCCESS) {
        fprintf(stderr, "task_set_exception_port failed: %d\n", kr);
        return 1;
    }

    /* One PROT_NONE page to fault on. */
    page_size = (size_t)sysconf(_SC_PAGESIZE);
    fault_page = mmap(NULL, page_size, PROT_NONE,
                      MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (fault_page == MAP_FAILED) {
        fprintf(stderr, "mmap failed\n");
        return 1;
    }

    if (pthread_barrier_init(&barrier, NULL, 2) != 0) {
        fprintf(stderr, "pthread_barrier_init failed\n");
        return 1;
    }

    /* Thread 0 = faulting thread, thread 1 = exception handler. */
    if (pthread_create(&threads[0], NULL, fault_fn, NULL) != 0) {
        fprintf(stderr, "failed to create fault thread\n");
        return 1;
    }
    if (pthread_create(&threads[1], NULL, handler_fn, NULL) != 0) {
        fprintf(stderr, "failed to create handler thread\n");
        return 1;
    }

    start = smp_now_sec();
    for (i = 0; i < 2; i++)
        if (pthread_join(threads[i], NULL) != 0) {
            fprintf(stderr, "failed to join thread %d\n", i);
            return 1;
        }
    elapsed = smp_now_sec() - start;

    pass = failures == 0 && exceptions_seen == 1 && recovered;
    smp_print_results("exception_stress", pass,
                      elapsed * 1000.0,
                      elapsed > 0.0 ? (double)exceptions_seen / elapsed : 0.0,
                      "exceptions/sec");

    munmap((void *)fault_page, page_size);
    mach_port_deallocate(mach_task_self(), exc_port);
    pthread_barrier_destroy(&barrier);
    return pass ? 0 : 1;
}
