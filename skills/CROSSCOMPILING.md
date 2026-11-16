# Skill: Cross-Compile for GNU/Hurd (gnumach as the worked example)

## Trigger

Use this skill when you need to build Hurd code for the guest on the Linux
host with the `x86_64-gnu` cross toolchain, in particular:

- building the **gnumach kernel binary** itself (for a custom-kernel VM
  image, `CUSTOM_KERNEL`), not just its headers,
- building any target program or driver by hand instead of through a
  `toolchain/Makefile` target,
- inspecting target artifacts (kernel, driver objects) with `nm`,
  `objdump`, `readelf` — which are **not** the host tools,
- diagnosing a cross build that produced objects a host compiler wrote.

For building *user-space drivers* through the toolchain and testing them in
the VM, see `skills/CROSS_TESTING.md`; for the toolchain's own stage list
see `toolchain/README.md`. This skill covers the build mechanics, the pin
system, the cross-binutils inspection workflow, and the kernel-image flow.

## Inputs

- `toolchain/Makefile` — the authority for every cross build. Targets:
  `3p-pin`, `binutils`, `gcc1`, `gnumach` (headers only), **`gnumach-kernel`**
  (kernel binary), `mig`, `glibc`, `gcc2`, `hurd`, `9pfs`, `virtio-9p`,
  `virtio-balloon`.
- `toolchain/pins.mk` — the 3p source URLs and pins, shared by the
  top-level Makefile (`fetch-3p`) and the toolchain.
- `$HOME/opt/x86_64-gnu/` — the install prefix: `bin/` (`x86_64-gnu-gcc`,
  `x86_64-gnu-nm`, `x86_64-gnu-objdump`, `x86_64-gnu-readelf`, `mig`, ...),
  `lib/`, `include/`, and the `x86_64-gnu/` tooldir symlinks.
- `$HOME/work/x86_64-gnu-build/` — out-of-tree build dirs (one per build:
  `glibc`, `mig`, `gnumach-kernel`, ...). Disposable by design.
- `3p/` — the pinned source checkouts. Read-only for builds: builds copy a
  checkout into the build dir first (autoreconf writes into the tree).
- `scripts/provision_stage.sh` + `scripts/provision_guest.py` — the kernel
  stage that ships a custom kernel into a VM image.

## The one rule

**Never run a compiler or `make` inside a configured target build dir by
hand.** A configure run records whatever `CC` it found — without the cross
tools on `PATH` it records the host `CC = gcc` into the generated Makefile,
and every object is then built by the wrong compiler. The results range
from a failed `-mcmodel=kernel` compile to *silently broken objects* (for
example an empty symbol table) that only fail later, at link time or in
the guest. A build dir that was ever touched this way is not repairable in
place: wipe it and reconfigure. Every toolchain target follows the safe
pattern; reuse it (or a toolchain target) instead of hand-driving a build.

## Procedure

### Step 1 — Pin the 3p sources

```sh
make -C toolchain 3p-pin
```

Pins live in `toolchain/pins.mk` in two forms:

- `repo:<sha>` — the repo is put on a detached HEAD at that commit.
- `repo:@branch` — the repo checks out the named *local branch* with
  tracking set to `origin/<branch>`; a later `3p-pin` run fetches and
  fast-forwards it, so an upstream push moves the build forward with it.

gnumach is pinned to `gnumach:@hurdkit`: that branch carries the shared-IRQ
fixes (recovered stranded shared-IRQ lines, masked-across-EOI while
disables are held) that the driver test matrices depend on. `3p-pin`
refuses to run when a repo has uncommitted changes, and never clobbers an
existing checkout — it only moves HEAD.

Gate: `git -C 3p/gnumach log --oneline -1` shows the expected branch tip
(`a6c33206` at the time of writing) and a clean `git status`.

### Step 2 — Build the kernel binary with `gnumach-kernel`

```sh
make gnumach-kernel        # top-level alias, or: make -C toolchain gnumach-kernel
```

The target copies the pinned `3p/gnumach` into
`$HOME/work/x86_64-gnu-build/gnumach-kernel-src`, runs `autoreconf -i`
there, configures out-of-tree in `$HOME/work/x86_64-gnu-build/gnumach-kernel`
and builds. The two critical overrides go to **configure**, not to make:

```sh
PATH=$HOME/opt/x86_64-gnu/bin:$PATH \
CC=$HOME/opt/x86_64-gnu/bin/x86_64-gnu-gcc \
MIG=$HOME/opt/x86_64-gnu/bin/mig \
/home/chz/work/x86_64-gnu-build/gnumach-kernel-src/configure \
    --prefix=$HOME/opt/x86_64-gnu --host=x86_64-gnu --build=x86_64-pc-linux-gnu
PATH=$HOME/opt/x86_64-gnu/bin:$PATH make -j$(nproc)
```

Why configure and not `make CC=...`: configure *checks* the compiler it
selects, so a wrong pick fails immediately with a real error; the
generated Makefile then carries `CC = .../x86_64-gnu-gcc` and plain `make`
is safe. Overriding CC only on the make command line works but leaves the
landmine in the Makefile — the next bare `make` in that dir compiles with
the host compiler again. `MIG` must point at the prefix `mig` (a host
tool); configure otherwise only warns and records `MIG=mig`, which
resolves to nothing outside the prefix PATH.

The target's gates (all must pass):

- the build Makefile records `CC = $HOME/opt/x86_64-gnu/bin/x86_64-gnu-gcc`,
- `file .../gnumach/gnumach` reports `ELF 64-bit ... statically linked`,
- `x86_64-gnu-nm .../gnumach | grep -q irq_disable_count_held` — the
  shared-IRQ fix symbol is in the kernel.

Output: `$HOME/work/x86_64-gnu-build/gnumach-kernel/gnumach`.

### Step 3 — Inspect with the cross binutils

The host's `nm`/`objdump`/`readelf` can often *read* a cross object, but
they are not trustworthy ground truth and their output mismatches target
ABIs. Always use the prefix tools; `file` is fine for a first look.

```sh
XBIN=$HOME/opt/x86_64-gnu/bin
KERNEL=$HOME/work/x86_64-gnu-build/gnumach-kernel/gnumach

file $KERNEL                       # ELF 64-bit, statically linked, x86-64
$XBIN/x86_64-gnu-nm $KERNEL        # symbol table (use -u for undefined only)
$XBIN/x86_64-gnu-nm $KERNEL | grep irq_disable_count_held
$XBIN/x86_64-gnu-objdump -d $KERNEL | less     # disassembly
$XBIN/x86_64-gnu-objdump -h $KERNEL            # section headers/sizes
$XBIN/x86_64-gnu-readelf -h $KERNEL            # ELF header (class, machine, entry)
$XBIN/x86_64-gnu-readelf -d /path/to/driver    # NEEDED libraries of a driver
$XBIN/x86_64-gnu-readelf -n $KERNEL            # notes (e.g. GNU-stack warnings)
```

Typical uses during kernel/driver debugging:

- **Is the fix in the binary?** `nm | grep <symbol>` on the final linked
  kernel (or on the object: `x86_64-gnu-nm $BUILD/i386/i386/irq.o`).
- **Why does a symbol not resolve at link time?** `nm -u gnumach.o` lists
  undefined symbols; compare against `nm` of the archive members.
- **Which code did the compiler actually emit?** `objdump -d` around a
  symbol (`objdump -d --disassemble=<symbol>` when supported).
- **Is an object broken or just unusual?** A healthy object shows a
  symbol table with `nm`; an empty result from the *cross* `nm` means the
  object really is broken (typically because a host compiler wrote it —
  see the pitfall table).

### Step 4 — Ship the kernel as a staged VM image

The image pipeline is `base -> accounts -> packages -> kernel`, one qcow2
per stage. The kernel stage overlays the *packages* image (it has the SSH
accounts/keys) and is rebuilt automatically when the kernel binary
changes (the binary is a make prerequisite of the image):

```sh
make kernel CUSTOM_KERNEL=$HOME/work/x86_64-gnu-build/gnumach-kernel/gnumach
# explicit equivalent:
make stage STAGE=kernel VARIANT=img/hurd.kernel.qcow2 \
    CUSTOM_KERNEL=$HOME/work/x86_64-gnu-build/gnumach-kernel/gnumach
```

What the kernel stage does: it boots the packages image in a throwaway
overlay, gzips the kernel binary and extracts an overlay tar over `/` so
the kernel lands at `/boot/gnumach-1.8-amd64-up.gz` (GRUB decompresses
gzip transparently; the multiboot line stays
`multiboot /boot/gnumach-1.8-amd64-up.gz root=part:2:device:wd0
console=com0`), validates it with `gzip -t` in the guest, repairs the
filesystem, and promotes the overlay to `img/hurd.kernel.qcow2`.

Constraints:

- `CUSTOM_KERNEL` and a kernel file inside `CUSTOM_OVERLAY` (at the boot
  path) are mutually exclusive; the provisioning rejects the ambiguity.
- Do not put the kernel binary in the `CUSTOM_OVERLAY` directory at
  `/boot/gnumach-1.8-amd64-up.gz` yourself — let the stage pack it.

### Step 5 — Run a harness against the custom-kernel image

Test harnesses take the image through `TEST_IMG` (their `BASE_IMG` is the
absolute path of it); the balloon harness example:

```sh
make virtio-balloon-test VIRTIO_DIR=$HOME/src/virtio-1 \
    TEST_IMG=$(pwd)/img/hurd.kernel.qcow2
```

The guest now runs the pinned hurdkit-branch kernel; a good first check
inside the guest is that the serial log shows the boot kernel and that
`/boot/gnumach-1.8-amd64-up.gz` in the image passes `gzip -t`.

## Verification table

| Check | Command | Expected |
|---|---|---|
| Pin applied | `git -C 3p/gnumach log --oneline -1` | branch tip of the pinned branch |
| Cross CC recorded | `grep '^CC = ' $BUILD/gnumach-kernel/Makefile` | the prefix `x86_64-gnu-gcc` path |
| Kernel ELF | `file $BUILD/gnumach-kernel/gnumach` | `ELF 64-bit ... statically linked` |
| Fix present | `x86_64-gnu-nm .../gnumach \| grep irq_disable_count_held` | one or more hits |
| Kernel image | `qemu-img info img/hurd.kernel.qcow2` | backing file: `img/hurd.packages.qcow2` |
| Guest runs it | boot + serial log | kernel banner from the custom kernel |

## Constraints

- The toolchain `Makefile` targets are the authority; if a build needs a
  new knob, add it to a target instead of hand-driving configure/make.
- Build dirs under `$HOME/work/x86_64-gnu-build/` are disposable; a
  suspicious state is fixed by `rm -rf` + re-run, never by patching
  objects in place.
- Builds never run inside `3p/` (autoreconf and configure write into the
  source tree; the checkouts are shared and pinned).
- Host libraries must not leak into target artifacts (see the zlib note
  on the `libpciaccess` target for why).

## Pitfalls

| Symptom | Likely cause | Fix |
|---|---|---|
| `CC = gcc` in the target build Makefile | configure ran without the cross tools in `PATH` (or without `CC=`) | wipe the build dir, reconfigure with `CC`/`PATH` set as in Step 2 |
| `error: switch '-mcmodel=kernel' ... unsupported` from gcc | the *host* gcc is compiling (it rejects `-mcmodel=kernel` without `-fno-pic`, and gnumach's Makefile sets the flags for the cross compiler) | same as above; never retry with hand-added flags |
| `nm` reports "no symbols" on an object that should have some | object was written by the host compiler (broken), *and/or* the host `nm` was used on a cross object | rebuild correctly, then inspect with `x86_64-gnu-nm` only |
| `mig: command not found` or `.defs` compile failures at build time | `MIG` not passed to configure; the Makefile's `MIG=mig` does not resolve | pass `MIG=$HOME/opt/x86_64-gnu/bin/mig` to configure |
| "we will not be able to build a kernel, only install headers" at configure | same missing-MIG situation | same as above |
| `checking for x86_64-gnu-gcc... no` in config.log | prefix not on `PATH` during configure | put `$HOME/opt/x86_64-gnu/bin` first on `PATH` for configure *and* make |
| undefined `__udivdi3`-style symbols at kernel link | cross libgcc missing/stale | rebuild `gcc1` (`make -C toolchain gcc1` after removing its skip marker or the prefix libgcc) |
| guest does not boot the new kernel | image rebuilt from a stale kernel binary, or `CUSTOM_KERNEL` path wrong | rebuild with `make kernel CUSTOM_KERNEL=...`; verify the binary's mtime and `file` output; check the serial log for the kernel banner |
| multi-line `{ ...; }` in a make recipe fails with "unexpected end of file" | make runs each recipe line in its own shell | keep `|| { ...; exit 1; }` on one line (or join with `\`) |

## Worked example: gnumach (hurdkit branch) for the balloon test

```sh
cd ~/src/hurdkit-2
make gnumach-kernel                    # pins 3p, builds the kernel binary, gates it
make kernel CUSTOM_KERNEL=$HOME/work/x86_64-gnu-build/gnumach-kernel/gnumach
make virtio-balloon-test VIRTIO_DIR=$HOME/src/virtio-1 \
    TEST_IMG=$(pwd)/img/hurd.kernel.qcow2
```

The kernel stage boots once to install the kernel (a few minutes); the
harness then boots `img/hurd.kernel.qcow2` snapshots and runs the balloon
matrix against the shared-IRQ-fixed kernel.
