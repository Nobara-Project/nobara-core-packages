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
elif [[ "$1" = "cuda-devel" ]]
then
	if rpm -q cuda-devel
	then
		# uninstall first
		rpm -e --nodeps cuda-devel
		rpm -e --nodeps cuda
		rpm -e --nodeps cuda-cccl-devel
		rpm -e --nodeps cuda-cudart
		rpm -e --nodeps cuda-cudart-devel
		rpm -e --nodeps cuda-cuobjdump
		rpm -e --nodeps cuda-cupti
		rpm -e --nodeps cuda-cupti-devel
		rpm -e --nodeps cuda-cuxxfilt-devel
		rpm -e --nodeps cuda-gcc
		rpm -e --nodeps cuda-gcc-c++
		rpm -e --nodeps cuda-libs
		rpm -e --nodeps cuda-nvcc
		rpm -e --nodeps cuda-nvprof
		rpm -e --nodeps cuda-nvprof-devel
		rpm -e --nodeps cuda-nvprune
		rpm -e --nodeps cuda-nvrtc
		rpm -e --nodeps cuda-nvrtc-devel
		rpm -e --nodeps cuda-nvtx
		rpm -e --nodeps cuda-nvtx-devel
		rpm -e --nodeps isl
		rpm -e --nodeps libcublas
		rpm -e --nodeps libcublas-devel
		rpm -e --nodeps libcufft
		rpm -e --nodeps libcufft-devel
		rpm -e --nodeps libcufile
		rpm -e --nodeps libcufile-devel
		rpm -e --nodeps libcurand
		rpm -e --nodeps libcurand-devel
		rpm -e --nodeps libcusolver
		rpm -e --nodeps libcusolver-devel
		rpm -e --nodeps libcusparse
		rpm -e --nodeps libcusparse-devel
		rpm -e --nodeps libnpp
		rpm -e --nodeps libnpp-devel
		rpm -e --nodeps libnvjitlink
		rpm -e --nodeps libnvjpeg
		rpm -e --nodeps libnvjpeg-devel
		rpm -e --nodeps librdmacm
		rpm -e --nodeps opencl-headers
		# reinstall
		dnf install -y isl
		dnf install -y cuda-devel
	else
		dnf install -y isl
		dnf install -y cuda-devel
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
elif [[ "$1" = "mesa-vulkan-drivers-git" ]]
then
	if rpm -q mesa-vulkan-drivers-git
	then
		rpm -e --nodeps mesa-vulkan-drivers-git.x86_64
		rpm -e --nodeps mesa-vulkan-drivers-git.i686
		dnf install -y mesa-vulkan-drivers.x86_64 mesa-vulkan-drivers.i686 --refresh
	else
		rpm -e --nodeps mesa-vulkan-drivers.x86_64
		rpm -e --nodeps mesa-vulkan-drivers.i686
		dnf install -y mesa-vulkan-drivers-git.x86_64 mesa-vulkan-drivers-git.i686 --refresh
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
