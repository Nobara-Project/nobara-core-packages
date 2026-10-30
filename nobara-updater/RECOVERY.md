# Recovering from a failed Nobara update

For an explanation of immediate updates, offline installation, restarts, and
recovery triggers, see [How Nobara handles system updates](UPDATES.md).

Open **System Update Recovery** in the application menu to view the error,
read the logs, or save a report for support. From a terminal, run:

```sh
nobara-sync recovery-report
```

The saved report and paste upload include bounded boot-journal excerpts as well
as updater logs. Available previous boots are labeled with their boot IDs;
rollback can hide the failed boot's journal, so `journalctl -b -1` may show the
offline installation or an earlier recovery session instead. If support needs
additional journals, first share `journalctl --list-boots` so the correct boot
can be identified. Older updater versions may not have saved these excerpts.

## If automatic rollback restored your previous system

For updates that require restarting, when automatic rollback is available:

1. If installation or startup validation fails, the system automatically
   selects the **previous system** recovery entry.
2. Review the error and logs in **System Update Recovery**.
3. Fix the reported problem in the recovered system. It is now your active
   system, and your repairs remain part of it.
4. Run the updater again in DNF App Center, or run `sudo nobara-sync cli`,
   to prepare a fresh transaction.
5. Restart if the updater asks you to. The system automatically enters
   offline update mode and attempts installation.
6. If installation succeeds, it restarts into the updated system using the
   selected kernel. If installation or startup validation fails again, it
   returns to recovery.
7. If the same failure persists, save the report bundle or upload the logs
   and share them with Nobara support. Resolve the cause before repeating
   the update.

**You normally do not need to select a boot entry manually.** After recovery,
the normal entry uses your restored system. Once boot is confirmed, unused
updater rollback snapshots and their recovery entries are removed. Your
active system remains in place.

Recovery uses the saved pre-update kernel and initramfs from `/boot`, with the
matching modules in the restored root. Before confirming recovery, the updater
checks that this kernel has its module tree, restores the saved recovery entry
if package scripts changed it, and selects the recovered kernel for normal boot.
Normal entries belonging to this installation whose modules are absent from
the restored root are moved out of the boot menu. Their saved entry contents
are included in the recovery report. Other installations, explicit snapshot
entries, and kernel images on `/boot` are preserved. This applies to Btrfs and
ext4/XFS on LVM; restoring root does not mean blindly restoring the whole ESP.

On Btrfs, the updater restores a confirmed recovered system to its original
root-subvolume name, usually `@`, while preserving changes you made in recovery.
This works with or without Timeshift. Systems left on
`.nobara-updater/<job>/root` by older updater versions are repaired automatically
on a confirmed boot or before the next fresh update. Do not delete that subvolume
manually while it is your active root. If layout repair is interrupted, boot
references retain the subvolume's stable ID and the updater resumes the repair.

If the updater reports missing recovery ancestry, a new system update is blocked
until the original root layout can be restored. This prevents repeated snapshots
of an unresolved recovery root. The updater first checks retained records and
its own saved snapshots for the missing information; the early updater/helper
upgrade can still deliver fixes unless a root move is already in progress.
If those records are gone, share `/var/lib/nobara-updater/update.log` and
`sudo btrfs subvolume list -p -u /` with Nobara support. Preserve the recovery
subvolumes and keep using the current system; an older `@` subvolume may contain
an outdated or partially updated installation and must not be selected blindly.

If the recorded pre-update root was inside `timeshift-btrfs/snapshots/`, the
updater leaves the recovered system in place and reports that its normal root
layout needs repair. It does not replace a Timeshift snapshot with the running
system or assume an existing `@` contains your latest changes. This can occur
after updating from a booted Timeshift snapshot. Share the updater report and
`sudo btrfs subvolume list -p -u /` with support to identify a suitable repair.

## If preparation stopped before installation

Some conflicts are detected before packages change. No reboot or rollback
is needed to address that failure. Review the error, resolve the conflict,
and run the updater again. Restart only if the updater asks you to after
preparing the new update.

### If a swapfile blocks a Btrfs recovery snapshot

The updater names swapfiles that are active or listed in `/etc/fstab` and belong
to the root subvolume. It stops preparation or scheduling before requesting a
restart and checks again during the update boot before creating the snapshot.
An inactive configured swapfile is included because it could activate on reboot,
and snapshotting it could prevent subsequent activation. The updater leaves swap
and its configuration unchanged. Ordinary live application updates do not need
this snapshot check.

To repair the layout:

1. Check the reported paths with `swapon --show` and `/etc/fstab`. Include any
   custom systemd swap units and hibernation configuration in your review.
2. Provide a separate Btrfs subvolume for swap, with a persistent mount in
   `/etc/fstab`. Calamares uses the top-level `@swap` subvolume mounted at `/swap`,
   alongside the root `@` subvolume. An ordinary `/swap` directory still belongs
   to root and does not solve this problem. Preserve an existing swap subvolume
   and its contents; do not format it or replace its mount blindly.
3. Create a new swapfile on that mounted subvolume, using
   `btrfs filesystem mkswapfile` with an appropriate size and an unused filename.
   This creates a suitable file without copy-on-write or compression. Activate
   and verify the replacement before retiring the old swapfile. Check RAM and
   swap capacity first: disabling swap can fail or exhaust memory. Do not merely
   disable the old file for snapshot creation and re-enable it afterwards.
4. Update the swap entry in `/etc/fstab` and any custom swap unit to use the new
   file. If using hibernation, update the resume device/offset and regenerate the
   affected boot configuration/initramfs; the old file's offset is not valid for
   its replacement. Disable the old swapfile only when capacity permits, then
   remove the retired file and its old configuration once the replacement works.
5. Run the updater again and restart only when it requests it. If relocation is
   unclear, share the recovery report with Nobara support before changing storage.

Swap partitions, zram, and swapfiles in a separate subvolume do not cause this
root-snapshot restriction. See the [Btrfs swapfile documentation](https://btrfs.readthedocs.io/en/latest/Swapfile.html)
for the filesystem and hibernation requirements.

## If automatic recovery could not complete

The updater allows one automatic recovery restart per update job. If that
restart does not reach a confirmed recovered system, further automatic restarts
stop. A bootable system shows a failure report; reaching the desktop does not
mean the update or rollback succeeded. A new update remains blocked.

Open **System Update Recovery** or run `nobara-sync recovery-report`. Save the
report bundle or upload its logs with the report's sharing action. Preserve the
snapshots, boot entries and updater state. To retry the saved recovery, run:

```sh
sudo nobara-sync retry-recovery
```

The command validates the existing snapshot and archived kernel/initramfs,
repairs the recovery boot entry, and selects it in GRUB. It supports the saved
Btrfs or ext4/XFS-on-LVM recovery for that update. It does not replay packages,
create another snapshot, confirm the interrupted installation, or reboot
immediately. If it succeeds, save your work and restart normally with the
desktop restart action or `sudo reboot`. Do not use `nobara-sync reboot`, which
is for scheduling prepared updates.

If validation fails, keep the saved recovery and share the refreshed report
with Nobara support. Missing, replaced, invalid or merging LVM snapshots, or
missing/invalid boot images, need investigation rather than a forced retry.
If the next boot still does not confirm recovery, the automatic restart limit
remains in place and the report is refreshed. Once recovery is confirmed,
follow the normal recovery workflow above. Do not delete the restart record
or repeatedly rerun the interrupted transaction.

Reports include selected kernel command-line fields, root and boot mount
identities, GRUB selection variables, active and archived boot entries, and EFI
boot-selection details. These are captured before and after a recovery retry
and included in the same offline bundle/paste upload as the failure log.
Arbitrary kernel arguments and private transaction state are omitted; review
the report before sharing filesystem identifiers and local paths.

Recovery boot entries are saved outside the normal boot-entry directory. Before
selecting recovery, the updater restores and validates them if a package script
rewrote their root options. Older entries can be repaired using the recorded
snapshot or LVM identity and existing archived boot files; missing or ambiguous
boot data requires support instead of guessing a replacement root.

## If installation finished but boot confirmation failed

An update can finish installation and validation, then fail confirmation
because the next boot selected an older kernel. If you subsequently boot the
expected updated kernel's normal entry successfully, a new updater command
can validate that installation without reverting it:

```sh
sudo nobara-sync confirm-update
```

Use a build containing this command. It runs the installed updater, so an old
job's frozen worker does not need to be edited. The command requires a saved
record of successful post-install validation and a recorded kernel selection.
It checks the original root and recovery snapshot, saved transaction integrity,
RPM database, dependencies, planned package results, release, running kernel,
module tree, normal boot entry, boot images, and login-manager status. Later
same-architecture package upgrades are allowed, including installing the fixed
updater; missing packages and versions older than the finalized plan are refused.

Only after these checks pass does it select the updated normal entry, mark the
update complete, and retire its rollback snapshot through normal cleanup. The
previous failure report and saved state are retained. Cleanup failures are
retried on later boots. The command does not replay packages, force a snapshot
merge, or reboot. You can then run the updater normally.

A partial installation, wrong kernel/root, pending offline update, missing
validation record, or failed check remains blocked with recovery retained.
Share the report or use `retry-recovery` when appropriate; do not edit
`state.json` to mark an update complete. This verification cannot certify
every application or hardware workload, and GRUB's `boot_success=1` alone is
not proof that the updater completed.

## If installation failed without automatic rollback

Reaching the desktop does not confirm that installation completed. Some
packages may have changed, and automatic retries remain blocked.

Read the report before making repairs. You can run
`sudo dnf5 check --dependencies --duplicates` to check installed dependencies,
conflicts, and duplicate package versions without changing packages. Plain
`dnf5 check` also reports obsolete packages; an obsolete-package notice alone
does not mean dependencies are broken and does not trigger updater rollback.
Save the report and ask Nobara support for repair steps appropriate to the
error. Rebooting alone does not repair or retry the failed installation.

After repairs, run **`sudo nobara-sync retry-update`**. This explicitly
acknowledges the repair, checks the RPM database and installed dependencies,
and prepares a new transaction from the current system. If checks fail, the
failed state remains blocked. The previous failure state and logs are kept.
Restart only if the updater requests it. This command is for a failed
installation without a created rollback snapshot; after booting a recovery
snapshot, use `sudo nobara-sync cli` normally.

Do not delete the lock file or `state.json`, enable services manually, or create
`/system-update` yourself. A blocked failed installation is a saved state,
not a stale process lock, and bypassing it cannot make the old plan safe.

## How packages are handled

- **The updater uses DNF's `distro-sync` behavior.** It can upgrade or
  downgrade packages according to repository priorities, versions, and
  dependencies.
- **Locally installed RPMs follow normal repository updates.** Packages
  installed from local files or URLs, or directly with `rpm`, are not held
  back because of their origin. This also applies when the installation
  repository is unknown. `distro-sync` can upgrade or downgrade them to a
  repository version, or replace them through RPM Obsoletes. A custom build
  with the same version as the repository is not automatically reinstalled.
  A local-only package is not removed merely because no repository provides it.
  Repository priorities, explicit exclusions/versionlocks, codec protections,
  and normal dependency/removal checks still apply.
- **Third-party repository packages remain eligible for replacement.**
  Repository priorities and dependencies determine whether a Nobara build
  replaces them. A newer version alone does not override repository
  priority. Conflicts can occur regardless of which repository has higher
  priority.
- **When failure evidence identifies an affected local or third-party
  package**, the report names it and its recorded origin. Where an enabled
  Nobara repository provides a counterpart, the report also names that
  repository. Unknown origin is reported as unverified; unrelated packages
  are not blamed merely because they are installed.
- **Source installations outside RPM's package database** are not managed
  or automatically updated by DNF.

## Sharing logs

Use **Save logs** to save the `.tar.gz` report, including when offline. The
bundle contains `README.txt` and `update.log` and can be attached to a support
request or issue.

When a driver build fails, the updater also tries to preserve the referenced
DKMS `make.log` before rollback restores the filesystem. Its bounded contents
are included in the report's `update.log`. Later confirmation or recovery
errors do not replace the original failure, and a journal collection timeout
does not prevent saving the rest of the report.

When online, **Upload logs** sends the text report to Nobara's paste service
and returns a link to share. Nothing is uploaded automatically. Review the
logs before sharing: they can contain package names, repository URLs, and
local file paths.

Terminal equivalents:

```sh
nobara-sync recovery-report --save ~/nobara-update-report.tar.gz
nobara-sync recovery-report --upload
```
