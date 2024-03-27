#!/bin/sh

# install rocm packages for opencl support
dnf5 install -y rocm-meta

# install runtime libraries ported from Fedora 37
dnf5 install -y nobara-resolve-runtime

# edit resolve shortcuts
for systemuser in $(ls /home/); do
    if [[ -d /home/$systemuser/Desktop/ ]]; then
		if [[ -f /home/$systemuser/Desktop/com.blackmagicdesign.resolve.desktop ]]; then
			sed -i 's|Exec=/opt/resolve/bin/resolve|Exec=/usr/bin/davinci-resolve|g' /home/$systemuser/Desktop/com.blackmagicdesign.resolve.desktop
		fi
	fi
done

if [[ -f /usr/share/applications/com.blackmagicdesign.resolve.desktop ]]; then
	sed -i 's|Exec=/opt/resolve/bin/resolve|Exec=/usr/bin/davinci-resolve|g' /usr/share/applications/com.blackmagicdesign.resolve.desktop
fi

echo "All done! Run resolve with 'davinci-resolve' command."
