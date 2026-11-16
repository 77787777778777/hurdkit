/* pset_stress.c — stress test for Mach processor sets on SMP gnumach.
 *
 * Exercises the SMP code paths that are compiled out on a uniprocessor
 * kernel (MACH_HOST=0, NCPUS=1):
 *
 *   host_processor_sets()   — must return >= 2 psets (default + slave)
 *   task_assign()           — ping-pong a task between default and slave pset
 *
 * Usage: pset_stress [iterations]
 *
 * Prints one line per iteration: iteration number, pset count, and the
 * result of each operation. Exits non-zero on any failure.
 *
 * The Mach traps are forward-declared instead of including <mach/mach_host.h>
 * because the guest's system header references processor_name_array_t, a type
 * that is not defined in this glibc.
 */

#include <mach/mach_types.h>
/* The guest's <mach/mach_host.h> (pulled in via <hurd.h>) references
   processor_name_array_t, which this glibc never defines. Define it here
   so <hurd.h> can compile. */
typedef natural_t *processor_name_array_t;
#include <hurd.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <error.h>

#include "smp_stress.h"

#define MAX_PSETS 16

extern kern_return_t host_processor_sets (mach_port_t,
                                          processor_set_name_array_t *,
                                          mach_msg_type_number_t *);
extern kern_return_t host_processor_set_priv (mach_port_t,
                                              processor_set_name_t,
                                              mach_port_t *);
extern kern_return_t task_assign (mach_port_t, processor_set_t, boolean_t);

static int
pset_pingpong (mach_port_t hostpriv, processor_set_t default_pset,
               processor_set_t slave_pset)
{
  kern_return_t kr;
  mach_port_t default_priv = MACH_PORT_NULL;
  mach_port_t slave_priv = MACH_PORT_NULL;

  kr = host_processor_set_priv (hostpriv, default_pset, &default_priv);
  if (kr != KERN_SUCCESS)
    {
      error (1, kr, "host_processor_set_priv(default)");
      return -1;
    }

  kr = host_processor_set_priv (hostpriv, slave_pset, &slave_priv);
  if (kr != KERN_SUCCESS)
    {
      mach_port_deallocate (mach_task_self (), default_priv);
      error (1, kr, "host_processor_set_priv(slave)");
      return -1;
    }

  /* Assign self to the slave pset. */
  kr = task_assign (mach_task_self (), slave_priv, FALSE);
  if (kr != KERN_SUCCESS)
    {
      mach_port_deallocate (mach_task_self (), default_priv);
      mach_port_deallocate (mach_task_self (), slave_priv);
      error (1, kr, "task_assign(slave)");
      return -1;
    }

  /* Assign self back to the default pset. */
  kr = task_assign (mach_task_self (), default_priv, FALSE);
  if (kr != KERN_SUCCESS)
    {
      mach_port_deallocate (mach_task_self (), default_priv);
      mach_port_deallocate (mach_task_self (), slave_priv);
      error (1, kr, "task_assign(default)");
      return -1;
    }

  mach_port_deallocate (mach_task_self (), default_priv);
  mach_port_deallocate (mach_task_self (), slave_priv);
  return 0;
}

int
main (int argc, char **argv)
{
  mach_port_t hostpriv;
  processor_set_name_array_t psets = {0};
  mach_msg_type_number_t pset_count = 0;
  kern_return_t kr;
  int iterations = 1;
  int i;

  if (argc > 1)
    iterations = atoi (argv[1]);

  kr = get_privileged_ports (&hostpriv, NULL);
  if (kr != KERN_SUCCESS)
    error (1, kr, "get_privileged_ports: are you root?");

  struct timespec ts_start, ts_end;
  clock_gettime (CLOCK_MONOTONIC, &ts_start);

  for (i = 0; i < iterations; i++)
    {
      kr = host_processor_sets (hostpriv, &psets, &pset_count);
      if (kr != KERN_SUCCESS)
        error (1, kr, "iteration %d: host_processor_sets", i);

      if (pset_count < 2)
        error (1, ENOSYS, "iteration %d: only %u pset(s), need >= 2 (SMP?)",
               i, pset_count);

      if (pset_pingpong (hostpriv, psets[0], psets[1]) != 0)
        error (1, 0, "iteration %d: pset ping-pong failed", i);

      printf ("iter=%d psets=%u ok\n", i, pset_count);
      fflush (stdout);

      for (unsigned j = 0; j < pset_count; j++)
        mach_port_deallocate (mach_task_self (), psets[j]);
    }

  clock_gettime (CLOCK_MONOTONIC, &ts_end);
  double elapsed = (double)(ts_end.tv_sec - ts_start.tv_sec)
    + (double)(ts_end.tv_nsec - ts_start.tv_nsec) / 1e9;
  double iters_per_sec = elapsed > 0.0 ? (double)iterations / elapsed : 0.0;

  mach_port_deallocate (mach_task_self (), hostpriv);
  if (smp_json_mode ())
    {
      printf ("{\"phase\":\"pset_stress\",\"passed\":true,\"iterations\":%d,\"elapsed_ms\":%.3f,\"rate\":%.0f,\"unit\":\"iters/sec\"}\n",
              iterations, elapsed * 1000.0, iters_per_sec);
    }
  else
    {
      printf ("pset_stress: %d iterations, %.3f s, %.0f iters/sec\n",
              iterations, elapsed, iters_per_sec);
    }
  return 0;
}
