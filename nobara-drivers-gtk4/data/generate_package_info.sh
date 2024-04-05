#! /bin/bash

if [[ $1 == "version" ]]
then
	#apt-cache show $2 | grep Version: | cut -d":" -f2 | head -n1
	dnf info $2 | grep Version | cut -d":" -f2 | head -n1
elif [[ $1 == "description" ]]
then
	#apt-cache show $2 | grep 'Description*' | cut -d":" -f2 | head -n1
	dnf info $2 | sed -n '/Description/,/^$/p' | awk 'NR==1,/^$/ {if (/^$/ && printed) exit; if (NF) printed=1; if (NR==1) sub(/Description *: */, ""); else sub(/^ *: */, ""); print}'
fi
