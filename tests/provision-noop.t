#!/usr/bin/env bash
# TAP regression test for repeat provisioning. Uses a tiny raw ext2 image;
# never boots QEMU and never touches img/hurd.base.qcow2.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_DIR="$(mktemp -d)"
trap 'rm -rf "$TEST_DIR"' EXIT
mkdir -p "$TEST_DIR/bin" "$TEST_DIR/img/keys"
BASE="$TEST_DIR/img/hurd.base.qcow2"
export TEST_DIR BASE

# The synthetic root is partition 2, like the Debian image.
for tool in sfdisk mke2fs debugfs qemu-img; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "1..0 # SKIP $tool is not installed"
        exit 0
    fi
done
truncate -s 16M "$TEST_DIR/disk.raw"
# Match the Debian image: partition 1 is swap, partition 2 is the root.
printf 'label: dos\n,1M,S\n,11M,L\n' | sfdisk -q "$TEST_DIR/disk.raw"
mke2fs -q -F -E offset=2097152 "$TEST_DIR/disk.raw" 11M
PART="$TEST_DIR/disk.raw?offset=2097152"
debugfs -w -R 'mkdir /root' "$PART" >/dev/null 2>&1
debugfs -w -R 'mkdir /root/.ssh' "$PART" >/dev/null 2>&1
debugfs -w -R 'mkdir /home' "$PART" >/dev/null 2>&1
debugfs -w -R 'mkdir /home/peon' "$PART" >/dev/null 2>&1
debugfs -w -R 'mkdir /home/peon/.ssh' "$PART" >/dev/null 2>&1
for account in root peon; do
    printf 'ssh-ed25519 AAAATEST-%s hurdkit-test\n' "$account" \
        > "$TEST_DIR/img/keys/${account}_ed25519.pub"
    printf 'present\n' > "$TEST_DIR/img/keys/${account}_ed25519"
    home="/$account"
    [[ "$account" == root ]] || home="/home/$account"
    if ! debugfs -w -R "write $TEST_DIR/img/keys/${account}_ed25519.pub $home/.ssh/authorized_keys" \
        "$PART" >/dev/null 2>&1; then
        echo "Bail out! Could not create $home/.ssh/authorized_keys"
        exit 1
    fi
done
printf 'provisioned\n' > "$TEST_DIR/marker"
debugfs -w -R "write $TEST_DIR/marker /root/.provisioned" "$PART" >/dev/null 2>&1
printf 'curl python3\n' > "$TEST_DIR/packages"
debugfs -w -R "write $TEST_DIR/packages /root/.provision-packages" "$PART" >/dev/null 2>&1
qemu-img convert -f raw -O qcow2 "$TEST_DIR/disk.raw" "$BASE"
sha256sum "$BASE" > "$TEST_DIR/before.sha"

cat > "$TEST_DIR/bin/python3" <<'SH'
#!/usr/bin/env bash
if [[ "${1:-}" == "-c" ]]; then exit 0; fi
printf 'the guest provisioning driver must not run\n' >&2
exit 90
SH
cat > "$TEST_DIR/bin/qemu-system-x86_64" <<'SH'
#!/usr/bin/env bash
printf 'QEMU must not run for a no-op\n' >&2
exit 91
SH
chmod +x "$TEST_DIR/bin/"*
trace=()
if [[ "${DEBUG_PROVISION_NOOP:-0}" == 1 ]]; then
    trap 'printf "probe kept at %s\n" "$TEST_DIR" >&2' EXIT
    trace=(-x)
fi
PATH="$TEST_DIR/bin:$PATH" RUN_APT_UPGRADE=0 GUEST_PACKAGES='curl python3' \
    bash "${trace[@]}" "$REPO_ROOT/scripts/provision_base.sh" "$BASE" 2222 \
    >"$TEST_DIR/output" 2>&1
rc=$?

echo '1..7'
failures=0
if (( rc == 0 )); then echo 'ok 1 - repeat provisioning succeeds'; else
    echo "not ok 1 - repeat provisioning exits $rc"
    sed 's/^/# /' "$TEST_DIR/output"
    failures=1
fi
if sha256sum -c "$TEST_DIR/before.sha" >/dev/null 2>&1; then
    echo 'ok 2 - base image is byte-for-byte unchanged'
else
    echo 'not ok 2 - base image was replaced'
    failures=1
fi
if grep -q 'already provisioned' "$TEST_DIR/output"; then
    echo 'ok 3 - no-op is reported'
else
    echo 'not ok 3 - no-op is not reported'
    sed 's/^/# /' "$TEST_DIR/output"
    failures=1
fi

# Changes to a key or the requested packages must reach the full
# provisioning path. The fake QEMU fails deliberately before booting, so
# these checks cannot modify the fixture or hang waiting for a guest.
# provision_base.sh masks the fake's status while diagnosing the failed
# boot; assert its message, not the resulting exit code.
printf 'ssh-ed25519 AAAATEST-new-peon hurdkit-test\n' \
    > "$TEST_DIR/img/keys/peon_ed25519.pub"
PATH="$TEST_DIR/bin:$PATH" RUN_APT_UPGRADE=0 GUEST_PACKAGES='curl python3' \
    bash "$REPO_ROOT/scripts/provision_base.sh" "$BASE" 2222 \
    >"$TEST_DIR/key-change-output" 2>&1
rc=$?
if grep -q 'QEMU must not run for a no-op' "$TEST_DIR/key-change-output"; then
    echo 'ok 4 - a changed key requires provisioning'
else
    echo "not ok 4 - changed key did not reach provisioning (exit $rc)"
    failures=1
fi
printf 'ssh-ed25519 AAAATEST-peon hurdkit-test\n' \
    > "$TEST_DIR/img/keys/peon_ed25519.pub"
PATH="$TEST_DIR/bin:$PATH" RUN_APT_UPGRADE=0 GUEST_PACKAGES='curl python3 gdb' \
    bash "$REPO_ROOT/scripts/provision_base.sh" "$BASE" 2222 \
    >"$TEST_DIR/package-change-output" 2>&1
rc=$?
if grep -q 'QEMU must not run for a no-op' "$TEST_DIR/package-change-output"; then
    echo 'ok 5 - a changed package list requires provisioning'
else
    echo "not ok 5 - changed packages did not reach provisioning (exit $rc)"
    failures=1
fi
# Old images have the marker but no package manifest. Treat only the
# original default package set as unchanged; a different set still boots.
debugfs -w -R 'rm /root/.provision-packages' "$PART" >/dev/null 2>&1
PATH="$TEST_DIR/bin:$PATH" RUN_APT_UPGRADE=0 GUEST_PACKAGES='curl python3' \
    bash "$REPO_ROOT/scripts/provision_base.sh" "$BASE" 2222 \
    >"$TEST_DIR/legacy-output" 2>&1
rc=$?
if (( rc == 0 )) && grep -q 'already provisioned' "$TEST_DIR/legacy-output" &&
   sha256sum -c "$TEST_DIR/before.sha" >/dev/null 2>&1; then
    echo 'ok 6 - legacy default packages are an unchanged no-op'
else
    echo 'not ok 6 - legacy default packages are not an unchanged no-op'
    failures=1
fi
PATH="$TEST_DIR/bin:$PATH" RUN_APT_UPGRADE=0 GUEST_PACKAGES='curl' \
    bash "$REPO_ROOT/scripts/provision_base.sh" "$BASE" 2222 \
    >"$TEST_DIR/legacy-change-output" 2>&1
rc=$?
if grep -q 'QEMU must not run for a no-op' "$TEST_DIR/legacy-change-output"; then
    echo 'ok 7 - a legacy non-default package list requires provisioning'
else
    echo "not ok 7 - legacy package change did not reach provisioning (exit $rc)"
    failures=1
fi
exit "$failures"
