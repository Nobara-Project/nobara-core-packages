#! /usr/bin/bash

echo "Installing Nvidia graphics drivers"
pkexec bash -c "dnf remove -y nvidia*; dnf remove -y kmod-nvidia*; dnf remove -y akmod-nvidia; dnf remove -y dkms-nvidia; rm -rf /var/lib/dkms/nvidia*; dnf install -y akmod-nvidia nvidia-driver nvidia-driver-NVML nvidia-driver-NVML.i686 nvidia-driver-NvFBCOpenGL nvidia-driver-cuda nvidia-driver-cuda-libs nvidia-driver-cuda-libs.i686 nvidia-driver-libs nvidia-driver-libs.i686 nvidia-kmod-common nvidia-libXNVCtrl nvidia-modprobe nvidia-persistenced nvidia-settings nvidia-xconfig nvidia-vaapi-driver nvidia-gpu-firmware --refresh; systemctl enable --now akmods; akmods"
        
REBOOT_REQUIRED="yes"
if [ "$REBOOT_REQUIRED" == "yes" ]; then

     	zenity --question \
       	--title="Reboot Required." \
       	--width=600 \
       	--text="`printf "The system requires a reboot before changes can take effect. Would you like to reboot now?\n\n"`"

     	if [ $? = 0 ]; then
     		shutdown -r now &>>/tmp/nvcheck.log || {
          			zenity --error --text="Failed to issue reboot:\n\n $(cat /tmp/nvcheck.log)\n\n Please reboot the system manually."
          			exit 1
        	}
     	else
   		exit 0
     	fi
	/etc/nobara/scripts/cosmo-nvidia-wizard/end.sh
fi	
