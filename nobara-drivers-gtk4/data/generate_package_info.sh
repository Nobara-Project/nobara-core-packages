#! /bin/bash

if [[ $1 == "version" ]]
then
	if [[ $2 == "nobara-rocm-meta" ]]
	then
		apt-cache show rocm-core | grep Version: | cut -d":" -f2 | head -n1
	else
		apt-cache show $2 | grep Version: | cut -d":" -f2 | head -n1
	fi
elif [[ $1 == "description" ]]
then
		apt-cache show $2 | grep 'Description*' | cut -d":" -f2 | head -n1
fi