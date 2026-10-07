# How Nobara handles system updates

Nobara installs eligible application updates immediately. Updates that affect
core system components are prepared while you use the desktop and installed
when you restart.

In this guide, **critical** means that an update affects components needed by
the running system or requires additional safeguards during installation. It
does not describe the security severity of a fix. A small update can require
a restart, and an important application security fix can be eligible for
immediate installation.

| Update type | When installation happens | What you need to do |
| --- | --- | --- |
| Eligible application-only update | During the current session | Restart the updated application to use its new version |
| Core system update or major Nobara version upgrade | In offline update mode at the next restart | Save your work and restart when ready |
| A batch containing both types | The entire batch installs offline | Save your work and restart when ready |

## Non-critical updates: immediate installation

An update can install immediately when all the changes are suitable for the
current session. This includes ordinary application executables, application
data, and libraries private to an application, provided the complete update
does not change core components or shared system files.

The updater still resolves dependencies, downloads and verifies the packages,
and tests the transaction before installation. It checks the installed
packages and dependencies afterward.

These updates do not schedule an offline installation or require a system
restart through the updater. Close and reopen updated applications to use
their new versions.

The updater does not create a system rollback snapshot for immediate
application updates. If installation fails, it reports the error; it does
not automatically roll back the operating system. Review the failure report
before attempting repairs or another update.

## Critical updates: installation after restarting

The updater requires offline installation when the planned changes include
any of the following:

- A major Nobara version upgrade, such as 43 to 44.
- Kernels, firmware, drivers, bootloader components, or boot images.
- Core system components such as the package manager, updater, Python,
  authentication, storage tools, networking, or system services.
- Graphics, desktop/session, login-manager, or audio components covered by
  the updater's core-component policy.
- Libraries shared across applications, or files used for system services
  and configuration.
- Package removals, downgrades, or deferred system configuration fixes.
- Packages whose file changes cannot be identified reliably. The updater
  treats those conservatively and requests offline installation.

Administrator settings can also require offline installation.

The decision covers the **whole resolved transaction**, including
dependencies. An application update that brings a shared library update may
therefore require a restart. A batch containing applications and a kernel
update also installs entirely offline. The updater keeps these dependent
changes together rather than installing them in separate groups.

The update log explains why a restart is required, grouping affected
packages under categories such as core components, shared libraries, or
system configuration.

## What happens before you restart

Before preparing a fresh system update, Nobara first upgrades the installed
`nobara-updater` and `drm-awaiter` packages and their required dependencies.
This small preliminary update happens immediately, so preparation and later
boot-image generation use their latest available fixes. Repository priorities,
exclusions, and normal transaction safety checks still apply. Already prepared
updates are left intact. This preliminary step has no rollback snapshot; if it fails,
follow [RECOVERY.md](RECOVERY.md) before retrying.

The updater refreshes repositories and prepares one dependency-resolved
transaction. It uses DNF's `distro-sync` behavior, which can upgrade or
downgrade packages according to repository priorities and available versions.
Locally installed/custom RPMs are eligible for repository replacements too;
see [package handling in the recovery guide](RECOVERY.md#how-packages-are-handled).

If a repository reports **Usable URL not found** while downloading metadata,
the updater clears that repository's cached metadata and mirror information
and retries with a fresh DNF5 instance. DNF App Center uses the same recovery
when checking for updates. Downloaded packages and repository settings are
preserved. If the retry still fails, the error is reported; an unavailable
repository is not silently disabled.

The Plasma Login migration removes SDDM's `kde-settings-sddm` settings package
alongside SDDM, so its dependency cannot block the replacement login manager.

Some installations have a newer `libdnf5-plugin-systemd-inhibit` package than
the DNF build available from the rolling repositories. If that optional plugin
has no available replacement and requires a library version the repositories
no longer offer, the updater can retire it while synchronizing the DNF packages
in the same offline transaction. A matching repository build is used when
available. This also applies to a locally installed plugin. Dependent applications
cannot be removed automatically. Nobara Updater's services provide their own
shutdown inhibition; this compatibility fixup does not remove the actions
plugin used for codec protection or other DNF plugins.

For older installations still using the retired PikaOS media URL ending in
`/nobara/media`, preparation uses the current `/nobara/media/$basearch/`
endpoint. The old URL continues to serve an outdated package set. This migration
preserves repository priorities, signature checks, exclusions, and custom
mirrors. Configuration changes are saved after installation, rather than while
checking or downloading updates. A disabled codec repository remains disabled
unless the existing codec opt-in rules or an explicit Codec Wizard request
enable it.

Before preparing a transaction, it checks that the current RPM database is
readable and valid. If an installed package has missing dependencies, the updater
tries to restore them within the same offline transaction. DNF selects providers
using the enabled repositories and their priorities, including the correct
32-bit libraries for 32-bit applications. Locally installed packages remain
protected; repairing their dependencies does not authorize replacing them.
Dependencies already satisfied by packages being kept or installed are not
requested again. For example, repairing an application's missing dependency
does not force a switch away from codec providers already selected by the
Codec Wizard fixups. The log identifies requirements still needing repair.

Before staging, it checks the complete proposed package set, including unchanged
and excluded packages, for missing dependencies, conflicts, and duplicate
versions. An unresolved problem stops preparation before installation or reboot.
It does not remove applications to force a repair. An installed package being
marked obsolete by another package does not by itself indicate a broken
dependency or trigger rollback.

DNF can remove a redundant `noarch` copy when the same package will remain
installed for the native architecture, or vice versa. The updater recognizes
that replacement without treating it as an unrelated package deletion. A
64-bit library is never treated as a replacement for its 32-bit counterpart
under this rule.

Obsolete-package cleanup applies to all package names. DNF handles incoming
replacements; the updater also finds obsolete leftovers whose replacement is
already installed. It uses RPM's version and epoch rules and checks the final
planned package set, including replacements that change version or architecture.
Cleanup occurs within the same prepared, tested transaction as the update.

The log names each obsolete package being removed and the replacement that
will remain installed. Essential packages and install-only packages such as
retained kernels remain protected. Being absent from a repository is not enough
to remove a package.

If a replacement is also being removed or no longer declares the old package
obsolete in its new version, that relationship cannot authorize cleanup.
Ambiguous replacement cycles stop preparation with the affected package names.
Unexpected removals of dependent applications also stop preparation. Resolve
those conflicts using [RECOVERY.md](RECOVERY.md); the updater does not erase
applications just to force obsolete-package cleanup through.

On systems using Plymouth, the updater also repairs missing BGRT theme files
and its `two-step` plugin. It installs missing packages or restores deleted
files from installed packages using an upgrade or reinstall. These RPMs are
prepared and verified before theme selection and initramfs rebuilding; a bad
repository payload stops preparation. Restoring the BGRT fallback preserves
custom theme selection and the existing SteamOS/BGRT session-selection policy.

Before declaring an update ready, it downloads the required RPMs, checks
their signatures, tests the planned installation, and checks required space
and other prerequisites. Conflicts or missing packages stop preparation
before installed packages change.

If the updater says **“System update will install on the next restart,”**
preparation has finished and installation is scheduled. The system packages
have not yet been installed. You can save your work and restart when ready.

If you install or remove RPM packages after preparation, the saved plan may
no longer match your system. The updater refuses to apply an outdated plan;
run it again to prepare a fresh one.

## Storage, logs, and the offline trigger

Downloaded RPMs and the saved transaction live under
`/var/lib/nobara-updater/jobs/<id>`, not on `/boot`. The updater uses systemd's
standard offline-update trigger: `/system-update` is a symlink to
`/var/lib/nobara-updater`. A Nobara service then installs the saved transaction
using DNF5 replay and manages snapshots, validation, and recovery. It does not
use DNF5's own `install --offline` job storage.

`/boot` holds kernel/initramfs images, recovery boot images, and bounded recovery
diagnostics. Boot recovery archives are stored with the job on the root/state
filesystem. Required free space depends on the transaction and recovery layout:
normal boot images determine the estimate, not the often much larger generic
`0-rescue` initramfs. Kernel or driver changes need room for new/temporary images;
a transaction that leaves them unchanged does not reserve space for rebuilding
them. Recovery space is included only when recovery is available. A 1 GiB
`/boot` is not automatically unsupported, but it still needs enough actual free
space. If it is full, review unused kernels with Nobara's kernel management tools;
do not delete the running kernel or updater recovery files by hand.

An installation without a separate `/boot` can still use offline updates unless
its administrator requires automatic recovery. Automatic rollback remains
unavailable for that layout. After a failure and manual repair, follow
[RECOVERY.md](RECOVERY.md) and use `sudo nobara-sync retry-update`.
Do not manually create `/system-update` or delete updater state to force a retry.

Logs are saved automatically in `/var/lib/nobara-updater/client.log` and
`/var/lib/nobara-updater/update.log`; service output is also in the system journal.
Use `nobara-sync recovery-report` for a readable failure report, or
`nobara-sync recovery-report --save update-logs.tar.gz` to export it without
internet access. Downloads, dependency checks, RPM verification, and rebuilding
drivers/initramfs can consume CPU; a busy CPU alone does not establish a stuck
update. Include these logs when reporting a stall or unusually long update.

## What happens during an offline update

**Offline** means installation happens before the normal desktop session
starts. You do not need to disconnect the network. The installation uses
the packages already downloaded and does not need repository access.

The usual sequence is:

**Desktop → restart → offline installation → automatic restart → normal startup**

1. You restart. The system automatically enters offline update mode.
2. The updater rechecks the saved packages and installed-package state. On
   a supported layout, it prepares rollback data and a recovery boot entry
   before changing system packages.
3. It installs the prepared transaction and performs required system fixes.
4. It builds and checks required kernel modules and boot files, then verifies
   the installed packages, dependencies, and Nobara version.
5. It automatically restarts into the updated system. When a kernel was
   updated, it selects the newly updated kernel for this boot and checks
   that the expected kernel is actually running.
6. Startup checks run before the update is marked complete. When a recovery
   target exists, the updated system gets a trial boot with recovery still
   available until confirmation.

**The second restart is expected.** It boots the completed installation,
including a new kernel when one was installed. You normally do not need to
choose a boot entry manually. Keep the computer powered on while installation
and boot-file preparation are in progress.

With the BGRT or Spinner Plymouth theme, the offline updater shows a dedicated
update screen with a persistent warning not to turn off the computer. The
progress bar advances as package operations finish and stays below 100% until
the installation and boot-file checks have passed. Progress measures update
stages, not time remaining: driver compilation and initramfs generation can
take several minutes without moving the bar. Keep the computer powered on;
press **Esc** to view the detailed console output if needed. Custom Plymouth
themes determine how they display update mode and messages.

## When the system enters recovery

**The updater's automatic snapshot recovery mode applies only to systems
using Btrfs, ext4 with LVM, or XFS with LVM.** It does not apply to plain
ext4 or XFS partitions without LVM, or to other filesystem layouts.

| Root filesystem layout | Automatic snapshot recovery |
| --- | --- |
| Btrfs | Supported with a suitable root subvolume layout |
| ext4 with LVM | Supported with a suitable root logical volume and recovery storage |
| XFS with LVM | Supported with a suitable root logical volume and recovery storage |
| ext4 or XFS without LVM | Not supported |
| Other filesystem layouts | Not supported |

The filesystem type alone does not guarantee recovery is available. These
layouts must also meet the updater's boot and storage requirements, including
a separate `/boot`, a supported bootloader configuration, and sufficient
recovery space. Automatic rollback requires a recovery target that was
successfully created before installation.

Boot entries may use GRUB's `$kernelopts` and TuneD's `$tuned_params` and
`$tuned_initrd` variables. Recovery entries store their resolved values, and
TuneD's extra initrd is saved with the other boot images. Entries that use
other GRUB variables are reported as unsupported.

When a recovered Btrfs root returns to its original subvolume name, normal
boot entries retain dynamic TuneD parameters. On ext4/XFS with LVM, the first
boot after restoring the snapshot uses the saved parameters. Once that boot
is confirmed, its normal entry follows TuneD profile changes again. The
copied kernel and initrd images remain available while that entry is selected.
Manually edited boot options are preserved; older LVM recovery archives without
saved TuneD options keep their existing parameters.

The updater reports whether automatic recovery is available. Installations
configured to require it stop before installation if it is unavailable.
Other configurations can permit offline updates without automatic rollback.

| Where a failure occurs | What happens |
| --- | --- |
| Download, dependency resolution, signature checks, or preparation tests | Preparation stops before system packages change. No rollback occurs. |
| Rechecking a staged update before installation starts | The updater refuses the stale or invalid plan. Prepare the update again. |
| Offline package installation or post-install validation, with a recovery target | The system selects the previous system and restarts into recovery. |
| Startup validation of the updated system, with a recovery target | The system selects the previous system and restarts into recovery. |
| An updated trial boot never reaches confirmation | Recovery remains selected for the next restart. A frozen machine may need a manual restart. |
| Installation fails without a recovery target | Automatic rollback cannot take place. The system may need manual repair; if it can boot, review its failure report. |

Examples of failures that can trigger recovery include RPM installation or
scriptlet errors, failed driver/module builds, invalid or missing boot files,
broken installed dependencies, an unexpected Nobara release, or booting the
wrong kernel after an update selected a particular one.

Startup confirmation also checks that the running kernel has its module
directory and that the configured login-manager service is active. These
checks do not test every application, game, driver feature, or graphical
session. An application crashing later does not automatically trigger a
system rollback.

After recovery, the restored system becomes your active system. The normal
boot entry uses it, so repairs made there persist across normal restarts.
**If an update fails, follow [RECOVERY.md](RECOVERY.md)** to review logs, resolve the problem,
and prepare a new update. Failed installations are not automatically retried
in a loop.

## When snapshots are created and removed

Checking for updates or downloading an update does not create a rollback
snapshot. Neither does an eligible immediate application update.

For an offline update on a supported layout, recovery data is created
**during the restart into offline update mode, after the saved plan is
rechecked and before system packages are changed**:

- **Btrfs:** The updater creates a read-only snapshot of the current system
  and a writable recovery copy, outside the active root subvolume.
- **ext4 or XFS with LVM:** The updater creates a snapshot of the root
  logical volume using the available recovery storage.

The updater also prepares a temporary **previous system** boot entry with
saved boot files. Recovery data remains available throughout installation,
post-install validation, and the updated system's trial boot.

### After a successful updated boot

Once startup checks confirm the updated system, the updater marks the update
complete and retires the temporary rollback data:

- **Btrfs:** It removes the unused snapshot and recovery copy, including the
  newest rollback set and older unused updater-owned sets.
- **ext4 or XFS with LVM:** It removes the rollback snapshot and restores
  the reserved storage for the next update.
- **All supported layouts:** It removes retired snapshot recovery entries
  from the boot menu. Normal boot entries remain selected, and boot files
  still referenced by them are preserved.

### After a successful recovery boot

If the update failed and the previous system was restored, that restored
system becomes the active system:

- **Btrfs:** The writable recovery subvolume becomes your active root. After
  confirmation, the updater restores its original root name (usually `@`) and
  points the normal kernel entry to it. Changes made in recovery are preserved.
  This also restores the root layout expected by Timeshift, where applicable;
  Timeshift does not need to be installed. The updater preserves this active root
  and removes its redundant read-only backup, other unused rollback copies,
  and the temporary recovery menu entries after recovery is confirmed.
- **ext4 or XFS with LVM:** Recovery merges the snapshot back into the root
  logical volume before starting the restored system. After confirmation,
  it retires the snapshot recovery entry and restores the recovery reserve.

Your active system is never deleted merely because it originated as a
recovery copy. Repairs made there remain part of the system used by normal
boot entries. Running the updater again prepares a fresh transaction; its
next offline installation creates new recovery data from that current,
repaired system.

### Cleanup and retention

A cleanup problem is logged and retried at a later boot; it does not turn a
successfully confirmed update into a failed update. Mounted subvolumes and
boot files still in use are protected. Cleanup applies only to the updater's
own recovery data, not snapshots created by other tools.

The temporary **previous system** entries are separate from ordinary kernel
entries and the generic rescue-kernel entry. Those entries are not rollback
snapshots and are not removed by snapshot cleanup.

Ordinary kernel packages have a separate cleanup step after a successful boot
and retirement of recovery data. It honors DNF's configured kernel retention
limit (normally three versions; zero means unlimited), including the matching
kernel development and module packages. The running kernel and its matching
packages are always retained, so an older running kernel can temporarily put
the total above the limit. Cleanup also catches up on the next updater run,
even if there are no new updates. If cleanup is unsafe or cannot finish, it is
logged and retried later without marking the successful update as failed.

After successful boot confirmation, the updater also removes obsolete fallback
kernels that it could not rebuild because their matching development packages
were unavailable. Old orphaned kernel module packages are cleaned up with their
version-specific generated kmod RPMs, including ones recorded as locally
installed. This targeted cleanup applies even below the retention limit or when
the limit is zero. It always protects the running kernel and waits until updater
recovery protection has been retired. Unrelated local packages and generic
driver packages are preserved. Standalone development packages alone are not
treated as obsolete.

Rollback protection covers installation and initial startup validation.
Once confirmation retires the snapshot, it is no longer available to undo
a problem discovered later. A future offline update creates fresh recovery
data when the system layout supports it.

## Media codec protection

Nobara manages the media packages detected or installed by its Codec Wizard.
DNF5 and DNF App Center block manual removals, provider swaps, downgrades,
and reinstalls of these packages and their installed required dependencies.
New codec providers must also be installed through the wizard. Ordinary
upgrades of the same package and architecture remain allowed.

Protection applies to the installed codec family before running the wizard
and to its replacements afterward, including required dependencies and
32-bit packages. The check uses the current installed package database for
every transaction, so no manual refresh or reboot is needed to protect the
new packages.

Mesa Vulkan driver selection remains available through **Nobara Driver
Manager**. The `mesa-vulkan-drivers`, `mesa-vulkan-drivers-freeworld`,
`mesa-vulkan-drivers-git`, and `mesa-vulkan-drivers-git-freeworld` packages are
excluded from codec protection, including their 32-bit variants and dependencies
used only by those drivers. Shared dependencies required by protected media
packages remain protected, as do Mesa's VA-API and Gallium codec packages.
The Codec Wizard can still select its usual freeworld Vulkan variants.

The optional `helium-bin` browser and its browser-only dependencies are also
excluded. Its bundled graphics libraries do not make it a protected codec;
users can remove the browser normally. Shared system codec dependencies
remain protected.

Use the **Codec Wizard** or `nobara-sync install-codecs` to change the codec
family. System updates through `nobara-sync cli` can also perform Nobara's
managed replacements. A blocked manual transaction lists the affected
packages and points to these tools.

This is protection against accidental package changes through DNF5 and its
App Center integration. It is not a restriction on administrators using RPM
directly or deliberately disabling DNF plugins.

## Scope of this guide

This describes RPM system updates run through DNF App Center's system updater
or `nobara-sync cli`. Flatpak updates use their own update mechanism. Updates
performed inside the installer also use a separate installation workflow.

For failure reports, offline log bundles, and support instructions, see
[Recovering from a failed Nobara update](RECOVERY.md). Technical and
administrator details are in [Nobara system updates](OFFLINE_UPDATES.md).

## Selecting updates in DNF App Center

Use **Select All** to check the available packages, deselect any you want to
leave for later, then choose **Update Selected**. Selecting packages does not
start an update. Updating a single package from the Updates page uses the same
Nobara updater as updating a selection.

On Nobara, App Center sends the selection to `nobara-sync`; it does not install
packages one at a time. Nobara's repository priorities, codec handling, fixups,
transaction checks, and live/offline decisions still apply to one resolved
transaction. Required dependencies and fixups can add
packages you did not select. Updater and `drm-awaiter` prerequisite upgrades still
run first. A Nobara release upgrade requires a full system synchronization and
includes all required release packages, even with a smaller selection.

One overall progress bar is mirrored at the bottom of the window and on the
Queue page, for example **0/25 processed items**, **12/25 processed items**, then
**25/25 processed items**. Both copies show the same count and progress. The total
includes required dependencies and fixups added to the resolved transaction.
Package details appear in the transaction log.

The status beside the bar identifies the current phase. Download progress comes
from DNF5 download callbacks; live installation progress comes from native DNF5
transaction output. The same bar resets for installation after downloading.
**Validating downloads** means validation is still underway. **Prepared for
restart** means the packages have been staged, not installed. Offline installation
runs after restarting, with progress on the boot splash. **Finished** is shown
after successful completion; if validation fails, the queue shows the failure.
The system update result remains visible until the next system update.

Selecting every available update performs the same full distro-sync as
`nobara-sync cli`. For a selection in a terminal, use, for example:

```sh
sudo nobara-sync cli --package=editor --package=library.i686
```

`--all` still means also update Flatpaks. `--progress` enables the structured
frontend progress stream; it is unnecessary for ordinary terminal use. If a
prepared update has a different selection, finish it or cancel it with
`sudo nobara-sync cancel-update` before preparing a new selection. See
[RECOVERY.md](RECOVERY.md) for failures and repair instructions.
