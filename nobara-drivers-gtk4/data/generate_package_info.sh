#! /bin/bash

if [[ $1 == "version" ]]
then
	#apt-cache show $2 | grep Version: | cut -d":" -f2 | head -n1
	echo "GE PUT A COMMAND HERE THAT GETS THE VERSION OF A DNF PACKAGE (WITHOUT ROOT)"
elif [[ $1 == "description" ]]
then
	#apt-cache show $2 | grep 'Description*' | cut -d":" -f2 | head -n1
	echo "GE PUT A COMMAND HERE THAT GETS THE DESCRIPTION OF A DNF PACKAGE (WITHOUT ROOT)"
fi