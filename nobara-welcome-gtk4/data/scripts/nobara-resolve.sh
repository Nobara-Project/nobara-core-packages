#! /usr/bin/bash
	pkexec sh -c "/usr/lib/nobara/nobara-welcome/scripts/pkexec-resolve.sh"

        # this is a hack to bypass the Davinci Resolve new install Welcome/Onboarding screen since it does not render properly and is not required.
        mkdir -p $HOME/.local/share/DaVinciResolve/configs/
    	echo "Onboarding.Version=100000" > $HOME/.local/share/DaVinciResolve/configs/.version
