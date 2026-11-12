# Fetch, convert, and boot a Debian GNU/Hurd x86_64 image via QEMU.
#
# The downloaded image is stored in img/ and is never deleted.
# A pristine qcow2 base disk is created from it (after offline
# preparation), and the VM boots from a temporary copy-on-write
# overlay so the base stays unchanged.

# Pinned Hurd image version
VERSION       := 20260314

# Configuration
IMG_DIR       := img
IMG_URL       := https://cdimage.debian.org/cdimage/ports/latest/hurd-amd64/debian-hurd-amd64-$(VERSION).img.tar.xz
MD5_SUM       := 91dc71608fcfc9f237028f1fe87c39c2
IMG_TAR       := $(IMG_DIR)/debian-hurd-amd64-$(VERSION).img.tar.xz
RAW_IMG       := $(IMG_DIR)/debian-hurd-amd64-$(VERSION).img
# Stamp for the extraction step: the tarball preserves the member mtime,
# which is older than the tarball itself, so the raw image would otherwise
# be extracted again on every make invocation.
EXTRACT_STAMP := $(IMG_DIR)/.extracted
WORK_IMG      := $(IMG_DIR)/hurd.work.img
BASE_IMG      := $(IMG_DIR)/hurd.base.qcow2
TMP_DIR       := $(IMG_DIR)/tmp
MEMORY        := 2G
CPU_COUNT     := 4
SSH_PORT      := 2222
SSH_HOST      := 127.0.0.1
ROOT_PASSWORD := abcdef
# The in-VM package upgrade is off by default: the sid archive ships a
# hurd-dev that cannot be unpacked on Hurd. Set it to 1 to run
# "apt-get update" and "apt-get upgrade" during the provisioning.
RUN_APT_UPGRADE := 0

# Toolchain
QEMU_SYSTEM   := qemu-system-x86_64
QEMU_IMG      := qemu-img
WGET          := wget
TAR           := tar
MKDIR_P       := mkdir -p
RM            := rm -f
# Serialises the image building steps: two concurrent builds would write to
# the same image files and corrupt them.
LOCK          := flock
LOCK_FILE     := $(IMG_DIR)/.build.lock

.PHONY: all download check base provision run run-nokvm ssh clean distclean

all: base

# Download the image if missing
$(IMG_TAR):
	$(MKDIR_P) $(IMG_DIR)
	$(WGET) -O $@ $(IMG_URL)

# Verify MD5 checksum
check: $(IMG_TAR)
	@echo "$$(md5sum $(IMG_TAR) | cut -d' ' -f1)  $(IMG_TAR)" | grep -q "^$(MD5_SUM)  " || { echo "Checksum failed"; exit 1; }

# Download the image only
download: check

# Extract the raw image from the tarball if missing
$(EXTRACT_STAMP): $(IMG_TAR)
	$(TAR) -xJf $< -C $(IMG_DIR)
	touch $@

# Prepare the raw image offline (GRUB serial console, passwords, sshd
# configuration), then convert it to a qcow2 base image.
base: $(BASE_IMG)

# The image is built in a temporary file and moved into place, so that an
# interrupted build never leaves a partially written base image behind.
# The preparation script is a prerequisite, so that changing it rebuilds
# the base image.
$(BASE_IMG): $(EXTRACT_STAMP) scripts/prepare_base.sh
	@$(MKDIR_P) $(IMG_DIR)
	@$(LOCK) $(LOCK_FILE) sh -c 'set -e; \
		ROOT_PASSWORD=$(ROOT_PASSWORD) bash scripts/prepare_base.sh $(RAW_IMG) $(WORK_IMG); \
		$(QEMU_IMG) convert -f raw -O qcow2 $(WORK_IMG) $(WORK_IMG).qcow2; \
		mv -f $(WORK_IMG).qcow2 $@; \
		rm -f $(WORK_IMG)'

# Provision the base image in-VM (idempotent: skips if already provisioned)
provision: $(BASE_IMG)
	@$(MKDIR_P) $(IMG_DIR)
	@$(LOCK) $(LOCK_FILE) env ROOT_PASSWORD=$(ROOT_PASSWORD) \
		CPU_COUNT=$(CPU_COUNT) MEMORY=$(MEMORY) \
		RUN_APT_UPGRADE=$(RUN_APT_UPGRADE) \
		bash scripts/provision_base.sh $(BASE_IMG) $(SSH_PORT)

# Boot from a temporary overlay so the base image stays pristine
run: $(BASE_IMG)
	$(MKDIR_P) $(TMP_DIR)
	@OVERLAY=$$(mktemp -u $(TMP_DIR)/XXXXXX.qcow2); \
	trap 'rm -f "$$OVERLAY"' EXIT; \
	$(QEMU_IMG) create -f qcow2 -b $(abspath $(BASE_IMG)) -F qcow2 $$OVERLAY; \
	if [ -r /dev/kvm ] && [ -w /dev/kvm ]; then \
		echo "Booting with KVM acceleration" >&2; \
		$(QEMU_SYSTEM) \
			-enable-kvm \
			-cpu host \
			-smp $(CPU_COUNT) \
			-m $(MEMORY) \
			-drive file=$$OVERLAY,if=ide,cache=writeback \
			-netdev user,id=net0,hostfwd=tcp:$(SSH_HOST):$(SSH_PORT)-:22 \
			-device e1000,netdev=net0 \
			-nographic; \
	else \
		echo "KVM is not available, booting without acceleration" >&2; \
		$(QEMU_SYSTEM) \
			-cpu max \
			-smp $(CPU_COUNT) \
			-m $(MEMORY) \
			-drive file=$$OVERLAY,if=ide,cache=writeback \
			-netdev user,id=net0,hostfwd=tcp:$(SSH_HOST):$(SSH_PORT)-:22 \
			-device e1000,netdev=net0 \
			-nographic; \
	fi

# Boot without KVM
run-nokvm: $(BASE_IMG)
	$(MKDIR_P) $(TMP_DIR)
	@OVERLAY=$$(mktemp -u $(TMP_DIR)/XXXXXX.qcow2); \
	trap 'rm -f "$$OVERLAY"' EXIT; \
	$(QEMU_IMG) create -f qcow2 -b $(abspath $(BASE_IMG)) -F qcow2 $$OVERLAY; \
	$(QEMU_SYSTEM) \
		-cpu max \
		-smp $(CPU_COUNT) \
		-m $(MEMORY) \
		-drive file=$$OVERLAY,if=ide,cache=writeback \
		-netdev user,id=net0,hostfwd=tcp:$(SSH_HOST):$(SSH_PORT)-:22 \
		-device e1000,netdev=net0 \
		-nographic

# SSH into the running VM
ssh:
	ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -p $(SSH_PORT) root@$(SSH_HOST)

# Remove temporary overlay images
clean:
	$(RM) $(TMP_DIR)/*.qcow2
	$(RM) $(IMG_DIR)/.provision.*.qcow2
	$(RM) $(IMG_DIR)/.repair.*.raw
	$(RM) $(IMG_DIR)/.repaired.*.qcow2
	$(RM) $(IMG_DIR)/hurd.work.img
	$(RM) $(IMG_DIR)/hurd.work.img.qcow2

# Remove all downloaded/generated files
distclean:
	$(RM) -r $(IMG_DIR)
