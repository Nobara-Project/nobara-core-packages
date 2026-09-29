#!/bin/bash
# This file and its configuration are copied into the PRE-update initramfs.
. /lib/dracut-lib.sh
# Dracut helpers deliberately use failing conditionals (debug_on, etc.).
# Run them before enabling errexit/nounset for the destructive operations.
requested_job=$(getarg nobara.rollback=)
set -euo pipefail
. /etc/nobara-rollback.conf
[[ $requested_job == "$NB_JOB" ]]
[[ $NB_JOB =~ ^[a-f0-9]{32}$ ]]
for value in "$NB_VG" "$NB_ORIGIN" "$NB_SNAPSHOT"; do
    [[ $value =~ ^[A-Za-z0-9_+][A-Za-z0-9_.+-]*$ ]]
done
origin="$NB_VG/$NB_ORIGIN"
snapshot="$NB_VG/$NB_SNAPSHOT"
field() { lvm lvs --noheadings -o "$2" "$1" | xargs; }
[[ $(field "$origin" lv_uuid) == "$NB_ORIGIN_UUID" ]]
if findmnt --mountpoint /sysroot >/dev/null 2>&1; then
    echo 'Refusing rollback: the target root is already mounted.' >&2
    exit 1
fi
if lvm lvs "$snapshot" >/dev/null 2>&1; then
    [[ $(field "$snapshot" lv_uuid) == "$NB_SNAPSHOT_UUID" ]]
    [[ $(field "$snapshot" origin_uuid) == "$NB_ORIGIN_UUID" ]]
    [[ ,$(field "$snapshot" lv_tags), == *,nobara.rollback.snapshot,* ]]
    [[ -z $(field "$snapshot" lv_snapshot_invalid) ]]
    # Write intent to VG metadata so a power loss during/after merge can be
    # distinguished from somebody deleting the snapshot before recovery.
    lvm lvchange --addtag "nobara.merge.$NB_JOB" "$origin"
    for attempt in {1..30}; do
        # Deactivating the origin also deactivates its snapshots. Naming a
        # snapshot itself asks an interactive confirmation on current LVM.
        if lvm lvchange --yes -an "$origin"; then break; fi
        [[ $attempt != 30 ]] || exit 1
        sleep 1
    done
    # A previously interrupted merge may already be queued.
    if [[ -z $(field "$snapshot" lv_merging) ]]; then
        lvm lvconvert --merge --yes "$snapshot"
    fi
    lvm lvchange -ay "$origin"
    while lvm lvs "$snapshot" >/dev/null 2>&1; do
        [[ -z $(field "$origin" lv_merge_failed) ]]
        sleep 1
    done
else
    [[ ,$(field "$origin" lv_tags), == *,nobara.merge.$NB_JOB,* ]]
fi
# A failed inventory command must not be mistaken for a completed merge.
[[ $(field "$origin" lv_uuid) == "$NB_ORIGIN_UUID" ]]
merge_failed=$(field "$origin" lv_merge_failed)
# Once the snapshot has disappeared, a normal LV reports this property as
# unknown rather than false. An explicit failure or failed query still stops.
[[ -z $merge_failed || $merge_failed == unknown ]]
sync
mkdir -p /run/nobara-rollback
printf '%s\n' "$NB_JOB" >/run/nobara-rollback/completed
