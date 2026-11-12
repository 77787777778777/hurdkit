#!/usr/bin/env bash
# Repair the ext2 root filesystem of a raw disk image offline.
#
# The GNU/Hurd root filesystem is ext2 without a journal. After the guest
# has been running (package installation creates and removes a large number
# of files) its shutdown path leaves the inode and block bitmaps out of
# sync with the inode table, and e2fsck reports:
#
#   Deleted inode <n> has zero dtime.
#   Block bitmap differences: ...
#   Inode bitmap differences: ...
#
# The metadata is intact, only the bookkeeping is stale, so e2fsck can
# repair the image while it is not mounted. This is done after the guest
# has powered off, before the image is used as the base disk.
#
# Usage: repair_filesystem.sh <raw.img> [partition-number]

set -euo pipefail

IMG="${1:?Usage: $0 <raw.img> [partition-number]}"
PART_NUM="${2:-2}"
BLOCK_SIZE=512

if [[ ! -f "$IMG" ]]; then
    echo "Image not found: $IMG" >&2
    exit 1
fi

if ! command -v e2fsck >/dev/null 2>&1; then
    echo "e2fsck is required to repair the filesystem" >&2
    exit 1
fi

if ! command -v parted >/dev/null 2>&1; then
    echo "parted is required to locate the partition" >&2
    exit 1
fi

# Locate the partition: parted prints "number:start:end:size:..." in
# machine-readable mode, with the sector counts suffixed by "s".
PART_INFO="$(parted -s -m "$IMG" unit s print | awk -F: -v n="$PART_NUM" '$1 == n { print $2, $4; exit }')"

if [[ -z "$PART_INFO" ]]; then
    echo "Partition $PART_NUM not found in $IMG" >&2
    exit 1
fi

START_SECTORS="${PART_INFO%% *}"
SIZE_SECTORS="${PART_INFO##* }"
START_SECTORS="${START_SECTORS%s}"
SIZE_SECTORS="${SIZE_SECTORS%s}"

if [[ -z "$START_SECTORS" || -z "$SIZE_SECTORS" ]]; then
    echo "Could not parse partition geometry: $PART_INFO" >&2
    exit 1
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
PART_FILE="$TMP_DIR/partition.img"

echo "Repairing partition $PART_NUM (start $START_SECTORS, $SIZE_SECTORS sectors) of $IMG"

dd if="$IMG" of="$PART_FILE" bs="$BLOCK_SIZE" skip="$START_SECTORS" count="$SIZE_SECTORS" status=none

# -f: the filesystem is not mounted, but it carries the "needs recovery"
# flag; -y: answer every question with yes so that the repair is not
# interactive.
#
# e2fsck exits with 1 when it corrected errors and with 2 when it corrected
# errors and the filesystem should be rebooted; both are success here.
set +e
e2fsck -fy "$PART_FILE"
FSCK_RC=$?
set -e
if (( FSCK_RC & ~3 )); then
    echo "e2fsck failed with status $FSCK_RC" >&2
    exit 1
fi

# Write the repaired filesystem back into the disk image. conv=notrunc keeps
# the rest of the image (partition table, swap, bootloader) untouched.
dd if="$PART_FILE" of="$IMG" bs="$BLOCK_SIZE" seek="$START_SECTORS" count="$SIZE_SECTORS" conv=notrunc status=none

# Verify the repaired filesystem.
e2fsck -fn "$PART_FILE"

echo "Filesystem repaired: $IMG"
