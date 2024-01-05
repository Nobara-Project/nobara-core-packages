NAME = fedora-logos
XML = backgrounds/desktop-backgrounds-fedora.xml

all: update-po bootloader/fedora.icns

bootloader/fedora.icns: pixmaps/fedora-logo-sprite.svg
	convert -background none -resize 128x128 pixmaps/fedora-logo-sprite.svg pixmaps/fedora-logo-sprite.png
	zopflipng -ym pixmaps/fedora-logo-sprite.png pixmaps/fedora-logo-sprite.png
	png2icns bootloader/fedora.icns pixmaps/fedora-logo-sprite.png

bootloader/bootlogo_128.png: pixmaps/fedora-logo-sprite.svg
	convert -background none -resize 128x128 pixmaps/fedora-logo-sprite.svg bootloader/bootlogo_128.png
	zopflipng -ym bootloader/bootlogo_128.png bootloader/bootlogo_128.png

bootloader/bootlogo_256.png: pixmaps/fedora-logo-sprite.svg
	convert -background none -resize 256x256 pixmaps/fedora-logo-sprite.svg bootloader/bootlogo_256.png
	zopflipng -ym bootloader/bootlogo_256.png bootloader/bootlogo_256.png

optimize:
	find . -name "*.png" -printf "%p %p\n" | \
	xargs -L 1 -P `getconf _NPROCESSORS_ONLN 2>/dev/null || echo 1` \
	zopflipng -ym

update-po:
	@echo "updating pot files..."
	sed -e "s/_name/name/g" $(XML).in > $(XML)
	# FIXME need to handle translations
	#
	#( cd po && intltool-update --gettext-package=$(NAME) --pot )
	#@echo "merging existing po files to xml..."
	#intltool-merge -x po $(XML).in $(XML)

clean:
	rm -f pixmaps/fedora-logo-sprite.png bootloader/fedora.icns bootloader/bootlogo_128.png bootloader/bootlogo_256.png
