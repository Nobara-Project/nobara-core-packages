#!/bin/sh

# fix libpango crash
if [[ ! -z $(ls /opt/resolve/libs | grep libglib-2) ]]; then
	mkdir -p /opt/resolve/libs/_disabled
	mv /opt/resolve/libs/libglib-2.0.so* /opt/resolve/libs/_disabled/
fi

# install rocm packages for opencl support
dnf install -y rocm-opencl

