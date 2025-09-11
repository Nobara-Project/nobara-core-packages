BIN_DIR := $(DESTDIR)/usr/bin
DESKTOP_DIR := $(DESTDIR)/usr/share/applications
POLKIT_DIR := $(DESTDIR)/usr/share/polkit-1/actions
LIBEXEC_DIR := $(DESTDIR)/usr/libexec
LICENSE_DIR := $(DESTDIR)/usr/share/licenses/nobara-automount

.PHONY: all install symlinks clean

all: install symlinks

install:
	@echo "Installing desktop file to $(DESKTOP_DIR)"
	mkdir -p $(DESKTOP_DIR)
	install -m 644 data/nobara-drive-mount-manager.desktop $(DESKTOP_DIR)/nobara-drive-mount-manager.desktop

	@echo "Installing automount polkit files to $(POLKIT_DIR)"
	mkdir -p $(POLKIT_DIR)
	install -m 644 data/nobara-automount/usr/share/polkit-1/actions/org.nobaraproject.automount.policy $(POLKIT_DIR)/org.nobaraproject.automount.policy

	@echo "Installing automount libexec files to $(LIBEXEC_DIR)"
	mkdir -p $(LIBEXEC_DIR)
	install -m 755 data/nobara-automount/usr/libexec/nobara-automount $(LIBEXEC_DIR)/nobara-automount

	@echo "Installing license file to $(LICENSE_DIR)"
	mkdir -p $(LICENSE_DIR)
	install -m 644 licenses/LICENSE $(LICENSE_DIR)/LICENSE

	@echo "Drive mount manager executable file to $(BIN_DIR)"
	mkdir -p $(BIN_DIR)
	install -m 755 src/nobara_drive_mount_manager.py $(BIN_DIR)/nobara-drive-mount-manager

symlinks:
	@echo "Creating symlinks in $(BIN_DIR)"
	ln -sf /usr/bin/nobara-updater $(BIN_DIR)/nobara-sync
	ln -sf /usr/share/nobara-codec-wizard/main.py $(BIN_DIR)/nobara-codec-wizard

clean:
	@echo "Cleaning up installed files and symlinks"
	rm -rf $(TARGET_DIR)
	rm -rf $(BIN_DIR)
	rm -rf $(DESKTOP_DIR)
	rm -rf $(POLKIT_DIR)
	rm -rf $(LIBEXEC_DIR)
	rm -rf $(LICENSE_DIR)
