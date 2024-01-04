#! /usr/bin/bash
echo "Removing Nvidia graphics drivers"
pkexec dnf remove -y nvidia-driver dkms-nvidia nvidia-settings nvidia-driver-cuda nvidia-kmod-common nvidia-driver-libs.i686 nvidia-driver-cuda-libs.i686
	
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
