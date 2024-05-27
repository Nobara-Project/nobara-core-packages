#!/usr/bin/bash

script=$(realpath $0)
displayuser="$2"
USERHOME=$(echo $(getent passwd "$displayuser" | cut -d: -f6))
loglocation="$USERHOME"/.nobara-sync.log

# Become root
if [[ $EUID != 0 ]]; then
	if [[ -n $DISPLAY ]]; then

		clear

		tput csr 5 $((LINES - 1))

		echo ""
		echo "==========================================="
		echo "Nobara System Updater"
		echo "==========================================="
		echo ""
		echo ""
		echo ""
		echo ""
		echo ""
		echo ""

		# We use $USER and $HOME here because $displayuser does not get set until after the script is re-executed
		exec script -q -c "pkexec env DISPLAY=$DISPLAY XAUTHORITY=$XAUTHORITY \"$script\" \"nobara-sync-gui\" \"$USER\"" | tee $HOME/.nobara-sync.log
	fi
	exit 1
elif [[ $EUID == 0 ]]; then
	if [[ ! -n $DISPLAY ]]; then
		if [[ "$1" == "nobara-cli" ]]; then

			clear

			tput csr 5 $((LINES - 1))

			echo ""
			echo "==========================================="
			echo "Nobara System Updater"
			echo "==========================================="
			echo ""
			echo ""
			echo ""
			echo ""
			echo ""
			echo ""

			rm $loglocation
			touch $loglocation
			# $2 is the user, we pass this so that we can give the user permissions to the log
			script -q -c "$(realpath $0) nobara-sync-cli $displayuser" /dev/null | tee $loglocation
			exit 1
		fi
	fi
fi

if [[ -n "$2" ]]; then
	chown $2:$2 $loglocation
fi

# The captured log output is unbuffered, so we need to clean it up
log_cleanup() {
	# ansi2txt and col read the unbuffered log data and interprets the control characters
	# then it prints the control character translated output to a temp file
	# then write the temp file contents back to the original
	cat $loglocation | ansi2txt | col -bx >/tmp/logcleanup
	cat /tmp/logcleanup >$loglocation
	rm /tmp/logcleanup &>/dev/null
	rm "$USERHOME"/typescript &>/dev/null
}

INTERNET="no"
DNF_STATE_STAGE=false

internet_check() {
	# Check for internet connection
	if wget -q --spider http://cloudflare.com; then
		export INTERNET="yes"
	fi
}

dnf_update() {
	echo "#####"
	echo "Updating Repository Packages"
	echo "#####"

	# Store shasums of fedora and nobara repos so we can check for changes

	SHAFEDORA=$(sha256sum /etc/yum.repos.d/fedora.repo)
	SHAFEDORAUPDATES=$(sha256sum /etc/yum.repos.d/fedora-updates.repo)
	SHAFEDORACISCO=$(sha256sum /etc/yum.repos.d/fedora-cisco-openh264.repo)
	SHANOBARA=$(sha256sum /etc/yum.repos.d/nobara.repo)

	# Get repo updates first

	sudo dnf5 update -y fedora-gpg-keys nobara-gpg-keys fedora-repos nobara-repos --nogpgcheck --refresh

	# Make sure rpmfusion repos never get removed -- this can happen by accident if migrating between gnome and kde

	if [[ ! -e /etc/yum.repos.d/rpmfusion-free.repo ]]; then
		sudo dnf5 install -y rpmfusion-free-release
	fi

	if [[ ! -e /etc/yum.repos.d/rpmfusion-free-updates.repo ]]; then
		sudo dnf5 install -y rpmfusion-free-release
	fi

	if [[ ! -e /etc/yum.repos.d/rpmfusion-nonfree.repo ]]; then
		sudo dnf5 install -y rpmfusion-nonfree-release
	fi

	if [[ ! -e /etc/yum.repos.d/rpmfusion-nonfree-updates.repo ]]; then
		sudo dnf5 install -y rpmfusion-nonfree-release
	fi

	sudo -S dnf5 update -y rpmfusion-nonfree-release rpmfusion-free-release

	# Check against shasums for new repo files
	# We don't check against rpmfusion because those should never change.

	REFRESH=""
	if [[ $SHAFEDORA != $(sha256sum /etc/yum.repos.d/fedora.repo) ]]; then
		REFRESH="--refresh"
	fi
	if [[ $SHAFEDORAUPDATES != $(sha256sum /etc/yum.repos.d/fedora-updates.repo) ]]; then
		REFRESH="--refresh"
	fi
	if [[ $SHAFEDORACISCO != $(sha256sum /etc/yum.repos.d/fedora-cisco-openh264.repo) ]]; then
		REFRESH="--refresh"
	fi
	if [[ $SHANOBARA != $(sha256sum /etc/yum.repos.d/nobara.repo) ]]; then
		REFRESH="--refresh"
	fi

	# Store shasums of welcome app update script so we can check for changes

	UPDATEREFRESH="$(rpm -qa | grep nobara-welcome)"

	# Update the welcome app to check for update script changes

	sudo -S dnf5 update -y nobara-welcome $REFRESH

	# If script has been updated, restart it:

	if [[ "$UPDATEREFRESH" != "$(rpm -qa | grep nobara-welcome)" ]]; then
		if [[ -n $DISPLAY ]]; then
			exec sh -c "$script nobara-sync-gui $displayuser"
		else
			exec sh -c "$script nobara-sync-cli $displayuser"
		fi
	fi

	# Check for non-fedora shipped codec/multimedia packages. If they are installed, trigger media fixup to reset them
	MEDIAFIXUP=0

	# We want to only use libavcodec-freeworld, not ffmpeg-libs
	if [[ ! -z $(rpm -qa | grep ffmpeg-libs | grep x86_64) ]]; then
		MEDIAFIXUP=1
	fi

	if [[ ! -z $(rpm -qa | grep ffmpeg-libs | grep i686) ]]; then
		MEDIAFIXUP=1
	fi

	# We want to only use x264-libs, not x264
	if [[ ! -z $(rpm -qa | grep x264 | grep -v libs | grep -v devel | grep -v obs | grep x86_64) ]]; then
		MEDIAFIXUP=1
	fi

	# We want to only use x265-libs, not x265
	if [[ ! -z $(rpm -qa | grep x265 | grep -v libs | grep -v devel | grep -v obs | grep x86_64) ]]; then
		MEDIAFIXUP=1
	fi

	# 64 bit libavcodec-freeworld check
	if [[ ! -z $(rpm -qa | grep libavcodec-freeworld | grep x86_64) ]]; then
		if [[ ! -z $(rpm -qa | grep noopenh264 | grep x86_64) ]]; then
			MEDIAFIXUP=1
		fi
		if [[ -z $(rpm -qa | grep libavcodec-free | grep x86_64) ]]; then
			MEDIAFIXUP=1
		fi
	fi

	if [[ -z $(rpm -qa | grep libavcodec-freeworld | grep x86_64) ]]; then
		MEDIAFIXUP=1
	fi

	# 32 bit libavcodec-freeworld check
	if [[ ! -z $(rpm -qa | grep libavcodec-freeworld | grep i686) ]]; then
		if [[ ! -z $(rpm -qa | grep noopenh264 | grep i686) ]]; then
			MEDIAFIXUP=1
		fi
		if [[ -z $(rpm -qa | grep libavcodec-free | grep i686) ]]; then
			MEDIAFIXUP=1
		fi
	fi

	if [[ -z $(rpm -qa | grep libavcodec-freeworld | grep i686) ]]; then
		MEDIAFIXUP=1
	fi

	if [[ ! -z $(rpm -qa | grep noopenh264 | grep x86_64) ]]; then
		MEDIAFIXUP=1
	fi

	if [[ ! -z $(rpm -qa | grep noopenh264 | grep i686) ]]; then
		MEDIAFIXUP=1
	fi

	# This should not be installed
	if [[ ! -z $(rpm -qa | grep qt5-qtwebengine-freeworld) ]]; then
		MEDIAFIXUP=1
	fi

	# These -should- be installed
	if [[ -z $(rpm -qa | grep mesa-va-drivers-freeworld) ]]; then
		MEDIAFIXUP=1
	fi

	if [[ -z $(rpm -qa | grep mesa-vdpau-drivers-freeworld) ]]; then
		MEDIAFIXUP=1
	fi

	if [[ -z $(rpm -qa | grep gstreamer1-plugins-bad-free-extras) ]]; then
		MEDIAFIXUP=1
	fi

	media_fixup() {

		echo "#####"
		echo "Performing Multimedia Codec installation."
		echo "#####"

		## RESET TO STOCK (LIMITED FUNCTIONALITY)
		echo "INFO: Resetting possible modified multimedia packages to stock."
		sudo rpm -e --nodeps ffmpeg &>/dev/null
		sudo rpm -e --nodeps ffmpeg-libs.x86_64 &>/dev/null
		sudo rpm -e --nodeps ffmpeg-libs.i686 &>/dev/null
		sudo rpm -e --nodeps libavcodec-freeworld.x86_64 &>/dev/null
		sudo rpm -e --nodeps libavcodec-freeworld.i686 &>/dev/null
		sudo rpm -e --nodeps libavdevice.x86_64 &>/dev/null
		sudo rpm -e --nodeps libavdevice.i686 &>/dev/null
		sudo rpm -e --nodeps obs-studio-gstreamer-vaapi.x86_64 &>/dev/null
		sudo rpm -e --nodeps openh264 &>/dev/null
		sudo rpm -e --nodeps mesa-va-drivers-freeworld &>/dev/null
		sudo rpm -e --nodeps mesa-vdpau-drivers-freeworld &>/dev/null
		sudo rpm -e --nodeps noopenh264.x86_64 &>/dev/null
		sudo rpm -e --nodeps noopenh264.i686 &>/dev/null
		sudo rpm -e --nodeps x264.x86_64 &>/dev/null
		sudo rpm -e --nodeps x264-libs.x86_64 &>/dev/null
		sudo rpm -e --nodeps x264-libs.i686 &>/dev/null
		sudo rpm -e --nodeps x265.x86_64 &>/dev/null
		sudo rpm -e --nodeps x265-libs.x86_64 &>/dev/null
		sudo rpm -e --nodeps x265-libs.i686 &>/dev/null
		sudo dnf5 remove -y qt5-qtwebengine-freeworld &>/dev/null
		sudo dnf5 remove -y mozilla-openh264 &>/dev/null

		sudo dnf5 install -y ffmpeg-free --refresh
		sudo dnf5 install -y libavcodec-free.x86_64 libavcodec-free.i686
		sudo dnf5 install -y libavutil-free.x86_64 libavutil-free.i686
		sudo dnf5 install -y libswresample-free.x86_64 libswresample-free.i686
		sudo dnf5 install -y libavformat-free.x86_64 libavformat-free.i686
		sudo dnf5 install -y libpostproc-free.x86_64 libpostproc-free.i686
		sudo dnf5 install -y libswscale-free.x86_64 libswscale-free.i686
		sudo dnf5 install -y libavfilter-free.x86_64 libavfilter-free.i686
		sudo dnf5 install -y libavdevice-free.x86_64 libavdevice-free.i686
		sudo dnf5 install -y mesa-va-drivers.x86_64
		sudo dnf5 install -y mesa-vdpau-drivers.x86_64
		sudo dnf5 install -y gstreamer1-plugins-bad-free-extras.x86_64

		echo "INFO: Updating multimedia packages to codec-enabled versions."
		sudo dnf5 swap -y noopenh264.x86_64 openh264.x86_64
		sudo dnf5 remove -y noopenh264.i686 &>/dev/null
		sudo dnf5 install -y mozilla-openh264.x86_64
		sudo dnf5 install -y x264-libs.x86_64
		sudo dnf5 install -y x264-libs.i686
		sudo dnf5 install -y x265-libs.x86_64
		sudo dnf5 install -y x265-libs.i686

		sudo dnf5 group install -y multimedia --exclude=ffmpeg,ffmpeg-libs,qt5-qtwebengine-freeworld

		if [[ $(rpm -qa | grep libavcodec-freeworld | wc -l) < 2 ]]; then
			sudo dnf5 install -y libavcodec-freeworld.x86_64 libavcodec-freeworld.i686
		fi

		# Swap limited VAAPI encoders with full function versions
		sudo dnf5 swap -y mesa-va-drivers mesa-va-drivers-freeworld
		sudo dnf5 swap -y mesa-vdpau-drivers mesa-vdpau-drivers-freeworld
	}

	# Check for media codecs
	if [[ $MEDIAFIXUP == 1 ]]; then
		if [[ -n $DISPLAY ]]; then
			if zenity --question --text="Multimedia Codec packages are missing or not installed properly! These packages are needed for video playback (decoding) and encoding. Would you like to fix this?" 2>/dev/null; then
				media_fixup
			else
				echo "INFO: Skipping Multimedia Codec updates."
			fi
		else
			read -rp "QUESTION: Multimedia Codec packages are missing or not installed properly! These packages are needed for video playback (decoding) and encoding. Would you like to fix this? [y/n] " yn
			case $yn in
			y)
				media_fixup
				;;
			n)
				echo "INFO: Skipping Multimedia Codec updates."
				;;
			*)
				media_fixup
				;;
			esac
		fi

	fi

	# This is to fix if nvidia drivers have been installed but packages are missing for some reason or another
	NVIDIA_FIXUPS=""

	nvkernmod=$(sudo lspci -kD | grep -iEA3 '^[[:alnum:]]{4}:[[:alnum:]]{2}:[[:alnum:]]{2}.*VGA|3D' | grep -iA3 nvidia | grep -i 'kernel driver' | grep -iE 'vfio-pci|nvidia')

	if [[ ! -z $(rpm -qa | grep kmod-nvidia | grep 545) ]]; then
		if [[ -z $(dnf5 repolist | grep nobara-nvidia-new-feature-39) ]]; then
			sudo dnf5 remove -y kmod-nvidia* &>/dev/null
			sudo dnf5 remove -y akmod-nvidia* &>/dev/null
			sudo dnf5 remove -y nvidia* &>/dev/null
		fi
	fi

	if [[ ! -z $nvkernmod ]]; then

		if [[ -z $(rpm -qa | grep akmod-nvidia) ]]; then
			NVIDIA_FIXUPS+="akmod-nvidia "
		fi

		if [[ -z $(rpm -qa | grep nvidia-driver-NVML | grep i686) ]]; then
			NVIDIA_FIXUPS+="nvidia-driver-NVML.i686 "
		fi

		if [[ -z $(rpm -qa | grep nvidia-driver-NVML | grep -v i686) ]]; then
			NVIDIA_FIXUPS+="nvidia-driver-NVML "
		fi

		if [[ -z $(rpm -qa | grep nvidia-driver-NvFBCOpenGL) ]]; then
			NVIDIA_FIXUPS+="nvidia-driver-NvFBCOpenGL "
		fi

		if [[ -z $(rpm -qa | grep nvidia-driver-cuda | grep -v libs) ]]; then
			NVIDIA_FIXUPS+="nvidia-driver-cuda "
		fi

		if [[ -z $(rpm -qa | grep nvidia-driver-cuda-libs | grep i686) ]]; then
			NVIDIA_FIXUPS+="nvidia-driver-cuda-libs.i686 "
		fi

		if [[ -z $(rpm -qa | grep nvidia-driver-cuda-libs | grep -v i686) ]]; then
			NVIDIA_FIXUPS+="nvidia-driver-cuda-libs "
		fi

		if [[ -z $(rpm -qa | grep nvidia-driver-libs | grep i686) ]]; then
			NVIDIA_FIXUPS+="nvidia-driver-libs.i686 "
		fi

		if [[ -z $(rpm -qa | grep nvidia-driver-libs | grep -v i686) ]]; then
			NVIDIA_FIXUPS+="nvidia-driver-libs "
		fi

		if [[ -z $(rpm -qa | grep nvidia-kmod-common) ]]; then
			NVIDIA_FIXUPS+="nvidia-kmod-common "
		fi

		if [[ -z $(rpm -qa | grep nvidia-libXNVCtrl) ]]; then
			NVIDIA_FIXUPS+="nvidia-libXNVCtrl "
		fi

		if [[ -z $(rpm -qa | grep nvidia-modprobe) ]]; then
			NVIDIA_FIXUPS+="nvidia-modprobe "
		fi

		if [[ -z $(rpm -qa | grep nvidia-persistenced) ]]; then
			NVIDIA_FIXUPS+="nvidia-persistenced "
		fi

		if [[ -z $(rpm -qa | grep nvidia-settings) ]]; then
			NVIDIA_FIXUPS+="nvidia-settings "
		fi

		if [[ -z $(rpm -qa | grep nvidia-xconfig) ]]; then
			NVIDIA_FIXUPS+="nvidia-xconfig "
		fi

		if [[ -z $(rpm -qa | grep libva-nvidia-driver) ]]; then
			NVIDIA_FIXUPS+="libva-nvidia-driver "
		fi

		if [[ -z $(rpm -qa | grep nvidia-gpu-firmware) ]]; then
			NVIDIA_FIXUPS+="nvidia-gpu-firmware "
		fi

		if [[ "$NVIDIA_FIXUPS" != "" ]]; then
			echo "#####"
			echo "Reinstalling some missing Nvidia packages"
			echo "#####"

			sudo dnf5 install -y $NVIDIA_FIXUPS
		fi

	fi

	# Install some special software for ROG Ally and Lenovo Legion Go special buttons
	if [[ -n $(sudo dmesg | grep 'ROG Ally') ]]; then
		if [[ -z $(rpm -qa | grep hhd) ]]; then
			echo "#####"
			echo "ROG Ally detected, installing Handheld Daemon (HHD) for functionality"
			echo "#####"
			sudo systemctl disable --now handycon &>/dev/null
			sudo dnf5 remove -y HandyGCCS &>/dev/null
			sudo dnf5 remove -y rogue-enemy &>/dev/null
			sudo dnf5 install -y hhd
		fi
	fi

	if [[ -n $(sudo dmesg | grep 'Legion Go') ]]; then
		if [[ -z $(rpm -qa | grep hhd) ]]; then
			echo "#####"
			echo "Lenovo Legion Go detected, installing Handheld Daemon (HHD) for functionality"
			echo "#####"
			sudo systemctl disable --now handycon &>/dev/null
			sudo dnf5 remove -y HandyGCCS &>/dev/null
			sudo dnf5 remove -y lgcd &>/dev/null
			sudo dnf5 install -y hhd
		fi
	fi

	# Fixup winehq-staging packaging changes
	if [[ $(dnf list installed | grep winehq-staging | awk '{print $2}' | cut -d ":" -f 2 | cut -d "-" -f 1) < 9.9 ]]; then
		echo "#####"
		echo "Upstream wine packaging has changed, fixing conflicts"
		echo "#####"
		sudo rpm -e --nodeps winehq-staging
		sudo rpm -e --nodeps wine-staging64
		sudo rpm -e --nodeps wine-staging-common
		sudo dnf5 install -y winehq-staging --refresh
	fi

	echo "#####"
	echo "Performing distribution sync to prevent package update mismatches"
	echo "#####"
	sudo -S dnf5 distro-sync -y | sed 's/plasma-lookandfeel-nobara-steamdeck-additions/steamdeck-additions/g'

	echo "#####"
	echo "Updating the system"
	echo "#####"
	sudo -S dnf5 update -y

	# Remove some known problematic packages
	# unneeded, bsdtar is shipped by fedora can extract rar files
	sudo dnf5 remove -y unrar &>/dev/null

	# unneeded and regularly cause breakage between rpmfusion and fedora upstream
	sudo dnf5 remove -y qt5-qtwebengine-freeworld &>/dev/null
	sudo dnf5 remove -y qt6-qtwebengine-freeworld &>/dev/null

	# known to be problematic/cause theming problems in obs-studio
	sudo dnf5 remove -y qgnomeplatform-qt6 &>/dev/null
	sudo dnf5 remove -y qgnomeplatform-qt5 &>/dev/null

	# accidentally added to ISOs
	sudo dnf5 remove -y musescore &>/dev/null

	if [ -e /etc/nobara/newinstall ]; then
		rm -Rf /etc/nobara/newinstall
	fi

}

dnf_sync_log_remove() {
	if [ -e /tmp/dnf.sync.success ]; then
		rm /tmp/dnf.sync.success
	fi
}

dnf_install_progress() {
	dnf_update
	if [[ $(grep 'Running transaction' $loglocation) ]]; then
		DNF_STATE_STAGE=true
	fi
	touch /tmp/dnf.sync.success
}

flatpak_install_progress() {
	export XDG_DATA_DIRS=$USERHOME/.local/share/flatpak/exports/share:/var/lib/flatpak/exports/share:/usr/local/share:/usr/share:/var/lib/snapd/desktop
	if [[ -n $DISPLAY ]]; then
		if zenity --question --text="Flatpak has been detected! Would like to update all Flatpaks on your system?" 2>/dev/null; then
			flatpak update --appstream -y
			flatpak update -y
			sudo -u $displayuser flatpak update --appstream -y
			sudo -u $displayuser flatpak update -y
		else
			echo "INFO: Skipping flatpak updates."
		fi
	else
		read -rp "QUESTION: Flatpak has been detected! Would like to update all Flatpaks on your system? [y/n] " yn
		case $yn in
		y)
			flatpak update --appstream -y
			flatpak update -y
			sudo -u $displayuser flatpak update --appstream -y
			sudo -u $displayuser flatpak update -y
			;;
		n)
			echo "INFO: Skipping flatpak updates."
			;;
		*)
			flatpak update --appstream -y
			flatpak update -y
			sudo -u $displayuser flatpak update --appstream -y
			sudo -u $displayuser flatpak update -y
			;;
		esac
	fi
}

snap_install_progress() {
	if [[ -n $DISPLAY ]]; then
		if zenity --question --text="Snap has been detected! Would like to update all Snaps on your system?" 2>/dev/null; then
			snap refresh
			sudo -u $displayuser snap refresh
		else
			echo "INFO: Skipping snap updates."
		fi
	else
		read -rp "QUESTION: Snap has been detected! Would like to update all Snaps on your system? [y/n] " yn
		case $yn in
		y)
			snap refresh
			sudo -u $displayuser snap refresh
			;;
		n)
			echo "INFO: Skipping snap updates."
			;;
		*)
			snap refresh
			sudo -u $displayuser snap refresh
			;;
		esac
	fi
}

internet_check

### DNF UPGRADE
if [[ $INTERNET == yes ]]; then
	dnf_install_progress
fi

### Flatpak UPGRADE
if [[ $INTERNET == yes ]] && [[ -x "$(command -v flatpak)" ]]; then
	flatpak_install_progress
fi

### Snap UPGRADE
if [[ $INTERNET == yes ]] && [[ -x "$(command -v snap)" ]]; then
	snap_install_progress
fi

### Final dialog
if cat /tmp/dnf.sync.success; then
	if [[ $DNF_STATE_STAGE == true ]]; then
		# always run akmods and dracut at the end of updates
		sudo akmods
		sudo dracut -f --regenerate-all

		echo "INFO: Update Complete!"
		echo "INFO: Log can be found at $loglocation."
		if [[ -n $DISPLAY ]]; then
			if zenity --question --title='Update my system' --text='It is recommended to reboot for changes to apply properly. Reboot now?' 2>/dev/null; then
				echo INFO: Rebooting...
				dnf_sync_log_remove
				log_cleanup
				systemctl reboot
			else
				echo INFO: Reboot skipped, exiting.
				dnf_sync_log_remove
				log_cleanup
				/usr/lib/nobara/nobara-welcome/scripts/updater/end.sh
			fi
		else
			read -rp "QUESTION: It is recommended to reboot for changes to apply properly. Reboot now? [y/n] " yn
			case $yn in
			y)
				echo INFO: Rebooting....
				dnf_sync_log_remove
				log_cleanup
				sudo systemctl reboot
				;;
			n)
				echo INFO: Reboot skipped, exiting.
				if [[ ! -z $(w | grep $2 | grep "gamescope-session-plus@steam.service") ]]; then
					read -n 1 -s -r -p "Press any key to exit."
					exit 0
				else
					exit 0
				fi
				dnf_sync_log_remove
				log_cleanup
				exit 0
				;;
			*)
				echo INFO: Rebooting....
				dnf_sync_log_remove
				log_cleanup
				sudo systemctl reboot
				;;
			esac
		fi
	else
		echo "INFO: No updates required, your system is already up to date!"
		echo "INFO: Log can be found at $loglocation."
		log_cleanup
		if [[ -n $DISPLAY ]]; then
			if zenity --info --title='Update my system' --text='No updates required, your system is already up to date!' 2>/dev/null; then
				/usr/lib/nobara/nobara-welcome/scripts/updater/end.sh
			fi
		else
			if [[ ! -z $(w | grep $2 | grep "gamescope-session-plus@steam.service") ]]; then
				read -n 1 -s -r -p "Press any key to exit."
				exit 0
			else
				exit 0
			fi
		fi
	fi
else
	echo "ERROR: Failed to update!"
	echo "INFO: Log can be found at $loglocation."
	log_cleanup
	if [[ -n $DISPLAY ]]; then
		zenity --error --title='Update my system' --text="Failed to update!" 2>/dev/null
		dnf_sync_log_remove
		/usr/lib/nobara/nobara-welcome/scripts/updater/end.sh
	else
		dnf_sync_log_remove
		if [[ ! -z $(w | grep $2 | grep "gamescope-session-plus@steam.service") ]]; then
			read -n 1 -s -r -p "Press any key to exit."
			exit 0
		else
			exit 1
		fi
	fi
fi
dnf_sync_log_remove
exit 0
