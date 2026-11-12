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
# The CPU count and the memory size are configurable so that the guest
# used for the provisioning runs with the same resources as the guest that
# is booted from the resulting image.
CPU_COUNT="${CPU_COUNT:-4}"
MEMORY="${MEMORY:-2G}"

# The package upgrade is off by default. The sid archive currently ships a
# hurd-dev that cannot be unpacked on Hurd (see the hurd-dev comment in
# the expect block below), and a usable image does not need the upgrade.
# Set RUN_APT_UPGRADE=1 to enable it again.
RUN_APT_UPGRADE="${RUN_APT_UPGRADE:-0}"

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
cleanup() {
    if [[ -n "$QEMU_PID" ]] && kill -0 "$QEMU_PID" 2>/dev/null; then
        kill "$QEMU_PID" 2>/dev/null || true
        wait "$QEMU_PID" 2>/dev/null || true
    fi
    rm -f "$QEMU_LOG" "$WORK_IMG"
}
trap cleanup EXIT

# A standalone copy: the guest writes to it directly, and the base image is
# only replaced once the guest has powered off successfully.
qemu-img convert -f qcow2 -O qcow2 "$BASE_IMG" "$WORK_IMG"

# KVM is used when it is available; without it the guest runs under
# emulation, which is much slower but works.
QEMU_ACCEL=()
if [[ -r /dev/kvm && -w /dev/kvm ]]; then
    QEMU_ACCEL=(-enable-kvm -cpu host)
else
    QEMU_ACCEL=(-cpu max)
    echo "KVM is not available, running the guest without acceleration" >&2
fi

qemu-system-x86_64 \
    "${QEMU_ACCEL[@]}" \
    -smp "$CPU_COUNT" \
    -m "$MEMORY" \
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
    RUN_APT_UPGRADE="$RUN_APT_UPGRADE" \
    expect - <<'EXPECT'
set ssh_host $env(SSH_HOST)
set ssh_port $env(SSH_PORT)
set password $env(ROOT_PASSWORD)

# The login phase has its own short timeout: the forwarded port accepts
# connections before sshd in the guest is ready, and a connection that is
# reset in the meantime would otherwise block for the whole session
# timeout instead of being retried.
#
# Exit codes of this script:
#   1 - the session could not be established (retryable)
#   2 - a command failed (not retryable, the guest needs a fresh start)
set timeout 120

spawn ssh \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    -o PreferredAuthentications=password \
    -o PubkeyAuthentication=no \
    -p $ssh_port root@$ssh_host

expect {
    -re "(?i)password:" { send "$password\r" }
    eof { puts "Connection closed before the password prompt"; exit 1 }
    timeout { puts "TIMEOUT waiting for password prompt"; exit 1 }
}

# The root shell prompt ends with "# ". A repeated password prompt means
# that the login was rejected.
expect {
    -re {# $} { }
    -re "(?i)password:" { puts "The login was rejected"; exit 1 }
    eof { puts "Connection closed during the login"; exit 1 }
    timeout { puts "TIMEOUT waiting for shell prompt"; exit 1 }
}
# The provisioning commands take minutes, so they get a long timeout.
set timeout 3600

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
        eof { puts "Connection closed while running: $cmd"; exit 1 }
        timeout { puts "TIMEOUT running: $cmd"; exit 1 }
    }
    expect {
        -re {# $} { }
        eof { puts "Connection closed while running: $cmd"; exit 1 }
        timeout { puts "TIMEOUT waiting for prompt after: $cmd"; exit 1 }
    }
    return $rc
}

# The image ships with an "unreleased" suite enabled. The packages of that
# suite are not stable, so it is removed before any package is fetched.
puts "Disabling the unreleased suite..."
run_command {sed -i '/unreleased/d' /etc/apt/sources.list} sources_prune
run_command {for f in /etc/apt/sources.list.d/*.list; do [ -e "$f" ] && sed -i '/unreleased/d' "$f"; done; true} sources_prune_d
set rc [run_command {if [ -d /etc/apt/sources.list.d ]; then grep -rqs unreleased /etc/apt/sources.list /etc/apt/sources.list.d; else grep -qs unreleased /etc/apt/sources.list; fi} sources_check]
if { $rc == 0 } {
    puts "The unreleased suite is still enabled in the apt sources"
    exit 2
}

# The hurd-dev package from sid cannot be unpacked on Hurd: dpkg fails
# with "unable to install new version of .../linux-headers/linux/hdlc",
# because the new version wants to replace a directory with a file. The
# package is held at the installed version and pinned below zero, so that
# an upgrade cannot select the broken version.
puts "Holding hurd-dev..."
run_command {apt-mark hold hurd-dev} hurd_dev_hold
run_command {mkdir -p /etc/apt/preferences.d && echo 'Package: hurd-dev' > /etc/apt/preferences.d/99-hurd-dev && echo 'Pin: release *' >> /etc/apt/preferences.d/99-hurd-dev && echo 'Pin-Priority: -1' >> /etc/apt/preferences.d/99-hurd-dev} hurd_dev_pin

# The package upgrade is off by default: the sid archive currently ships a
# hurd-dev that cannot be unpacked on Hurd, and a usable image does not
# need the upgrade. Set RUN_APT_UPGRADE=1 to enable it again.
set run_apt 0
if {[info exists env(RUN_APT_UPGRADE)] && $env(RUN_APT_UPGRADE) eq "1"} {
    set run_apt 1
}

# Check if already provisioned. The exit status of the test is the result,
# so no marker text has to be matched at all.
set rc [run_command "test -f /root/.provisioned" marker]
if { $rc == 0 } {
    puts "Image is already provisioned."
} else {
    if { $run_apt } {
        # Run setup commands from the Hurd image instructions.
        #
        # The downloaded .deb files stay in the package cache, which needs
        # around 180 MB; the guest filesystem has enough room for that.
        set apt_opts "-o APT::Install-Recommends=false"
        puts "Running apt update..."
        set rc [run_command "DEBIAN_FRONTEND=noninteractive apt-get $apt_opts update" apt_update]
        if { $rc != 0 } {
            puts "apt-get update failed with status $rc"
            exit 2
        }
        puts "Running apt upgrade..."
        set rc [run_command "DEBIAN_FRONTEND=noninteractive apt-get $apt_opts -y -o Dpkg::Options::=--force-confold upgrade" apt_upgrade]
        if { $rc != 0 } {
            puts "apt-get upgrade failed with status $rc"
            puts "The output of the failed command is in /tmp/provision_apt_upgrade.log in the guest"
            # A failed upgrade leaves dpkg in an inconsistent state. Repairing
            # it in the guest is worth one attempt, but a second failure needs
            # a fresh guest: the ext2 filesystem of the image has no journal
            # and does not survive a failed unpack.
            run_command "dpkg --configure -a" dpkg_configure
            set rc [run_command "apt-get -f -y install" apt_fix]
            if { $rc != 0 } {
                puts "apt-get -f install failed with status $rc"
                exit 2
            }
        }
        # Reclaim the space taken by the downloaded packages
        run_command "apt-get clean" apt_clean
    } else {
        puts "Skipping apt update and apt upgrade (RUN_APT_UPGRADE is not set)."
    }
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
# The connection is closed when the guest is gone. The end of the file may
# already have been seen above, in which case there is nothing left to wait
# for and expect would fail on the closed spawn.
catch { expect eof }
EXPECT
    then
        SSH_RC=0
        break
    else
        # The exit status of the failed attempt has to be read here: after
        # the fi it is the status of the if statement, which is 0 even when
        # the attempt failed.
        SSH_RC=$?
    fi
    if (( SSH_RC == 2 )); then
        # A command failed in the guest. The same guest cannot fix that,
        # so retrying would only repeat the failure.
        echo "The provisioning failed in the guest, not retrying" >&2
        exit 1
    fi
    echo "SSH session failed (attempt $attempt), retrying in 10 seconds..."
    sleep 10
done

if (( SSH_RC != 0 )); then
    echo "Could not establish an SSH session with the guest" >&2
    exit 1
fi

# Wait for QEMU to shut down cleanly after the guest powers off. The wait
# is bounded: a guest that does not power off would otherwise block the
# whole pipeline forever.
POWER_OFF_TIMEOUT=300
for _ in $(seq 1 $((POWER_OFF_TIMEOUT / 5))); do
    if ! kill -0 "$QEMU_PID" 2>/dev/null; then
        break
    fi
    sleep 5
done

if kill -0 "$QEMU_PID" 2>/dev/null; then
    echo "The guest did not power off within ${POWER_OFF_TIMEOUT}s, terminating QEMU" >&2
    kill "$QEMU_PID" 2>/dev/null || true
    sleep 5
    kill -9 "$QEMU_PID" 2>/dev/null || true
    wait "$QEMU_PID" 2>/dev/null || true
    QEMU_PID=""
    echo "QEMU log:" >&2
    cat "$QEMU_LOG" >&2
    exit 1
fi

wait "$QEMU_PID" 2>/dev/null || true
QEMU_PID=""

# Repair the filesystem of the provisioned copy. The guest ran apt-get
# upgrade, which created and removed thousands of files; without a journal
# the shutdown leaves stale inode and block bitmaps behind. The repair runs
# on the image itself: qemu-nbd serves it, nbdfuse exposes it as a file and
# partitionfs exposes the partition, so no conversion is needed.
bash "$SCRIPT_DIR/repair_filesystem.sh" "$WORK_IMG"

# Replace the base image with the provisioned copy.
mv -f "$WORK_IMG" "$BASE_IMG"
trap - EXIT
rm -f "$QEMU_LOG"

echo "Provisioning complete."
