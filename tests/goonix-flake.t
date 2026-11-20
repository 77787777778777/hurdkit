#!/usr/bin/env bash
# TAP: the goonix flake entry point evaluates the same Hurd system as the
# established declaration. This builds derivations, not an image or a VM.
set -uo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || exit 1
GUIX="${GUIX:-guix}"
export GUILE_AUTO_COMPILE=0
export LC_ALL=C.UTF-8

if ! "$GUIX" flake --help >/dev/null 2>&1; then
    echo '1..0 # SKIP this goonix has no flake evaluator'
    exit 0
fi
if [[ ! -f flake.lock ]]; then
    echo '1..0 # SKIP run guix flake lock with the final goonix revision first'
    exit 0
fi

number=0
failures=0
result() {
    local rc="$1" description="$2" log="$3"
    number=$((number + 1))
    if ((rc == 0)); then
        echo "ok $number - $description"
    else
        echo "not ok $number - $description"
        sed 's/^/# /' "$log" | tail -n 25
        failures=$((failures + 1))
    fi
}

log="$(mktemp)"
trap 'rm -f "$log"' EXIT
: > "$log"
if "$GUIX" flake lock >"$log" 2>&1; then
    result 0 'locking only verifies the committed lock' "$log"
else
    result 1 'locking only verifies the committed lock' "$log"
fi

: > "$log"
if "$GUIX" flake show >"$log" 2>&1 && \
        grep -q '^packages hurd ' "$log" && \
        grep -q '^systems hurd64 ' "$log" && \
        grep -q '^images hurd64 ' "$log" && \
        grep -q '^shells default ' "$log"; then
    result 0 'show lists the typed project outputs' "$log"
else
    result 1 'show lists the typed project outputs' "$log"
fi

if [[ ! -f img/keys/root_ed25519.pub || ! -f img/keys/peon_ed25519.pub ]]; then
    number=$((number + 1))
    echo "ok $number - flake image equals the established declaration # SKIP no img/keys"
else
    flake_drv=''
    file_drv=''
    : > "$log"
    flake_drv="$("$GUIX" system image -d .#hurd64 2>>"$log")"
    flake_rc=$?
    file_drv="$("$GUIX" system image -L guix/modules -t hurd64-qcow2 \
        -d guix/hurd64.scm 2>>"$log")"
    file_rc=$?
    if ((flake_rc == 0 && file_rc == 0)) && \
            [[ -n "$flake_drv" && "$flake_drv" == "$file_drv" ]]; then
        result 0 'flake image equals the established declaration' "$log"
    else
        result 1 'flake image equals the established declaration' "$log"
        echo "# flake=$flake_drv file=$file_drv"
    fi
fi

echo "1..$number"
exit "$failures"
