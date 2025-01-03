PYTHON_SITE_PACKAGES := $(shell python3 -c "import site; print(site.getsitepackages()[0].replace('lib64', 'lib'))")
TARGET_DIR := $(DESTDIR)$(PYTHON_SITE_PACKAGES)/nobara_updater
BIN_DIR := $(DESTDIR)/usr/bin
DESKTOP_DIR := $(DESTDIR)/usr/share/applications
ICON_DIR := $(DESTDIR)/usr/share/icons/hicolor/64x64/apps
BROWSER_SELECT_DIR := $(DESTDIR)/usr/share/nobara-gamescope/browser-select/
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
	install -m 644 data/nobara-tweak-tool.desktop $(DESKTOP_DIR)/nobara-tweak-tool.desktop
	install -m 644 data/nobara-updater.desktop $(DESKTOP_DIR)/nobara-rpm-installer.desktop

	@echo "Installing icon file to $(ICON_DIR)"
	mkdir -p $(ICON_DIR)
	install -m 644 icons/hicolor/64x64/apps/nobara-updater.svg $(ICON_DIR)/nobara-updater.svg

	@echo "Installing gamescope browser select files to $(BROWSER_SELECT_DIR)"
	mkdir -p $(BROWSER_SELECT_DIR)
	install -m 644 icons/browser-select/brave_icon.png $(BROWSER_SELECT_DIR)/brave_icon.png
	install -m 644 icons/browser-select/chrome_icon.png $(BROWSER_SELECT_DIR)/chrome_icon.png
	install -m 644 icons/browser-select/edge_icon.png $(BROWSER_SELECT_DIR)/edge_icon.png
	install -m 644 icons/browser-select/firefox_icon.png $(BROWSER_SELECT_DIR)/firefox_icon.png

	@echo "Installing license file to $(LICENSE_DIR)"
	mkdir -p $(LICENSE_DIR)
	install -m 644 licenses/LICENSE $(LICENSE_DIR)/LICENSE

	@echo "Main executable file to $(BIN_DIR)"
	mkdir -p $(BIN_DIR)
	install -m 755 src/nobara_sync.py $(BIN_DIR)/nobara-updater

	@echo "RPM Installer $(BIN_DIR)"
	mkdir -p $(BIN_DIR)
	install -m 755 src/rpm_installer.py $(BIN_DIR)/nobara-rpm-installer

	@echo "Gamescope updater executable file to $(BIN_DIR)"
	mkdir -p $(BIN_DIR)
	install -m 755 src/gamescope_gui.py $(BIN_DIR)/nobara-updater-gamescope-gui

	@echo "Gamescope browser choice executable file to $(BIN_DIR)"
	mkdir -p $(BIN_DIR)
	install -m 755 src/browser_select.py $(BIN_DIR)/nobara-browser-select

	@echo "Tweak tool executable file to $(BIN_DIR)"
	mkdir -p $(BIN_DIR)
	install -m 755 src/nobara_tweak_tool.py $(BIN_DIR)/nobara-tweak-tool

symlinks:
	@echo "Creating symlinks in $(BIN_DIR)"
	ln -sf /usr/bin/nobara-updater $(BIN_DIR)/nobara-sync

clean:
	@echo "Cleaning up installed files and symlinks"
	rm -rf $(TARGET_DIR)
	rm -f $(BIN_DIR)/nobara-sync
	rm -f $(BIN_DIR)/nobara-updater
	rm -f $(BIN_DIR)/nobara-updater-gamescope-gui
	rm -f $(BIN_DIR)/nobara-browser-select
	rm -f $(BIN_DIR)/nobara-tweak-tool
	rm -f $(DESKTOP_DIR)/nobara-updater.desktop
	rm -f $(DESKTOP_DIR)/nobara-tweak-tool
	rm -f $(ICON_DIR)/nobara-updater.svg
