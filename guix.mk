# Guix GNU/Hurd (x86_64-gnu) guest. Included by the top-level Makefile.
#
# The system is declared in guix/hurd64.scm. Run these targets inside
# "nix develop ./nix#guix": the guix command has to be the pinned goonix.
# See skills/GUIX.md.
#
# This file reads no variable of the Makefile that includes it, and runs
# none of the Debian pipeline's scripts. Its own variables start with
# GUIX_, so that nothing collides either.

# The command that runs guix-authorize-host as root.
GUIX_SUDO        ?= sudo

.PHONY: guix-authorize-host guix-test

# Generate the host's archive-signing key if it has none. On NixOS without
# /run/wrappers/bin in PATH: GUIX_SUDO=/run/wrappers/bin/sudo
guix-authorize-host:
	@bash scripts/guix_authorize_host.sh "$(GUIX_SUDO)"

# The TAP tests of the scripts; no guest is booted.
guix-test:
	@set -e; for test in tests/*.t; do echo "# $$test"; bash "$$test"; done
