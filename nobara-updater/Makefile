PYTHON_SITE_PACKAGES := $(shell python3 -c "import site; print(site.getsitepackages()[0])")
TARGET_DIR := $(PYTHON_SITE_PACKAGES)/nobara-updater
BIN_DIR := /usr/bin

.PHONY: all install symlinks clean

all: install symlinks

install:
	@echo "Installing Python files to $(TARGET_DIR)"
	mkdir -p $(TARGET_DIR)
	install -m 644 dnf4.py $(TARGET_DIR)
	install -m 644 dnf5.py $(TARGET_DIR)
	install -m 644 nobara_sync.py $(TARGET_DIR)
	install -m 644 quirks.py $(TARGET_DIR)
	install -m 644 run_as_user_target.py $(TARGET_DIR)
	install -m 644 shared_functions.py $(TARGET_DIR)

symlinks:
	@echo "Creating symlinks in $(BIN_DIR)"
	ln -sf $(TARGET_DIR)/nobara_sync.py $(BIN_DIR)/nobara-sync
	ln -sf $(TARGET_DIR)/nobara_sync.py $(BIN_DIR)/nobara-updater

clean:
	@echo "Cleaning up installed files and symlinks"
	rm -rf $(TARGET_DIR)
	rm -f $(BIN_DIR)/nobara-sync
	rm -f $(BIN_DIR)/nobara-updater
