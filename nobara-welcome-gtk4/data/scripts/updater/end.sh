#! /usr/bin/bash
process=$(ps aux | grep "/usr/lib/nobara/nobara-welcome/scripts/updater/process.py" | grep -v grep | awk {'print $2'})
kill $process
