# Skill: Boot and Provision the Debian GNU/Hurd Image

## Trigger

Use this skill whenever work touches the Hurd disk image or its boot path:

- building or refreshing `img/hurd.base.qcow2` (`make base`, `make provision`),
- changing what the guest does at first boot (GRUB, serial console, sshd, passwords),
- changing what the in-VM provisioning step installs or configures,
- debugging a guest that does not boot, does not answer on SSH, or whose
  base image turns out to be dirty or corrupt,
- changing the image version, the root password, the SSH port, or the disk layout.

## Inputs

- `Makefile` - pipeline definition and all tunables (version, URL, MD5, ports, password).
- `scripts/prepare_base.sh` - offline changes applied to a copy of the raw image.
- `scripts/provision_base.sh` - in-VM provisioning driven over SSH.
- `scripts/repair_filesystem.sh` - offline ext2 repair of the provisioned image.
- `img/debian-hurd-amd64-<VERSION>.img.tar.xz` - the downloaded Debian GNU/Hurd image
  (pinned to `VERSION=20260314`, MD5 `91dc71608fcfc9f237028f1fe87c39c2`).

## Goal

A reproducible pipeline that produces a clean, provisioned, bootable Hurd image
without root privileges, and that boots the guest from a throw-away overlay so
the base image stays byte-for-byte pristine:

```
download -> offline preparation -> qcow2 base -> in-VM provisioning -> overlay boot
```

The acceptance criteria for a finished image are:

1. `make base` produces `img/hurd.base.qcow2` whose root filesystem passes
   `e2fsck -fn` (exit status 0).
2. `make provision` completes and leaves the same property (see "Known issue:
   ext2 without a journal" for why a repair step is needed).
3. `make run` boots the guest, SSH login as `root` with the configured password
   works, and the MD5 of `img/hurd.base.qcow2` is unchanged after the run.
4. `make provision` a second time is a no-op ("Image is already provisioned.").

## Environment (this sandbox)

- Tools available: `qemu-img` / `qemu-system-x86_64` 10.2.3, `wget`, `tar`, `xz`,
  `expect`, `ssh`, `md5sum`, `debugfs` 1.47.4, `dumpe2fs`, `e2fsck`, `sfdisk`,
  `parted`, `fuse2fs`, `fusermount`, `openssl`, `flock`, `jq`, `python3`.
- Tool **not** available: `sudo`, `nbd`, `guestfish`, `virt-customize`, `socat`,
  `sshpass`. There is no root access, so loop mounts and libguestfs are out; the
  offline write path is `partitionfs` + `debugfs`.
- `partitionfs` (FUSE, exposes the partitions of a disk image as files) is built
  at `/home/chz/src/partitionfs/build/partitionfs`; the path is overridable with
  the `PARTFS` environment variable.
- The Hurd sources are checked out next to the project in `hurd/`; read them
  (`hurd/ext2fs`, `hurd/libdiskfs`) when the behaviour of the translator matters.
- KVM is available at `/dev/kvm`.
- Disk layout of the image (partition table is a DOS/MBR table):
  - partition 1: swap, starts at sector 2048, 1951744 sectors,
  - partition 2: ext2 root filesystem, starts at sector 1953792, 6236160 sectors
    (byte offset 1000341504, about 3.0 GB with roughly 1.5 GB free).
  - The root filesystem has **no journal**; this matters for the shutdown path.

## Procedure

### Stage 1 - download (`make download`)

`$(IMG_TAR)` is downloaded from
`https://cdimage.debian.org/cdimage/ports/latest/hurd-amd64/debian-hurd-amd64-$(VERSION).img.tar.xz`
and checked against `MD5_SUM` before it is used.

The tarball is unpacked into `img/` by the rule for `img/.extracted`. The stamp
file exists because `tar` restores the *member* mtime of the image, which is
older than the tarball itself; without the stamp, make would consider the raw
image out of date and re-extract it on every invocation.

### Stage 2 - offline preparation (`scripts/prepare_base.sh`)

`prepare_base.sh <source.raw> <work.raw> [root_password]` never touches the
downloaded image. It copies it to `img/hurd.work.img` and edits the copy:

1. `cp` the raw image, then `sync "$WORK_IMG"`. This is mandatory: `partitionfs`
   probes the image through the block layer, which ignores the page cache, so an
   unflushed copy looks like it has no partition table.
2. Mount the copy with `partitionfs` in a fresh `mktemp -d` directory; the root
   filesystem is then the file `<mountdir>/2`.
3. Apply the offline edits with `debugfs -w`:
   - `/boot/grub/grub.cfg` - serial console and auto-boot (see below),
   - `/etc/default/grub` - the same settings, so `update-grub` keeps them,
   - `/etc/shadow` - password hash for `root` and `demo`,
   - `/etc/ssh/sshd_config.d/10-local.conf` - permit root password login.

The GRUB configuration that makes the image boot unattended:

```
set default=0
set timeout=0
set terminal=console

menuentry "GNU/Hurd" {
    multiboot /boot/gnumach-1.8-amd64-up.gz root=part:2:device:wd0 console=com0
    ...
}
```

`timeout=0` removes the menu delay, `terminal=console` keeps GRUB on the serial
line, and `console=com0` hands the console to the Hurd kernel. Without these the
guest waits at the GRUB prompt and never boots unattended.

The password hash is generated with `openssl passwd -6` (`$6$`, SHA-512).
`/etc/login.defs` of the image names `YESCRYPT` as the preferred method, but the
Hurd libc `crypt` does not support `$y$` hashes, so `$6$` is the only safe
choice.

### Stage 3 - base image (`make base`)

`make base` runs the preparation under `flock img/.build.lock`, converts the work
image to qcow2 (`qemu-img convert -f raw -O qcow2`) and moves the result into
place with `mv -f`. The build therefore never leaves a half-written base image
behind, and two concurrent builds cannot interleave.

### Stage 4 - in-VM provisioning (`scripts/provision_base.sh`)

`provision_base.sh <base.qcow2> [ssh_port]` provisions a **private copy** of the
base image and only replaces the base when everything succeeded:

1. `qemu-img convert` the base into `img/.provision.XXXXXX.qcow2`.
2. Boot that copy with QEMU (`if=ide`, `-device e1000`, user networking with
   `hostfwd` of the SSH port to port 22 of the guest).
3. Wait until the forwarded port accepts connections (sshd starts during boot).
4. Drive the guest with `expect` over SSH (the login is retried, see pitfall 19):
   - log in as `root` with the password that was set offline,
   - `stty -echo` so that the echoed command line cannot be mistaken for
     command output,
   - `run_command <cmd> <tag>` sends a command, captures its exit status through
     a `__RC__<n>__` marker and leaves the output in the guest's
     `/tmp/provision_<tag>.log`,
   - if `/root/.provisioned` exists, stop here ("Image is already provisioned."),
   - otherwise run `apt-get update`, `apt-get upgrade` and `apt-get clean`, then
     create the marker,
   - `poweroff` the guest and wait for QEMU to exit. The SSH connection dies
     together with the guest, so the script does not wait for the exit status
     or the shell prompt of the poweroff command.
5. Repair the ext2 filesystem offline (`scripts/repair_filesystem.sh`), see below.
6. `mv -f` the provisioned copy over `img/hurd.base.qcow2`.

The marker file `/root/.provisioned` is what makes the step idempotent. It
lives in the guest, so it survives in the base image and is visible in every
overlay booted from it.

### Stage 5 - runtime (`make run`, `make run-nokvm`)

Both targets create a temporary overlay and boot it:

```
qemu-img create -f qcow2 -b $(abspath img/hurd.base.qcow2) -F qcow2 img/tmp/XXXXXX.qcow2
qemu-system-x86_64 -enable-kvm -cpu host -smp 4 -m 2G \
    -drive file=<overlay>,if=ide,cache=writeback \
    -netdev user,id=net0,hostfwd=tcp:127.0.0.1:2222-:22 \
    -device e1000,netdev=net0 -nographic
```

The backing file must be an **absolute** path: `qemu-img` resolves a relative
backing path against the directory of the overlay, so `-b img/hurd.base.qcow2`
from `img/tmp/` looks for `img/tmp/img/hurd.base.qcow2`.

The base image is only ever used as a read-only backing file, so it stays
pristine; all writes land in the overlay in `img/tmp/`.

### Offline filesystem repair (`scripts/repair_filesystem.sh`)

`repair_filesystem.sh <raw.img> [partition]` locates the partition with
`parted -s -m <img> unit s print`, extracts it with `dd`, runs `e2fsck -fy` on
the extracted file and writes it back with `conv=notrunc`.

This step exists because of the "Known issue" below; it runs after the guest
has powered off, while the image is not mounted.

## Pitfalls

Every one of these was hit while building this pipeline.

1. **Root SSH login is rejected.** The shipped `/etc/ssh/sshd_config` has
   `#PermitRootLogin prohibit-password` commented out, so the default
   `prohibit-password` applies and password authentication for root is refused.
   Fix: drop-in `/etc/ssh/sshd_config.d/10-local.conf` with `PermitRootLogin yes`
   and `PasswordAuthentication yes`. The main config contains
   `Include /etc/ssh/sshd_config.d/*.conf`, and sshd uses the first value it
   obtains, so the drop-in wins.

2. **An empty root password is not enough.** Even with `PermitRootLogin yes`,
   `PermitEmptyPasswords` defaults to `no`. The image ships `root` and `demo`
   with empty password fields, so a non-empty password has to be set.

3. **Rewriting `/etc/shadow` from scratch breaks everything.** The file has one
   line per account and **nine** colon-separated fields. A rewrite that emits
   eight fields and only the root line deletes 20 accounts and makes the
   malformed entry crash the console `login` with SIGSEGV. Fix: dump the file
   with `debugfs`, replace **only field 2** of the `root` and `demo` lines with
   `awk -F: -v OFS=: ...`, and write it back with mode `0100640`, uid 0, gid 42
   (the `shadow` group). Note the `=` in `-v OFS=:`; `-v OFS:` is an awk usage
   error.

4. **`partitionfs` sees no partitions after `cp`.** libparted reads the image
   through the block layer and ignores the page cache. Fix: `sync "$WORK_IMG"`
   after every copy of the image.

5. **`debugfs write` cannot overwrite an existing file.** The command fails
   with "Could not create file". Fix: `rm <file>` first, then `write`.
   The debugfs command file has no shell syntax and no `#` comments.

6. **`fuse2fs` refuses to write root-owned files.** It enforces strict
   ownership, and without root privileges nothing can be written. Fix: use
   `partitionfs` (which exposes the partition as a plain file) plus
   `debugfs -w` on that file.

7. **expect matches the wrong branch.** `NOT_PROVISIONED` contains the
   substring `PROVISIONED`, and the guest echoes the command line, which
   contains the marker text. Fix: never match on marker text; use the exit
   status of the command and disable the terminal echo with `stty -echo`.

8. **Concurrent builds corrupt the base image.** Running `make run` and
   `make provision` at the same time made `qemu-img convert` rewrite
   `img/hurd.base.qcow2` while QEMU had it open for writing; the result was a
   filesystem with inode and bitmap damage and an "Exec format error" for every
   binary in the guest. Fix: `flock img/.build.lock` around `base` and
   `provision`, build into a temporary file and `mv -f` it into place, and
   provision on a private copy.

9. **`apt-get` fails with "/dev/nullpartial is missing" (exit status 100).**
   Caused by `-o Dir::Cache::archives=/dev/null`, which makes apt look for the
   partial directory of the cache. Fix: keep the default cache location; the
   image has enough free space for the roughly 180 MB of downloaded packages,
   and `apt-get clean` reclaims it afterwards.

10. **`qemu-img create -b img/hurd.base.qcow2` fails** with
    "Could not open 'img/tmp/img/hurd.base.qcow2'". Fix: use
    `$(abspath $(BASE_IMG))`.

11. **make re-extracts the image on every run.** `tar` restores the older
    mtime of the image inside the tarball, so the raw image always looks older
    than the tarball. Fix: the `img/.extracted` stamp file.

12. **Stale FUSE mounts block the next run.** `partitionfs` fails with
    "mountpoint is not empty" when the mount directory still holds content, and
    removing such a directory fails with "Function not implemented". Fix:
    `fusermount -u` before cleanup, and use a fresh `mktemp -d` mount point per
    run.

13. **The guest has no virtio drivers.** With `if=virtio` the disk is never
    found. Fix: `if=ide` and `-device e1000` for the network.

14. **The guest does not boot unattended** without the GRUB changes of stage 2.

15. **Yescrypt hashes do not work on Hurd.** `/etc/login.defs` says
    `ENCRYPT_METHOD YESCRYPT`, but the Hurd libc `crypt` cannot verify `$y$`
    hashes, so logins fail. Fix: `openssl passwd -6` (`$6$`).

16. **The provisioned image is not clean.** See "Known issue" below.

17. **The guest prints "mount: cannot remount /: Device or resource busy"**
    during shutdown. This is harmless: the root filesystem cannot be remounted
    read-only, the guest halts anyway, and the metadata is repaired offline.

18. **The guest filesystem is small.** The root partition is about 3.0 GB with
    roughly 1.5 GB free, so package installations must not use a huge cache
    and should clean up afterwards.

19. **An open forwarded port does not mean that sshd is ready.** QEMU's
    `hostfwd` accepts the connection as soon as QEMU runs, and resets it when
    the guest has no sshd yet ("kex_exchange_identification: read: Connection
    reset by peer"). A check with `/dev/tcp` is therefore useless as a
    readiness test. Fix: retry the SSH login until it succeeds, which matters
    most without KVM, where the boot takes minutes.

## Known issue: ext2 without a journal

The root filesystem of the Hurd image is ext2 **without a journal**. The guest
creates and removes tens of thousands of files during `apt-get upgrade`, and
its shutdown path does not flush the inode and block bitmaps to disk. The
resulting image passes the boot test, but `e2fsck -fn` reports:

```
Deleted inode <n> has zero dtime.
Block bitmap differences: -(33875--34388) ...
Inode bitmap differences: -1691 -8193 ...
Free blocks count wrong ...
```

Facts established while investigating this:

- The pristine image is clean (`e2fsck -fn` exits 0), so the inconsistency is
  created by the guest, not by the download or by the offline preparation.
- Running `sync` (twice) in the guest before `poweroff` does **not** fix it.
- The metadata itself is intact; only the bookkeeping is stale, so
  `e2fsck -fy` repairs the image completely (a second `e2fsck -fn` then exits 0).

That is why `scripts/provision_base.sh` repairs the filesystem offline after the
guest has powered off. The repair needs the image as a raw file, so the
provisioned copy is converted to raw, repaired, and converted back to qcow2.

## How to update it

### Change the Hurd release

1. Set `VERSION` and `MD5_SUM` in `Makefile` (the MD5 of the new tarball).
2. `make download` to fetch and verify it.
3. Remove the old base image (`rm -f img/hurd.base.qcow2`) and run
   `make base && make provision`.
4. Check whether the new image still ships `/etc/ssh/sshd_config.d/`, whether
   the `root` account is still in `/etc/shadow`, and whether the GRUB layout in
   `prepare_base.sh` still matches the shipped `/boot/grub/grub.cfg`.

### Change the root password

Set `ROOT_PASSWORD` in the `Makefile`. It is passed to `prepare_base.sh`
(offline hash) and to `provision_base.sh` (SSH login). Changing it requires a
fresh base image: `rm -f img/hurd.base.qcow2 && make base && make provision`.

### Add or change offline file edits

Edit the `debugfs` command list in `scripts/prepare_base.sh`. Remember that
`debugfs write` cannot overwrite, so every file needs `rm` before `write`, and
that ownership has to be restored with `sif <file> uid/gid/mode` for files
outside `/etc` (the defaults are taken from the file being replaced).

### Add or change in-VM provisioning steps

Edit the `expect` block in `scripts/provision_base.sh` and add a `run_command`
call with a **unique** tag; the tag names the log file in the guest
(`/tmp/provision_<tag>.log`). `run_command` returns the exit status of the
command, so check it and fail the provisioning when it fails.

Steps that must run before the marker is written belong above
`run_command "touch /root/.provisioned" marker_write`; steps that must run on
every boot belong below it.

### Change the SSH port or the network device

`SSH_PORT` in the `Makefile` is the port on the host that is forwarded to port
22 in the guest; the guest's sshd always listens on 22. The network device is
`-device e1000` in `Makefile` and in `scripts/provision_base.sh`; the Hurd image
has no virtio driver, so keep the emulated e1000 or add another driver to the
guest.

### Change the disk layout

The root filesystem is partition 2. It is referenced in three places:
`root=part:2:device:wd0` in the GRUB config of `scripts/prepare_base.sh`, the
`PART_FILE="$WORK_DIR/2"` in the same script, and the partition number argument
of `scripts/repair_filesystem.sh` (default 2).

### Rebuild from scratch

```
make distclean     # removes img/ entirely, including the download
make base          # offline preparation, result: img/hurd.base.qcow2
make provision     # in-VM provisioning and offline filesystem repair
```

## Verification

Check that the root filesystem of the base image is clean:

```
qemu-img convert -f qcow2 -O raw img/hurd.base.qcow2 /tmp/base.raw
dd if=/tmp/base.raw of=/tmp/base.p2.img bs=512 skip=1953792 count=6236160 status=none
e2fsck -fn /tmp/base.p2.img      # expect exit status 0
```

Check that the base image survives a run unchanged:

```
md5sum img/hurd.base.qcow2
make run                           # boot the overlay, log in with SSH
md5sum img/hurd.base.qcow2        # must be identical
```

Check the SSH login from the host (no `sshpass` on this machine, so use
`expect`):

```
expect -c 'spawn ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o PreferredAuthentications=password -o PubkeyAuthentication=no \
    -p 2222 root@127.0.0.1 id; expect "password:"; send "abcdef\r"; expect eof'
```

## Constraints

- No root privileges: no loop mounts, no `guestfish`, no `virt-customize`.
- The downloaded image is immutable; every change is applied to a copy.
- The base image is only used as a read-only backing file for overlays.
- The root password is shared and weakly chosen; the image is for local
  experiments only, never expose the forwarded port to an untrusted network.
