#! /usr/bin/bash
echo "Removing Nvidia graphics drivers"
if rpm -q nvidia-driver
then
	if rpm -q nvidia-driver
	then
		pkexec dnf remove -y nvidia*
		pkexec dnf remove -y kmod-nvidia*
		pkexec dnf remove -y akmod-nvidia
		pkexec dnf remove -y dkms-nvidia
		pkexec rm -rf /var/lib/dkms/nvidia*
		# always run akmods and dracut at the end of updates
		pkexec akmods
		pkexec dracut -f --regenerate-all
	else
		pkexec dnf remove -y nvidia*
		pkexec dnf remove -y kmod-nvidia*
		pkexec dnf remove -y akmod-nvidia
		pkexec dnf remove -y dkms-nvidia
		pkexec rm -rf /var/lib/dkms/nvidia*
		pkexec dnf install -y akmod-nvidia \
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
		pkexec systemctl enable --now akmods;
		# always run akmods and dracut at the end of updates
		pkexec akmods
		pkexec dracut -f --regenerate-all
	fi

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
