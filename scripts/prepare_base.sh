#!/usr/bin/env bash
# Prepare the Hurd base image: copy the pristine raw disk image and apply
# offline changes to the copy (the downloaded image stays untouched).
#
# The root filesystem lives on partition 2 (ext2). partitionfs exposes the
# partitions of the disk image as regular files, and debugfs edits that file
# in place. fuse2fs cannot be used for writing because it enforces strict
# ownership, and root-owned files cannot be modified without root privileges.
#
# Changes applied offline (no VM boot required):
#   * /boot/grub/grub.cfg              - serial console + auto-boot
#   * /etc/default/grub                - same settings, for update-grub
#   * /etc/shadow                      - password hashes for root and demo
#   * /etc/ssh/sshd_config.d/10-local.conf - permit root password login
#
# The resulting image is expected to be converted to qcow2 by the caller.

set -euo pipefail

SRC_IMG="${1:?Usage: $0 <source.raw> <work.raw> [root_password]}"
WORK_IMG="${2:?Usage: $0 <source.raw> <work.raw> [root_password]}"
ROOT_PASSWORD="${3:-abcdef}"
PARTFS="${PARTFS:-/home/chz/src/partitionfs/build/partitionfs}"

if [[ ! -f "$SRC_IMG" ]]; then
    echo "Source image not found: $SRC_IMG" >&2
    exit 1
fi

if [[ ! -x "$PARTFS" ]]; then
    echo "partitionfs binary not found or not executable: $PARTFS" >&2
    exit 1
fi

if ! command -v debugfs >/dev/null 2>&1; then
    echo "debugfs is required for offline image preparation" >&2
    exit 1
fi

if ! command -v openssl >/dev/null 2>&1; then
    echo "openssl is required to generate the password hash" >&2
    exit 1
fi

# The password hash must use a crypt(3) method supported by the Hurd libc.
# $6$ (SHA-512) is supported by glibc; yescrypt ($y$) is not, even though
# /etc/login.defs names it as the preferred method.
PASSWORD_HASH="$(openssl passwd -6 -salt hurdhurd "$ROOT_PASSWORD")"

TMP_DIR="$(mktemp -d)"
# A dedicated mount point per run: a leftover FUSE mount from an earlier run
# would make partitionfs fail with "mountpoint is not empty".
WORK_DIR="$(mktemp -d "$(dirname "$WORK_IMG")/partitions.XXXXXX")"
PARTFS_PID=""
cleanup() {
    fusermount -u "$WORK_DIR" 2>/dev/null || true
    if [[ -n "$PARTFS_PID" ]]; then
        kill "$PARTFS_PID" 2>/dev/null || true
        wait "$PARTFS_PID" 2>/dev/null || true
    fi
    rmdir "$WORK_DIR" 2>/dev/null || true
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

# --- New grub.cfg: serial console, auto-boot, no timeout ---
cat > "$TMP_DIR/grub.cfg" <<'GRUBCFG'
set default=0
set timeout=0
set terminal=console

menuentry "GNU/Hurd" {
    multiboot /boot/gnumach-1.8-amd64-up.gz root=part:2:device:wd0 console=com0
    module /hurd/pci-arbiter.static pci-arbiter --host-priv-port='${host-port}' --device-master-port='${device-port}' --next-task='${acpi-task}' '$(pci-task=task-create)' '$(task-resume)'
    module /hurd/acpi.static acpi --next-task='${disk-task}' '$(acpi-task=task-create)'
    module /hurd/rumpdisk.static rumpdisk --next-task='${fs-task}' '$(disk-task=task-create)'
    module /hurd/ext2fs.static ext2fs --multiboot-command-line='${kernel-command-line}' --exec-server-task='${exec-task}' -T typed '${root}' '$(fs-task=task-create)'
    module /lib/ld-x86-64.so.1 exec /hurd/exec '$(exec-task=task-create)'
}
GRUBCFG

# --- New /etc/default/grub: persistent serial console ---
cat > "$TMP_DIR/default_grub" <<'GRUBDEF'
GRUB_DEFAULT=0
GRUB_TIMEOUT=0
GRUB_DISTRIBUTOR=$( . /etc/os-release && echo ${NAME} )
GRUB_CMDLINE_LINUX_DEFAULT=""
GRUB_CMDLINE_LINUX="console=ttyS0"
GRUB_CMDLINE_GNUMACH="console=com0"
GRUB_TERMINAL=console
GRUB_DISABLE_OS_PROBER=true
GRUB_DISABLE_RECOVERY=true
GRUBDEF

# --- sshd drop-in: allow root to authenticate with a password ---
# OpenSSH refuses password logins for root unless this is set explicitly,
# and refuses empty passwords unless PermitEmptyPasswords is enabled.
cat > "$TMP_DIR/sshd_local.conf" <<'SSHDCONF'
# Permit root to log in with a password so the image can be provisioned
# over SSH without an interactive console.
PermitRootLogin yes
PasswordAuthentication yes
SSHDCONF

# Work on a copy: the downloaded image must stay pristine so that the
# preparation can be repeated from a known state.
cp "$SRC_IMG" "$WORK_IMG"
# libparted probes the image through the block layer, which ignores the
# page cache; without flushing the copy the partition table reads back empty
# and partitionfs exposes no partitions.
sync "$WORK_IMG"

# Expose partitions as numbered files
"$PARTFS" "$WORK_IMG" "$WORK_DIR" &
PARTFS_PID=$!

# Wait for partition file to appear
for _ in $(seq 1 10); do
    if [[ -f "$WORK_DIR/2" ]]; then
        break
    fi
    sleep 1
done

if [[ ! -f "$WORK_DIR/2" ]]; then
    echo "Partition 2 not found after partitionfs mount" >&2
    exit 1
fi

PART_FILE="$WORK_DIR/2"

# --- Patch /etc/shadow in place ---
# The shipped file has one line per account and nine colon-separated
# fields. Rewriting it from scratch drops the accounts and produces
# malformed entries, which breaks logins for every user.
# Dump it, replace only the password field of root and demo, and write it
# back with the original ownership and permissions.
debugfs -R "dump /etc/shadow $TMP_DIR/shadow.orig" "$PART_FILE" 2>/dev/null

awk -F: -v OFS=: -v hash="$PASSWORD_HASH" '
    $1 == "root" || $1 == "demo" { $2 = hash }
    { print }
' "$TMP_DIR/shadow.orig" > "$TMP_DIR/shadow.new"

# Build debugfs command script (no shell syntax, no comments)
CMDS_FILE="$TMP_DIR/debugfs.cmds"
{
    echo "rm /boot/grub/grub.cfg"
    echo "write $TMP_DIR/grub.cfg /boot/grub/grub.cfg"
    echo "rm /etc/default/grub"
    echo "write $TMP_DIR/default_grub /etc/default/grub"
    echo "rm /etc/shadow"
    echo "write $TMP_DIR/shadow.new /etc/shadow"
    echo "sif /etc/shadow mode 0100640"
    echo "sif /etc/shadow uid 0"
    echo "sif /etc/shadow gid 42"
    echo "rm /etc/ssh/sshd_config.d/10-local.conf"
    echo "write $TMP_DIR/sshd_local.conf /etc/ssh/sshd_config.d/10-local.conf"
    echo "sif /etc/ssh/sshd_config.d/10-local.conf mode 0100644"
    echo "sif /etc/ssh/sshd_config.d/10-local.conf uid 0"
    echo "sif /etc/ssh/sshd_config.d/10-local.conf gid 0"
    echo "quit"
} > "$CMDS_FILE"

debugfs -w -f "$CMDS_FILE" "$PART_FILE"

echo "Base image prepared offline: $WORK_IMG"
