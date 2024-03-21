#! /bin/bash

set -e

if [[ "$1" = "nvidia-driver" ]]
then
	if rpm -q nvidia-driver
	then
		dnf remove -y nvidia*
		dnf remove -y kmod-nvidia*
		dnf remove -y akmod-nvidia
		dnf remove -y dkms-nvidia
		rm -rf /var/lib/dkms/nvidia*
	else
		dnf remove -y nvidia*
		dnf remove -y kmod-nvidia*
		dnf remove -y akmod-nvidia
		dnf remove -y dkms-nvidia
		rm -rf /var/lib/dkms/nvidia*
		dnf install -y akmod-nvidia \
			nvidia-driver nvidia-driver-NVML \
			nvidia-driver-NVML.i686 \
			nvidia-driver-NvFBCOpenGL \
			nvidia-driver-cuda \
			nvidia-driver-cuda-libs \
			nvidia-driver-cuda-libs.i686 \
			nvidia-driver-libs \
			nvidia-driver-libs.i686 \
			nvidia-kmod-common \
			nvidia-libXNVCtrl \
			nvidia-modprobe \
			nvidia-persistenced \
			nvidia-settings \
			nvidia-xconfig \
			nvidia-vaapi-driver \
			nvidia-gpu-firmware \
			--refresh
		systemctl enable --now akmods; akmods
	fi
	exit 0
fi

if [[ "$1" = "xone" ]]
then
	if rpm -q xone
	then
		pkcon remove -y lpf-xone-firmware xone xpadneo xone-firmware
	else
		dnf install -y "dnf5-command(builddep)"
		usermod -aG pkg-build $USER && dnf4 install -y lpf-xone-firmware xone xpadneo && dnf4 remove -y xone-firmware
		lpf reset xone-firmware
		lpf update xone-firmware
	fi
	exit 0
fi

if [[ "$1" = "rocm-meta" ]]
then
	if rpm -q rocm-meta
	then
		pkcon remove -y rocm-meta
	else
		pkcon install -y rocm-meta
	fi
	exit 0
fi