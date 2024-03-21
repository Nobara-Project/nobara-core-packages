#!/usr/bin/bash

INTERNET="no"

internet_check() {
      # Check for internet connection
      wget -q --spider http://google.com
      if [ $? -eq 0 ]; then
          export INTERNET="yes"
      fi
}

internet_check

if [[ $INTERNET == yes ]]; then

	APPNICK=$(echo $2 | awk -F. '{print $NF}')

	if [[ $1 == 'install' ]]; then
		FLATPAKCOMMAND="flatpak --user $1 -y flathub $2 $3 $4 $5 $6 $7 $8 $9"
		$FLATPAKCOMMAND
		if [[ ! -z $(flatpak list | grep -i $2) ]]; then
			zenity --notification --text="$APPNICK has been installed!" && export SUCCESS=yes
		else
			zenity --notification --text="$APPNICK $1 has failed!" && export SUCCESS=no
		fi
	fi

	if [[ $1 == 'uninstall' ]]; then
                FLATPAKCOMMAND="flatpak --user $1 -y $2 $3 $4 $5 $6 $7 $8 $9"
                $FLATPAKCOMMAND
		if [[ -z $(flatpak list | grep -i $2) ]]; then
			zenity --notification --text="$APPNICK has been uninstalled!" && export SUCCESS=yes
		else
			zenity --notification --text="$APPNICK $1 has failed!" && export SUCCESS=no
		fi
	fi
	
	if [[ $1 == 'check' ]]; then
		if [[ -z $(flatpak list | grep -i $2) ]]; then
			exit 1
		else
			exit 0
		fi
	fi
else
	zenity --error --text="No internet connection."
fi


