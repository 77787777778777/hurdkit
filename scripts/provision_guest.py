#!/usr/bin/env python3
"""Drive the Hurd guest over SSH while the base image is provisioned.

The script logs in as root, prepares the apt sources of the guest, renames
the user account of the image, installs the SSH keys of the accounts and,
when it is asked to, runs the package upgrade. It does the same work as
the expect script that used to live in scripts/provision_base.sh, but
without the quoting games of expect and Tcl.

The configuration comes from the environment:

    SSH_HOST          the host that the SSH port of the guest is
                      forwarded to (default 127.0.0.1)
    SSH_PORT          the forwarded SSH port (default 2222)
    ROOT_PASSWORD     the password of the root account
    STAGES            comma-separated stages to run (default
                      "accounts,packages", the full base provisioning);
                      see STAGE_DESCRIPTIONS below
    RUN_APT_UPGRADE   "1" runs "apt-get update" and "apt-get upgrade"
                      (packages stage only)
    GUEST_PACKAGES    the packages that the guest gets (packages stage;
                      default "curl python3 gdb")
    CUSTOM_KERNEL     path to a host-side gnumach ELF binary to ship in
                      the overlay as the boot kernel of the guest
                      (kernel stage; optional)
    CUSTOM_OVERLAY    host directory whose contents are copied into the
                      guest filesystem (kernel stage; optional)
    SSH_KEY_DIR       the directory with the SSH keys of the accounts
                      (default "img/keys")

Exit codes:

    0   the guest was provisioned
    1   the session could not be established, the caller may retry
    2   a command failed in the guest, retrying with the same guest
        cannot help
"""

import gzip
import io
import os
import re
import subprocess
import sys
import tarfile
import tempfile

try:
    import pexpect
except ImportError:
    sys.exit("pexpect is required for the provisioning (pip install pexpect)")

# The forwarded port accepts connections before sshd in the guest is
# ready, so the login phase gets a timeout of its own.
LOGIN_TIMEOUT = 120
# The package upgrade takes minutes.
COMMAND_TIMEOUT = 3600
# The login with a key is a local connection to a running guest.
KEY_LOGIN_TIMEOUT = 60

# The name of the user account in the image. The image calls it "demo",
# which says more about the image than about the account, so the
# provisioning renames it. This is the only place that knows the name of
# the account of the image.
OLD_ACCOUNT = "demo"
NEW_ACCOUNT = "peon"
NEW_ACCOUNT_HOME = "/home/peon"

# The accounts that get an SSH key, with their home directory. The key
# pairs are generated on the host and are kept there, so that the guest
# can be logged into without a password.
KEY_ACCOUNTS = (("root", "/root"), (NEW_ACCOUNT, NEW_ACCOUNT_HOME))

# The kernel that the GRUB entry of the image boots; CUSTOM_KERNEL ships
# its replacement inside the overlay tar (see pack_overlay).
STOCK_KERNEL = "/boot/gnumach-1.8-amd64-up.gz"

# The provisioning stages, each idempotent on its own, in the order in
# which they are meant to be applied to a chain of images:
#
#   accounts: rename the account of the image, set the passwords,
#             install the SSH keys ("image with sshd/account changes")
#   packages: prune the apt sources, install GUEST_PACKAGES, optionally
#             upgrade ("image with apt packages")
#   kernel:   copy the CUSTOM_OVERLAY directory and/or the CUSTOM_KERNEL
#             boot kernel into the guest filesystem ("image with custom
#             kernel, custom drivers, init scripts etc")
#
# A stage is applied to a copy of the image that carries the stages
# before it (scripts/provision_stage.sh chains them).  Every stage
# re-runs safely on an image that already has it.
STAGE_DESCRIPTIONS = "accounts, packages, kernel"

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
        self.host = host
        self.port = port
        self.password = password
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
        #
        # The history of the shell is disabled as well: the session is
        # interactive, so every command would end up in the history file
        # of the guest, including the commands that carry a password hash.
        self.child.send("stty -echo; export HISTFILE=/dev/null; set +o history\r")
        index = self.child.expect(
            [PROMPT, pexpect.EOF, pexpect.TIMEOUT], timeout=LOGIN_TIMEOUT
        )
        if index != 0:
            raise GuestError("Timeout preparing the shell of the guest", 1)

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

    def copy_to_guest(self, host_path, guest_path):
        """Copy a host file into the guest with scp.

        scp uses the legacy protocol (-O): the guest has the scp binary
        (openssh-server depends on openssh-client), while the sftp server
        is one more moving part than the copy needs.  The authentication
        mirrors the login: password only, host key not checked.
        """
        child = pexpect.spawn(
            "scp",
            [
                "-O",
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", "PreferredAuthentications=password",
                "-o", "PubkeyAuthentication=no",
                "-P", str(self.port),
                host_path,
                f"root@{self.host}:{guest_path}",
            ],
            encoding="utf-8",
            timeout=KEY_LOGIN_TIMEOUT,
        )
        index = child.expect(
            [PASSWORD_PROMPT, pexpect.EOF, pexpect.TIMEOUT],
            timeout=KEY_LOGIN_TIMEOUT,
        )
        if index == 1:
            raise GuestError("scp closed the connection before the password prompt", 1)
        if index == 2:
            raise GuestError("Timeout starting scp", 1)
        child.send(self.password + "\r")
        # The copy of a multi-megabyte kernel takes longer than the login.
        index = child.expect(
            [pexpect.EOF, PASSWORD_PROMPT, pexpect.TIMEOUT], timeout=COMMAND_TIMEOUT
        )
        if index == 1:
            raise GuestError("scp was rejected, the password is wrong", 1)
        if index == 2:
            raise GuestError("Timeout copying the file into the guest", 1)

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
        "-o Dpkg::Options::=--force-confold "
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


def set_account_password(session, account, password):
    """Set the password of an account in the guest.

    The hash is made on the host, so that the plaintext password never
    reaches the guest. The hash is written with usermod, which is the
    tool of the image for the password of an account.
    """
    digest = subprocess.run(
        ["openssl", "passwd", "-6", "-stdin"],
        input=password,
        capture_output=True,
        text=True,
        check=True,
        timeout=KEY_LOGIN_TIMEOUT,
    ).stdout.strip()

    session.run_command(f"usermod -p '{digest}' {account}", f"password_{account}")

    status = session.run_command(
        f"grep -qF '{account}:{digest}:' /etc/shadow", f"password_{account}_check"
    )
    if status != 0:
        raise GuestError(f"The password of {account} was not applied")


def rename_account(session, old_name, new_name):
    """Rename the user account of the image in the guest.

    The account of the image has a name that says more about the image
    than about the account, so it gets the name of a person. The group is
    renamed first, because usermod refuses to rename a user whose primary
    group has the same name as the user.
    """
    user_is_old = (
        session.run_command(f"grep -q '^{old_name}:' /etc/passwd", "account_check")
        == 0
    )
    user_is_new = (
        session.run_command(f"grep -q '^{new_name}:' /etc/passwd", "account_check_new")
        == 0
    )
    if not user_is_old and not user_is_new:
        raise GuestError(
            f"The guest has neither a {old_name} nor a {new_name} account"
        )

    if session.run_command(f"grep -q '^{old_name}:' /etc/group", "account_group_check") == 0:
        print(f"Renaming the account {old_name} to {new_name}...", flush=True)
        session.run_command(f"groupmod -n {new_name} {old_name}", "account_group")
    elif not user_is_old:
        print(f"The account is already named {new_name}.", flush=True)

    if user_is_old:
        # The home directory of the account moves with it. An image without
        # a home directory for the account needs no move.
        session.run_command(
            f"if [ -d /home/{old_name} ]; then "
            f"usermod -l {new_name} -d /home/{new_name} -m {old_name}; "
            f"else usermod -l {new_name} -d /home/{new_name} {old_name}; fi",
            "account_rename",
        )
        # The comment field of the account still holds the old name.
        session.run_command(
            f"usermod -c '{new_name},,,' {new_name}", "account_comment"
        )

    # A half-renamed account leaves the old name behind in the shadow file
    # or in the group file, and the login of the account then fails.
    for path in ("/etc/passwd", "/etc/shadow", "/etc/group"):
        if session.run_command(f"grep -q '^{old_name}:' {path}", "account_verify") == 0:
            raise GuestError(f"The account {old_name} is still in {path}")
    for path in ("/etc/passwd", "/etc/shadow"):
        if session.run_command(f"grep -q '^{new_name}:' {path}", "account_verify_new") != 0:
            raise GuestError(f"The account {new_name} is missing in {path}")
    if session.run_command(f"test -d /home/{new_name}", "account_home") != 0:
        raise GuestError(f"The home directory /home/{new_name} is missing")


def ensure_keypair(key_dir, account):
    """Make sure that an SSH key pair for the account exists.

    The keys are generated on the host and are never replaced: the public
    key of the pair is installed in the guest, and a new pair would lock
    the guest out of the key login until it is installed again.
    """
    os.makedirs(key_dir, mode=0o700, exist_ok=True)
    os.chmod(key_dir, 0o700)
    private_key = os.path.join(key_dir, f"{account}_ed25519")
    public_key = f"{private_key}.pub"

    if not os.path.exists(private_key):
        print(f"Generating the SSH key of {account}...", flush=True)
        subprocess.run(
            [
                "ssh-keygen",
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-C",
                f"{account}@hurd",
                "-f",
                private_key,
            ],
            check=True,
            timeout=KEY_LOGIN_TIMEOUT,
        )
    # ssh refuses a private key that other users can read.
    os.chmod(private_key, 0o600)

    if not os.path.exists(public_key):
        # The public key can be derived from the private one.
        derived = subprocess.run(
            ["ssh-keygen", "-y", "-f", private_key],
            check=True,
            capture_output=True,
            text=True,
            timeout=KEY_LOGIN_TIMEOUT,
        )
        with open(public_key, "w", encoding="utf-8") as public_file:
            public_file.write(derived.stdout)

    return private_key, public_key


def install_authorized_key(session, account, home, public_key_path):
    """Install the public key of an account in the guest.

    The key is appended, so that the keys of other people survive a
    provisioning run.
    """
    with open(public_key_path, encoding="utf-8") as key_file:
        key = key_file.read().strip()
    if "'" in key:
        raise GuestError(f"The public key of {account} contains a quote")

    session.run_command(
        f"mkdir -p {home}/.ssh && chmod 700 {home}/.ssh && "
        f"touch {home}/.ssh/authorized_keys && "
        f"{{ grep -qxF '{key}' {home}/.ssh/authorized_keys || "
        f"printf '%s\\n' '{key}' >> {home}/.ssh/authorized_keys; }} && "
        f"chmod 600 {home}/.ssh/authorized_keys && "
        f"chown -R {account}:{account} {home}/.ssh",
        f"authorized_key_{account}",
    )
    # An install that failed in the guest has to fail the provisioning here:
    # the key login check below would otherwise report an indirect error.
    status = session.run_command(
        f"grep -qxF '{key}' {home}/.ssh/authorized_keys", f"authorized_key_{account}_check"
    )
    if status != 0:
        raise GuestError(
            f"The authorized key of {account} was not installed in the guest "
            f"(see /tmp/provision_authorized_key_{account}.log)"
        )


def install_authorized_keys(session, key_dir):
    """Install the public keys of the accounts in the guest."""
    key_pairs = []
    for account, home in KEY_ACCOUNTS:
        private_key, public_key = ensure_keypair(key_dir, account)
        print(f"Installing the SSH key of {account}...", flush=True)
        install_authorized_key(session, account, home, public_key)
        key_pairs.append((account, private_key))
    return key_pairs


def verify_key_login(host, port, account, private_key):
    """Log into the guest with the key of an account.

    The client is told not to ask for a password, so the login only works
    when the key is really accepted by the guest.
    """
    result = subprocess.run(
        [
            "ssh",
            "-i",
            private_key,
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "BatchMode=yes",
            "-o",
            "PasswordAuthentication=no",
            # Only offer the given key: ssh otherwise tries the keys of the
            # agent first, and a loaded agent exhausts the authentication
            # attempt limit of sshd (default 6) before the right key is even
            # offered ("Too many authentication failures").
            "-o",
            "IdentitiesOnly=yes",
            "-p",
            str(port),
            f"{account}@{host}",
            "id -un",
        ],
        capture_output=True,
        text=True,
        timeout=KEY_LOGIN_TIMEOUT,
    )
    if result.returncode != 0 or result.stdout.strip() != account:
        raise GuestError(
            f"The SSH key of {account} does not log into the guest: "
            f"{result.stderr.strip()}"
        )


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


def pack_overlay(overlay_dir, kernel_binary):
    """Pack the overlay tarball on the host.

    The tar carries the CUSTOM_OVERLAY tree (extracted over / in the
    guest) and, when CUSTOM_KERNEL names a kernel binary, that binary
    gzipped at the boot path.  Packing the kernel into the same tar
    keeps a single apply path on the guest side: the whole kernel stage
    is one overlay, whatever it carries.  Shipping the boot kernel in
    both ways at once is ambiguous and rejected.

    Returns (tarball path, whether the tar carries a boot kernel); the
    caller removes the tarball.
    """
    fd, tarball = tempfile.mkstemp(suffix=".tgz")
    os.close(fd)

    def root_owned(info):
        info.uid = info.gid = 0
        info.uname = info.gname = "root"
        return info

    boot_kernel = STOCK_KERNEL.lstrip("/")
    ships_kernel = False
    with tarfile.open(tarball, "w:gz") as tar:
        if overlay_dir:
            if kernel_binary and os.path.exists(os.path.join(overlay_dir, boot_kernel)):
                raise GuestError(
                    f"The overlay already ships {boot_kernel}; drop "
                    "CUSTOM_KERNEL or the file from the overlay"
                )
            ships_kernel = os.path.exists(os.path.join(overlay_dir, boot_kernel))
            tar.add(overlay_dir, arcname=".", filter=root_owned)
        if kernel_binary:
            # Compressed where it is built: GRUB decompresses gzip
            # transparently, and the tar transport preserves the bytes.
            with open(kernel_binary, "rb") as kernel:
                data = kernel.read()
            gz = gzip.compress(data, compresslevel=9)
            info = tarfile.TarInfo(boot_kernel)
            info.size = len(gz)
            info.mode = 0o644
            info.mtime = int(os.stat(kernel_binary).st_mtime)
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            tar.addfile(info, io.BytesIO(gz))
            ships_kernel = True
    return tarball, ships_kernel


def apply_overlay(session, overlay_dir, kernel_binary):
    """Copy the overlay into the guest filesystem.

    The overlay is packed on the host (pack_overlay) and extracted over
    / in the guest, so modes and symlinks survive and files land at
    exactly the paths they have relative to the overlay directory.
    Every entry is rewritten to be owned by root: the tar would
    otherwise carry the uid and gid of the host account that built it,
    and the guest services expect their files root-owned.  A boot
    kernel in the overlay is checked with gzip -t after the extraction,
    so a corrupt one fails the stage instead of the next boot.
    """
    print("Applying the overlay...", flush=True)
    tarball, ships_kernel = pack_overlay(overlay_dir, kernel_binary)
    check = f" && gzip -t {STOCK_KERNEL}" if ships_kernel else ""
    try:
        session.copy_to_guest(tarball, "/tmp/overlay.tgz")
        status = session.run_command(
            f"tar -xzf /tmp/overlay.tgz -C /{check} && rm -f /tmp/overlay.tgz",
            "overlay_apply",
        )
    finally:
        os.remove(tarball)
    if status != 0:
        raise GuestError(
            "Applying the overlay failed "
            "(see /tmp/provision_overlay_apply.log in the guest)"
        )


def stage_accounts(session, password, key_dir):
    """Stage "accounts": rename the account of the image, set its
    password and install the SSH keys.  The account and the keys are set
    up on every run, so that they also reach the images that were
    provisioned before."""
    rename_account(session, OLD_ACCOUNT, NEW_ACCOUNT)
    set_account_password(session, NEW_ACCOUNT, password)
    return install_authorized_keys(session, key_dir)


def stage_packages(session, run_apt_upgrade, packages):
    """Stage "packages": prune the apt sources, install GUEST_PACKAGES
    and optionally upgrade.  The installation runs on every invocation
    (a package added to the list reaches images provisioned before it
    was added); the marker only gates the upgrade."""
    disable_unreleased_suite(session)
    install_packages(session, packages)

    # Record what this provisioning installed. The host-side no-op check
    # compares this manifest with GUEST_PACKAGES, so adding a package later
    # still boots an already provisioned image. Images provisioned before
    # this file existed retain the default package list in the host check.
    package_manifest = " ".join(packages)
    status = session.run_command(
        f"printf '%s\\n' '{package_manifest}' > /root/.provision-packages",
        "package_manifest",
    )
    if status != 0:
        raise GuestError("Could not write the package manifest in the guest")

    if session.run_command("test -f /root/.provisioned", "marker") == 0:
        print("Image is already provisioned.", flush=True)
        return

    if run_apt_upgrade:
        # Keep hurd-dev at the version that is installed. The hurd-dev
        # package from sid cannot be unpacked on Hurd: dpkg fails with
        # "unable to install new version of .../linux-headers/linux/hdlc",
        # because the new version wants to replace a directory with a file.
        # The package is held and pinned below zero, so that an upgrade
        # cannot select the broken version.
        hold_hurd_dev(session)
        upgrade_packages(session)
    else:
        print("Skipping apt update and apt upgrade (RUN_APT_UPGRADE is not set).", flush=True)

    session.run_command("touch /root/.provisioned", "marker_write")


def stage_kernel(session, custom_kernel, custom_overlay):
    """Stage "kernel": copy the CUSTOM_OVERLAY directory and/or the
    CUSTOM_KERNEL boot kernel into the guest filesystem."""
    if not custom_kernel and not custom_overlay:
        raise GuestError("The kernel stage needs CUSTOM_KERNEL or CUSTOM_OVERLAY")
    apply_overlay(session, custom_overlay, custom_kernel)


def main():
    host = os.environ.get("SSH_HOST", "127.0.0.1")
    port = int(os.environ.get("SSH_PORT", "2222"))
    password = os.environ.get("ROOT_PASSWORD", "")
    run_apt_upgrade = os.environ.get("RUN_APT_UPGRADE", "0") == "1"
    packages = os.environ.get("GUEST_PACKAGES", "curl python3 gdb").split()
    custom_kernel = os.environ.get("CUSTOM_KERNEL", "")
    custom_overlay = os.environ.get("CUSTOM_OVERLAY", "")
    key_dir = os.environ.get("SSH_KEY_DIR", "img/keys")
    stages = [s.strip() for s in os.environ.get("STAGES", "accounts,packages").split(",")
              if s.strip()]

    known_stages = {"accounts", "packages", "kernel"}
    unknown = [s for s in stages if s not in known_stages]
    if unknown:
        print(f"Unknown stage(s) {', '.join(unknown)}; "
              f"known stages: {STAGE_DESCRIPTIONS}", file=sys.stderr, flush=True)
        return 2
    if custom_overlay and not os.path.isdir(custom_overlay):
        print(f"The overlay is not a directory: {custom_overlay}",
              file=sys.stderr, flush=True)
        return 2

    try:
        session = GuestSession(host, port, password)
        key_pairs = []
        if "accounts" in stages:
            key_pairs = stage_accounts(session, password, key_dir)
        if "packages" in stages:
            stage_packages(session, run_apt_upgrade, packages)
        if "kernel" in stages:
            stage_kernel(session, custom_kernel, custom_overlay)
        for account, private_key in key_pairs:
            print(f"Checking the SSH key of {account}...", flush=True)
            verify_key_login(host, port, account, private_key)
            print(f"The SSH key of {account} works.", flush=True)
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
