#!/bin/bash

SteamShutdown() {
        if [[ ! -z $(ps aux | grep "steam.sh" | grep -v color | grep -v grep) ]]; then
		steamwasrunning=1
                steam -shutdown  > /dev/null 2>&1 &
                while [[ ! -z $(ps aux | grep "steam.sh" | grep -v color | grep -v grep) ]]; do
                        sleep 1
                done
        fi
}

SteamStartup() {
	if [[ ! -z $steamwasrunning ]]; then
		gio launch /usr/share/applications/steam.desktop > /dev/null 2>&1 &
	fi
}

TF2() {
ex /home/$systemuser/.steam/steam/userdata/$steamuser/config/localconfig.vdf <<eof
$appsentryline insert
                                        "440"
                                        {
                                                "LaunchOptions"         "LD_PRELOAD=/lib/libtcmalloc_minimal.so.4 %command%"
                                        }
.
xit
eof
fixesapplied=1
}

DODSOURCE() {
ex /home/$systemuser/.steam/steam/userdata/$steamuser/config/localconfig.vdf <<eof
$appsentryline insert
                                        "300"
                                        {
                                                "LaunchOptions"         "LD_PRELOAD=/lib/libtcmalloc_minimal.so.4 %command%"
                                        }
.
xit
eof
fixesapplied=1
}

FISTOFFRAGS() {
ex /home/$systemuser/.steam/steam/userdata/$steamuser/config/localconfig.vdf <<eof
$appsentryline insert
                                        "265630"
                                        {
                                                "LaunchOptions"         "LD_PRELOAD=/lib/libtcmalloc_minimal.so.4 %command%"
                                        }
.
xit
eof
fixesapplied=1
}

GARRYSMOD() {
ex /home/$systemuser/.steam/steam/userdata/$steamuser/config/localconfig.vdf <<eof
$appsentryline insert
                                        "4000"
                                        {
                                                "LaunchOptions"         "LD_PRELOAD=/lib/libtcmalloc_minimal.so.4 %command%"
                                        }
.
xit
eof
fixesapplied=1
}

NOMOREROOMINHELL() {
ex /home/$systemuser/.steam/steam/userdata/$steamuser/config/localconfig.vdf <<eof
$appsentryline insert
                                        "224260"
                                        {
                                                "LaunchOptions"         "LD_PRELOAD=/lib/libtcmalloc_minimal.so.4 %command%"
                                        }
.
xit
eof
fixesapplied=1
}


PAYDAY2() {
ex /home/$systemuser/.steam/steam/userdata/$steamuser/config/localconfig.vdf <<eof
$appsentryline insert
                                        "218620"
                                        {
                                                "LaunchOptions"         "MESA_LOADER_DRIVER_OVERRIDE=zink %command%"
                                        }
.
xit
eof
fixesapplied=1
}

for systemuser in $(ls /home/); do
    if [[ -d /home/$systemuser/.steam/steam/userdata/ ]]; then
	    for steamuser in $(ls /home/$systemuser/.steam/steam/userdata/); do
    		if [[ -d /home/$systemuser/.steam/steam/userdata/$steamuser/config ]]; then
    			appsline=$(awk '/^				"apps"/{ print NR; exit }' /home/$systemuser/.steam/steam/userdata/$steamuser/config/localconfig.vdf)
    			appsentryline=$(($appsline+2))

    			# TF2 LLVM16 workaround
    			if [[ ! -z $(cat /home/$systemuser/.steam/steam/userdata/$steamuser/config/localconfig.vdf | grep "\"440\"") ]]; then
    				if [[ -z $(cat /home/$systemuser/.steam/steam/userdata/$steamuser/config/localconfig.vdf | grep "LD_PRELOAD=/lib/libtcmalloc_minimal.so.4") ]]; then
					SteamShutdown
					TF2
    				fi
    			fi

    			# DODSOURCE LLVM16 workaround
    			if [[ ! -z $(cat /home/$systemuser/.steam/steam/userdata/$steamuser/config/localconfig.vdf | grep "\"300\"") ]]; then
    				if [[ -z $(cat /home/$systemuser/.steam/steam/userdata/$steamuser/config/localconfig.vdf | grep "LD_PRELOAD=/lib/libtcmalloc_minimal.so.4") ]]; then
					SteamShutdown
					DODSOURCE
    				fi
    			fi

    			# FISTOFFRAGS LLVM16 workaround
    			if [[ ! -z $(cat /home/$systemuser/.steam/steam/userdata/$steamuser/config/localconfig.vdf | grep "\"265630\"") ]]; then
    				if [[ -z $(cat /home/$systemuser/.steam/steam/userdata/$steamuser/config/localconfig.vdf | grep "LD_PRELOAD=/lib/libtcmalloc_minimal.so.4") ]]; then
					SteamShutdown
					FISTOFFRAGS
    				fi
    			fi

    			# GARRYSMOD LLVM16 workaround
    			if [[ ! -z $(cat /home/$systemuser/.steam/steam/userdata/$steamuser/config/localconfig.vdf | grep "\"4000\"") ]]; then
    				if [[ -z $(cat /home/$systemuser/.steam/steam/userdata/$steamuser/config/localconfig.vdf | grep "LD_PRELOAD=/lib/libtcmalloc_minimal.so.4") ]]; then
					SteamShutdown
					GARRYSMOD
    				fi
    			fi

    			# NOMOREROOMINHELL LLVM16 workaround
    			if [[ ! -z $(cat /home/$systemuser/.steam/steam/userdata/$steamuser/config/localconfig.vdf | grep "\"224260\"") ]]; then
    				if [[ -z $(cat /home/$systemuser/.steam/steam/userdata/$steamuser/config/localconfig.vdf | grep "LD_PRELOAD=/lib/libtcmalloc_minimal.so.4") ]]; then
					SteamShutdown
					NOMOREROOMINHELL
    				fi
    			fi

    			# Payday 2 OpenGL workaround
    			if [[ ! -z $(cat /home/$systemuser/.steam/steam/userdata/$steamuser/config/localconfig.vdf | grep "\"218620\"") ]]; then
    				if [[ -z $(cat /home/$systemuser/.steam/steam/userdata/$steamuser/config/localconfig.vdf | grep "MESA_LOADER_DRIVER_OVERRIDE=zink") ]]; then
					SteamShutdown
					PAYDAY2
    				fi
    			fi
    		fi
    	done
    fi
done

if [[ ! -z $fixesapplied ]]; then
	SteamStartup
fi
