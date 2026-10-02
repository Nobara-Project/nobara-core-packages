# Recovering from a failed Nobara update

For an explanation of immediate updates, offline installation, restarts, and
recovery triggers, see [How Nobara handles system updates](UPDATES.md).

Open **System Update Recovery** in the application menu to view the error,
read the logs, or save a report for support. From a terminal, run:

```sh
nobara-sync recovery-report
```

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

## If preparation stopped before installation

Some conflicts are detected before packages change. No reboot or rollback
is needed to address that failure. Review the error, resolve the conflict,
and run the updater again. Restart only if the updater asks you to after
preparing the new update.

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
- **Locally installed RPMs are preserved.** This includes local RPM
  installations and packages with no recorded repository origin, except when
  their installed RPM signatures identify a Fedora/Nobara distribution build.
  Missing history alone is not proof of a custom build. Explicit command-line
  installations remain protected even when signed by the distribution. Installing
  a repository package with `dnf install package-name` does not exempt it
  from updates. These preservation rules apply to Nobara's updater, not
  standalone DNF commands. RPM Fusion's free/nonfree repository release
  packages (including tainted and rawhide variants) are exempt from this
  protection; the updater no longer schedules fixups for them.
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
- **Source installations outside RPM's package database** cannot be
  protected by this mechanism.

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
