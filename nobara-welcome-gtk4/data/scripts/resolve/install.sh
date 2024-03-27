#! /usr/bin/bash


	pkexec sh -c "/usr/lib/nobara/nobara-welcome/scripts/resolve/pkexec-resolve.sh"

        # this is a hack to bypass the Davinci Resolve new install Welcome/Onboarding screen since it does not render properly and is not required.
        mkdir -p $HOME/.local/share/DaVinciResolve/configs/
    	echo "Onboarding.Version=100000" > $HOME/.local/share/DaVinciResolve/configs/.version

        zenity --info  --title="Complete" --text="Davinci Resolve package dependency installation complete!"
	if [ $? = 0 ]; then
	    /usr/lib/nobara/nobara-welcome/scripts/resolve/end.sh
	fi
