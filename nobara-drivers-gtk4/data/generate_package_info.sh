#! /bin/bash

if [[ $1 == "version" ]]
then
	#apt-cache show $2 | grep Version: | cut -d":" -f2 | head -n1
	dnf info available $2 | grep Version | cut -d":" -f2 | head -n1
elif [[ $1 == "description" ]]
then
	#apt-cache show $2 | grep 'Description*' | cut -d":" -f2 | head -n1
	dnf info available $2 | grep Description | cut -d":" -f2 | head -n1
fi
