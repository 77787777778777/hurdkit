#!/usr/bin/env python3
"""Drive the Hurd guest over SSH while the base image is provisioned.

The script logs in as root, prepares the apt sources of the guest and,
when it is asked to, runs the package upgrade. It does the same work as
the expect script that used to live in scripts/provision_base.sh, but
without the quoting games of expect and Tcl.

The configuration comes from the environment:

    SSH_HOST          the host that the SSH port of the guest is
                      forwarded to (default 127.0.0.1)
    SSH_PORT          the forwarded SSH port (default 2222)
    ROOT_PASSWORD     the password of the root account
    RUN_APT_UPGRADE   "1" runs "apt-get update" and "apt-get upgrade"
    GUEST_PACKAGES    the packages that the guest gets (default
                      "curl python3")

Exit codes:

    0   the guest was provisioned
    1   the session could not be established, the caller may retry
    2   a command failed in the guest, retrying with the same guest
        cannot help
"""

import os
import re
import sys

try:
    import pexpect
except ImportError:
    sys.exit("pexpect is required for the provisioning (pip install pexpect)")

# The forwarded port accepts connections before sshd in the guest is
# ready, so the login phase gets a timeout of its own.
LOGIN_TIMEOUT = 120
# The package upgrade takes minutes.
COMMAND_TIMEOUT = 3600

PROMPT = re.compile(r"# $")
PASSWORD_PROMPT = re.compile(r"(?i)password:")
EXIT_STATUS = re.compile(r"__RC__(-?[0-9]+)__")


class GuestError(Exception):
    """A command in the guest failed.

    The exit code tells the caller whether a retry can help: a session
    that could not be established may work on the next attempt, a failed
    command will fail again in the same guest.
    """

    def __init__(self, message, exit_code=2):
        super().__init__(message)
        self.exit_code = exit_code


class GuestSession:
    """An SSH session with the guest, driven through pexpect."""

    def __init__(self, host, port, password):
        self.child = pexpect.spawn(
            "ssh",
            [
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", "PreferredAuthentications=password",
                "-o", "PubkeyAuthentication=no",
                "-p", str(port),
                f"root@{host}",
            ],
            encoding="utf-8",
            timeout=LOGIN_TIMEOUT,
        )
        self._login(password)

    def _login(self, password):
        index = self.child.expect(
            [PASSWORD_PROMPT, pexpect.EOF, pexpect.TIMEOUT], timeout=LOGIN_TIMEOUT
        )
        if index == 1:
            raise GuestError("The connection was closed before the password prompt", 1)
        if index == 2:
            raise GuestError("Timeout waiting for the password prompt", 1)
        self.child.send(password + "\r")

        # The root shell prompt ends with "# ". A repeated password prompt
        # means that the login was rejected.
        index = self.child.expect(
            [PROMPT, PASSWORD_PROMPT, pexpect.EOF, pexpect.TIMEOUT],
            timeout=LOGIN_TIMEOUT,
        )
        if index == 1:
            raise GuestError("The login was rejected", 1)
        if index == 2:
            raise GuestError("The connection was closed during the login", 1)
        if index == 3:
            raise GuestError("Timeout waiting for the shell prompt", 1)

        # The guest echoes the command lines, and the echoed text cannot be
        # told apart from the output of the command. Turning the echo off
        # makes the shell prompt the only synchronisation point.
        self.child.send("stty -echo\r")
        index = self.child.expect(
            [PROMPT, pexpect.EOF, pexpect.TIMEOUT], timeout=LOGIN_TIMEOUT
        )
        if index != 0:
            raise GuestError("Timeout disabling the terminal echo", 1)

    def run_command(self, command, tag):
        """Run a command in the guest and return its exit status.

        The output of the command is left in /tmp/provision_<tag>.log in
        the guest, so that a failure can be inspected there.

        The command runs in a group, so that the redirection of the output
        covers the whole command. A redirection inside the command would
        otherwise win over the redirection of the group, and the output of
        the last command of a chain would be written to its own file
        instead of the log file.
        """
        logfile = f"/tmp/provision_{tag}.log"
        # A trailing semicolon would turn into ";;", which is a syntax error
        # in the group.
        command = command.rstrip().rstrip(";").rstrip()
        self.child.send(
            f'{{ {command} ; }} >{logfile} 2>&1; rc=$?; echo "__RC__${{rc}}__"\r'
        )
        index = self.child.expect(
            [EXIT_STATUS, pexpect.EOF, pexpect.TIMEOUT], timeout=COMMAND_TIMEOUT
        )
        if index == 1:
            raise GuestError(f"The connection was closed while running: {command}", 1)
        if index == 2:
            raise GuestError(f"Timeout while running: {command}", 1)
        status = int(self.child.match.group(1))

        index = self.child.expect(
            [PROMPT, pexpect.EOF, pexpect.TIMEOUT], timeout=COMMAND_TIMEOUT
        )
        if index == 1:
            raise GuestError(f"The connection was closed after running: {command}", 1)
        if index == 2:
            raise GuestError(f"Timeout waiting for the prompt after: {command}", 1)
        return status

    def poweroff(self, timeout=60, attempts=2):
        """Ask the guest to power off.

        The SSH connection dies together with the guest, so the end of the
        connection is all that can be waited for. A guest that is still
        booting can miss the first request, so it is sent again.
        """
        for attempt in range(attempts):
            self.child.send("poweroff\r")
            try:
                self.child.expect(pexpect.EOF, timeout=timeout)
                return
            except pexpect.TIMEOUT:
                print(
                    f"The guest did not power off within {timeout}s, "
                    "sending the request again",
                    flush=True,
                )
        raise GuestError("The guest did not power off", 2)


def disable_unreleased_suite(session):
    """Remove the "unreleased" suite from the sources of the guest.

    The packages of that suite are not stable, so it is removed before
    anything is fetched.
    """
    print("Disabling the unreleased suite...", flush=True)
    session.run_command(
        "sed -i '/unreleased/d' /etc/apt/sources.list", "sources_prune"
    )
    session.run_command(
        "for f in /etc/apt/sources.list.d/*.list; do "
        '[ -e "$f" ] && sed -i "/unreleased/d" "$f"; done; true',
        "sources_prune_d",
    )
    status = session.run_command(
        "if [ -d /etc/apt/sources.list.d ]; then "
        "grep -rqs unreleased /etc/apt/sources.list /etc/apt/sources.list.d; "
        "else grep -qs unreleased /etc/apt/sources.list; fi",
        "sources_check",
    )
    if status == 0:
        raise GuestError("The unreleased suite is still enabled in the apt sources")


def hold_hurd_dev(session):
    """Keep the hurd-dev package at the version that is installed.

    The hurd-dev package from sid cannot be unpacked on Hurd: dpkg fails
    with "unable to install new version of .../linux-headers/linux/hdlc",
    because the new version wants to replace a directory with a file. The
    package is held and pinned below zero, so that an upgrade cannot
    select the broken version.
    """
    print("Holding hurd-dev...", flush=True)

    # The pin is written before the hold, because a pin without a priority
    # makes every apt command fail with "No priority (or zero) specified
    # for pin".
    session.run_command(
        "mkdir -p /etc/apt/preferences.d && "
        "printf 'Package: hurd-dev\\nPin: release *\\nPin-Priority: -1\\n' "
        "> /etc/apt/preferences.d/99-hurd-dev",
        "hurd_dev_pin",
    )
    status = session.run_command(
        "grep -q '^Pin-Priority: -1$' /etc/apt/preferences.d/99-hurd-dev",
        "hurd_dev_pin_check",
    )
    if status != 0:
        raise GuestError("The apt pin for hurd-dev was not written correctly")

    session.run_command("apt-mark hold hurd-dev", "hurd_dev_hold")
    status = session.run_command(
        "apt-mark showhold | grep -q '^hurd-dev$'", "hurd_dev_hold_check"
    )
    if status != 0:
        raise GuestError("The hurd-dev package is not held")


def install_packages(session, packages):
    """Install the packages that the guest needs.

    The check is by command, not by package status: the packages are only
    needed as working tools, and the check has to work for a guest that
    was provisioned before the package was added to the list. The install
    itself names all the packages, so that the ones that are already
    installed are brought up to the version of the archive as well.

    The installation needs the package lists of the archive, which the
    image does not ship, so the lists are fetched first.
    """
    if not packages:
        print("No packages to install.", flush=True)
        return

    missing = []
    for package in packages:
        status = session.run_command(
            f"command -v {package} >/dev/null 2>&1", f"check_{package}"
        )
        if status != 0:
            missing.append(package)
    if not missing:
        print(f"{' and '.join(packages)} are already installed.", flush=True)
        return

    print("Updating the package lists...", flush=True)
    status = session.run_command(
        "apt-get update -o Acquire::Languages=none", "apt_update"
    )
    if status != 0:
        raise GuestError(f"apt-get update failed with status {status}")

    print(f"Installing {' and '.join(packages)}...", flush=True)
    status = session.run_command(
        "DEBIAN_FRONTEND=noninteractive apt-get -y "
        "-o APT::Install-Recommends=false install " + " ".join(packages),
        "apt_install",
    )
    if status != 0:
        print(f"apt-get install failed with status {status}", flush=True)
        print(
            "The output of the failed command is in "
            "/tmp/provision_apt_install.log in the guest",
            flush=True,
        )
        # A failed installation leaves dpkg in an inconsistent state. Repairing
        # it in the guest is worth one attempt, but a second failure needs
        # a fresh guest: the ext2 filesystem of the image has no journal
        # and does not survive a failed unpack.
        session.run_command("dpkg --configure -a", "dpkg_configure")
        status = session.run_command("apt-get -f -y install", "apt_fix")
        if status != 0:
            raise GuestError(f"apt-get -f install failed with status {status}")

    # Reclaim the space taken by the downloaded packages
    session.run_command("apt-get clean", "apt_clean")

    for package in packages:
        status = session.run_command(
            f"command -v {package} >/dev/null 2>&1", f"verify_{package}"
        )
        if status != 0:
            raise GuestError(f"{package} is missing after the installation")


def upgrade_packages(session):
    """Update the packages of the guest."""
    # The downloaded .deb files stay in the package cache, which needs
    # around 180 MB; the guest filesystem has enough room for that.
    apt_opts = "-o APT::Install-Recommends=false"
    print("Running apt update...", flush=True)
    status = session.run_command(
        f"DEBIAN_FRONTEND=noninteractive apt-get {apt_opts} update", "apt_update"
    )
    if status != 0:
        raise GuestError(f"apt-get update failed with status {status}")

    print("Running apt upgrade...", flush=True)
    status = session.run_command(
        f"DEBIAN_FRONTEND=noninteractive apt-get {apt_opts} -y "
        "-o Dpkg::Options::=--force-confold upgrade",
        "apt_upgrade",
    )
    if status != 0:
        print(f"apt-get upgrade failed with status {status}", flush=True)
        print(
            "The output of the failed command is in "
            "/tmp/provision_apt_upgrade.log in the guest"
        )
        # A failed upgrade leaves dpkg in an inconsistent state. Repairing
        # it in the guest is worth one attempt, but a second failure needs
        # a fresh guest: the ext2 filesystem of the image has no journal
        # and does not survive a failed unpack.
        session.run_command("dpkg --configure -a", "dpkg_configure")
        status = session.run_command("apt-get -f -y install", "apt_fix")
        if status != 0:
            raise GuestError(f"apt-get -f install failed with status {status}")

    # Reclaim the space taken by the downloaded packages
    session.run_command("apt-get clean", "apt_clean")


def provision(session, run_apt_upgrade, packages):
    disable_unreleased_suite(session)
    hold_hurd_dev(session)

    # The packages are installed on every run, not only on the first one:
    # a package that is added to the list has to reach the images that
    # were provisioned before it was added.
    install_packages(session, packages)

    if session.run_command("test -f /root/.provisioned", "marker") == 0:
        print("Image is already provisioned.", flush=True)
        return

    if run_apt_upgrade:
        upgrade_packages(session)
    else:
        print("Skipping apt update and apt upgrade (RUN_APT_UPGRADE is not set).", flush=True)

    # Write the provision marker
    session.run_command("touch /root/.provisioned", "marker_write")


def main():
    host = os.environ.get("SSH_HOST", "127.0.0.1")
    port = int(os.environ.get("SSH_PORT", "2222"))
    password = os.environ.get("ROOT_PASSWORD", "")
    run_apt_upgrade = os.environ.get("RUN_APT_UPGRADE", "0") == "1"
    packages = os.environ.get("GUEST_PACKAGES", "curl python3").split()

    try:
        session = GuestSession(host, port, password)
        provision(session, run_apt_upgrade, packages)
        session.poweroff()
    except GuestError as error:
        print(str(error), file=sys.stderr, flush=True)
        return error.exit_code
    except pexpect.ExceptionPexpect as error:
        print(f"The SSH session failed: {error}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
