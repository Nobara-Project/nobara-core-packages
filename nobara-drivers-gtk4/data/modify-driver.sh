#! /bin/bash

set -e

# Special override for nvidia-driver
if [[ "$1" = "nvidia-driver" ]]
then
	if rpm -q nvidia-driver
	then
		dnf remove -y nvidia*
		dnf remove -y kmod-nvidia*
		dnf remove -y akmod-nvidia
		dnf remove -y dkms-nvidia
		rm -rf /var/lib/dkms/nvidia*
		# reinstall firmware
		dnf install -y nvidia-gpu-firmware
		# always run akmods and dracut at the end of updates
		akmods
		dracut -f --regenerate-all
	else
		dnf remove -y nvidia*
		dnf remove -y kmod-nvidia*
		dnf remove -y akmod-nvidia
		dnf remove -y dkms-nvidia
		rm -rf /var/lib/dkms/nvidia*
		dnf install -y akmod-nvidia \
			nvidia-driver libnvidia-ml \
			libnvidia-ml.i686 \
			libnvidia-fbc \
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
			libnvidia-cfg \
			--refresh
		systemctl enable --now akmods;
		# always run akmods and dracut at the end of updates
		akmods
		dracut -f --regenerate-all
	fi

	exit 0
elif [[ "$1" = "rocm-meta" ]]
then
	if rpm -q rocm-meta
	then
		dnf remove -y rocm-meta \
			rocm-comgr \
			rocm-runtime \
			rocm-smi \
			rocm-clinfo \
			rocm-cmake \
			rocm-core \
			rocm-rpm-macros \
			python3-torch-rocm-gfx9 \
			python3-torchaudio-rocm-gfx9 \
			rocprim-devel \
			rocblas \
			rocsparse \
			rocminfo \
			rocrand \
			hipblas \
			hipfft \
			hipsolver \
			rocclr \
			rocfft \
			rocsolver \
			hipblaslt \
			rocalution \
			roctracer \
			rocm-opencl \
			comgr \
			hip-devel \
			hip-runtime-amd \
			hipcc \
			hsa-rocr \
			hsa-rocr-devel \
			hsakmt-roct-devel \
			openmp-extras-runtime \
			rocm-core \
			rocm-device-libs \
			rocm-hip-runtime \
			rocm-language-runtime \
			rocm-llvm \
			rocm-opencl \
			rocm-opencl-icd-loader \
			rocm-opencl-runtime \
			rocm-smi-lib \
			rocminfo \
			rocprofiler-register
	else
		dnf install -y rocm-meta \
			rocm-comgr \
			rocm-runtime \
			rocm-smi \
			rocm-clinfo \
			rocm-cmake \
			rocm-core \
			rocm-rpm-macros \
			python3-torch-rocm-gfx9 \
			python3-torchaudio-rocm-gfx9 \
			rocprim-devel \
			rocblas \
			rocsparse \
			rocminfo \
			rocrand \
			hipblas \
			hipfft \
			hipsolver \
			rocclr \
			rocfft \
			rocsolver \
			hipblaslt \
			rocalution \
			roctracer \
			rocm-opencl \
		--refresh
	fi
	exit 0
# Special override for xone
elif [[ "$1" = "xone" ]]
then
	if rpm -q xone
	then
		dnf remove -y lpf-xone-firmware xone xone-firmware xpad-noone
	else
		dnf install -y "dnf5-command(builddep)"
		usermod -aG pkg-build $SUDO_USER && dnf4 install -y lpf-xone-firmware xone && dnf4 remove -y xone-firmware
		exit 0
	fi
	exit 0
# Special override for asusctl
elif [[ "$1" = "asusctl" ]]
then
	if rpm -q asusctl
	then
		dnf remove -y asusctl asusctl-rog-gui
	else
		dnf install -y asusctl asusctl-rog-gui
	fi
	exit 0
# Standard case
else
	if rpm -q "$1"
	then
		dnf remove -y "$1"
	else
		dnf install -y "$1"
	fi
	exit 0
fi
