export PATH := $(PWD):$(PATH)

all:
	true
#
build:
	cargo fetch
	cargo build --release

install_no_build:
	mkdir -p $(DESTDIR)/usr/bin/
	cp -vf target/release/nobara-welcome $(DESTDIR)/usr/bin/
	chmod 755 $(DESTDIR)/usr/bin/nobara-welcome
	cp -vf data/bin/nobara-welcome-autostart $(DESTDIR)/usr/bin/
	chmod 755 $(DESTDIR)/usr/bin/nobara-welcome-autostart
	cp -vf data/bin/nobara-resolve-wizard $(DESTDIR)/usr/bin/
	chmod 755 $(DESTDIR)/usr/bin/nobara-resolve-wizard
	mkdir -p $(DESTDIR)/usr/share/glib-2.0/schemas/
	mkdir -p $(DESTDIR)/usr/share/
	mkdir -p $(DESTDIR)/usr/lib/nobara/nobara-welcome/
	cp -rvf data/scripts $(DESTDIR)/usr/lib/nobara/nobara-welcome/
	chmod 755 $(DESTDIR)/usr/lib/nobara/nobara-welcome/scripts/*.sh
	cp data/com.github.nobaraproject.nobarawelcome.gschema.xml $(DESTDIR)/usr/share/glib-2.0/schemas/
	mkdir -p $(DESTDIR)/usr/share/applications
	cp -vf data/com.github.nobaraproject.nobarawelcome.desktop  $(DESTDIR)/usr/share/applications/
	mkdir -p $(DESTDIR)/etc/xdg/autostart
	cp -vf data/nobara-welcome-autostart.desktop  $(DESTDIR)/etc/xdg/autostart/
	mkdir -p $(DESTDIR)/usr/share/icons/hicolor/scalable/apps
	cp -vf data/com.github.nobaraproject.nobarawelcome.svg $(DESTDIR)/usr/share/icons/hicolor/scalable/apps/
	cp -rvf data/icons $(DESTDIR)/usr/share/
	mkdir -p $(DESTDIR)/usr/share/nobara/nobara-welcome
	cp -rvf data/config $(DESTDIR)/usr/share/nobara/nobara-welcome/

install:
	mkdir -p $(DESTDIR)/usr/bin/
	cargo fetch
	cargo build --release
	cp -vf target/release/nobara-welcome $(DESTDIR)/usr/bin/
	chmod 755 $(DESTDIR)/usr/bin/nobara-welcome
	cp -vf data/bin/nobara-welcome-autostart $(DESTDIR)/usr/bin/
	chmod 755 $(DESTDIR)/usr/bin/nobara-welcome-autostart
	cp -vf data/bin/nobara-resolve-wizard $(DESTDIR)/usr/bin/
	chmod 755 $(DESTDIR)/usr/bin/nobara-resolve-wizard
	mkdir -p $(DESTDIR)/usr/share/glib-2.0/schemas/
	mkdir -p $(DESTDIR)/usr/share/
	mkdir -p $(DESTDIR)/usr/lib/nobara/nobara-welcome/
	cp -rvf data/scripts $(DESTDIR)/usr/lib/nobara/nobara-welcome/scripts/
	chmod 755 $(DESTDIR)/usr/lib/nobara/nobara-welcome/scripts/*.sh
	cp data/com.github.nobaraproject.nobarawelcome.gschema.xml $(DESTDIR)/usr/share/glib-2.0/schemas/
	mkdir -p $(DESTDIR)/usr/share/applications
	cp -vf data/com.github.nobaraproject.nobarawelcome.desktop  $(DESTDIR)/usr/share/applications/
	mkdir -p $(DESTDIR)/etc/xdg/autostart
	cp -vf data/nobara-welcome-autostart.desktop  $(DESTDIR)/etc/xdg/autostart/
	mkdir -p $(DESTDIR)/usr/share/icons/hicolor/scalable/apps
	cp -vf data/com.github.nobaraproject.nobarawelcome.svg $(DESTDIR)/usr/share/icons/hicolor/scalable/apps/
	cp -rvf data/icons $(DESTDIR)/usr/share/
	mkdir -p $(DESTDIR)/usr/share/nobara/nobara-welcome
	cp -rvf data/config $(DESTDIR)/usr/share/nobara/nobara-welcome/
