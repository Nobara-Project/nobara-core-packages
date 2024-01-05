ANACONDADATADIR = /usr/share/anaconda
ANACONDABOOTDIR = $(ANACONDADATADIR)/boot

all: 
	@echo "Nothing to do."

splash.lss: syslinux-splash.png
	./splashtolss.sh syslinux-splash.png splash.lss

install:
	@if [ "$(DESTDIR)" = "" ]; then \
		echo " "; \
		echo "ERROR: A destdir is required"; \
		exit 1; \
	fi

	mkdir -p $(DESTDIR)/$(ANACONDADATADIR)
	mkdir -p $(DESTDIR)/$(ANACONDABOOTDIR)
	mkdir -p $(DESTDIR)/$(ANACONDADATADIR)/pixmaps
	install -m 644 anaconda_header.png $(DESTDIR)/$(ANACONDADATADIR)/pixmaps
	install -m 644 progress_first.png $(DESTDIR)/$(ANACONDADATADIR)/pixmaps
	install -m 644 splash.png $(DESTDIR)/$(ANACONDADATADIR)/pixmaps

	install -m 644 syslinux-splash.png $(DESTDIR)/$(ANACONDABOOTDIR)/syslinux-splash.png
	# install -m 755 splashtolss.sh $(DESTDIR)/$(ANACONDADATADIR)
	install -m 644 syslinux-vesa-splash.png $(DESTDIR)/$(ANACONDABOOTDIR)/splash.png

	install -m 644 fedora.css $(DESTDIR)/$(ANACONDADATADIR)/pixmaps
	install -m 644 sidebar-logo.png $(DESTDIR)/$(ANACONDADATADIR)/pixmaps
	install -m 644 sidebar-bg.png $(DESTDIR)/$(ANACONDADATADIR)/pixmaps
	install -m 644 topbar-bg.png $(DESTDIR)/$(ANACONDADATADIR)/pixmaps

install-lss: splash.lss
	install -m 644 splash.lss $(DESTDIR)/$(ANACONDABOOTDIR)

clean:
	rm -f splash.lss
