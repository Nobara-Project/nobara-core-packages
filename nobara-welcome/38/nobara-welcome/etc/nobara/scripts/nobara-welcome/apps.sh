#!/usr/bin/bash
if echo $XDG_SESSION_DESKTOP | grep -i KDE ;
then
/usr/bin/plasma-discover
else
/usr/bin/gnome-software
fi
