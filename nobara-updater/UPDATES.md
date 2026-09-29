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

The updater refreshes repositories and prepares one dependency-resolved
transaction. It uses DNF's `distro-sync` behavior, which can upgrade or
downgrade packages according to repository priorities and available versions.
The local RPM protection rules described in the [recovery guide](RECOVERY.md#how-packages-are-handled)
also apply.

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

- **Btrfs:** The writable recovery subvolume becomes your active root. The
  normal kernel entry points to it. The updater preserves this active root
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

Rollback protection covers installation and initial startup validation.
Once confirmation retires the snapshot, it is no longer available to undo
a problem discovered later. A future offline update creates fresh recovery
data when the system layout supports it.

## Scope of this guide

This describes RPM system updates run through DNF App Center's system updater
or `nobara-sync cli`. Flatpak updates use their own update mechanism. Updates
performed inside the installer also use a separate installation workflow.

For failure reports, offline log bundles, and support instructions, see
[Recovering from a failed Nobara update](RECOVERY.md). Technical and
administrator details are in [Nobara system updates](OFFLINE_UPDATES.md).
