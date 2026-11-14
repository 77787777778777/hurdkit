# Plan: a 64-bit GNU/Hurd system created, booted and managed with Guix

Status: **plan, not implemented.** Nothing described here exists in the
repository yet except this file. The commands were tried on the machine the
plan was written on; section 14 says, step by step, what was verified and
what was not.

## 0. How to use this plan

This plan is written to be executed by someone who has not seen the
discussion that led to it. Follow these rules.

1. **Do the phases in order.** Each phase ends with a *gate*: a few commands
   with the output they must produce. Do not start the next phase until the
   gate passes. If a gate fails, look in section 12 (pitfalls) and section 13
   (troubleshooting) first.
2. **Copy file contents verbatim.** Where the plan says "create FILE with
   this content", the block is complete and tested. Do not paraphrase it,
   reorder it or "improve" it.
3. **One step, one command.** Run the commands one at a time and read the
   output. Do not chain a whole phase into one shell line: the exit status
   of a pipe is the status of its last command, and a failed step would be
   hidden.
4. **Never edit, move or delete** `img/debian-hurd-amd64-*.img*`,
   `img/hurd.base.qcow2` or `img/keys/`: they belong to the Debian pipeline
   (`skills/BOOT.md`). Everything this plan creates lives under `img/guix/`
   and `guix/`.
5. **Do not change the host system.** The plan needs a Guix build daemon on
   the host (phase 0). If it is missing, stop and tell the user; do not edit
   `/etc/nixos` or run `nixos-rebuild` yourself.
6. **Do not fetch Guix sources or documentation from the web.** They are on
   this machine (section 3). The Guix project asks automated clients to
   stay away from its web interface; `git` and the substitute servers are
   fine.
7. **Do not run `guix pull`.** It would install upstream Guix in front of
   the pinned fork (pitfall G1).
8. **Report honestly.** If a step did not work, write down the command, the
   output and what you tried. Do not mark a gate as passed because it
   "probably" passes.
9. Commit messages are plain descriptions of the change, in the style of
   `git log`. Do not add tool or vendor attribution lines.

Words used below:

| Word | Meaning |
|---|---|
| host | The Linux machine that runs QEMU and Guix (here: NixOS, x86_64). |
| guest | The GNU/Hurd system running inside QEMU. |
| Guix | The package manager and system builder. On the host it only *builds* the guest; the host stays NixOS. |
| goonix | Our fork of Guix, `github.com/77787777778777/goonix`. The `guix` command used here is built from it. |
| store | `/gnu/store`, where Guix keeps everything it builds or downloads. Read-only. |
| substitute | A pre-built store item downloaded from a build farm (`ci.guix.gnu.org`) instead of being built locally. |
| derivation | The exact build recipe of a store item. Same derivation, same store path. |
| `x86_64-gnu` | The 64-bit GNU/Hurd platform (Guix calls the triplet `x86_64-pc-gnu`). |

## 1. What is being built

hurdkit today produces a Debian GNU/Hurd VM in five stages: download an
image, patch it offline, convert it, provision it over SSH, boot it from an
overlay. Every stage exists because the image is somebody else's and has to
be bent into shape after the fact.

With Guix the system is **declared** instead: one Scheme file says which
accounts, keys, services and packages the guest has, and
`guix system image` cross-compiles a bootable disk image from it on the
Linux host. There is nothing to patch and nothing to provision.

```
guix/hurd64.scm ──► guix system image -t hurd64-qcow2 ──► /gnu/store/…-disk-image
   (declaration)        (cross-build on the host)              (read-only, compressed)
                                                                      │ qemu-img convert
                                                                      ▼
                                              img/guix/hurd64.base.qcow2   (never booted directly)
                                                                      │ qemu-img create -b
                                                                      ▼
                                              img/guix/tmp/XXXXXX.qcow2    (overlay, thrown away)
                                                                      │
                                                                      ▼
                                              qemu-system-x86_64 -M q35 …  ──► SSH on 127.0.0.1:2223
```

Design decisions, with the reason for each:

| Decision | Reason |
|---|---|
| 64-bit only (`x86_64-gnu`) | It matches hurdkit's Debian image and its `x86_64-gnu` cross toolchain. Guix also supports 32-bit (`i586-gnu`); this plan does not use it. |
| Guix comes from goonix, pinned in `flake.lock` | One commit of Guix decides every byte of the guest. Pinning it in the flake replaces Guix's own mechanism (channels, `guix pull`, `guix time-machine`), which is per-user state outside the repository. |
| The guest is rebuilt, not mutated | To change the guest, edit `guix/hurd64.scm` and build a new image. In-place upgrades of a running 64-bit Hurd are experimental (phase 5.7). |
| Host port 2223 | The Debian guest uses 2222. Both guests can run at the same time. |
| Keys only, no passwords over SSH | The Debian pipeline needs a password because it provisions over SSH. Here the public keys are part of the declaration. |
| Same key pairs as the Debian guest | `img/keys/{root,peon}_ed25519`. One set of keys for both guests. |
| Same account names | `root` and `peon`, so that `skills/CROSS_TESTING.md` carries over. |

Not in scope: a graphical console, running the guest on real hardware,
32-bit images, changing packages inside goonix.

## 2. Facts and pins

These were established on 2026-11-14. Do not re-derive them; check them
with the gate of phase 0 and phase 2.

| Item | Value |
|---|---|
| goonix repository | `https://github.com/77787777778777/goonix`, branch `main` |
| goonix fork point (upstream Guix commit) | `fd49d69fbe07557f0bf55bc7e556e60fc5b13082` (2026-11-11) |
| Why that commit | The upstream build farm built its Hurd images from it (evaluation 2220365, specification `images`), so the substitutes for it exist. |
| Reference image, 64-bit | Build 27202766, `/gnu/store/2pwmm7x0zzsafhzmdiafbi63jdcvclmn-hurd64-barebones.qcow2`, about 444 MB to download |
| Reference image, 32-bit (not used) | Build 27202765, `/gnu/store/6vh19cg376ivd3dklza6px58akdnmvv9-hurd-barebones.qcow2` |
| Where the reference image is defined | `gnu/system/images/hurd.scm` in goonix: `hurd64-barebones-qcow2-image` |
| Image type for our own image | `hurd64-qcow2` (`guix system image --list-image-types`) |
| Disk layout of a Guix Hurd image | DOS/MBR table, **one** partition: ext2 root, label `Guix_image`. No swap partition. The root filesystem is partition **1** (it is 2 in the Debian image). The label written in the system declaration is replaced by the image builder. |
| sshd in the guest | port **2222** (not 22) |
| QEMU machine | `-M q35` is required for the 64-bit Hurd (the default machine has problems with high memory) |
| Network device | `-device e1000` with QEMU user networking; the guest configures `10.0.2.15`, gateway `10.0.2.2`, DNS `10.0.2.3` statically |
| Guest memory | 2048 MB |

## 3. Reference material on this machine

Read these instead of searching the web.

| What | Where |
|---|---|
| Guix sources (goonix checkout) | `~/goonix` |
| Guix manual (Texinfo source) | `~/goonix/doc/guix.texi`; search it with `grep -n`. Sections worth knowing: `@node Invoking guix system`, `@anchor{hurd-vm}`, `@node Invoking guix copy`, `@node Invoking guix archive`, `@node Hurd Services` |
| How Guix defines the Hurd images | `~/goonix/gnu/system/images/hurd.scm` |
| Default Hurd system, packages and services | `~/goonix/gnu/system/hurd.scm` |
| Example configurations | `~/goonix/gnu/system/examples/bare-hurd64.tmpl` |
| How Guix itself runs a Hurd VM (QEMU options) | `~/goonix/gnu/services/virtualization.scm`, search for `hurd-vm-shepherd-service` |
| What goonix is and how it is pinned | `~/goonix/GOONIX.md` |
| Hurd, GNU Mach, glibc, MIG sources | `3p/hurd`, `3p/gnumach`, `3p/glibc`, `3p/mig` (after `make fetch-3p`) |
| Hurd wiki (a shallow git copy) | `3p/hurd-web`, in particular `hurd/running/Guix.mdwn` and `hurd/running/qemu.mdwn` |
| The Debian pipeline of this repository | `skills/BOOT.md`, `skills/CROSS_TESTING.md` |

If `~/goonix` does not exist: `git clone https://github.com/77787777778777/goonix ~/goonix`.
If `3p/hurd-web` does not exist:
`git clone --depth 1 https://git.savannah.gnu.org/git/hurd/web.git 3p/hurd-web`.

## 4. Phase 0 — Check the host

Nothing is created in this phase. It establishes that the host can do the
work. Run every command from the root of the repository.

**0.1 The Guix build daemon is running.**

```sh
systemctl is-active guix-daemon.socket
ls -d /gnu/store /var/guix/daemon-socket
```

Expected: `active`, and both directories exist. If not: stop (rule 5). On
NixOS the daemon is enabled by `services.guix.enable = true;` in the system
configuration; that is a decision for the user.

**0.2 Hardware virtualisation is usable.**

```sh
test -r /dev/kvm && test -w /dev/kvm && echo "kvm ok"
```

Expected: `kvm ok`. Without KVM everything still works, but the guest boots
in minutes instead of seconds; multiply every timeout below by ten.

**0.3 The build directory of the daemon is on ext4.**

```sh
findmnt -no FSTYPE -T /tmp
```

Expected: `ext4`. This matters (pitfall G4): while it builds the image,
Guix records the Hurd's translators as `gnu.*` extended attributes on files
under `/tmp`, and of the common Linux filesystems only ext4 accepts them.
On tmpfs or btrfs the build prints `Ignoring...Good Luck!` and produces an
image that does not boot properly.

**0.4 There is disk space.**

```sh
df -h /gnu/store .
```

Expected: at least 10 GB available on each line. A first image build puts
about 3 GB into `/gnu/store`; each image copy under `img/guix/` is 1.5 to
3 GB.

**0.5 The development shell works.**

```sh
nix develop -c qemu-system-x86_64 --version
```

Expected: a line starting with `QEMU emulator version`.

**0.6 The SSH key pairs exist.**

```sh
ls img/keys/root_ed25519.pub img/keys/peon_ed25519.pub
```

If they are missing (the Debian pipeline, `make provision`, creates them),
create them; do **not** replace existing ones:

```sh
mkdir -p img/keys && chmod 700 img/keys
ssh-keygen -q -t ed25519 -N '' -C hurdkit-root -f img/keys/root_ed25519
ssh-keygen -q -t ed25519 -N '' -C hurdkit-peon -f img/keys/peon_ed25519
```

**0.7 Host port 2223 is free.**

```sh
ss -ltn | grep -c ':2223 '
```

Expected: `0`.

**Gate 0:** all seven checks give the expected result.

## 5. Phase 1 — Pin goonix in the flake

The `guix` command must be the one built from the pinned goonix commit.
This phase adds goonix as a flake input and a second development shell that
contains it.

**1.1 Edit `flake.nix`.** Three changes.

(a) After the `inputs.nixpkgs.url` line, add:

```nix
  # goonix is our fork of GNU Guix. flake.lock pins the exact commit of it;
  # that pin is what replaces Guix channels and "guix pull" here.
  inputs.goonix.url = "github:77787777778777/goonix";
```

(b) Change the argument list of `outputs` from `{ self, nixpkgs }:` to:

```nix
    { self, nixpkgs, goonix }:
```

(c) Inside `devShells.${system} = { … };`, directly after the closing `};`
of the `default` shell, add a second shell. It must be a sibling of
`default`, inside the same braces:

```nix

        # The default shell plus the `guix` command of the pinned goonix,
        # for the Guix GNU/Hurd targets (make guix-image, guix-run, ...).
        # It is a separate shell because building goonix takes about half
        # an hour and the Debian pipeline does not need it.
        guix = pkgs.mkShell {
          name = "hurdkit-guix";
          inputsFrom = [ self.devShells.${system}.default ];
          packages = [ goonix.packages.${system}.default ];
        };
```

Do not write `devShells.${system}.guix = …` as a separate top-level
attribute: Nix cannot merge two definitions that go through the dynamic
name `${system}` and fails with `'devShells' is not an attribute set`.

**1.2 Lock it.**

```sh
nix flake lock
git add flake.nix flake.lock
```

Expected: `Added input 'goonix'` with a `github:77787777778777/goonix/<commit>`
line. `git add` is required: Nix only sees files that git knows about.

**1.3 Enter the shell.** The first time, this builds goonix: 20 to 40
minutes, no output for long stretches. Do not interrupt it. (On the machine
this plan was written on it is already built for commit `630ee3040cf5`, and
the shell opens at once.)

```sh
nix develop .#guix -c guix --version
```

Expected first line: `guix (GNU Guix) 1.5.0-goonix.<7 hex digits>`. If it
says `1.5.0-unstable-…` instead, you are running the system's Guix, not
goonix: you forgot `.#guix`.

**1.4 The Hurd image types are known.**

```sh
nix develop .#guix -c guix system --list-image-types | grep hurd64
```

Expected: two lines, `hurd64-raw` and `hurd64-qcow2`.

**Gate 1:** 1.3 prints a `goonix` version and 1.4 prints both image types.
Commit `flake.nix` and `flake.lock` ("Pin goonix and add a Guix
development shell").

From here on, every `guix` and `make guix-…` command is run inside this
shell. Either prefix each command with `nix develop .#guix -c`, or enter
the shell once with `nix develop .#guix`.

## 6. Phase 2 — Baseline: boot the image the Guix build farm built

Before building anything of our own, boot a known-good image. If this
phase fails, the problem is the host, QEMU or the network path, not our
configuration; that is the whole point of doing it first.

**2.1 Download the reference image by its store path.**

```sh
guix build /gnu/store/2pwmm7x0zzsafhzmdiafbi63jdcvclmn-hurd64-barebones.qcow2
```

Expected: about 444 MB are downloaded from `ci.guix.gnu.org` (one minute
on a fast line) and the command prints the same path.

If it fails with "…is not in the store" or "no substitutes", the build
farm has deleted the image. Then build the same image locally instead; it
gets a different store path, which is fine (pitfall G3), and you use that
path in 2.2:

```sh
guix system image -e '(@ (gnu system images hurd) hurd64-barebones-qcow2-image)'
```

**2.2 Boot it.** `-snapshot` keeps the image untouched: all writes go to a
temporary file that QEMU deletes on exit.

```sh
mkdir -p img/guix
qemu-system-x86_64 -M q35 -m 2048 -enable-kvm \
    -device e1000,netdev=net0 \
    -netdev user,id=net0,hostfwd=tcp:127.0.0.1:2223-:2222 \
    -snapshot -hda /gnu/store/2pwmm7x0zzsafhzmdiafbi63jdcvclmn-hurd64-barebones.qcow2 \
    -display none -serial file:img/guix/baseline-serial.log \
    -pidfile img/guix/baseline.pid -daemonize
```

Expected: the command returns at once and prints nothing; QEMU runs in the
background. (Leave out `-enable-kvm` if 0.2 failed.)

**2.3 Log in.** The reference image lets root in with an empty password.
sshd needs 15 to 20 seconds after the start; a connection that is reset
before that is normal (QEMU accepts the forwarded port before the guest
listens), so repeat the command until it answers.

```sh
ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o IdentityAgent=none -o BatchMode=yes -p 2223 root@127.0.0.1 uname -a
```

Expected: `GNU guixygnu 0.9 GNU-Mach 1.8/Hurd-0.9 x86_64 GNU`

**2.4 Look around** (same ssh options, other commands):

| Command in the guest | Expected |
|---|---|
| `guix --version` | first line `guix (GNU Guix) 1.5.0-7.5ceffb6` (the Guix *inside* the guest; not goonix) |
| `df -h /` | `/dev/wd0s1`, 1.7G, about 87% used |
| `herd status` | `Started:` list containing `ssh-daemon`, `networking`, `guix-daemon`, `term-tty1` |

**2.5 Stop the guest.**

```sh
kill "$(cat img/guix/baseline.pid)"
```

`img/guix/baseline-serial.log` is empty for this image: the reference
image does not put its console on the serial line. Ours will (phase 3).

**Gate 2:** 2.3 printed the `uname` line with `x86_64`, and after 2.5
`pgrep -f baseline.pid` prints nothing.

## 7. Phase 3 — Declare the system and build the image

**3.1 Create `guix/hurd64.scm`** with exactly this content:

```scheme
;; GNU/Hurd (x86_64-gnu) system for hurdkit, built with Guix (goonix).
;;
;; Build a disk image:
;;
;;     guix system image --image-type=hurd64-qcow2 guix/hurd64.scm
;;
;; The public SSH keys of the accounts are read from img/keys/, the same
;; key pairs that the Debian pipeline uses (make provision creates them).

(use-modules (gnu) (gnu system hurd))
(use-service-modules ssh)
(use-package-modules ssh)

(operating-system
  (inherit %hurd-default-operating-system)
  (host-name "hurdkit")
  (timezone "Etc/UTC")

  ;; The Mach console goes to the first serial line, so that the boot
  ;; messages are visible in a headless QEMU ("-serial file:...").
  (kernel-arguments '("console=com0"))

  (bootloader (bootloader-configuration
               (bootloader grub-minimal-bootloader)
               (targets '("/dev/sdX"))
               (timeout 0)
               (terminal-outputs '(console serial))
               (terminal-inputs '(console serial))
               (serial-unit 0)
               (serial-speed 115200)))

  (file-systems (cons (file-system
                        (device (file-system-label "my-root"))
                        (mount-point "/")
                        (type "ext2"))
                      %base-file-systems))

  (users (cons (user-account
                (name "peon")
                (comment "peon")
                (group "users")
                (supplementary-groups '("wheel")))
               %base-user-accounts))

  (packages (cons openssh-sans-x %base-packages/hurd))

  (services
   (cons (service openssh-service-type
                  (openssh-configuration
                   (openssh openssh-sans-x)
                   (use-pam? #f)
                   (port-number 2222)
                   ;; Keys only: no password login over the network.
                   (permit-root-login 'prohibit-password)
                   (password-authentication? #f)
                   (authorized-keys
                    `(("root" ,(local-file "../img/keys/root_ed25519.pub"))
                      ("peon" ,(local-file "../img/keys/peon_ed25519.pub"))))))
         %base-services+qemu-networking/hurd)))
```

What the parts mean, so that you can change them later (phase 5.2):

| Part | Meaning |
|---|---|
| `(inherit %hurd-default-operating-system)` | Start from Guix's default Hurd system (`gnu/system/hurd.scm`): GNU Mach as the kernel, the Hurd servers, no initrd. Everything below overrides a field of it. |
| `kernel-arguments` | Passed to GNU Mach by GRUB. `console=com0` is the same setting the Debian pipeline patches into `grub.cfg`. |
| `bootloader` | GRUB. `(targets '("/dev/sdX"))` is a placeholder that the image builder ignores; `(timeout 0)` boots without waiting at the menu. |
| `file-systems` | The root filesystem. The label is replaced by the image builder (the image gets `Guix_image`). |
| `users` | `peon`, in addition to the default accounts (`root`). No passwords are set: `root` has an empty password, which works on the QEMU console only, because sshd accepts keys only. |
| `packages` | What is in `PATH` for every user. `%base-packages/hurd` is the default set (shell, coreutils, the Hurd tools, guile, …). |
| `openssh-service-type` | sshd on port 2222 with the two public keys. `(use-pam? #f)` is needed on the Hurd. |
| `%base-services+qemu-networking/hurd` | The default services plus a static network configuration for QEMU's user network (`10.0.2.15`). |

`local-file` names are relative to the directory of `hurd64.scm`, hence
`../img/keys/…`. The key files are copied into the image at build time:
if they change, the next build produces a different image.

**3.2 Build the image by hand once**, to see Guix work before the Makefile
hides it:

```sh
guix system image --image-type=hurd64-qcow2 guix/hurd64.scm
```

Expected: 5 to 15 minutes (11 minutes on a busy machine when this plan
was written), most of it spent writing and checking the filesystem of the
image (`partition.img`). Guix downloads what the build farm has built
(about 15 MB after phase 2; a few hundred MB if you skipped it), builds
around 50 small derivations locally (configuration files, `grub.cfg`,
`partition.img`, `disk-image`), and prints a store path as its **last
line**:

```
/gnu/store/…-disk-image
```

There must be **no** line containing `Ignoring...Good Luck!` in the output
(pitfall G4). No compiler should run: if Guix starts building packages such
as `gcc-cross-…` or `glibc-cross-…`, the substitutes are missing; stop and
read pitfall G2.

**Gate 3:** the command exits with status 0 and its last line is a
`/gnu/store/…-disk-image` path that exists (`ls -l` it).

## 8. Phase 4 — Make targets for building, booting and managing

Typing QEMU command lines by hand is how mistakes happen (pitfalls G5,
G6, G10). This phase puts them into one script and a few `make` targets.

**4.1 Create `scripts/guix_vm.sh`** with exactly this content, then
`chmod +x scripts/guix_vm.sh`:

```bash
#!/usr/bin/env bash
# Build, boot and manage the Guix GNU/Hurd (x86_64-gnu) guest that is
# declared in guix/hurd64.scm.
#
# Usage: guix_vm.sh <command> [arguments]
#
#   image              build the declared system into img/guix/hurd64.base.qcow2
#   start [persist]    boot the guest in the background and wait for SSH;
#                      with "persist" its disk state is kept between runs
#   status             say whether the guest runs and answers on SSH
#   ssh USER [CMD...]  run a command in the guest (a login shell without CMD)
#   stop               shut the guest down (halt, then stop QEMU)
#   screenshot [FILE]  save the VGA console of the guest as a PPM picture
#   run                boot in the foreground with a throw-away disk
#   repair             check and repair the root filesystem of the kept state
#   reset-state        delete the kept disk state
#
# The base image is never booted: the guest always runs from a
# copy-on-write overlay, so the base image stays as Guix built it.
#
# Every setting below can be overridden from the environment; the Makefile
# passes its own values.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

GUIX="${GUIX:-guix}"
GUIX_OS="${GUIX_OS:-$REPO_ROOT/guix/hurd64.scm}"
GUIX_IMAGE_TYPE="${GUIX_IMAGE_TYPE:-hurd64-qcow2}"
# Extra options for "guix system image", for example "--image-size=8G".
GUIX_IMAGE_OPTS="${GUIX_IMAGE_OPTS:-}"
GUIX_DIR="${GUIX_DIR:-$REPO_ROOT/img/guix}"
SSH_KEY_DIR="${SSH_KEY_DIR:-$REPO_ROOT/img/keys}"
SSH_HOST="${SSH_HOST:-127.0.0.1}"
GUIX_SSH_PORT="${GUIX_SSH_PORT:-2223}"
GUIX_MEMORY="${GUIX_MEMORY:-2048}"
QEMU_SYSTEM="${QEMU_SYSTEM:-qemu-system-x86_64}"
QEMU_IMG="${QEMU_IMG:-qemu-img}"
# How long one boot may take until SSH answers, and how often a boot that
# hangs is tried again (see "start" below). Without KVM a boot takes
# minutes; raise the timeout then.
GUIX_BOOT_TIMEOUT="${GUIX_BOOT_TIMEOUT:-180}"
GUIX_BOOT_ATTEMPTS="${GUIX_BOOT_ATTEMPTS:-3}"

BASE_IMG="$GUIX_DIR/hurd64.base.qcow2"
# A symlink into /gnu/store. It is a garbage collector root: as long as it
# exists, "guix gc" does not delete the image that Guix built.
STORE_LINK="$GUIX_DIR/hurd64.store-image"
STATE_IMG="$GUIX_DIR/state.qcow2"
RUN_IMG="$GUIX_DIR/tmp/run.qcow2"
SERIAL_LOG="$GUIX_DIR/serial.log"
PID_FILE="$GUIX_DIR/qemu.pid"
# The monitor socket does not live in GUIX_DIR: the path of a Unix socket
# is limited to 107 bytes, and a checkout in a deep directory exceeds that
# ("UNIX socket path must be less than 108 bytes"). One socket per SSH
# port, so that guests of two checkouts do not collide.
MONITOR_SOCKET="${XDG_RUNTIME_DIR:-/tmp}/hurdkit-guix-${GUIX_SSH_PORT}.monitor"
LOCK_FILE="$GUIX_DIR/.build.lock"
# The root filesystem of a Guix GNU/Hurd image is its only partition.
ROOT_PARTITION=1
# The sshd of the guest listens on this port (not 22).
GUEST_SSH_PORT=2222

die() {
    echo "guix_vm: $*" >&2
    exit 1
}

need() {
    command -v "$1" >/dev/null 2>&1 || die "$1 is required but not in PATH"
}

# The process ID of the running QEMU, or nothing.
qemu_pid() {
    local pid
    [[ -f "$PID_FILE" ]] || return 0
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
        echo "$pid"
    fi
}

# guest_ssh [-b] USER [COMMAND...]
#
# Run COMMAND in the guest as USER, or a login shell without COMMAND.
# -b is batch mode: ssh never asks for anything and fails instead.
guest_ssh() {
    local -a batch=()
    if [[ "${1:-}" == "-b" ]]; then
        batch=(-o BatchMode=yes)
        shift
    fi
    local user="$1"; shift
    local key="$SSH_KEY_DIR/${user}_ed25519"
    [[ -f "$key" ]] || die "no SSH key for $user: $key"
    # IdentitiesOnly and IdentityAgent=none: offer this key and nothing
    # else. ssh would otherwise try every key of a running agent first, and
    # sshd closes the connection after six failed attempts.
    ssh -i "$key" -o IdentitiesOnly=yes -o IdentityAgent=none \
        -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o LogLevel=ERROR -o ConnectTimeout=10 "${batch[@]}" \
        -p "$GUIX_SSH_PORT" "$user@$SSH_HOST" "$@"
}

# Wait up to TIMEOUT seconds until root can log in. The forwarded port
# accepts connections as soon as QEMU runs, long before sshd listens, so
# only a successful login counts.
wait_for_ssh() {
    local timeout="$1" deadline
    deadline=$(( SECONDS + timeout ))
    while (( SECONDS < deadline )); do
        if guest_ssh -b root true 2>/dev/null; then
            return 0
        fi
        [[ -n "$(qemu_pid)" ]] || die "QEMU exited while the guest was booting; see $SERIAL_LOG"
        sleep 3
    done
    return 1
}

# Stop QEMU without asking the guest. Only for a guest that is hung or
# already halted.
kill_qemu() {
    local pid="$1" i
    kill "$pid" 2>/dev/null || true
    for (( i = 0; i < 10; i++ )); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 1
    done
    kill -9 "$pid" 2>/dev/null || true
    rm -f "$PID_FILE" "$MONITOR_SOCKET"
}

qemu_command() {
    local disk="$1"; shift
    local -a accel=()
    if [[ -r /dev/kvm && -w /dev/kvm ]]; then
        accel=(-enable-kvm)
    else
        echo "guix_vm: KVM is not available, the guest runs without acceleration" >&2
    fi
    # -M q35: required for the 64-bit Hurd, the default machine type has
    # problems with memory above 4 GB of address space.
    # e1000: the network card the guest has a driver for.
    "$QEMU_SYSTEM" -M q35 "${accel[@]}" -m "$GUIX_MEMORY" \
        -device e1000,netdev=net0 \
        -netdev "user,id=net0,hostfwd=tcp:${SSH_HOST}:${GUIX_SSH_PORT}-:${GUEST_SSH_PORT}" \
        -hda "$disk" "$@"
}

new_overlay() {
    local overlay="$1"
    mkdir -p "$(dirname "$overlay")"
    rm -f "$overlay"
    # The backing file has to be an absolute path: qemu-img resolves a
    # relative one against the directory of the overlay.
    "$QEMU_IMG" create -q -f qcow2 -b "$(readlink -f "$BASE_IMG")" -F qcow2 "$overlay"
}

cmd_image() {
    need "$GUIX"; need "$QEMU_IMG"; need flock
    [[ -f "$GUIX_OS" ]] || die "system declaration not found: $GUIX_OS"
    [[ -z "$(qemu_pid)" ]] || die "the guest is running; stop it first (make guix-stop)"
    if [[ -e "$STATE_IMG" ]]; then
        die "$STATE_IMG is a kept disk state on top of the current base image and
would be corrupt on top of a new one. Remove it first: make guix-reset-state"
    fi
    mkdir -p "$GUIX_DIR"
    # The build is serialised: two concurrent builds would write the same
    # base image.
    exec 9>"$LOCK_FILE"
    flock 9

    local store_image previous=""
    if [[ -L "$STORE_LINK" ]]; then
        previous="$(readlink -f "$STORE_LINK")"
    fi
    # guix refuses to replace an existing root ("failed to create GC
    # root"), so the old one is removed first. It cannot be renamed into
    # place either: guix registers the root under the path given to
    # --root, and a link moved away from that path no longer protects the
    # image from "guix gc".
    rm -f "$STORE_LINK"
    # guix prints the store file name of the image as the last line of its
    # standard output; the build log goes to standard error.
    # shellcheck disable=SC2086
    store_image="$("$GUIX" system image --image-type="$GUIX_IMAGE_TYPE" \
        $GUIX_IMAGE_OPTS --root="$STORE_LINK" "$GUIX_OS" | tail -n 1)"
    [[ -f "$store_image" ]] || die "guix did not produce an image (got: '$store_image')"
    echo "guix_vm: built $store_image"
    if [[ -f "$BASE_IMG" && "$store_image" == "$previous" ]]; then
        echo "guix_vm: nothing changed, the base image is up to date"
        return 0
    fi

    # The image in the store is read-only and compressed. The base image is
    # an uncompressed copy next to the overlays; it is written to a
    # temporary file first, so that an interrupted build never leaves a
    # partial base image behind.
    "$QEMU_IMG" convert -f qcow2 -O qcow2 "$store_image" "$BASE_IMG.tmp"
    mv -f "$BASE_IMG.tmp" "$BASE_IMG"
    echo "guix_vm: base image $BASE_IMG"
}

cmd_start() {
    local mode="${1:-}" disk
    need "$QEMU_SYSTEM"; need "$QEMU_IMG"
    [[ -f "$BASE_IMG" ]] || die "no base image; build it first (make guix-image)"
    [[ -z "$(qemu_pid)" ]] || die "the guest is already running (pid $(qemu_pid))"
    case "$mode" in
        "")
            disk="$RUN_IMG"
            new_overlay "$disk"
            ;;
        persist)
            disk="$STATE_IMG"
            [[ -f "$disk" ]] || new_overlay "$disk"
            ;;
        *)
            die "unknown start mode: $mode (use nothing or 'persist')"
            ;;
    esac
    # The 64-bit Hurd sometimes hangs at boot: the TCP/IP server (pfinet)
    # never finishes starting, shepherd waits for it forever with the
    # guest idle, and sshd never starts. A second boot of the same disk
    # works. So a boot that does not answer on SSH in time is stopped and
    # tried again.
    local attempt
    for (( attempt = 1; attempt <= GUIX_BOOT_ATTEMPTS; attempt++ )); do
        : > "$SERIAL_LOG"
        rm -f "$MONITOR_SOCKET"
        # The monitor socket lets the host talk to QEMU itself, for example
        # to take a screenshot of the VGA console (make guix-screenshot).
        qemu_command "$disk" -display none -serial "file:$SERIAL_LOG" \
            -monitor "unix:$MONITOR_SOCKET,server,nowait" \
            -pidfile "$PID_FILE" -daemonize
        echo "guix_vm: boot $attempt/$GUIX_BOOT_ATTEMPTS: QEMU started (pid $(qemu_pid)), disk $disk"
        if wait_for_ssh "$GUIX_BOOT_TIMEOUT"; then
            echo "guix_vm: the guest answers on SSH port $GUIX_SSH_PORT"
            return 0
        fi
        echo "guix_vm: no SSH after ${GUIX_BOOT_TIMEOUT}s; last lines of $SERIAL_LOG:" >&2
        tail -n 5 "$SERIAL_LOG" >&2 || true
        kill_qemu "$(qemu_pid)"
        if [[ "$mode" == persist ]]; then
            # The guest was stopped without a shutdown; the root filesystem
            # (ext2, no journal) is checked before the next boot.
            echo "guix_vm: checking the kept state after the forced stop" >&2
            bash "$SCRIPT_DIR/repair_filesystem.sh" "$STATE_IMG" "$ROOT_PARTITION" >/dev/null \
                || die "the repair of $STATE_IMG failed; run make guix-repair to see why"
        fi
    done
    die "the guest did not answer on SSH in $GUIX_BOOT_ATTEMPTS boots; see $SERIAL_LOG"
}

cmd_status() {
    local pid
    pid="$(qemu_pid)"
    if [[ -z "$pid" ]]; then
        echo "stopped"
        return 1
    fi
    if guest_ssh -b root true 2>/dev/null; then
        echo "running (pid $pid), SSH answers on port $GUIX_SSH_PORT"
    else
        echo "running (pid $pid), SSH does not answer"
        return 2
    fi
}

cmd_ssh() {
    [[ $# -ge 1 ]] || die "usage: guix_vm.sh ssh USER [COMMAND...]"
    local user="$1"; shift
    guest_ssh "$user" "$@"
}

cmd_stop() {
    local pid i
    pid="$(qemu_pid)"
    if [[ -z "$pid" ]]; then
        echo "guix_vm: the guest is not running"
        rm -f "$PID_FILE" "$RUN_IMG"
        return 0
    fi
    # A clean shutdown matters for a kept state: the root filesystem is
    # ext2 without a journal. "halt" shuts the Hurd down but does not power
    # the virtual machine off; GNU Mach ends in a loop after printing this
    # line on its console (the serial line, see console=com0 in
    # guix/hurd64.scm). From then on QEMU can be stopped safely.
    local done_line="Shutdown completed successfully"
    guest_ssh -b root halt >/dev/null 2>&1 || true
    for (( i = 0; i < 60; i++ )); do
        kill -0 "$pid" 2>/dev/null || break
        grep -a -q "$done_line" "$SERIAL_LOG" 2>/dev/null && break
        sleep 1
    done
    if grep -a -q "$done_line" "$SERIAL_LOG" 2>/dev/null || ! kill -0 "$pid" 2>/dev/null; then
        echo "guix_vm: the guest has shut down"
    else
        echo "guix_vm: the guest did not shut down within 60 s; stopping QEMU anyway" >&2
        echo "guix_vm: run make guix-repair before the next persistent boot" >&2
    fi
    kill_qemu "$pid"
    rm -f "$RUN_IMG"
    echo "guix_vm: stopped"
}

cmd_screenshot() {
    local out="${1:-$GUIX_DIR/screen.ppm}"
    [[ -n "$(qemu_pid)" ]] || die "the guest is not running"
    [[ -S "$MONITOR_SOCKET" ]] || die "no monitor socket: $MONITOR_SOCKET"
    need python3
    # The QEMU monitor writes the picture of the VGA screen as a PPM file.
    python3 -I - "$MONITOR_SOCKET" "$(readlink -f "$(dirname "$out")")/$(basename "$out")" <<'PY'
import socket, sys, time
path, out = sys.argv[1], sys.argv[2]
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect(path)
s.settimeout(2)
try:
    s.recv(65536)
except socket.timeout:
    pass
s.sendall(("screendump " + out + "\n").encode())
time.sleep(1)
s.close()
PY
    [[ -s "$out" ]] || die "QEMU did not write $out"
    echo "guix_vm: screenshot $out"
}

cmd_run() {
    need "$QEMU_SYSTEM"; need "$QEMU_IMG"
    [[ -f "$BASE_IMG" ]] || die "no base image; build it first (make guix-image)"
    [[ -z "$(qemu_pid)" ]] || die "the guest is already running (pid $(qemu_pid))"
    local disk
    mkdir -p "$GUIX_DIR/tmp"
    disk="$(mktemp -u "$GUIX_DIR/tmp/XXXXXX.qcow2")"
    trap 'rm -f "$disk"' EXIT
    new_overlay "$disk"
    echo "guix_vm: booting; the console of the guest follows. SSH: make guix-ssh" >&2
    echo "guix_vm: to stop: 'halt' in the guest, or Ctrl-A X here" >&2
    # -nographic puts the serial line, which carries the console of the
    # guest, on this terminal.
    qemu_command "$disk" -nographic
}

cmd_repair() {
    [[ -z "$(qemu_pid)" ]] || die "the guest is running; stop it first (make guix-stop)"
    [[ -f "$STATE_IMG" ]] || die "there is no kept state to repair ($STATE_IMG)"
    bash "$SCRIPT_DIR/repair_filesystem.sh" "$STATE_IMG" "$ROOT_PARTITION"
}

cmd_reset_state() {
    [[ -z "$(qemu_pid)" ]] || die "the guest is running; stop it first (make guix-stop)"
    rm -f "$STATE_IMG"
    echo "guix_vm: kept state removed"
}

command="${1:-}"
[[ -n "$command" ]] || die "usage: guix_vm.sh image|start [persist]|status|ssh USER [CMD...]|stop|screenshot [FILE]|run|repair|reset-state"
shift
case "$command" in
    image)       cmd_image "$@" ;;
    start)       cmd_start "$@" ;;
    status)      cmd_status "$@" ;;
    ssh)         cmd_ssh "$@" ;;
    stop)        cmd_stop "$@" ;;
    screenshot)  cmd_screenshot "$@" ;;
    run)         cmd_run "$@" ;;
    repair)      cmd_repair "$@" ;;
    reset-state) cmd_reset_state "$@" ;;
    *)           die "unknown command: $command" ;;
esac
```

**4.2 Create `guix.mk`** (a Makefile fragment, next to `Makefile`) with
exactly this content. Recipe lines start with a **tab**, not spaces:

```make
# Guix GNU/Hurd (x86_64-gnu) guest. Included by the top-level Makefile.
#
# The system is declared in guix/hurd64.scm; scripts/guix_vm.sh builds,
# boots and manages it. Run these targets inside "nix develop .#guix": the
# guix command has to be the pinned goonix.

# The system declaration that guix-image builds.
GUIX_OS         := guix/hurd64.scm
# The port on the host that is forwarded to the sshd of the guest. The
# Debian guest uses SSH_PORT (2222), so both guests can run together.
GUIX_SSH_PORT   := 2223
# Memory of the guest in MiB.
GUIX_MEMORY     := 2048
# Extra options for "guix system image", for example --image-size=8G
GUIX_IMAGE_OPTS :=

GUIX_VM := env GUIX_OS="$(abspath $(GUIX_OS))" \
	GUIX_SSH_PORT=$(GUIX_SSH_PORT) GUIX_MEMORY=$(GUIX_MEMORY) \
	GUIX_IMAGE_OPTS="$(GUIX_IMAGE_OPTS)" SSH_HOST=$(SSH_HOST) \
	SSH_KEY_DIR="$(abspath $(SSH_KEY_DIR))" \
	GUIX_DIR="$(abspath $(IMG_DIR)/guix)" \
	QEMU_SYSTEM=$(QEMU_SYSTEM) QEMU_IMG=$(QEMU_IMG) \
	bash scripts/guix_vm.sh

.PHONY: guix-image guix-start guix-start-persist guix-status guix-ssh \
	guix-ssh-peon guix-stop guix-screenshot guix-run guix-repair \
	guix-reset-state

# Build guix/hurd64.scm into img/guix/hurd64.base.qcow2
guix-image:
	@$(GUIX_VM) image

# Boot in the background from a throw-away overlay and wait for SSH
guix-start:
	@$(GUIX_VM) start

# The same, but the disk state is kept in img/guix/state.qcow2
guix-start-persist:
	@$(GUIX_VM) start persist

guix-status:
	@$(GUIX_VM) status

guix-ssh:
	@$(GUIX_VM) ssh root

guix-ssh-peon:
	@$(GUIX_VM) ssh peon

# Shut the guest down
guix-stop:
	@$(GUIX_VM) stop

# Save the VGA console of the running guest as img/guix/screen.ppm
guix-screenshot:
	@$(GUIX_VM) screenshot

# Boot in the foreground, console on this terminal, throw-away overlay
guix-run:
	@$(GUIX_VM) run

# Check and repair the root filesystem of the kept state (guest stopped)
guix-repair:
	@$(GUIX_VM) repair

# Delete the kept state
guix-reset-state:
	@$(GUIX_VM) reset-state
```

**4.3 Include it from `Makefile`.** Add this line directly after the
`include toolchain/pins.mk` line:

```make
include guix.mk
```

`guix.mk` uses `IMG_DIR`, `SSH_KEY_DIR`, `SSH_HOST`, `QEMU_SYSTEM` and
`QEMU_IMG` from `Makefile`; they are defined above that line.

**4.4 Build and boot through the targets.**

```sh
make guix-image
make guix-start
make guix-ssh             # an interactive shell as root; leave with "exit"
make guix-ssh-peon
make guix-stop
```

Expected:

| Command | Expected output |
|---|---|
| `make guix-image` | `guix_vm: built /gnu/store/…-disk-image` and `guix_vm: base image …/img/guix/hurd64.base.qcow2`. The first time it takes as long as step 3.2; afterwards seconds. |
| `make guix-image` again | `guix_vm: nothing changed, the base image is up to date` |
| `make guix-start` | `boot 1/3: QEMU started …`, then within about a minute `the guest answers on SSH port 2223`. A line `no SSH after 180s` followed by `boot 2/3` is the known boot hang (pitfall G15) being handled; it is not an error. |
| `make guix-status` | `running (pid …), SSH answers on port 2223` |
| `make guix-ssh` | a root prompt on host `hurdkit` |
| `make guix-stop` | `the guest has shut down`, then `stopped`, within about 15 s (60 s and a warning if 9pfs was used: G20) |

**4.5 What the targets do**, for when something goes wrong:

| Target | What happens |
|---|---|
| `guix-image` | `guix system image -t hurd64-qcow2 --root=img/guix/hurd64.store-image $(GUIX_OS)`, then `qemu-img convert` of the store image into `img/guix/hurd64.base.qcow2` (through a temporary file). Refuses while the guest runs or while a kept state exists (G8). |
| `guix-start` | Creates `img/guix/tmp/run.qcow2`, an empty overlay on top of the base image, and boots it in the background. Console → `img/guix/serial.log`, QEMU monitor → `$XDG_RUNTIME_DIR/hurdkit-guix-2223.monitor` (G19), PID → `img/guix/qemu.pid`. Waits until root can log in; retries a hung boot (G15). |
| `guix-start-persist` | The same with `img/guix/state.qcow2`, which is kept across boots. |
| `guix-stop` | `halt` in the guest, waits for GNU Mach's `Shutdown completed successfully` line in the serial log (G16), then stops QEMU and deletes the throw-away overlay. |
| `guix-run` | Boots in the foreground with the console on the terminal (a fresh throw-away overlay, deleted at exit). Leave with `halt` in the guest, then Ctrl-A X. |
| `guix-screenshot` | Saves the VGA screen as `img/guix/screen.ppm` through the monitor socket. |
| `guix-repair` | `e2fsck` of partition 1 of the kept state, with the guest stopped. |
| `guix-reset-state` | Deletes the kept state. |

**Gate 4:** the six commands of 4.4 give the expected output, and after
`make guix-stop` the command `make guix-status` prints `stopped`. Commit
`scripts/guix_vm.sh`, `guix.mk`, the `Makefile` line and
`guix/hurd64.scm` ("Add a Guix-built 64-bit GNU/Hurd guest").

## 9. Phase 5 — Manage the system

**5.1 Everyday use.**

```sh
make guix-start           # background, throw-away disk
make guix-ssh             # or: bash scripts/guix_vm.sh ssh root 'COMMAND'
make guix-stop
```

Files: `scp` with the same key and port (note `-P`, capital, for scp):

```sh
scp -P 2223 -i img/keys/root_ed25519 -o IdentitiesOnly=yes -o IdentityAgent=none \
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    FILE root@127.0.0.1:/tmp/
```

**5.2 Change the system — the normal way.** Every change to the guest is a
change to `guix/hurd64.scm` (or to a file it loads, 5.3), followed by:

```sh
make guix-stop
make guix-image
make guix-start
```

The guest then runs the new system from a fresh disk. Nothing done inside
the previous guest survives; that is the point. To see what a change would
build before waiting for it:

```sh
guix system image -t hurd64-qcow2 --dry-run guix/hurd64.scm
```

Read pitfall G2 before adding packages: a package is cheap if the build
farm has cross-built it for `x86_64-pc-gnu`, and expensive (or failing)
otherwise. The dry run tells you which.

Examples of changes, each a single edit of `guix/hurd64.scm`:

| Change | Edit |
|---|---|
| Another account | add a `(user-account …)` to the `users` list, and its key to `authorized-keys` |
| More memory for the guest | `make guix-start GUIX_MEMORY=4096` (no rebuild) |
| More disk | `GUIX_IMAGE_OPTS := --image-size=8G` in `guix.mk`, then rebuild (G11) |
| A package | `(packages (cons* openssh-sans-x (specification->package "NAME") %base-packages/hurd))` |
| A program of our own | phase 6 |

**5.3 Keep data across boots.** `make guix-start-persist` boots from
`img/guix/state.qcow2` instead of a fresh overlay; whatever is written in
the guest stays until `make guix-reset-state`. Use it for long-running
experiments, not as a substitute for 5.2: the kept state belongs to one
base image (G8), so a rebuild means losing it. Copy what you need out with
`scp` first.

After a stop that was not clean (`guix_vm: the guest did not shut down`),
or a power cut, check the filesystem before the next persistent boot:

```sh
make guix-repair
```

Expected: `e2fsck` passes 1 to 5 and `Filesystem repaired: …state.qcow2`.

**5.4 Look at a guest that does not answer.**

```sh
tail -n 40 img/guix/serial.log     # kernel and service manager messages
make guix-screenshot               # the VGA console: img/guix/screen.ppm
```

Open the PPM with any image viewer. The VGA console shows the login
prompt of `tty1`; root has an empty password there (the console is not
reachable over the network).

**5.5 Guix inside the guest.** The guest runs its own `guix-daemon`
(`herd status guix-daemon`). Building inside the guest is possible in
principle but not useful here: only a small part of the packages exist as
*native* `x86_64-gnu` substitutes, the disk has about 200 MB free (G11),
and everything a guest needs can be built faster on the host (5.2, 6.1).
Not verified.

**5.6 Garbage collection on the host.** Every image build leaves store
items behind. The current image is protected by the root
`img/guix/hurd64.store-image`. To reclaim space:

```sh
guix gc --list-roots | grep hurdkit     # the image root must be listed
guix gc -d 1m                            # delete what no root needs any more
```

**5.7 In-place upgrades: not supported here.** Guix can upgrade a running
system (`guix system reconfigure` inside it, or `guix deploy` from the
host). For this guest neither is part of the plan:

- `guix system reconfigure` in the guest would build a native `x86_64-gnu`
  system inside the guest (5.5).
- `guix deploy` builds the system for the machine's own system type
  (`x86_64-gnu`) on the host, which can only run `x86_64-linux` builds.
  It would need a Hurd build machine for offloading (Guix's `hurd-vm`
  service sets that up on Guix System hosts; this host is NixOS).

Rebuild and replace (5.2) instead. Not verified, and deliberately so.

## 10. Phase 6 — Run our own drivers in the Guix guest

`skills/CROSS_TESTING.md` tests a driver built with hurdkit's cross
toolchain in the **Debian** guest. Those binaries do not run in the Guix
guest as they are:

- Their ELF interpreter is `/lib/ld-x86-64.so.1`. A Guix system has no
  `/lib` at all (checked: `ls /lib` → `No such file or directory`); every
  library lives in `/gnu/store`.
- They are linked against the glibc and Hurd libraries of the toolchain
  (`3p/glibc`, `3p/hurd` at the pins of `toolchain/pins.mk`), not against
  the versions in the guest.

In the Guix guest, a driver is therefore built **by Guix**, as a package,
cross-compiled on the host. The result links against exactly the
libraries of the guest, and it gets into the guest the same way as every
other package: through the system declaration.

**6.1 Package the driver.** Create `guix/9pfs.scm` with exactly this
content. It builds the `3p/9pfs` checkout (run `make fetch-3p` first if
`3p/9pfs` is missing):

```scheme
;; The 9pfs translator from the 3p/9pfs checkout, built by Guix for the
;; Guix GNU/Hurd guest.
;;
;; Cross-build it on the host for the 64-bit Hurd:
;;
;;     guix build --target=x86_64-pc-gnu -f guix/9pfs.scm
;;
;; The result links against the C library and the Hurd libraries of the
;; guest's own Guix, so it runs in the guest without copying any library.

(use-modules (guix packages)
             (guix gexp)
             (guix utils)
             (guix build-system gnu)
             ((guix licenses) #:prefix license:)
             (gnu packages cross-base)
             (gnu packages hurd))

(define %source-directory
  (string-append (dirname (current-filename)) "/../3p/9pfs"))

;; Everything in the checkout except git metadata and build products.
(define (source-file? file stat)
  (let ((name (basename file)))
    (not (or (string=? name ".git")
             (string-suffix? ".o" name)
             (string-suffix? "Server.c" name)
             (string-suffix? "_S.h" name)
             (and (eq? 'regular (stat:type stat))
                  (string=? name "9pfs"))))))

(package
  (name "9pfs")
  (version "0-hurdkit")
  (source (local-file %source-directory "9pfs-checkout"
                      #:recursive? #t
                      #:select? source-file?))
  (build-system gnu-build-system)
  (arguments
   (list
    #:tests? #f
    #:make-flags
    #~(list (string-append "CC=" #$(cc-for-target))
            #$(if (%current-target-system)
                  (string-append "MIG=" (%current-target-system) "-mig")
                  "MIG=mig")
            ;; libc needs libhurduser and libmachuser, but GNU ld does not
            ;; resolve the references of 9pfs through a dependency of a
            ;; dependency ("DSO missing from command line"), so they are
            ;; named explicitly, as in toolchain/Makefile.
            "LIBS=shouldbeinlibc ihash ports iohelp fshelp pager hurduser machuser")
    #:phases
    #~(modify-phases %standard-phases
        (delete 'configure)              ;there is no configure script
        (replace 'install
          (lambda _
            (install-file "9pfs" (string-append #$output "/bin")))))))
  (native-inputs
   (list (if (%current-target-system)
             (cross-mig (%current-target-system))
             mig)))
  (inputs (list hurd))
  (supported-systems '("i586-gnu" "x86_64-gnu"))
  (home-page "https://github.com/bugaevc/9pfs")
  (synopsis "9P2000.L client translator for the Hurd")
  (description "9pfs is a Hurd translator that mounts a directory exported by
a 9P2000.L server.")
  (license license:gpl3+))
```

Build it on its own first:

```sh
guix build --target=x86_64-pc-gnu -f guix/9pfs.scm
```

Expected: under a minute; Guix downloads the cross compiler, the cross
MIG and the Hurd headers (about 20 MB), builds only `9pfs-0-hurdkit`, and
prints `/gnu/store/…-9pfs-0-hurdkit`. Check it:

```sh
file "$(guix build --target=x86_64-pc-gnu -f guix/9pfs.scm)/bin/9pfs"
```

Expected: `ELF 64-bit LSB executable, x86-64, … interpreter
/gnu/store/…-glibc-cross-x86_64-pc-gnu-2.41/lib/ld-x86-64.so.1`.

Notes on the package, for writing the next one:

| Field | Why |
|---|---|
| `local-file … #:select? source-file?` | The source is the working tree of `3p/9pfs`, without `.git` and without build products (`toolchain/Makefile` builds in place). The tree is read from disk, so uncommitted changes are included. |
| `#:make-flags` | `CC` is the cross compiler, `MIG` the cross MIG (`x86_64-pc-gnu-mig`), `LIBS` adds `hurduser machuser` exactly as `toolchain/Makefile` does. |
| `(delete 'configure)` | 9pfs has a plain Makefile. |
| `(replace 'install …)` | 9pfs has no install target. |
| `native-inputs` / `inputs` | MIG runs on the host; the Hurd libraries are linked into the target program. |

**6.2 Put the driver into the guest.** Create `guix/hurd64-drivers.scm`
with exactly this content. It is `hurd64.scm` plus a list of driver
packages, so the base declaration stays as it is:

```scheme
;; guix/hurd64.scm plus the drivers under test (here: 9pfs).
;;
;;     make guix-image GUIX_OS=guix/hurd64-drivers.scm
;;
;; The drivers are packages in guix/; adding one to the list below puts its
;; programs into the PATH of every user of the guest.

(use-modules (gnu))

(define %hurdkit-os
  (load "hurd64.scm"))

(define %drivers
  (list (load "9pfs.scm")))

(operating-system
  (inherit %hurdkit-os)
  (packages (append %drivers
                    (operating-system-packages %hurdkit-os))))
```

Build and boot it:

```sh
make guix-stop
make guix-image GUIX_OS=guix/hurd64-drivers.scm
make guix-start
```

To make it the default, set `GUIX_OS := guix/hurd64-drivers.scm` in
`guix.mk`.

**6.3 Smoke test.**

```sh
bash scripts/guix_vm.sh ssh root 'command -v 9pfs; 9pfs --version'
```

Expected: a path under `/run/current-system/profile/bin/`, then
`9pfs 0.1.0` (or the version the checkout reports) and no error.

**6.4 End-to-end: mount a host directory.** In one terminal on the host:

```sh
mkdir -p ~/work/9p-root
echo "hello from the host" > ~/work/9p-root/canary.txt
python3 toolchain/9p-server.py ~/work/9p-root 127.0.0.1:1564
```

Wait until it prints `9p-server: serving … on 127.0.0.1:1564` (pitfall
G21). Then, in another terminal:

```sh
bash scripts/guix_vm.sh ssh root 'settrans -c /host "$(command -v 9pfs)" 10.0.2.2:1564'
bash scripts/guix_vm.sh ssh root 'ls -la /host; cat /host/canary.txt'
bash scripts/guix_vm.sh ssh root 'echo "written from the guest" > /host/write-test.txt'
cat ~/work/9p-root/write-test.txt
bash scripts/guix_vm.sh ssh root 'settrans -g -f /host; rm -f /host'
```

Expected: the guest lists `canary.txt` (owner `nobody`) and prints
`hello from the host`; the host prints `written from the guest`. Stop the
server with Ctrl-C afterwards. Expect the next `make guix-stop` to take
60 s (pitfall G20). `10.0.2.2` is the host as seen
from QEMU's user network. `settrans` needs the full path of the
translator: a translator is started by the filesystem, not by your shell,
so `PATH` does not apply.

**6.5 Another driver.** Copy `guix/9pfs.scm` to `guix/NAME.scm`, change
`name`, `%source-directory`, `#:make-flags` and the `install` phase to
match its build system (a driver with a `configure` script keeps the
`configure` phase; Guix passes `--host=x86_64-pc-gnu` itself), and add
`(load "NAME.scm")` to `%drivers` in `guix/hurd64-drivers.scm`. Build it
alone first (6.1) so that a build error is not buried in an image build.

**Gate 6:** 6.3 prints the version, and both directions of 6.4 work.
Commit `guix/9pfs.scm` and `guix/hurd64-drivers.scm` ("Build 9pfs with
Guix and add it to the Guix guest").

## 11. Phase 7 — Documentation

**7.1 Create `skills/GUIX.md`**, a skill in the style of `skills/BOOT.md`,
with these sections and nothing invented beyond this plan:

- *Trigger*: work on the Guix guest (`guix/`, `guix.mk`,
  `scripts/guix_vm.sh`), or a Guix guest that does not boot or answer.
- *Inputs*: the files of phases 1, 3, 4 and 6, `img/guix/`, `img/keys/`.
- *Goal*: the acceptance criteria of section 15.
- *Procedure*: the commands of 4.4, 5.2, 5.3 and 6.1–6.4, each with its
  expected output.
- *Pitfalls*: section 12 of this plan, copied with their numbers.

**7.2 README.** Add a section "Guix guest (64-bit GNU/Hurd)" after "Quick
start": three sentences on what it is, the commands
`nix develop .#guix`, `make guix-image`, `make guix-start`,
`make guix-ssh`, `make guix-stop`, and a pointer to `skills/GUIX.md`. Add
the `guix-*` targets to the "Main targets" table and `guix/`, `guix.mk`,
`scripts/guix_vm.sh` to "Project layout".

**7.3 CROSS_TESTING.md.** Add a short section "Testing in the Guix guest"
that says why toolchain binaries do not run there (6, first paragraph)
and points to phase 6 / `skills/GUIX.md`.

**Gate 7:** the three documents are updated, every command in them was run
once while writing, and `git status` shows nothing untracked except
`img/` (ignored). Commit ("Document the Guix guest").

## 12. Pitfalls

Each of these was met, or measured, while this plan was written. They are
numbered G1, G2, … so that they cannot be confused with the pitfalls of
`skills/BOOT.md`.

**G1. `guix` outside the development shell is not goonix.** The host also
has a system-wide Guix (it runs the daemon). Outside `nix develop .#guix`,
`guix --version` prints `1.5.0-unstable-…` (or whatever a `guix pull` put
into `~/.config/guix/current`), and an image built with it is not the
image the flake pins. Check the first line of `guix --version`; it must
contain `goonix`. Never run `guix pull` for this work.

**G2. Substitutes exist only for commits the build farm built.** Everything
heavy in the guest (GNU Mach, the Hurd, glibc, the cross toolchain) is
downloaded pre-built. That works because goonix has the same package
definitions as the upstream commit named in section 2. If the pin is moved
to a goonix commit that changes package definitions, or to an upstream
commit that was never built, Guix cross-compiles a toolchain and a C
library locally: hours instead of minutes. Check before a long wait:
`guix system image --dry-run --image-type=hurd64-qcow2 guix/hurd64.scm`
lists what would be built and how much would be downloaded. A healthy run
builds about 50 derivations with names like `shepherd-….scm`,
`sshd_config`, `grub.cfg`, `partition.img`, `disk-image`, and none named
`gcc-…`, `glibc-…`, `hurd-…` or `gnumach-…`.

**G3. Our image cannot itself be downloaded, and its store path differs
from the build farm's.** The store path of an image depends on its exact
derivation. The build farm computes the reference image with a Guix that
was installed through channels; goonix is installed through Nix, and the
derivation of the image differs (checked: the farm's is
`6b17i4zj…-hurd64-barebones.qcow2.drv`, goonix computes
`b5zh5ns7…-hurd64-barebones.qcow2.drv` for the same expression). This is
harmless: the packages inside are identical and are still downloaded. It
only means that the reference image is fetched by its store path
(step 2.1), not by rebuilding its expression.

**G4. The image build needs `/tmp` on ext4.** While it populates the root
filesystem, Guix records each Hurd translator (`/dev` nodes, `/servers`)
as a `gnu.translator` extended attribute on a file in the build directory
of the daemon, under `/tmp`. Linux accepts `gnu.*` attributes on ext4
only. On tmpfs or btrfs the build log shows `Operation not supported`
followed by `Ignoring...Good Luck!` for each of them, the build still
"succeeds", and the guest does not come up. Check with step 0.3 before
building, and search the build output for `Good Luck` after.

**G5. sshd in the guest listens on 2222, not 22.** The port forward has to
end in `-:2222`. With `-:22` the connection is accepted by QEMU and reset
by the guest, exactly like a guest that is still booting.

**G6. The 64-bit guest needs `-M q35`.** With QEMU's default machine type
the 64-bit Hurd has problems with memory; Guix's own VM service passes
`-M q35` for the `hurd64` image type and so does `scripts/guix_vm.sh`.

**G7. The root filesystem is partition 1.** A Guix GNU/Hurd image has one
partition; the Debian image has swap first and root second. Offline tools
therefore take `1`: `bash scripts/repair_filesystem.sh IMAGE 1`.

**G8. A kept state is tied to one base image.** `img/guix/state.qcow2` is
a copy-on-write overlay: it stores only the blocks that differ from
`hurd64.base.qcow2`. Replace the base image and the overlay describes
changes to a disk that no longer exists; the guest sees a corrupt
filesystem. `guix_vm.sh image` refuses to run while a kept state exists.
Remove it first (`make guix-reset-state`); what you need from it has to
be copied out with `scp` before.

**G9. Nix only sees files that git knows.** After editing `flake.nix`, run
`git add flake.nix flake.lock` before `nix develop`. Otherwise Nix
evaluates the old file, or fails with "path … is not tracked by Git".
(`guix/hurd64.scm` is read by Guix from the working directory, not through
Nix, so this does not apply to it.)

**G10. The store image is read-only and compressed.** The file that
`guix system image` prints lives in `/gnu/store`: it cannot be written,
it is zstd-compressed qcow2, and `guix gc` deletes it unless a root points
at it. `guix_vm.sh image` therefore passes `--root=` (the symlink
`img/guix/hurd64.store-image`) and converts the image into
`img/guix/hurd64.base.qcow2`. Never boot the store file without
`-snapshot`.

**G11. The guest has little free disk space by default.** The image is
sized to its content: about 1.7 GB with roughly 200 MB free. That is
enough to boot and log in, not to install or build anything inside. Set
`GUIX_IMAGE_OPTS := --image-size=8G` in `guix.mk` when you need room.

**G12. The interactive shell on this machine is zsh, with traps.** `cp`
and `mv` are aliased to ask before overwriting, so a command line with
`cp a b` over an existing file waits for an answer forever when there is
no terminal: use `command cp -f`. `noclobber` is set, so `> FILE` on an
existing file fails with `file exists` and the command that was meant to
write it silently uses the old content: write files with an editor, or
with `>|`. zsh also reads `$var:rest` as a modifier on `$var`
(`$sha:refs/heads/main` loses characters): write `${sha}:refs/…`.
Anything longer than a line belongs in a bash script, run with `bash`.

**G13. Parallel heavy jobs make everything look hung.** Building goonix,
building an image and running a guest at the same time saturate the disk;
`nix develop` then waits silently on a store lock for minutes. Run the
heavy steps one after another, and before concluding that something hangs
look at `cat /proc/pressure/io` (a `full avg10` above 40 means the disk
is the bottleneck).

**G14. An open port is not a ready guest.** As in the Debian pipeline
(BOOT.md pitfall 19): QEMU accepts the forwarded port at once and resets
the connection until sshd runs. Only a successful login proves the guest
is up; `guix_vm.sh start` waits for exactly that.

**G15. The 64-bit guest sometimes hangs at boot, in the network setup.**
Of eleven boots on the machine this plan was written on, two (one of the
build farm's reference image, one of ours) stopped at
`shepherd[1]: Starting service networking...`: the `loopback` and
`networking` services never finish, so sshd never starts, and the guest
sits idle at about 1% CPU. The SSH client then fails with
`Connection timed out during banner exchange`. Both hangs happened within
the same three minutes, while the host was at its busiest (three builds
running); the nine other boots, including the following ones of the very
same disks, came up in 15 to 40 seconds. The cause was not found; both
services start `pfinet`, the Hurd's TCP/IP server. `guix_vm.sh start`
handles it: a boot without SSH after `GUIX_BOOT_TIMEOUT` (180 s) is
stopped and retried, up to `GUIX_BOOT_ATTEMPTS` (3) times, and a kept
state is checked with `e2fsck` between attempts. (The retry itself has not
been seen in action yet: no boot hung after it was added.) If all three
attempts hang, report it with `img/guix/serial.log` and a screenshot.

**G16. `halt` does not power the virtual machine off.** The Hurd shuts
down, GNU Mach prints `Shutdown completed successfully, now in tight
loop.` on its console, and QEMU keeps running. Waiting for QEMU to exit
therefore always runs into the timeout. `guix_vm.sh stop` waits for that
line in `img/guix/serial.log` instead and stops QEMU when it appears;
this is why the declaration puts the console on the serial line
(`console=com0`).

**G17. Ignore the "Consider running 'guix pull'" warning.** Every
`guix system image` prints it, because goonix is newer than its own
release date suggests and has no channel information. It does not mean
anything is out of date. See G1: never run `guix pull` for this work.

**G18. `guix system image --root=LINK` does not replace an existing link.**
It fails with `failed to create GC root`. `guix_vm.sh image` removes the
old link first. Do not "fix" it by building into a temporary link and
renaming it: Guix registers the root under the path given to `--root`,
and a renamed link no longer protects the image from `guix gc`.

**G19. A Unix socket path is limited to 107 bytes.** QEMU refuses
`-monitor unix:PATH` with `UNIX socket path must be less than 108 bytes`
when the checkout lives in a deep directory. `guix_vm.sh` therefore puts
the monitor socket into `$XDG_RUNTIME_DIR` (`/run/user/UID`), named after
the SSH port, not into `img/guix/`.

**G20. After 9pfs has been used, `halt` does not finish.** Three times out
of three, a guest in which a 9pfs translator had run (even after
`settrans -g` and with no 9pfs process left) stopped its SSH server on
`halt` and then never reached GNU Mach's `Shutdown completed` line; five
shutdowns without 9pfs were all clean. The cause was not found.
`guix_vm.sh stop` stops QEMU after 60 s anyway and says so. With the
throw-away disk nothing is lost; with a kept state, run `make guix-repair`
before the next `make guix-start-persist`.

**G21. Start the 9P server before mounting, and wait for it.** If the
guest connects before `9p-server.py` listens, 9pfs exits at once and every
access to the mount point fails with `Translator died`. On a loaded host
Python can take seconds to start. Wait for its line
`9p-server: serving … on 127.0.0.1:1564`, and if a mount already died,
`settrans -g -f /host` and mount again.


## 13. Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| `guix: command not found`, or `guix --version` without `goonix` | not in the Guix shell | `nix develop .#guix` (G1) |
| `nix develop .#guix` builds for half an hour | goonix is built from source the first time | wait; later runs are instant |
| `error: 'devShells' is not an attribute set` | `devShells.${system}.guix` written as a separate attribute | put `guix = …` inside the `devShells.${system} = { … }` braces (step 1.1 c) |
| `path '…/flake.nix' is not tracked by Git` | new or changed flake file not added | `git add flake.nix flake.lock` (G9) |
| `failed to connect to /var/guix/daemon-socket/socket` | no Guix daemon on the host | stop and ask the user (rule 5, step 0.1) |
| the image build starts compiling `gcc-cross-…`, `glibc-cross-…` | substitutes missing for this goonix commit | G2; check the pin; do not wait hours without telling the user |
| `Ignoring...Good Luck!` in the build output | build directory not on ext4 | G4 |
| `failed to create GC root` | old root link in the way | G18 (fixed in `guix_vm.sh`) |
| `is a kept disk state … Remove it first` | `make guix-image` with a kept state | copy data out, `make guix-reset-state`, rebuild (G8) |
| `no SSH after 180s` then `boot 2/3` | the known boot hang | nothing; it is retried (G15) |
| `did not answer on SSH in 3 boots` | hang every time, or the guest is broken | `tail -n 40 img/guix/serial.log`, `make guix-screenshot`, report both |
| `Connection reset by peer` right after start | sshd not up yet | wait; `make guix-start` already does (G14) |
| `Permission denied (publickey)` | wrong key, or the key changed after the image was built | `ls img/keys/`; rebuild the image after changing keys (3.1) |
| `Too many authentication failures` | ssh offered agent keys first | use `scripts/guix_vm.sh ssh`, which passes `IdentitiesOnly=yes` |
| `the guest is already running` | QEMU still running from earlier | `make guix-stop` (a stale `qemu.pid` of a QEMU that is gone is ignored) |
| `make guix-stop` says `did not shut down within 60 s` | the guest hung | `make guix-repair` before the next persistent boot |
| `No such file or directory` running a toolchain binary in the guest | no `/lib/ld-x86-64.so.1` in a Guix system | build the driver with Guix (phase 6) |
| `settrans: …: No such file or directory` | translator given without a full path | `settrans -c /host "$(command -v 9pfs)" …` (6.4) |
| guest disk full | image sized to its content | G11 |
| `UNIX socket path must be less than 108 bytes` | monitor socket in a deep directory | fixed in `guix_vm.sh` (G19); check `XDG_RUNTIME_DIR` is set |
| `make guix-stop` always times out after using 9pfs | known (G20) | nothing for a throw-away disk; `make guix-repair` for a kept state |
| `Translator died` on every access to `/host` | 9pfs could not reach the 9P server | start the server first and wait for its `serving` line (G21), then `settrans -g -f /host` and mount again |

## 14. What was verified

On 2026-11-14, on NixOS 26.11 (x86_64, KVM, 16 cores, `/tmp` on ext4),
goonix commit `630ee3040cf5`, QEMU 11.1.2, while other heavy jobs ran on
the same machine:

"Verified" means the step was run as written (or through
`scripts/guix_vm.sh` / `guix.mk`, which are the files quoted in this plan)
and gave the expected output. Steps that were run in an equivalent form say
how.

| Step | Status | Notes |
|---|---|---|
| 0.1–0.7 host checks | verified | daemon active, `/tmp` on ext4, KVM usable |
| 1.1–1.4 goonix pin and `.#guix` shell | verified | in a copy of the flake: `nix develop .#guix -c guix --version` → `1.5.0-goonix.630ee30`; both `hurd64` image types listed; the shell inherits the default shell's `PATH` setup. The goonix derivation from `github:77787777778777/goonix` is identical to the one built from `~/goonix`. |
| 2.1 reference image by store path | verified | 444 MB from `ci.guix.gnu.org` in about one minute |
| 2.2–2.5 boot the reference image | verified | same QEMU options through a script; root login with empty password, `uname`, `guix --version`, `df`, `herd status` as in the table. One of two boots hung (G15). |
| 3.1 `guix/hurd64.scm` | verified | the file quoted is the tested file |
| 3.2 image build | verified | 11.5 min; 53 small derivations built, nothing compiled, no `Good Luck` lines |
| 4.1–4.3 script and targets | verified | in a copy of the repository; the quoted files are the tested files |
| 4.4 `guix-image` | verified | first build, then `nothing changed, the base image is up to date` |
| 4.4 `guix-start`, `guix-status` | verified | eight boots through the script, all on the first attempt, 15–40 s each |
| 4.4 `guix-ssh`, `guix-ssh-peon` | verified (as `guix_vm.sh ssh USER CMD`) | key logins work; a login without key → `Permission denied (publickey)` |
| 4.4 `guix-stop` | verified | clean in about 10 s without 9pfs (G16); 60 s fallback after 9pfs (G20) |
| `guix-screenshot` | verified | 640×400 PPM of the VGA console (login prompt) |
| `guix-run` (foreground) | **not verified** | needs a terminal; it is the same QEMU command with `-nographic` |
| 5.2 change → rebuild → boot | verified | with `hurd64-drivers.scm` as the change |
| 5.3 kept state, repair, reset | verified | a file survives `stop`/`start-persist`; `guix-image` refuses while the state exists; `guix-repair` passes (3 min through the NBD chain on a busy disk); after `guix-reset-state` the file is gone |
| 5.4 serial log and screenshot | verified | |
| 5.5 Guix inside the guest | **not verified** | only `guix --version` and `herd status guix-daemon` |
| 5.6 garbage collection | **not verified** | `guix gc` was not run, to keep the store of the host intact |
| 5.7 in-place upgrades | **not verified**, by design | |
| 6.1 `guix/9pfs.scm` | verified | 17 s; 19 MB of cross tools downloaded, only `9pfs-0-hurdkit` built; interpreter in `/gnu/store` |
| 6.2 drivers image | verified | 11 min; 9pfs not rebuilt (same derivation as 6.1) |
| 6.3 smoke test | verified | `/run/current-system/profile/bin/9pfs`, `9pfs 0.1.0` |
| 6.4 mount test | verified | list, read, write (seen on the host), mkdir, stat, detach. Failed once with `Translator died` because the server was not listening yet (G21). |
| 6.5 another driver | **not verified** | |
| G15 boot retry | **not verified in action** | the hang occurred before the retry was written, never after |
| Phase 7 documentation | not started | it is part of the implementation |

## 15. Acceptance criteria

The work is done when all of these hold, checked in this order on a
clean checkout (`img/guix/` absent):

1. `nix develop .#guix -c guix --version` prints a `goonix` version.
2. `make guix-image` builds `img/guix/hurd64.base.qcow2` without compiling
   any package (only configuration derivations, `partition.img` and
   `disk-image` are built), and a second run says it is up to date.
3. `make guix-start` brings the guest up; `make guix-ssh` and
   `make guix-ssh-peon` log in with the keys of `img/keys/`; a password
   login is refused.
4. `make guix-stop` shuts the guest down cleanly in under 30 s, and
   `make guix-status` then prints `stopped`.
5. A file written in the guest after `make guix-start-persist` is still
   there after `make guix-stop` and another `make guix-start-persist`,
   `make guix-repair` passes, and it is gone after
   `make guix-reset-state`.
6. With `GUIX_OS=guix/hurd64-drivers.scm`, `9pfs --version` runs in the
   guest and the mount test of 6.4 works in both directions.
7. The Debian pipeline is unaffected: `make -n run` does not mention
   `guix`, and `img/hurd.base.qcow2` is not touched by any `guix-*`
   target.
8. `skills/GUIX.md`, the README and `skills/CROSS_TESTING.md` describe what
   was built, and every command in them was run.
