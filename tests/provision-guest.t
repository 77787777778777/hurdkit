#!/usr/bin/env bash
# Existing Python provisioning driver regression test. No network or disk.
set -uo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_DIR="$(mktemp -d)"
trap 'rm -rf "$TEST_DIR"' EXIT
echo '1..1'
failures=0
if python3 -I - "$REPO_ROOT/scripts" <<'PY'
import sys
from unittest.mock import patch

sys.path.insert(0, sys.argv[1])
import provision_guest


class Session:
    def run_command(self, command, tag):
        if tag == "package_manifest":
            return 1
        if tag == "marker":
            return 0
        return 0


with patch.object(provision_guest, "disable_unreleased_suite"), \
     patch.object(provision_guest, "hold_hurd_dev"), \
     patch.object(provision_guest, "install_packages"), \
     patch.object(provision_guest, "rename_account"), \
     patch.object(provision_guest, "set_account_password"), \
     patch.object(provision_guest, "install_authorized_keys", return_value=[]):
    try:
        provision_guest.stage_packages(Session(), False, ["curl", "python3"])
    except provision_guest.GuestError as error:
        assert "manifest" in str(error), error
    else:
        raise AssertionError("a failed package manifest write was accepted")
PY
then
    echo 'ok 1 - a failed package manifest write fails provisioning'
else
    echo 'not ok 1 - a failed package manifest write fails provisioning'
    failures=1
fi
exit "$failures"
