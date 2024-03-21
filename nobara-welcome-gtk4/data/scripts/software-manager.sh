#!/bin/bash
if echo $XDG_SESSION_DESKTOP | grep -i -E 'gnome|pika|ubuntu'
then
  gnome-software "$@"
elif echo $XDG_SESSION_DESKTOP | grep -i -E 'plasma|kde'
then
  plasma-discover "$@"
else
  zenity --error --text "$XDG_SESSION_DESKTOP does have a registered software-manager"
fi