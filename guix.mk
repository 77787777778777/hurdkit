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

# --- The guest: scripts/guix_vm.sh builds, boots and manages it. ---

# The system declaration that guix-image builds.
GUIX_OS          ?= guix/hurd64.scm
# The port on the host that is forwarded to the sshd of the guest.
GUIX_SSH_PORT    ?= 2223
# Memory of the guest in MiB.
GUIX_MEMORY      ?= 2048
# Extra options for "guix system image", for example --image-size=8G
GUIX_IMAGE_OPTS  ?=
# Extra QEMU arguments for guix-start, as a shell word list.
GUIX_QEMU_ARGS   ?=
# One remote command for guix-ssh; pipe a script into it for more.
GUIX_SSH_COMMAND ?=

GUIX_VM = env GUIX_OS="$(abspath $(GUIX_OS))" \
	GUIX_SSH_PORT=$(GUIX_SSH_PORT) GUIX_MEMORY=$(GUIX_MEMORY) \
	GUIX_IMAGE_OPTS="$(GUIX_IMAGE_OPTS)" \
	bash scripts/guix_vm.sh

.PHONY: guix-image guix-start guix-start-persist guix-status guix-ssh \
	guix-ssh-peon guix-stop guix-run guix-repair guix-reset-state \
	guix-screenshot

# Build guix/hurd64.scm into img/guix/hurd64.base.qcow2
guix-image:
	@$(GUIX_VM) image

# Boot in the background from a throw-away overlay and wait for SSH
guix-start:
	@$(GUIX_VM) start -- $(GUIX_QEMU_ARGS)

# The same, but the disk state is kept in img/guix/state.qcow2
guix-start-persist:
	@$(GUIX_VM) start persist -- $(GUIX_QEMU_ARGS)

guix-status:
	@$(GUIX_VM) status

guix-ssh:
	@$(GUIX_VM) ssh root $(if $(GUIX_SSH_COMMAND),"$(GUIX_SSH_COMMAND)")

guix-ssh-peon:
	@$(GUIX_VM) ssh peon $(if $(GUIX_SSH_COMMAND),"$(GUIX_SSH_COMMAND)")

guix-stop:
	@$(GUIX_VM) stop

# Boot in the foreground, console on this terminal, throw-away overlay
guix-run:
	@$(GUIX_VM) run

# Check and repair the root filesystem of the kept state (guest stopped)
guix-repair:
	@$(GUIX_VM) repair

guix-reset-state:
	@$(GUIX_VM) reset-state

# Save the guest's screen as img/guix/screen.ppm
guix-screenshot:
	@$(GUIX_VM) screenshot

# --- The niri platform guest (guix/hurd64-platform.scm) and its tests. ---

.PHONY: stage1-check

stage1-check:
	@bash scripts/stage1_check.sh

# --- The Wayland guest (guix/hurd64-wayland.scm): libwayland and a
# software Mesa. stage2-reference renders the frames on this machine only. ---

.PHONY: stage2-check stage2-reference

stage2-check:
	@bash scripts/stage2_check.sh

stage2-reference:
	@bash scripts/stage2_check.sh reference
