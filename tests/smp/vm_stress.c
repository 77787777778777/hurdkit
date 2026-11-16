/* vm_stress.c — stress VM map operations across CPUs.
 *
 * Spawns one thread per online CPU.  Each thread repeatedly allocates and
 * deallocates pages of virtual memory.  Allocations and deallocations on
 * one CPU trigger pmap_update IPIs to the other CPUs (`CALL_PMAP_UPDATE`),
 * exercising the cross-CPU TLB shootdown path that plain atomics and Mach
 * IPC do not touch.
 *
 * Exits non-zero if any VM operation fails or any thread fails to run.
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
#define ITERATIONS 1000
#define MAP_SIZE 4096   /* one page */

typedef natural_t *processor_name_array_t;

extern kern_return_t vm_allocate_rpc (mach_port_t, uintptr_t *, uintptr_t,
                                      int);
extern kern_return_t vm_deallocate_rpc (mach_port_t, uintptr_t, uintptr_t);

static int ncpus;
static long my_allocs[MAX_CPUS];   /* successful allocations per thread */
static int thread_seen[MAX_CPUS];
static pthread_barrier_t barrier;
static volatile int failures;

static void *
thread_fn (void *arg)
{
  int idx = *(int *)arg;
  mach_port_t map = mach_task_self ();
  vm_offset_t addr;

  /* Synchronize start so all threads allocate concurrently. */
  pthread_barrier_wait (&barrier);

  /* Mark this thread as having run. */
  thread_seen[idx] = 1;

  for (long i = 0; i < ITERATIONS; i++)
    {
      kern_return_t kr = vm_allocate_rpc (map, &addr, MAP_SIZE, TRUE);
      if (kr != KERN_SUCCESS)
        {
          fprintf (stderr, "thread %d iter %ld: vm_allocate_rpc failed: %d\n",
                   idx, i, kr);
          __sync_add_and_fetch (&failures, 1);
          break;
        }

      /* Touch the page so it's actually backed (faults in). */
      memset ((void *)(uintptr_t)addr, 0, MAP_SIZE);

      kr = vm_deallocate_rpc (map, addr, MAP_SIZE);
      if (kr != KERN_SUCCESS)
        {
          fprintf (stderr, "thread %d iter %ld: vm_deallocate_rpc failed: %d\n",
                   idx, i, kr);
          __sync_add_and_fetch (&failures, 1);
          break;
        }

      my_allocs[idx]++;
    }

  return NULL;
}

int
main (int argc, char **argv)
{
  pthread_t threads[MAX_CPUS];
  int args[MAX_CPUS];
  void *work_args[MAX_CPUS];
  int iterations = smp_parse_iterations (argc, argv, ITERATIONS);
  int i;
  long total_allocs = 0;
  double elapsed, ops_per_sec;

  ncpus = smp_check_ncpus ();
  if (ncpus < 0)
    return 1;

  if (pthread_barrier_init (&barrier, NULL, ncpus) != 0)
    {
      fprintf (stderr, "pthread_barrier_init failed\n");
      return 1;
    }

  for (i = 0; i < ncpus; i++)
    my_allocs[i] = 0;

  for (i = 0; i < ncpus; i++)
    {
      args[i] = i;
      work_args[i] = &args[i];
    }

  /* smp_run_threads creates the threads, joins them, and returns the
     elapsed seconds of the join window. */
  elapsed = smp_run_threads (ncpus, thread_fn, work_args, NULL);
  if (elapsed < 0.0)
    {
      pthread_barrier_destroy (&barrier);
      return 1;
    }

  for (i = 0; i < ncpus; i++)
    total_allocs += my_allocs[i];
  ops_per_sec = elapsed > 0.0 ? (double)total_allocs / elapsed : 0.0;

  for (i = 0; i < ncpus; i++)
    {
      if (!thread_seen[i])
        {
          fprintf (stderr, "thread %d never ran\n", i);
          failures = 1;
        }
      if (my_allocs[i] != ITERATIONS)
        {
          fprintf (stderr, "thread %d did %ld allocs, expected %d\n",
                   i, my_allocs[i], ITERATIONS);
          failures = 1;
        }
    }

  smp_print_results ("vm_stress", failures == 0,
                     elapsed * 1000.0, ops_per_sec, "ops/sec");

  pthread_barrier_destroy (&barrier);
  return failures;
}
