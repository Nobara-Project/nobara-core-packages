#!/bin/bash
# Never mark a failed/partial merge as restored.
set -euo pipefail
if [ -f /run/nobara-rollback/completed ]; then
    . /etc/nobara-rollback.conf
    mount -o remount,rw /sysroot
    printf '{"job":"%s","backend":"lvm","restored_entry":"%s"}\n' \
        "$NB_JOB" "$NB_RESTORED_ENTRY" >/sysroot/etc/.nobara-updater-recovery.tmp
    mv /sysroot/etc/.nobara-updater-recovery.tmp /sysroot/etc/nobara-updater-recovery.json
    sync
else
    echo 'Root restoration has not completed.' >&2
    exit 1
fi
