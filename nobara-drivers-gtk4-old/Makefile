all:
	true

build:
	cargo fetch
	cargo build --release

install_no_build:
	mkdir -p $(DESTDIR)/usr/bin/
	cp -vf target/release/nobara-drivers $(DESTDIR)/usr/bin/
	chmod 755 $(DESTDIR)/usr/bin/nobara-drivers
	mkdir -p $(DESTDIR)/usr/lib/nobara/drivers/
	mkdir -p $(DESTDIR)/usr/share/glib-2.0/schemas/
	cp data/*.sh $(DESTDIR)/usr/lib/nobara/drivers/
	cp data/*.xml $(DESTDIR)/usr/share/glib-2.0/schemas/
	chmod 755 $(DESTDIR)/usr/lib/nobara/drivers/*.sh
	mkdir -p $(DESTDIR)/usr/share/applications
	mkdir -p $(DESTDIR)/usr/share/icons/hicolor/scalable/apps
	cp -vf data/com.github.nobaraproject.nobaradrivers.svg $(DESTDIR)/usr/share/icons/hicolor/scalable/apps/
	cp -vf data/com.github.nobaraproject.nobaradrivers.desktop  $(DESTDIR)/usr/share/applications/

install:
	mkdir -p $(DESTDIR)/usr/bin/
	cargo fetch
	cargo build --release
	cp -vf target/release/nobara-drivers $(DESTDIR)/usr/bin/
	chmod 755 $(DESTDIR)/usr/bin/nobara-drivers
	mkdir -p $(DESTDIR)/usr/lib/nobara/drivers/
	mkdir -p $(DESTDIR)/usr/share/glib-2.0/schemas/
	cp data/*.sh $(DESTDIR)/usr/lib/nobara/drivers/
	cp data/*.xml $(DESTDIR)/usr/share/glib-2.0/schemas/
	chmod 755 $(DESTDIR)/usr/lib/nobara/drivers/*.sh
	mkdir -p $(DESTDIR)/usr/share/applications
	mkdir -p $(DESTDIR)/usr/share/icons/hicolor/scalable/apps
	cp -vf data/com.github.nobaraproject.nobaradrivers.svg $(DESTDIR)/usr/share/icons/hicolor/scalable/apps/
	cp -vf data/com.github.nobaraproject.nobaradrivers.desktop  $(DESTDIR)/usr/share/applications/
