# Driver / PCI / IRQ debugging skill (GNU/Hurd guests under QEMU)

Debugging device-driver translators in this repo (virtio-9p today, any PCI
device tomorrow), above all interrupt-delivery bugs on shared PCI lines,
including when the normal SSH control channel is already dead.

## Control channels, in order of trust

1. **QEMU serial log** (`img/tmp/virtio-test-qemu.log`, `$QEMU_LOG`): driver
   `fprintf(stderr, ...)` and kernel prints always land here, even when SSH is
   dead. The harness truncates it per boot and keeps it on failure. This is
   the printf of last resort: put diagnostics in stderr, not stdout.
2. **SSH** (`scripts/virtio_test.py` `GuestSession`): first thing to die.
   Distinguish the failure modes: `Timed out after Ns while running: CMD` =
   the session is alive but the guest is starved; `Connection lost while
   running: CMD` = the network (e1000) is dead.
3. **Host `/proc` on the QEMU pid**: names the burning thread.
   `/proc/<qemu-pid>/task/*/stat` fields 14+15 (utime+stime), sampled twice a
   few seconds apart. `CPU 0/KVM` growing = guest vCPU 0 spinning (guest-side
   bug); `qemu-system` main thread or `IOThread` = host-side device model.
   `top -H` thread-name greps are unreliable here; use `/proc/<pid>/task`.

## Reaching the guest when SSH is dead

- For manual debug boots, do not redirect serial to a file: keep `-nographic`
  on your terminal (you can log in on the console) or add
  `-monitor unix:/tmp/mon,server,nowait` for the QEMU monitor.
- Before reproducing, arm a guest-side watchdog that needs no SSH to be read:
  `nohup sh -c 'while :; do date +%s; ps -A -o pid,time,comm; echo ---; done >>
  /dev/console' >/dev/null 2>&1 </dev/null &`. It must be fully detached
  (all fds away from the pty) or the SSH session never exits.
- In-guest gdb (base image already has it):
  `gdb /hurd/<bin> -p <pid> -batch -ex 'thread apply all bt'` — pass the
  binary or the unwinding is garbage. `GDB=1 make virtio-test` makes the
  harness dump stacks of stuck `virtio-9p`/`9pfs` processes automatically
  (needs working networking, so it fails late in a storm).

## Shared-level-IRQ model (the shape of every bug here)

- On the default `pc` machine e1000 and virtio-9p share IRQ 11, level
  triggered. gnumach masks the line on fire (`queue_intr` → `__disable_irq`)
  and unmasks only via `irq_acknowledge` → `__enable_irq`. `irqhelp` acks
  **unconditionally after every handler call**, so a handler must answer
  "is this mine?" by reading its own device ISR and return untouched when it
  is not; the driver must never call `device_intr_ack`/`__enable_irq` itself.
- QEMU legacy virtio-pci: INTA asserts on used-buffer push, deasserts on ISR
  read. ISR bit 0 = queue completion, 0x3 = config change, so check `& 1` and
  let the waiter re-check `used.idx` instead of trusting the signal.
- Kernel serial-log markers: `new delivery port ... for <task>` (registered),
  `release a dead delivery port` + `still N unacked irqs in entry` (a driver
  exited between fire and ack; the kernel compensates with one `__enable_irq`
  per unacked). Benign in isolation; count them during churn. The
  `new delivery port` lines double as a **process bring-up counter**: exactly
  one per pid is the invariant, because `irqhelp_init` is once-per-process and
  each `virtio_irq_install` registers one port. Two lines for one pid = the
  process brought the device up twice, i.e. main fell through from a one-shot
  mode into the daemon loop (see the virtio-blk lesson below). Reused-run logs
  also contain NUL holes (the harness truncates the log while QEMU keeps its
  fd and writes at the old offset): grep with `-a`, pipe through
  `tr -d '\000'`, and remember grep line numbers include the hole.
- The ISR read is read-to-clear: two handlers for the same virtio device (a
  selftest next to a live translator) steal each other's interrupt and one
  waiter times out. One owner per device at a time.
- Storm signature: one vCPU pegged + SSH session alive but stalled + netdde
  CPU climbing + no `v9fs_*` traces in the serial log.

## Repro loops

- End-to-end until it wedges: `for i in $(seq 1 N); do GDB=1 make virtio-test
  || break; done` (a full run is ~80 s; the wedge is intermittent, expect
  ~1-in-3).
- Add/remove churn (registration/teardown path): `make virtio-irq-stress`.
- Manual single shot: boot, install binaries, `settrans -a /dev/virtio-9p0
  /hurd/virtio-9p --mount-tag=hostshare`, watch serial + netdde CPU.

## Live forensics on a wedged run (QEMU side)

The harness passes `-monitor unix:img/tmp/virtio-test-monitor.sock` to
every boot, so a wedge is dissectable without SSH. Work the scene in this
order:

1. `stop` via the monitor (freeze the VM) before any gdb attach; `cont`
   only deliberately. Talk to the socket with python (`socat`/`ncat` are
   not installed); strip readline echo from replies.
2. `info ioapic` — **the signature of the stalled-shared-IRQ wedge**: the
   pin is `masked` in its redir entry AND its number appears on the
   `IRR` line (an interrupt fired but was never delivered). Both devices
   on the line starve from that moment.
3. gdb on the QEMU pid (host build has DWARF):
   `set $dev=(V9fsVirtioState*)object_resolve_path_type("", "virtio-9p-device", 0)`
   (same pattern for `"e1000"`). `status`/`isr`/`vq[0].last_avail_idx`
   separate "guest never submitted" from "QEMU never replied". Gotchas:
   `E1000State->mac_reg[]` is indexed by register offset >> 2 (ICR =
   `mac_reg[0xc0>>2]`, IMS = `mac_reg[0xd0>>2]`); `vring.avail`/`used`
   are plain `hwaddr` GPAs — read guest RAM with the monitor's
   `xp /Nwx <gpa>` (e.g. `avail+2` is the live avail.idx), not by
   dereferencing them.
4. QEMU IRQ tracing for the mask/unmask story:
   `QTRACE='ioapic_*' make virtio-irq-stress` logs every device assert
   (`ioapic_set_irq`) and every guest mask/unmask write
   (`ioapic_mem_write`) to img/tmp/qtrace.log. **Requires
   `QEMU_EXTRA='-machine kernel_irqchip=split'`**: with the default
   in-kernel irqchip the guest IOAPIC belongs to KVM and the trace log
   stays 0 bytes. Split also slows IRQ delivery: boots may need
   `SSH_PROBE_ATTEMPTS=400`, and one split boot showed a self-healing
   wd0 lost-interrupt (IDE serves its IRQ through the same irqhelp
   path) — treat isolated soft-corrected ATA timeouts as latency
   artifacts, not media failure.
5. Reading files out of the qcow2: qemu-nbd + nbdfuse + debugfs with the
   `file?offset=<partition-2-byte-offset>` syntax (offset 1000341504 for
   this image). The daemons must be backgrounded with polling loops (the
   pattern in scripts/repair_filesystem.sh); nbdfuse never exits in the
   foreground. The image's `/boot/gnumach-1.8-amd64-up.gz` is stripped —
   no kernel symbols — but the kernel prints live kernel VAs of its own
   structures into the serial log, and guest-physical = VA -
   0xffffffff80000000 (`KERNEL_MAP_BASE` in the gnumach x86_64 build).

## The shared-IRQ-line wedge (open issue)

Under add/remove churn with virtio-9p sharing IRQ 11 with the e1000,
gnumach intermittently leaves the IOAPIC pin masked with IRR latched and
never unmasks: the whole line dies (netdde and the translator both
starve, arbitrary victim point — first exchange or teardown). The QEMU
threads stay healthy and the vCPUs idle; the guest freezes blocked. Not a
driver bug: putting virtio-9p on its own line (slot ≡ 1 mod 4 → PIRQ A →
IRQ 10, harness default `VIRTIO_ADDR=0x5`) makes it disappear 4/4, and
`VIRTIO_ADDR=` reproduces it. Kernel-side mechanism identified
2026-11-15: gnumach's nested `nested_irqs[].ndisabled` counters
(`i386/i386/irq.c:43-92`) keep a shared pin masked until **every**
alive entry on the line acks, so one lost/unacked notification (wedged
driver thread, `deliver_intr` `ikm_alloc` failure) starves the whole
line with no recovery sweep for alive entries; the ioapic RMW itself is
lock-protected (`ioapic_lock`), and this QEMU's IOAPIC v0x20 uses the
specific-EOI path (no mask+edge workaround). An `ioapic_*` qtrace of a
wedged run (split irqchip) showing the last `ioapic_set_irq` followed
by mask=1 and no unmask write is the confirmation to capture.

## Landmines learned the hard way

- pexpect output carries ANSI/bracketed-paste escapes; strip before string
  compares (harness `clean()`).
- `pkill`/`pgrep` patterns for QEMU are unreliable; the harness uses a pidfile
  (`img/tmp/virtio-test-qemu.pid`) plus `/proc/$PID/comm` verification.
  `/bin/kill -9` (the shell builtin `kill` is unsupported in this context).
- `settrans -c` is passive-only; `-a` starts actively. Removal form is
  `settrans -c NODE ''` (prints ERANGE, rc=5, but works).
- glibc port I/O is `outb(value, port)`; swapped args SIGILL the driver.
- `vm_allocate_contiguous` fragments under repeated 4 MiB requests; a fresh
  boot fixes it.
- A `/dev/tcp` boot probe races sshd; probe with a real SSH login instead.
- Working notes and evidence captures live in untracked scratch
  directories; do not rely on them being in git.
- The driver's 30 s `ETIMEDOUT` prints `virtio-9p: device did not reply`; if
  a hung run never prints it, the hang is before the condvar wait, not in it.
- When a bash boot script and the python core share an implicit contract
  (REUSE=1 promises "guest left running"), move the contract into the
  environment the core can read: the .sh exports REUSE and the shared
  `finish()` skips its poweroff-on-success under REUSE=1. A silent violation
  surfaces as the *next* REUSE run failing with "no guest answers on port".
- Device-model evidence for blk datapath assertions comes from QEMU trace
  events (`virtio_blk_handle_read`/`_write` with sector+nsectors,
  `virtio_blk_rw_complete` with ret, `virtio_blk_req_complete` with status),
  enabled via `VT_TRACE="virtio_blk*"`; the same pattern as the 9p
  `v9fs_*` traces, grep the qemu log with `-a`.

## One-shot modes in a daemon-default driver (virtio-blk harness lesson)

A CLI binary whose default mode is a daemon (`virtio-blk`, and the planned
shape of most drivers here) needs an explicit `return 0` from `main` after
**every** one-shot mode (`--status`, `--selftest`, `--read-range`). A missing
return falls through into the daemon loop: the process stays alive forever,
its second bring-up shows as a second `new delivery port` line for the same
pid, and any pipeline consuming its stdout (`--read-range | sha256sum`)
blocks forever on EOF -- a harness timeout with no driver-side error and no
`device did not reply`, because the driver is not stuck, it is *idling by
design*. The give-away: the daemon's exit-stats line (`reads N (M bytes)...`)
appearing in output of a run that was only supposed to do a one-shot read.
When timing guest one-shots through a pipe, remember `cmd | sha256sum;
echo $?` reports the *last* stage's rc -- a hung producer looks like a
successful pipeline that simply never finishes; wrap in `time (...)` and
treat tens of seconds for a 64 KiB read as the bug, not the digest.
Note also that a harness session timeout (`Timed out after Ns`) does not
kill the remote process; the stray exits later on its own (or on the guest
side only if something signals it), so post-mortem IRQ-log timelines need
the timeout moment marked before correlating with `release a dead delivery
port` lines.

## Guest-visible process table gotchas (balloon harness lessons)

On this Hurd guest, plain `ps` (no args) prints **nothing** and exits 1 for
every process whose TTY column is "-" — which is all detached daemons
(`setsid`'d, no controlling tty), e.g. `hurd/virtio-balloon`. `ps | grep
<name>` therefore looks like "process dead" while it is alive, and
`ps | awk ... {print $1}` yields an empty PID field anyway ($1 is empty on
a blank line). A harness that detects or kills daemons must use `ps -ef` (or
`ps -A`) and read the PID from column 2 of the matching COMMAND line:
`ps -ef | awk '/hurd\/<name>/ && !/awk/ {print $2}'`. This silently-empty
`ps` behavior also explains daemon-detection false alarms ("daemon exited
during bring-up") when the daemons were in fact alive and accumulating as
strays; clean strays with `kill -TERM <pid>` before re-running.

## Translator stderr under settrans-detached (virtio-blk Stage C lesson)

A translator's stderr does **not** reach the QEMU serial log when started
via `settrans -c` detached. The harness saw `virtio-blk --serve` publish
and answer RPCs, but nothing from its init or handler paths appeared in
the serial log. Capture it with a wrapper script passed to settrans:

    #!/bin/sh
    exec /hurd/virtio-blk --serve -v 2>>/tmp/serve.log

This exposed the per-request server trace (`demux id`, `open`, `read
recnum ...`) and made the difference between "client-side EIO" and
"server-side silence" visible.

## Storeio --no-cache byte-to-block sign error (virtio-blk Stage C lesson)

The first storeio read through a translator succeeded; every subsequent
read failed instantly with EIO without reaching the server. Root cause:
`hurd/storeio/dev.c` converted file offsets to device blocks with
`offs << log2_block_size` (multiplying) instead of `offs >> log2_block_size`
(dividing) in the `--no-cache` direct read/write paths. Block 0 worked by
accident (`0 << n == 0 >> n == 0`); block N landed `N * block_size` sectors
past the device end, which `libstore/device.c` mapped to `D_INVALID_RECNUM`
→ `dev_error` → `EIO`. Every other offset conversion in `dev.c` used `>>`;
this was a lone sign error in the no-cache fast paths (read line 484,
write line 388). Fix committed on 3p/hurd branch
`fix/storeio-no-cache-block-shift`. The harness now omits `-c` from the
storeio spec so the buffered path (which divides correctly) is exercised.

## mig defs file argument (virtio-blk Stage C lesson)

`mig` needs the `.defs` file passed as an argument, not via stdin redirect:
a redirect silently produces no output. Use `mig ... X.sdefsi` (file as
argument), not `mig ... < X.sdefsi`.

## Dual-translator ISR stealing (virtio-blk Stage C lesson)

Two translators serving the same virtio-blk device steal each other's
read-to-clear ISR; one waiter always times out. A 30 s "timeout" observed
during early Stage C testing was this, not a server bug — one translator
per device at a time.

## Reuse-mode vCPU livelock (open issue)

Repeatedly booting the balloon harness against the same live guest
(`REUSE=1`) can leave QEMU's `CPU 0/KVM` thread pegged at ~100% while SSH
dies and the monitor answers: guest-side livelock with no serial-log
progress, lasting minutes. `info balloon` on the monitor still answers
(with a growing `actual` MB). The first stock-kernel run reached phase-3
churn fine; the wedge appeared on the second reuse run, suggesting the
shared-IRQ strandings the hurdkit branch fixes (ioapic pin left masked
with IRR latched) rather than a deterministic driver bug — but the driver's
teardown path (deflate-all then uninstall handler) must still be exercised
on the hurdkit kernel before declaring the livelock kernel-side. If the
wedge reproduces on the hurdkit-branch kernel, the livelock is
balloon-driver-adjacent (the `virtio_irq_wait_config`/`vballoon_converge`
interplay under churn) and needs driver-side investigation; if it does not,
the shared-IRQ fix is the resolution and the driver teardown stands as
audited. Kill the wedged QEMU (`kill -9` on the pidfile pid), remove the
pidfile, and start fresh — a stale overlay in `img/tmp/` is harmless
(`make clean` removes it).
