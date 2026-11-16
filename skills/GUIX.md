# Skill: Build and Manage the Guix 64-bit GNU/Hurd Guest

## Trigger

Use this skill for work on the Guix guest (`guix/`, `guix.mk`,
`scripts/guix_*.sh`), or when a Guix guest does not boot or answer.

## Inputs

- `nix/flake.nix` and `nix/flake.lock`: the `guix` development shell, with
  goonix (our fork of GNU Guix) at a pinned commit.
- `guix/hurd64.scm`: the system declaration of the guest.
- `guix/goonix.scm`: goonix as the guest's own Guix (same commit as the lock).
- `guix.mk`: the make targets of the guest.
- `img/keys/`: the SSH keys of the guest's root and peon accounts.

## Goal

Build and boot a 64-bit GNU/Hurd guest from a declarative Guix system,
with key-based SSH.

## Constraints

- The Guix guest and the Debian pipeline do not depend on each other.
  Nothing of the guest reads a variable of the Makefile or runs a script of
  the Debian pipeline (`tests/guix-standalone.t` checks that), and the
  Debian pipeline uses nothing of the guest: the Makefile only includes
  `guix.mk`. The two share `img/keys/`.
- Whatever the guest needs from another repository, or from this one's
  other half, is a Guix package or origin at a pinned commit with its hash.
  A pin moves only when someone moves it, so a change elsewhere cannot
  break a Guix target.
- This document states no counts, durations, versions or test results. A
  pin or a default that moves must not make it wrong: name the variable or
  the command that tells instead.
- Never edit goonix for this work, and never change the guest's glibc:
  every package links against it, so a changed glibc rebuilds the whole
  guest locally.

## Procedure

### Build the image

Host, shell and guest all run goonix at the commit that `nix/flake.lock`
pins: the host's daemon (see "Host daemon"), the `guix` command of the
`nix develop ./nix#guix` shell, and the guest's Guix through
`guix/goonix.scm`. Run every Guix command in that shell and never run
`guix pull` for this work.

```sh
nix develop ./nix#guix
guix system image --image-type=hurd64-qcow2 guix/hurd64.scm
```

The declaration reads the accounts' public keys from `img/keys/`
(`root_ed25519.pub`, `peon_ed25519.pub`); `ssh-keygen -q -t ed25519 -N ''
-f img/keys/root_ed25519` creates a missing pair.

`guix system image` prints the image in the store: a read-only, compressed
qcow2. The first build on a machine cross-compiles goonix for the guest;
everything else is downloaded from the build farm. Boot it without changing
it:

```sh
qemu-system-x86_64 -M q35 -enable-kvm -m 2048 -snapshot -hda IMAGE \
    -device e1000,netdev=net0 \
    -netdev user,id=net0,hostfwd=tcp:127.0.0.1:2223-:2222 -nographic
ssh -i img/keys/root_ed25519 -p 2223 root@127.0.0.1 guix --version
```

The guest's `guix --version` prints the same goonix line as on the host.

### Host daemon

The host's Guix daemon has to be goonix too. On NixOS, with goonix as an
input of the system flake (`goonix.url = "github:hurdkit/goonix/goonix";`,
locked at the commit `nix/flake.lock` pins; goonix's `main` branch mirrors
upstream Guix, its `goonix` branch has the fork):

```nix
services.guix.enable = true;
services.guix.package = inputs.goonix.packages.${pkgs.stdenv.hostPlatform.system}.default;
services.avahi.enable = false;  # services.guix would switch it on
```

After a switch that changes the daemon, check `systemctl is-active
guix-daemon.socket`. If the service restarted before its socket ("Socket
service guix-daemon.service already active, refusing"), run `systemctl stop
guix-daemon.service` and `systemctl start guix-daemon.socket`.

To move the goonix pin, update `nix/flake.lock` (`nix flake update goonix
--flake ./nix`), the system flake's lock, and `%goonix-commit` with its
hash in `guix/goonix.scm`; `tests/goonix-pin.t` compares the last two.

### Host archive signing

`guix/hurd64.scm` authorizes the host's archive signing key,
`/etc/guix/signing-key.pub`, when the host has one, so `guix copy` from the
host into the guest works. `make guix-authorize-host` creates a missing key
pair (the private key stays in `/etc/guix/signing-key.sec`); on NixOS
without `/run/wrappers/bin` in `PATH`, add
`GUIX_SUDO=/run/wrappers/bin/sudo`. An image built before the key existed
does not trust it: rebuild the image.

## Pitfalls

**G1. An image built by another Guix is a different system.** Another
`guix` can come first in `PATH`, for example one installed by `guix pull`
into `~/.config/guix/current`. Its package graph differs, so the image
differs from the declared one even with identical files. Check that the
first line of `guix --version` contains `goonix`.

**G2. Substitutes exist only for commits the build farm built.** GNU Mach,
the Hurd, glibc and the cross toolchain of the guest are downloaded
pre-built because goonix has the package definitions of an upstream Guix
commit that the farm built. A goonix commit that changes those definitions
makes Guix build a toolchain and a C library locally, which takes hours.
Check before a long wait: `guix system image --dry-run
--image-type=hurd64-qcow2 guix/hurd64.scm` lists configuration files,
`goonix-…` and nothing named `gcc-…`, `glibc-…`, `hurd-…` or `gnumach-…`.

**G3. Our image itself cannot be downloaded.** Its derivation differs from
the build farm's, so its store path does too. The packages inside are the
same and are downloaded.

**G4. The image build needs `/tmp` on ext4.** Guix records each Hurd
translator as a `gnu.translator` extended attribute in the daemon's build
directory under `/tmp`; Linux accepts `gnu.*` attributes on ext4 only. On
tmpfs or btrfs the build prints `Operation not supported` and `Ignoring...Good
Luck!`, "succeeds", and the guest does not come up. Check `findmnt -no
FSTYPE -T /tmp`.

**G5. sshd in the guest listens on 2222, not 22.** The port forward ends in
`-:2222`. With `-:22` QEMU accepts the connection and the guest resets it,
exactly like a guest that is still booting.

**G6. The 64-bit guest needs `-M q35`.** The default machine type gives the
64-bit Hurd memory problems.

**G9. Nix only sees files that git knows.** After editing `nix/flake.nix`,
`git add` it before `nix develop`. (Guix reads `guix/` from the working
directory, so this does not apply there.)

**G13. Parallel heavy jobs make everything look hung.** Building goonix or
an image while a guest runs saturates the disk, and `nix develop` waits
silently on a store lock. Run heavy steps one after another; `cat
/proc/pressure/io` shows whether the disk is the bottleneck.

**G14. An open port is not a ready guest.** QEMU accepts the forwarded port
at once. Only a successful login proves the guest is up.

**G17. Ignore "Consider running 'guix pull'".** goonix has no channel
information, so `guix` prints it every time. See G1.

## Verification

Run these in `nix develop ./nix#guix`. Each command exits with zero status
when its checks pass and prints what failed otherwise.

| Check | Command |
|---|---|
| Scripts and declarations (boots nothing) | `make guix-test` |
