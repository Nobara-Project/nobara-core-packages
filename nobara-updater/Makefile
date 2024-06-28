PYTHON_SITE_PACKAGES := $(shell python3 -c "import site; print(site.getsitepackages()[0].replace('lib64', 'lib'))")
TARGET_DIR := $(DESTDIR)$(PYTHON_SITE_PACKAGES)/nobara_updater
BIN_DIR := $(DESTDIR)/usr/bin
DESKTOP_DIR := $(DESTDIR)/usr/share/applications
ICON_DIR := $(DESTDIR)/usr/share/icons/hicolor/64x64/apps
LICENSE_DIR := $(DESTDIR)/usr/share/licenses/nobara-updater

.PHONY: all install symlinks clean

all: install symlinks

install:
	@echo "Installing Python files to $(TARGET_DIR)"
	mkdir -p $(TARGET_DIR)
	install -m 644 src/dnf4.py $(TARGET_DIR)/dnf4.py
	install -m 644 src/dnf5.py $(TARGET_DIR)/dnf5.py
	install -m 644 src/quirks.py $(TARGET_DIR)/quirks.py
	install -m 644 src/run_as.py $(TARGET_DIR)/run_as.py
	install -m 644 src/run_as_user_target.py $(TARGET_DIR)/run_as_user_target.py
	install -m 644 src/shared_functions.py $(TARGET_DIR)/shared_functions.py

	@echo "Installing desktop file to $(DESKTOP_DIR)"
	mkdir -p $(DESKTOP_DIR)
	install -m 644 data/nobara-updater.desktop $(DESKTOP_DIR)/nobara-updater.desktop

	@echo "Installing icon file to $(ICON_DIR)"
	mkdir -p $(ICON_DIR)
	install -m 644 icons/hicolor/64x64/apps/nobara-updater.svg $(ICON_DIR)/nobara-updater.svg

	@echo "Installing license file to $(LICENSE_DIR)"
	mkdir -p $(LICENSE_DIR)
	install -m 644 licenses/LICENSE $(LICENSE_DIR)/LICENSE

	@echo "Main executable file to $(BIN_DIR)"
	mkdir -p $(BIN_DIR)
	install -m 755 src/nobara_sync.py $(BIN_DIR)/nobara-updater

symlinks:
	@echo "Creating symlinks in $(BIN_DIR)"
	ln -sf /usr/bin/nobara-updater $(BIN_DIR)/nobara-sync

clean:
	@echo "Cleaning up installed files and symlinks"
	rm -rf $(TARGET_DIR)
	rm -f $(BIN_DIR)/nobara-sync
	rm -f $(BIN_DIR)/nobara-updater
	rm -f $(DESKTOP_DIR)/nobara-updater.desktop
	rm -f $(ICON_DIR)/nobara-updater.svg
