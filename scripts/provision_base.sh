#!/usr/bin/env bash
# Provision the Hurd base qcow2 image by booting it once and applying
# the setup steps from the Debian GNU/Hurd image instructions:
#   https://www.gnu.org/software/hurd/hurd/running/flash_qemu_img.html
#
# The VM is booted and driven over SSH:
#   1. Wait for sshd to accept connections
#   2. Log in as root with the password set during offline preparation
#   3. Check the provision marker; if absent, run apt update/upgrade
#   4. Power off
#
# A marker file at /root/.provisioned makes the script idempotent.
#
# The provisioning runs on a private copy of the base image. Only a
# successful run replaces the base image, so a failed or interrupted
# provisioning never leaves a half-written base image behind.
#
# After the guest has powered off, the ext2 filesystem is repaired offline.
# The root filesystem of the Hurd image has no journal, and the shutdown
# path of the guest leaves the inode and block bitmaps out of sync with the
# inode table, which makes e2fsck complain about the resulting image.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_IMG="${1:?Usage: $0 <base.qcow2> [ssh_port]}"
SSH_PORT="${2:-2222}"
SSH_HOST="127.0.0.1"
ROOT_PASSWORD="${ROOT_PASSWORD:-abcdef}"

if [[ ! -f "$BASE_IMG" ]]; then
    echo "Base image not found: $BASE_IMG" >&2
    exit 1
fi

if ! command -v expect >/dev/null 2>&1; then
    echo "expect is required for provisioning" >&2
    exit 1
fi

# Resolve to absolute paths so the drive paths work from any cwd
BASE_IMG="$(cd "$(dirname "$BASE_IMG")" && pwd)/$(basename "$BASE_IMG")"
BASE_DIR="$(dirname "$BASE_IMG")"

# The working copy lives next to the base image so that replacing the base
# image is a rename within the same filesystem.
WORK_IMG="$(mktemp -p "$BASE_DIR" .provision.XXXXXX.qcow2)"
QEMU_LOG="$(mktemp)"
QEMU_PID=""
REPAIR_RAW="$(mktemp -p "$BASE_DIR" .repair.XXXXXX.raw)"
REPAIRED_IMG="$(mktemp -p "$BASE_DIR" .repaired.XXXXXX.qcow2)"
rm -f "$REPAIR_RAW" "$REPAIRED_IMG"
cleanup() {
    if [[ -n "$QEMU_PID" ]] && kill -0 "$QEMU_PID" 2>/dev/null; then
        kill "$QEMU_PID" 2>/dev/null || true
        wait "$QEMU_PID" 2>/dev/null || true
    fi
    rm -f "$QEMU_LOG" "$WORK_IMG" "$REPAIR_RAW" "$REPAIRED_IMG"
}
trap cleanup EXIT

# A standalone copy: the guest writes to it directly, and the base image is
# only replaced once the guest has powered off successfully.
qemu-img convert -f qcow2 -O qcow2 "$BASE_IMG" "$WORK_IMG"

qemu-system-x86_64 \
    -enable-kvm \
    -cpu host \
    -smp 2 \
    -m 2G \
    -drive file="$WORK_IMG",if=ide,cache=writeback \
    -netdev user,id=net0,hostfwd=tcp:"$SSH_HOST":"$SSH_PORT"-:22 \
    -device e1000,netdev=net0 \
    -nographic > "$QEMU_LOG" 2>&1 &
QEMU_PID=$!

# Wait for the SSH port to accept connections. sshd starts during boot,
# so this usually succeeds within a few seconds.
port_open() {
    (exec 3<>"/dev/tcp/$SSH_HOST/$SSH_PORT") 2>/dev/null
}

for _ in $(seq 1 120); do
    if port_open; then
        break
    fi
    if ! kill -0 "$QEMU_PID" 2>/dev/null; then
        echo "QEMU exited during boot:" >&2
        cat "$QEMU_LOG" >&2
        exit 1
    fi
    sleep 2
done

if ! port_open; then
    echo "Timed out waiting for SSH on port $SSH_PORT" >&2
    cat "$QEMU_LOG" >&2
    exit 1
fi

# Drive the guest over SSH:
#   1. Log in as root with the password set during offline preparation
#   2. Check the provision marker; if absent, run apt update/upgrade
#   3. Write the marker and power off
#
# Terminal echo is disabled first: otherwise the echoed command line is
# indistinguishable from the command output when matching markers.
#
# The forwarded port accepts connections as soon as QEMU is running, but
# sshd in the guest only listens a little later. The login is therefore
# retried until the guest is ready.
SSH_RC=0
for attempt in $(seq 1 12); do
    if SSH_HOST="$SSH_HOST" SSH_PORT="$SSH_PORT" ROOT_PASSWORD="$ROOT_PASSWORD" \
    expect - <<'EXPECT'
set ssh_host $env(SSH_HOST)
set ssh_port $env(SSH_PORT)
set password $env(ROOT_PASSWORD)

set timeout 3600

spawn ssh \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    -o PreferredAuthentications=password \
    -o PubkeyAuthentication=no \
    -p $ssh_port root@$ssh_host

expect {
    -re "(?i)password:" { send "$password\r" }
    timeout { puts "TIMEOUT waiting for password prompt"; exit 1 }
}

# The root shell prompt ends with "# ".
expect {
    -re {# $} { }
    timeout { puts "TIMEOUT waiting for shell prompt"; exit 1 }
}

# Stop echoing the command lines so that markers cannot match their own
# echo; the shell prompt is the only reliable synchronisation point.
send "stty -echo\r"
expect {
    -re {# $} { }
    timeout { puts "TIMEOUT disabling terminal echo"; exit 1 }
}

# Run a command, capture its exit status and its output, and return the
# exit status. The output is left in the guest's /tmp for inspection.
proc run_command {cmd tag} {
    set logfile "/tmp/provision_${tag}.log"
    send "$cmd >$logfile 2>&1; rc=\$?; echo \"__RC__\${rc}__\"\r"
    expect {
        -re {__RC__(-?[0-9]+)__} { set rc $expect_out(1,string) }
        timeout { puts "TIMEOUT running: $cmd"; exit 1 }
    }
    expect {
        -re {# $} { }
        timeout { puts "TIMEOUT waiting for prompt after: $cmd"; exit 1 }
    }
    return $rc
}

# Check if already provisioned. The exit status of the test is the result,
# so no marker text has to be matched at all.
set rc [run_command "test -f /root/.provisioned" marker]
if { $rc == 0 } {
    puts "Image is already provisioned."
} else {
    # Run setup commands from the Hurd image instructions.
    #
    # The downloaded .deb files stay in the package cache, which needs
    # around 180 MB; the guest filesystem has enough room for that.
    set apt_opts "-o APT::Install-Recommends=false"
    puts "Running apt update..."
    set rc [run_command "DEBIAN_FRONTEND=noninteractive apt-get $apt_opts update" apt_update]
    if { $rc != 0 } {
        puts "apt-get update failed with status $rc"
        exit 1
    }
    puts "Running apt upgrade..."
    set rc [run_command "DEBIAN_FRONTEND=noninteractive apt-get $apt_opts -y -o Dpkg::Options::=--force-confold upgrade" apt_upgrade]
    if { $rc != 0 } {
        puts "apt-get upgrade failed with status $rc"
        exit 1
    }
    # Reclaim the space taken by the downloaded packages
    run_command "apt-get clean" apt_clean
    # Write the provision marker
    run_command "touch /root/.provisioned" marker_write
}

# Power the guest off. The SSH connection dies together with the guest, so
# neither the exit status nor the shell prompt can be waited for reliably.
send "poweroff\r"
expect {
    -re {__RC__(-?[0-9]+)__} { }
    eof { }
    timeout { puts "TIMEOUT waiting for poweroff"; exit 1 }
}
expect {
    eof { }
    timeout { puts "TIMEOUT waiting for the connection to close"; exit 1 }
}
EXPECT
    then
        SSH_RC=0
        break
    fi
    SSH_RC=$?
    echo "SSH session failed (attempt $attempt), retrying in 10 seconds..."
    sleep 10
done

if (( SSH_RC != 0 )); then
    echo "Could not establish an SSH session with the guest" >&2
    exit 1
fi

# Wait for QEMU to shut down cleanly after the guest powers off
wait "$QEMU_PID" 2>/dev/null || true
QEMU_PID=""

# Repair the filesystem of the provisioned copy. The guest ran apt-get
# upgrade, which created and removed thousands of files; without a journal
# the shutdown leaves stale inode and block bitmaps behind. The repair is
# done on the raw image and the result is converted back to qcow2.
qemu-img convert -f qcow2 -O raw "$WORK_IMG" "$REPAIR_RAW"
bash "$SCRIPT_DIR/repair_filesystem.sh" "$REPAIR_RAW"
qemu-img convert -f raw -O qcow2 "$REPAIR_RAW" "$REPAIRED_IMG"
mv -f "$REPAIRED_IMG" "$WORK_IMG"
rm -f "$REPAIR_RAW"

# Replace the base image with the provisioned copy.
mv -f "$WORK_IMG" "$BASE_IMG"
trap - EXIT
rm -f "$QEMU_LOG"

echo "Provisioning complete."
