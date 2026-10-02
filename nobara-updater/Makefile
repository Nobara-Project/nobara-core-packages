PYTHON ?= /usr/bin/python3
PYTHON_SITE_PACKAGES ?= $(shell $(PYTHON) -c "import sysconfig; print(sysconfig.get_path('purelib', 'posix_prefix', vars={'base': '/usr', 'platbase': '/usr'}))")
TARGET_DIR := $(DESTDIR)$(PYTHON_SITE_PACKAGES)/nobara_updater
BIN_DIR := $(DESTDIR)/usr/bin
LICENSE_DIR := $(DESTDIR)/usr/share/licenses/nobara-updater
SHARE_DIR := $(DESTDIR)/usr/share/nobara-updater
LIBEXEC_DIR := $(DESTDIR)/usr/libexec
SYSTEMD_DIR := $(DESTDIR)/usr/lib/systemd/system
CODEC_WIZARD_DIR := $(DESTDIR)/usr/share/nobara-codec-wizard
DESKTOP_DIR := $(DESTDIR)/usr/share/applications
OFFLINE_GUARDS := dnf-system-upgrade.service dnf-system-upgrade-cleanup.service dnf5-offline-transaction.service dnf5-offline-transaction-cleanup.service packagekit-offline-update.service

.PHONY: all install symlinks clean

all: install symlinks

install:
	@echo "Installing Python files to $(TARGET_DIR)"
	mkdir -p $(TARGET_DIR)
	install -m 644 src/dnf.py $(TARGET_DIR)/dnf.py
	install -m 644 src/update_*.py $(TARGET_DIR)/
	mkdir -p $(LIBEXEC_DIR) $(SYSTEMD_DIR)/system-update.target.wants $(SYSTEMD_DIR)/multi-user.target.wants
	install -m 755 src/update_worker.py $(LIBEXEC_DIR)/nobara-update-worker
	install -m 755 src/update_notice.py $(LIBEXEC_DIR)/nobara-update-notice
	install -m 755 data/nobara-codec-guard $(LIBEXEC_DIR)/nobara-codec-guard
	install -D -m 644 data/nobara-codecs.actions $(DESTDIR)/etc/dnf/libdnf5-plugins/actions.d/nobara-codecs.actions
	install -D -m 644 data/nobara-update-notice.desktop $(DESTDIR)/etc/xdg/autostart/nobara-update-notice.desktop
	install -D -m 644 data/nobara-update-recovery.desktop $(DESKTOP_DIR)/nobara-update-recovery.desktop
	mkdir -p $(DESTDIR)/etc/grub.d
	install -m 755 data/42_nobara_update $(DESTDIR)/etc/grub.d/42_nobara_update
	mkdir -p $(DESTDIR)/usr/lib/dracut/modules.d/91nobara-rollback
	install -m 755 data/dracut/91nobara-rollback/*.sh $(DESTDIR)/usr/lib/dracut/modules.d/91nobara-rollback/
	install -m 644 data/dracut/91nobara-rollback/*.service $(DESTDIR)/usr/lib/dracut/modules.d/91nobara-rollback/
	install -m 644 data/nobara-updater-*.service $(SYSTEMD_DIR)/
	ln -sf ../nobara-updater-offline.service $(SYSTEMD_DIR)/system-update.target.wants/nobara-updater-offline.service
	ln -sf ../nobara-updater-confirm.service $(SYSTEMD_DIR)/multi-user.target.wants/nobara-updater-confirm.service
	$(foreach unit,$(OFFLINE_GUARDS),install -D -m 644 data/nobara-offline-guard.conf $(SYSTEMD_DIR)/$(unit).d/nobara-updater.conf;)

	mkdir -p $(CODEC_WIZARD_DIR) $(DESKTOP_DIR)
	install -m 755 data/nobara-codec-wizard/main.py data/nobara-codec-wizard/process.py $(CODEC_WIZARD_DIR)/
	install -m 644 data/nobara-codec-wizard/main.ui data/nobara-codec-wizard/process.ui $(CODEC_WIZARD_DIR)/
	install -m 644 data/nobara-codec-wizard.desktop $(DESKTOP_DIR)/nobara-codec-wizard.desktop

	@echo "Installing license file to $(LICENSE_DIR)"
	mkdir -p $(LICENSE_DIR)
	install -m 644 licenses/LICENSE $(LICENSE_DIR)/LICENSE

	@echo "Installing administrator documentation to $(SHARE_DIR)"
	mkdir -p $(SHARE_DIR)
	install -m 644 OFFLINE_UPDATES.md $(SHARE_DIR)/OFFLINE_UPDATES.md
	install -m 644 RECOVERY.md $(SHARE_DIR)/RECOVERY.md
	install -m 644 UPDATES.md $(SHARE_DIR)/UPDATES.md

	@echo "Main executable file to $(BIN_DIR)"
	mkdir -p $(BIN_DIR)
	install -m 755 src/nobara_sync.py $(BIN_DIR)/nobara-updater

symlinks:
	@echo "Creating symlinks in $(BIN_DIR)"
	ln -sf /usr/bin/nobara-updater $(BIN_DIR)/nobara-sync
	ln -sf /usr/share/nobara-codec-wizard/main.py $(BIN_DIR)/nobara-codec-wizard

clean:
	@echo "Cleaning up installed files and symlinks"
	rm -rf $(TARGET_DIR)
	rm -f $(BIN_DIR)/nobara-updater $(BIN_DIR)/nobara-sync $(LIBEXEC_DIR)/nobara-update-worker
	rm -f $(LIBEXEC_DIR)/nobara-update-notice $(DESTDIR)/etc/xdg/autostart/nobara-update-notice.desktop $(DESKTOP_DIR)/nobara-update-recovery.desktop
	rm -f $(LIBEXEC_DIR)/nobara-codec-guard $(DESTDIR)/etc/dnf/libdnf5-plugins/actions.d/nobara-codecs.actions
	rm -f $(BIN_DIR)/nobara-codec-wizard $(DESKTOP_DIR)/nobara-codec-wizard.desktop
	rm -rf $(CODEC_WIZARD_DIR)
	rm -f $(SYSTEMD_DIR)/nobara-updater-*.service
	rm -f $(DESTDIR)/etc/grub.d/42_nobara_update
	rm -rf $(DESTDIR)/usr/lib/dracut/modules.d/91nobara-rollback
	rm -f $(SYSTEMD_DIR)/system-update.target.wants/nobara-updater-offline.service
	rm -f $(SYSTEMD_DIR)/multi-user.target.wants/nobara-updater-confirm.service
	$(foreach unit,$(OFFLINE_GUARDS),rm -f $(SYSTEMD_DIR)/$(unit).d/nobara-updater.conf;)
	rm -rf $(SHARE_DIR)
