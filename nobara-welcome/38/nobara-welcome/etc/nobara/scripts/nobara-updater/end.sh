#! /usr/bin/bash
process=$(ps aux | grep "/etc/nobara/scripts/nobara-updater/process.py" | grep -v grep | awk {'print $2'})
kill $process
