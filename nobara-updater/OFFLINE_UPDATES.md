# Nobara system updates

User guides: [How updates are handled](UPDATES.md) and
[Recovering from a failed update](RECOVERY.md).

The system updater is now a CLI/backend; its embedded GTK updater window and
launcher have been removed. The update frontend is dnf-app-center, and the
existing `nobara-sync cli [--all]` invocation is preserved. The separate
`nobara-codec-wizard` GUI, launcher, and dependencies remain available for the
welcome center's existing codec-wizard script.

## What happens

1. A systemd service refreshes the existing enabled repositories, detects the
   target release, and resolves one distro-sync transaction, including known
   package migrations and updates to installed group/environment members.
2. It refuses broken dependencies, unexpected removals, missing required
   repositories, untrusted packages, or insufficient space. Every RPM is
   downloaded, signature-checked, and RPM-tested before the update is ready.
3. It saves the exact replay transaction, comps definitions, RPMs, checksums,
   RPM database fingerprint, and a copy of the worker under
   `/var/lib/nobara-updater/jobs/<id>`.
4. `cli` installs eligible application-only transactions immediately through a
   system service. Core and major-release transactions are scheduled for the
   next ordinary restart. `--all` additionally updates system Flatpaks and the
   invoking user's Flatpaks while the normal session is running.
5. For offline updates, at reboot the worker rechecks the saved payload and installed-package
   fingerprint, creates recovery data where supported, then invokes native
   DNF5 replay with every remote repository disabled. Installation is never
   retried automatically.
6. A fresh Python interpreter runs the saved post-install worker. It performs
   deferred configuration migrations, builds/checks kernel modules and boot
   images, checks installed inbound RPMs and dependencies, verifies the release,
   and selects the updated kernel for a trial boot. A startup service checks the
   release, dependencies, running kernel module directory, and exact selected
   kernel version and enabled login-manager service before recording completion.

No repository publication changes or custom upstream metadata are required.
The standard release provide is used where present; Nobara's
`nobara-release-common` RPM Version is the fallback because current Nobara
release packages do not advertise `system-release(releasever)`. A different
release causes the complete goal to be rebuilt with that releasever. Release
packages are updated in the same transaction as the rest of the OS.

Repository priorities and package excludes remain in effect. The updater does
not downgrade its own package, preserving its boot/recovery protocol. Locally
installed RPMs are held at their installed build, including the updater itself.
Repository-installed updater builds remain eligible for normal upgrades. The updater does
not automatically upgrade group/environment definitions: Nobara copies Fedora
comps unchanged, and newly added Fedora defaults can conflict with Nobara's
desktop replacements (for example, plasma-setup requiring Fedora's appearance
package). Installed group packages are updated through distro-sync. Required
new Nobara packages are added through explicit migrations, without rewriting
repository metadata or changing installed group membership.

### Local RPMs and third-party repositories

DNF5's recorded installation repository determines package origin, not whether
the user typed a command or the RPM's installation reason is `User`. A normal
`dnf install package-name` from a configured repository remains eligible for
updates. See [DNF5's package-origin API](https://dnf5.readthedocs.io/en/stable/api/python/libdnf5_rpm.html#libdnf5.rpm.Package.get_from_repo_id).

The updater preserves RPMs recorded as `@commandline`/`commandline`, and
conservatively preserves packages whose origin is missing, `<unknown>`, or
`@System` (including direct `rpm` installs). It excludes repository replacements
before solving and rejects any resolved migration that would remove, replace,
or reinstall a protected local RPM. Different-name replacements through RPM
Obsoletes are excluded too. Unrelated updates proceed when dependencies allow.
These holds are computed for each updater transaction; they do not create
permanent DNF versionlocks or prevent a deliberate manual package change.
To resume repository updates, explicitly install/reinstall the chosen repository
build using DNF. Source installs outside the RPM database cannot be identified
or protected by this policy; neither can unrecorded edits to packaged files.

The Nobara repository IDs are `nobara`, `nobara-updates`,
`nobara-kernel-mainline`, `nobara-kernel-lts`, `nobara-pikaos-additional`,
`nobara-nvidia-production`, and `nobara-nvidia-new-feature`. Packages installed
from other repositories are still eligible for distro-sync under normal repo
priorities, including replacement by a Nobara build. When a conflict identifies
such an installed package and an enabled Nobara repository supplies its name
and architecture, diagnostics list the installed NEVRA, third-party repo ID,
and the Nobara counterpart repository IDs. Unknown origin is reported as
unverified, not falsely attributed to a particular third party.

Solver conflicts stop preparation before RPM writes or reboot. They produce
the same user-readable failure report and offline tarball as installation
failures, without claiming rollback occurred. The saved transaction and
separate boot diagnostics retain origin information for errors after reboot.
Reports identify exact packages named by failure evidence; the presence of a
local/third-party package alone is not treated as proof it caused an error.

If a transaction includes comps definitions, its local XML comes from libdnf5's
serializer. A repository cutover which deletes an RPM while downloading fails
preparation; the next attempt uses fresh metadata. Once staging succeeds,
deleted upstream RPMs cannot break replay.

DNF controls RPM dependency ordering within one transaction. The former
core/desktop/kernel groups no longer commit separate partial OS updates.
Package fixups do not execute during an update check. There is no unsigned
bootstrap, erase-and-reinstall gap, or dependency on downloading old RPMs for
DNF history undo.

## Immediate application updates

The default is to install application-only transactions during the current
session. Classification covers the entire solved transaction, including
dependencies and outgoing package files. Incoming file inventories are read
from the signature-checked RPMs, so incomplete repository filelists cannot
hide a shared library or service. Unknown/empty inventories require offline
installation.

Restart explanations in the log are grouped by category and wrapped across
short lines. A package is shown once even if multiple checks identify it;
the saved plan retains all reasons. DNF's metadata-only `Reason Change`
operations do not force an offline update because they replace no files.

Release transitions, core packages (kernel, drivers, system libraries,
package manager, Python, updater, and desktop/session components), shared
libraries, system services/configuration, removals, downgrades, and deferred
fixups keep the entire transaction offline. A batch containing both ordinary
applications and core changes therefore still requires a restart. The updater
does not split a solved transaction into separately installed subsets.

Live jobs retain download/signature/RPM checks, saved-payload verification,
the installed-package fingerprint check, exact native DNF5 replay with remote
repositories disabled, and post-install package/dependency validation. The
`nobara-updater-live.service` owns installation independently of the client and
holds a sleep/shutdown inhibitor while it runs. Live jobs do not build boot
images or create system recovery snapshots. Users should restart updated
applications to load their new versions. `live-complete` means installation
and validation finished, with no system restart requested.

Classification is a conservative package/file policy, not a sandbox for RPM
scriptlets or a guarantee about every running application. Installation
failures remain recorded and are never replayed automatically. An interrupted
application-only job blocks another update until repaired, but does not
automatically invoke boot recovery or force the desktop into emergency mode.

Already prepared jobs from older updater versions remain offline; already
scheduled jobs keep their restart schedule. To reconsider a job which has not
started installing, cancel it with `nobara-sync cancel-update`, then run
`nobara-sync cli` again. The new preparation uses current repository contents.

## Kernel selection after an update

When the transaction installs/upgrades/reinstalls a `kernel` or `kernel-core`
RPM, the updater selects the newest kernel supplied by that transaction using
RPM epoch/version/release ordering. It does not select a rescue image, recovery
snapshot entry, or an older retained kernel based on menu position. An LTS
update stays on the updated LTS kernel even if a newer mainline kernel remains
installed from an earlier switch.

After package, driver-module, initramfs, and dependency checks pass, a protected
update selects the kernel's normal GRUB/BLS entry for one trial boot. The
previous-system entry remains the persistent fallback until startup validation
succeeds. Private GRUB environment variables keep this fallback effective even
when package scriptlets change `saved_entry`. Startup confirmation saves the
validated kernel as the normal default and clears the fallback. Installer
updates and updates without recovery select the normal default directly.
Pending overrides are cleared and GRUB environment changes are read back.
When necessary, GRUB configuration is regenerated, syntax-checked, and replaced
atomically, preserving other settings. A failed selection prevents completion.

The exact selected kernel is recorded in the update state. The automatic
post-update reboot must start that kernel before startup confirmation can mark
the update complete. Booting an older kernel, including a manual selection,
fails this confirmation and invokes the existing recovery policy. Previous
kernels remain available. If the trial kernel cannot reach startup validation,
the next reset boots recovery automatically. A hard hang can still require a
physical reset; this implementation does not provide a hardware watchdog.

This selection backend supports Nobara's GRUB with split kernel/initramfs BLS
entries. Kernel updates on other loaders or UKI-only layouts are rejected
during preparation, before RPM installation. Ordinary non-kernel updates do
retain the existing kernel while using the same recovery trial. Installer updates select the kernel inside the
Calamares target and retain that expectation across a following codec job.
Previously prepared jobs retain their frozen engine; cancel and reprepare an
unstarted job to use this behavior.

## Commands and frontend contract

| Command | Effect |
| --- | --- |
| `nobara-sync cli` | Install eligible application updates now; schedule core/release updates for restart |
| `nobara-sync cli --all` | Same, plus live system/user Flatpak updates |
| `nobara-sync prepare-update` | Prepare without installing or scheduling |
| `nobara-sync install-codecs` | Stage optional codecs and OS updates together |
| `nobara-sync repair` | Stage distro-sync and known package migrations |
| `nobara-sync update-status --json` | Read persistent state as JSON |
| `nobara-sync schedule-update` | Schedule an already prepared job |
| `nobara-sync reboot` | Schedule and restart immediately |
| `nobara-sync cancel-update` | Cancel a job before installation begins |

`install-updates` is the legacy alias including Flatpaks.
`install-fixups` includes migrations in the complete staged transaction.
Explicit `sudo` works; a terminal invocation without root elevates with sudo.
The app center's existing privileged helper can invoke the CLI directly.
The CLI has no GTK import, display-server access, or interactive codec prompt.
The standalone codec wizard keeps the consent UI and delegates to
`nobara-sync install-codecs`; on a desktop it reports that a restart is required
after preparation, rather than claiming the packages have already installed.
Existing codec selections remain part of automatic fixups: an enabled codec
repository or installed freeworld packages triggers codec repair in the normal
update goal. Fresh installations are not opted into codecs without a request.

Exit 0 means the requested CLI operation succeeded, not necessarily that the
OS update has been installed. A successful CLI operation ends with a result
marker; for an offline transaction:

```text
NOBARA_UPDATE_RESULT {"status": "scheduled", "message": "System update will install on the next restart.", "reboot_required": true, "automatic_recovery": false}
```

Frontends must use the status/message instead of unconditionally saying
"System update completed." The companion change is in
`integrations/dnf-app-center-offline.patch`. It handles both the privileged
helper and direct-root execution, and avoids changing displayed installed
package versions after staging. App Center consumes the marker without logging
the raw JSON, shows a restart notice when `reboot_required` is true, and avoids
repeating identical completion messages. An immediate installation instead
reports `status: "live-complete"` and `reboot_required: false`; installed
versions are refreshed from the package database when the queue finishes.

Closing the CLI or app center does not kill the preparation or live installation
service. If the client disconnects after preparation but before installation or
scheduling, the job remains ready; invoking `cli` again resumes that next step.
If another package manager changes installed packages after
staging, the next update invocation invalidates the unstarted plan, removes
only its own boot trigger, and prepares a fresh transaction. Repeated CLI calls
revalidate scheduling even when saved state already says `scheduled`. A missing
trigger is restored only after validating the payload and package fingerprint;
masked/missing offline services or missing target dependencies block scheduling.
Read-only status never calls a missing trigger scheduled, and startup detects
missed scheduled updates. A job whose RPM installation started is never retried
by this mechanism. A failed Flatpak update returns failure but does not discard an
already scheduled system update; status identifies that situation.

## Calamares and other installer chroots

The existing Calamares calls work unchanged: `nobara-sync cli`, followed by
`nobara-sync install-codecs` when selected. Calamares uses `dontChroot: false`.
The CLI detects an actual chroot using `systemd-detect-virt --chroot` and runs
the worker synchronously inside that target. A missing system bus or the
`/etc/nobara/newinstall` marker alone never enables this mode.

The target path still resolves, downloads, signature-checks, RPM-tests, and
replays the complete transaction. It then starts the saved validation worker
under the target's new Python interpreter and returns its exit status to
Calamares. It does not start the live ISO's preparation service, create an
offline reboot trigger, snapshot the live system, or request a reboot.
`SYSTEMD_OFFLINE=1` is inherited by RPM scriptlets, and service-enable hooks
explicitly operate on `--root=/` inside the chroot.

A successful target transaction records `installer-complete`. The installer
can then run the codec operation and its remaining configuration/package steps.
The normal startup service can confirm the resulting installation on first
boot. A transaction or validation failure returns nonzero immediately; it does
not reboot or automatically retry a partly installed target. Recovery in this
mode belongs to the installer. `--all` user Flatpak updates and explicit reboot
or scheduling commands are rejected in the target root.

Codec installation includes system synchronization in both modes, so packages
from a rolled-over repository are not mixed into an otherwise old OS release.

## Guided installation, SSDs and recovery storage

The erase-disk choices are **ext4 with LVM** (default), **xfs with LVM**, and
**btrfs**. Existing separate `/boot` and UEFI `/boot/efi` partitions are preserved
in the layout. Selecting encryption puts the complete LVM PV, including home,
swap and recovery reserve, inside LUKS2. Btrfs keeps its existing subvolume
layout and optional encrypted container. Manual/replacement partitioning does
not silently convert existing filesystems to LVM.

The LVM plan provides separate root and home LVs, optional swap, and an
unformatted read-only reserve LV tagged as updater-owned. Root gets 45% of
capacity remaining after swap and overhead, capped at 96 GiB; it must be at
least 16 GiB and 1.5 times Calamares's required installation storage. Home gets
the remainder and must have at least 4 GiB. The rollback reserve covers a full
root volume plus metadata, so it cannot run out merely because every root
block changes. The installer previews these sizes and rejects disks too small
for the complete plan. A 40 GiB disk with hibernation swap may be too small.
User files under `/home` are outside OS rollback; files elsewhere in root are
subject to rollback. Ext4 root and home are formatted with `metadata_csum`
explicitly enabled and verified before installation continues.

| Filesystem | Mount defaults relevant to SSDs | TRIM strategy |
| --- | --- | --- |
| Btrfs | `relatime,discard=async,X-fstrim.notrim` | Asynchronous continuous discard; excluded from periodic fstrim |
| Ext4 on LVM | `relatime,nodiscard` | Weekly `fstrim.timer` |
| XFS on LVM | `relatime,nodiscard` | Weekly `fstrim.timer` |

Btrfs retains zstd compression. Its swap subvolume also uses `relatime`, async
discard and the periodic-TRIM exclusion. The timer remains enabled for other
filesystems, including `/boot` and the ESP; these do not duplicate Btrfs TRIM.
Fresh encrypted containers allow discards through dm-crypt using crypttab and
initramfs settings. Discards expose allocation patterns, not plaintext.

Classic LVM snapshots temporarily suppress TRIM on their origin. The updater
therefore retains the snapshot through installation and boot validation, then
removes it and recreates the reserved storage. This restores root TRIM and
avoids ongoing snapshot write overhead. Btrfs also retires rollback snapshots
after confirmation. Neither backend retains an updater rollback snapshot for
reverting a problem discovered later; a future update creates a new one.

## Recovery policy and limits

Automatic recovery supports a Btrfs root subvolume or a linear, fully
provisioned ext4/XFS root LV with independent persistent `/home` and sufficient
updater-owned reserve/free VG space. Thin and RAID LVs are currently excluded.
The root must contain system state and the RPM database. Both backends need
a separate /boot filesystem and GRUB with BLS split kernel/initramfs entries
and GRUB_DEFAULT=saved. External system-state mounts,
unsupported nested subvolumes, ambiguous boot entries, and UKI-only recovery
layouts are rejected by the recovery probe.

Nested `/var/lib/machines` and `/var/lib/portables` subvolumes are supported as
shared container data. Btrfs root snapshots omit nested subvolume contents, so
the updater records their filesystem UUID and stable subvolume ID and adds
explicit mounts to the recovery clone's `/etc/fstab`. Container contents and
guest subvolumes remain accessible and retain their current data across OS
rollback. This does not roll back guest/container data. The running system's
fstab and subvolume layout are unchanged; empty directories need not be
deleted or converted. Existing persistent mounts are preserved, while
uncovered host OS state (for example a separate RPM database) still blocks
automatic recovery. See the [Btrfs nested-subvolume documentation](https://btrfs.readthedocs.io/en/latest/btrfs-subvolume.html#nested-subvolumes).

For Btrfs the worker creates a read-only root snapshot and a writable recovery
clone outside the active root subvolume. For LVM it creates a classic root
snapshot and builds a recovery initramfs before changing packages. This image
validates LV identities and ownership, merges the snapshot before mounting
root, and marks the restored system. Persistent merge intent permits retry
after power loss during/after merging. Recovery does not depend on the updated
system's Python or libraries. LUKS volumes are unlocked before merging.

Separate BLS entries use copied pre-update kernel/initramfs files. The updater
arms the recovery default before RPM writes and allows only one trial of the
updated system. Detected installation or startup validation failures select
recovery and reboot. A separate systemd failure action handles failures that
prevent the Python recovery worker from starting. Failure to confirm a trial
leaves recovery selected for the next reset.
The old boot and EFI files are also archived in the job directory for manual
recovery. The archives are not automatically written over a working bootloader.

A completed startup removes all unused updater-owned Btrfs rollback snapshots
and their recovery menu entries, including the latest rollback set. A recovered
subvolume that became the active root remains in place behind the normal kernel
entry; its redundant saved snapshot and recovery-labelled entry are removed.
Mounted subvolumes and subvolumes referenced by remaining boot entries are
protected. Cleanup refuses to run while a fallback/trial boot is armed.
On LVM, completed startup or recovery retires the snapshot and restores the
reserve. Copied boot files still referenced by normal entries are retained.
Cleanup failures do not invalidate a confirmed boot and are retried at the next
startup. Failure reports are copied to the restored system before cleanup.
Obsolete staged payloads/caches are removed. Kernels are retained during the
upgrade rather than removed before the replacement has booted; ordinary kernel
retention policy can be applied separately after validation.

On unsupported layouts the default permits offline updates and explicitly
reports that automatic rollback is unavailable. New guided installations set
`require_offline_recovery: true`, refusing an offline update before RPM writes
if recovery becomes unavailable while still permitting eligible live app
updates. Administrators can use `/etc/nobara-updater/offline.json`:

```json
{"require_offline_recovery": true, "prepare_attempts": 3}
```

`require_recovery: true` requires recovery and also forces offline installation. To require offline
installation without requiring automatic recovery, set `live_updates: false`.
Without installer/administrator settings, the defaults are `live_updates: true`,
`require_recovery: false`, and `require_offline_recovery: false`.

Attempts must be 1–5. Deterministic policy/validation failures are not retried.
Only preparation can retry. An interrupted or failed installation blocks a new
update until recovery; it is never silently rerun. Without a supported recovery
snapshot, an offline installation failure enters emergency recovery.

This is not an atomic filesystem deployment. RPM transactions can partially
apply, and a power failure, full Btrfs metadata allocation, or bootloader/disk
failure can still require manual recovery. Automatic fallback needs a working
GRUB, boot partition, storage stack and retained recovery images. Hard hangs
need a reset. Startup checks verify the configured login-manager service is
active but cannot certify every graphical session, GPU workload or application.
Snapshots are not external backups, and this cannot guarantee recovery from
every possible failure.

## Packaging and rollout

Release `2.0.1-53` bundles `UPDATES.md`, explaining immediate versus offline
updates, reboot sequencing, automatic recovery triggers, and snapshot cleanup.
It links to the companion recovery guide; update behavior is unchanged.

Release `2.0.1-52` bundles [user recovery instructions](RECOVERY.md). Failure
reports and their offline tarballs explain the repair/retry/boot sequence,
local RPM preservation, and third-party repository handling. Instructions
distinguish a restored system, a preparation failure before RPM writes, and
a partial installation without rollback; the latter never suggests blindly
rerunning the updater.

Release `2.0.1-51` protects locally installed RPMs from distro-sync replacements
and identifies implicated local/third-party packages in preparation and recovery
reports. Rebuild/install `nobara-updater`; DNF App Center and the codec/installer
callers use the existing CLI/report interfaces. Already prepared transactions
must be cancelled and prepared again to use the new policy.

Release `2.0.1-50` retires Btrfs rollback snapshots and their GRUB entries after
successful confirmation, with the same retention period as LVM. Existing
completed/recovered jobs also receive this cleanup at startup. Active recovered
roots, referenced boot images, and snapshots owned by other tools are preserved.

Release `2.0.1-49` runs native DNF with umask `022` while retaining `077` for
private updater state. This keeps DNF's system-state TOMLs readable by ordinary
users. The RPM post-transaction script repairs the six known TOMLs written with
root-only permissions by earlier workers.

After a Btrfs rollback, the recovered subvolume becomes the active system.
Normal BLS entries and `/etc/kernel/cmdline` must follow that root; retaining
`subvol=@` boots the abandoned installation instead. Confirmation restores the
booted recovery kernel/initramfs to normal paths and promotes its canonical
entry without moving the active subvolume. Recovery entries remain until
confirmation retires them. Before later updates rebuild boot files, the updater also synchronizes
normal entry root arguments and future kernel-install options. Even a transaction
without a new kernel selects the normal entry for its trial boot. Existing
completed jobs using the old recovery-labelled default are repaired at confirmation,
provided no other update has an armed fallback.

Release `2.0.1-48` isolates native replay from repository definitions and plugins.
DNF5 5.4.3 otherwise reuses disabled remote repo objects for saved repo IDs and
can reject local RPMs in cache-only mode. Preparation now tests the serialized
transaction with native `dnf5 replay` and `tsflags=test`, after clearing metadata
cache and releasing the libdnf lock. No RPMs or scriptlets are installed by this
test. Recovery finishes before the fallback reboot service can run.

Failed updates preserve a bounded log and failure record under
`/boot/nobara-updater/<job>/`, outside root snapshot rollback. After recovery,
confirmation publishes a user-readable report under
`/var/lib/nobara-updater-reports/<job>/`, including an offline `.tar.gz` bundle.
Only updater logs and selected updater service journal entries are included;
common URL credentials are redacted. Users should still review before sharing.
The prearmed generic report also covers an interrupted update when Python
cannot record a specific exception. It cannot reconstruct logs never written.

Desktop login shows a notice once per recovered job per user. **System Update
Recovery** in the application menu reopens its error, log viewer, offline Save
logs action and explicit Upload logs action. Upload uses `pbcli` and returns a
Nobara paste link; nothing is uploaded automatically. Saving/viewing needs no
network or administrator authentication. CLI equivalents are:

```sh
nobara-sync recovery-report
nobara-sync recovery-report --save ~/nobara-update-report.tar.gz
nobara-sync recovery-report --upload
```

When no snapshot is available, diagnostics stay in the running root instead.
An otherwise bootable system also publishes a report and desktop notice after
a failed or interrupted update, explicitly saying that no rollback occurred.
Failed startup validation without a recovery target preserves FAILED status
but does not divert normal boot to an unavailable recovery system. A partial
installation stays blocked from automatic retry; the report suggests
`sudo dnf5 check` and error-specific repair or support. Merely reaching the
desktop never marks the update successful. No notification can be shown if
the machine cannot reach a working desktop session.

Package `nobara-update-notice`, both recovery desktop files, and `update_report.py`
with the worker. The notification/report UI uses GTK4, libnotify and pbcli; it
does not restore the retired updater GUI. Retained reports remain available
after a successful later update, which retires the login notification.

The implementation targets DNF5/libdnf5 5.4.3 or newer. Ship and test this backend
on the source release before relying on it to perform a major transition.
DNF's replay serialization API is marked experimental upstream; rerun the
integration suite when changing DNF versions.

`make DESTDIR=<staging-root>` installs the CLI, worker, units, and documentation.
Rebuild both nobara-updater and dnf-app-center for the live/result-UI changes.
The guided LVM installer/recovery changes additionally require
`nobara-updater-2.0.1-46` and `calamares-3.3.14-136` (or newer builds), and an ISO
containing both. The Calamares source patch and build notes are under
`integrations/calamares-lvm/`. Existing plain ext4/XFS systems are not converted.
Package `/etc/grub.d/42_nobara_update`, the `91nobara-rollback` dracut module and
all updater service units together with `update_lvm.py`.
Include `update_policy.py`, `update_boot.py`, and `nobara-updater-live.service` in the updater RPM
(the Makefile and wildcard file lists include them). Package upgrades must
reload systemd units. The offline unit is pulled in by
system-update.target; the confirmation unit is pulled in by multi-user.target.
Drop-ins prevent native DNF4/DNF5/PackageKit offline services and DNF cleanup
services from competing for
Nobara's trigger while allowing their own offline jobs to continue working.
Another updater's /system-update symlink is never replaced.
An existing `/etc/system-update` trigger also blocks Nobara scheduling.
Release `2.0.1-47` adds stale-plan reconciliation, trigger revalidation, service
readiness checks and updater downgrade protection.

Remove only obsolete updater-window files from the external RPM file list;
retain the codec-wizard files and their GTK/VTE dependencies. The legacy group-runner source is retained for compatibility
tests but is no longer installed or called by the CLI.

Logs and state are root-owned:

- `/var/lib/nobara-updater/state.json`
- `/var/lib/nobara-updater/client.log`
- `/var/lib/nobara-updater/update.log`
- `journalctl -u nobara-updater-live -u nobara-updater-offline -u nobara-updater-confirm -u nobara-updater-recovery`

## Validation

Run the unit/CLI suite with:

```sh
/usr/bin/python3 -B -m unittest discover -s tests -v
```

Real libdnf5 tests use disposable local repositories and an installation root.
They require rpmbuild and createrepo_c and use an unprivileged user namespace
for RPM's test chroot, without adding host privileges:

```sh
unshare --user --map-root-user /usr/bin/python3 -B -m unittest discover -s tests -p test_offline_integration.py -v
```

The real systemd boot generator can be exercised in a disposable chroot inside
the same user namespace; this never creates a host update trigger:

```sh
unshare --user --map-root-user python3 -B -m unittest discover -s tests -p test_offline_boot.py -v
```

The opt-in Btrfs integration test uses a disposable 256 MiB sparse loopback
image in `/tmp`, real nested subvolumes and snapshots, and a private mount
namespace. It verifies shared container data and retirement of actual read-only
snapshots and writable clones while preserving a mounted recovered root:

```sh
sudo unshare --mount --propagation private env NOBARA_BTRFS_INTEGRATION=1 /usr/bin/python3 -B -m unittest discover -s tests -p test_btrfs_recovery.py -v
```

The opt-in LVM suite uses disposable 44 GiB sparse loop images and a restricted
LVM inventory. It exercises installer provisioning, ext4 metadata checksums,
plain/LUKS2 ext4/XFS snapshot creation, merge retry, home preservation,
reserve restoration, successful-update snapshot retirement and real TRIM:

```sh
sudo unshare --mount --propagation private env PYTHONDONTWRITEBYTECODE=1 NOBARA_LVM_INTEGRATION=1 python3 -B -m unittest discover -s tests -p test_lvm_integration.py -v
```

Adding `NOBARA_LVM_VM=1` boots those images with QEMU and the actual pre-update
dracut recovery module, validating restore before root mount and switch-root.
These tests use a minimal test root and direct kernel boot, not GRUB or the
complete installer. An encrypted test-only keyfile is supplied for unattended
VM execution; production images use password unlocking. Generated mount/fstab
tests use `CALAMARES_SOURCE=/path/to/patched/source` with `test_calam_lvm.py`.

Before distribution rollout, boot-test representative Nobara VMs and hardware:
43→44 and same-release synchronization; Btrfs, guided LVM and unsupported layouts;
encrypted root; NVIDIA/DKMS and akmods; split images and UKIs; interrupted
downloads; repository cutover during staging; tampering and changed RPM database;
low /boot and root space; scriptlet/module-build failure; power loss during
installation; manual and automatic recovery; repeated updates from a recovered
root; and updater/Python self-upgrades. Local transaction tests do not replace
these reboot and hardware tests.
Also test a full Calamares installation with updates only, codecs only, both,
and neither selected; the welcome-center launcher; and failed target updates.
Test application-only updates with applications open, a mixed core/application
batch, App Center closure during live installation, shutdown inhibition, and
the restart notice after offline staging.
For kernel updates, also test a previously pinned older kernel, a pending
one-time GRUB entry, LTS updates with retained mainline kernels, kernel build
failure, and intentionally booting the wrong kernel after installation.
