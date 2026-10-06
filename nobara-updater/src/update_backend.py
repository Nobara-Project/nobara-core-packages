"""System-service orchestration for prepared Nobara updates."""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from collections import deque
from pathlib import Path

from .update_boot import newest_updated_kernel, pin_kernel, kernel_entry, synchronize_boot_root, installed_boot_kernels
from .update_recovery import create_recovery, probe_recovery, select_recovery, prune_recovery, trial_boot, confirm_trial, grub_environment, check_recovery_space
from .update_report import arm_report, publish_recovery, publish_failure, resolve_notice
from .update_origins import annotate_failure
from .update_progress import OfflineProgress, PackageProgress
from .update_state import (ACTIVE, STATE_DIR, TRIGGER, UpdateError, atomic_json, file_digest,
                           in_installer_root, os_release, package_selection, read_state, rpm_fingerprint, status_message, update_lock,
                           verify_files, write_state)

LOG = logging.getLogger(__name__)
SPLASH = OfflineProgress()
ENGINE_FILES = ("update_state.py", "update_migrations.py", "update_plan.py", "update_policy.py", "update_boot.py", "update_recovery.py", "update_lvm.py", "update_btrfs.py", "update_backend.py", "update_report.py", "update_origins.py", "update_codecs.py", "update_health.py", "update_repositories.py", "update_progress.py", "update_retention.py")
EARLY_PACKAGES = ("nobara-updater", "drm-awaiter")


def announce(message: str, *, percent: int | None = None) -> None:
    LOG.info("%s", message)
    SPLASH.message(message, percent=percent)


def run(command: list[str]) -> None:
    LOG.info("Running: %s", " ".join(command))
    replaying = Path(command[0]).name == "dnf5" and "replay" in command
    progress = None
    if replaying and "--setopt=tsflags=test" not in command:
        path = Path(command[-1]) / "progress.json"
        if path.is_file():
            try:
                saved = json.loads(path.read_text())
                progress = PackageProgress(path.parent.name, saved["packages"], stage=saved["stage"])
            except (OSError, ValueError, TypeError, KeyError) as error:
                # Presentation metadata is optional; never fail a validated
                # RPM transaction because its progress description was lost.
                LOG.warning("Package progress details are unavailable: %s", error)
            if progress:
                progress.plan()
                for record in progress.records:
                    progress.package(record, "waiting-install")
    columns = str(max([120, *[len(record["nevra"]) + 90 for record in progress.records]])) if progress else "120"
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding="utf-8", errors="replace", bufsize=1,
                               # Our job state is private (umask 077), but DNF
                               # writes public system-state TOMLs and runs RPM
                               # scriptlets. Use normal package-manager modes.
                               umask=0o022 if Path(command[0]).name == "dnf5" else -1,
                               # Preserve complete NEVRAs in native DNF5 progress lines. Its
                               # default non-TTY width otherwise silently truncates them.
                               env=dict(os.environ, LC_ALL="C.UTF-8", FORCE_COLUMNS=columns, DNF5_FORCE_COLUMNS=columns))
    assert process.stdout is not None
    rpm_errors = deque(maxlen=20)
    last_lines = deque(maxlen=5)
    for line in process.stdout:
        LOG.info("%s", line.rstrip())
        if replaying:
            SPLASH.transaction_line(line)
            if progress:
                progress.transaction_line(line)
        if line.strip():
            last_lines.append(line.strip()[-1000:])
        # Native DNF5 can exit 0 after nonfatal RPM scriptlet failures. Its
        # C-locale callback diagnostics must also be treated as a failure.
        if "replay" in command and re.search(r"(?:[Ee]rror in .* scriptlet:|[Uu]npack error:|[Cc]pio error:|scriptlet failed, exit status)", line):
            rpm_errors.append(line.strip()[-2000:])
    process.stdout.close()
    if process.wait() != 0:
        details = dict.fromkeys([*rpm_errors, *last_lines])
        raise UpdateError(f"{command[0]} failed with exit code {process.returncode}:\n" + "\n".join(details))
    if rpm_errors:
        raise UpdateError("RPM reported installation errors:\n" + "\n".join(rpm_errors))
    if progress:
        for record in progress.records:
            progress.package(record, "applied", 1)


def job_directory(state: dict, root: Path = STATE_DIR) -> Path:
    identifier = state.get("job", "")
    if not re.fullmatch("[a-f0-9]{32}", identifier):
        raise UpdateError("Invalid saved update identifier.")
    return root / "jobs" / identifier


def policy() -> dict:
    path = Path("/etc/nobara-updater/offline.json")
    settings = {"require_recovery": False, "require_offline_recovery": False, "prepare_attempts": 3, "live_updates": True}
    if path.exists():
        settings.update(json.loads(path.read_text()))
    if not isinstance(settings["require_recovery"], bool):
        raise UpdateError("require_recovery must be true or false.")
    if not isinstance(settings["require_offline_recovery"], bool):
        raise UpdateError("require_offline_recovery must be true or false.")
    if not isinstance(settings["live_updates"], bool):
        raise UpdateError("live_updates must be true or false.")
    attempts = settings["prepare_attempts"]
    if not isinstance(attempts, int) or not 1 <= attempts <= 5:
        raise UpdateError("prepare_attempts must be between 1 and 5.")
    return settings


def prune_payloads(root: Path, keep_job: str = "") -> None:
    """Discard obsolete download data; never remove recovery boot archives."""
    for job in (root / "jobs").glob("*"):
        if job.name == keep_job or not re.fullmatch(r"[a-f0-9]{32}", job.name) or job.is_symlink():
            continue
        for name in ("cache", "packages", "comps", "engine"):
            path = job / name
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)


def reconcile_pending(root: Path, state: dict) -> None:
    """Reconcile an unstarted plan under update_lock; never retry RPM writes."""
    if state.get("status") not in {"ready", "scheduled"} or state.get("started"):
        return
    owned_trigger = TRIGGER.is_symlink() and TRIGGER.resolve() == root.resolve()
    if state.get("fingerprint") != rpm_fingerprint():
        if owned_trigger:
            TRIGGER.unlink()
        message = "Installed packages changed since preparation. Preparing a fresh update is required."
        write_state(root, state, "failed", error=message)
        LOG.warning("%s", message)
    elif state["status"] == "scheduled" and not owned_trigger:
        write_state(root, state, "ready")
        LOG.warning("The prepared update was not installed: its boot trigger is missing or belongs to another updater. Run the updater again to schedule it.")


def refresh_pending(root: Path = STATE_DIR) -> None:
    with update_lock(root):
        reconcile_pending(root, read_state(root))


def preparation_state(root: Path, *, installer: bool = False) -> dict:
    """Check eligibility under update_lock before any package changes."""
    if installer and not in_installer_root():
        raise UpdateError("Installer updates require an actual chroot target.")
    previous = read_state(root)
    if previous.get("status") in ACTIVE - {"preparing"}:
        raise UpdateError(status_message(previous))
    completed = {"complete", "live-complete", "recovered"} | ({"installer-complete"} if installer else set())
    if previous.get("started") and previous.get("status") not in completed:
        if not previous.get("recovery", {}).get("created"):
            raise UpdateError("The previous installation did not complete and no automatic rollback is available. Run nobara-sync recovery-report, repair the reported problem, then run sudo nobara-sync retry-update to validate repairs and prepare a fresh update.")
        raise UpdateError("The previous installation did not complete. Recover it before preparing another update.")
    if any(path.is_symlink() or path.exists() for path in (TRIGGER, Path("/etc/system-update"))):
        raise UpdateError("Another offline update is scheduled. Finish or cancel it first.")
    return previous


def upgrade_early(root: Path = STATE_DIR, *, installer: bool = False) -> None:
    """Update the preparation engine and initramfs helper before the main plan.

    The caller must start a fresh interpreter afterwards, including when RPM
    updated Python or its dependencies. Never use this from a frozen replay.
    """
    from .update_plan import prepare_early_transaction
    with update_lock(root):
        previous = preparation_state(root, installer=installer)
        if not installer and not maintain_btrfs_root(root, previous):
            from .update_btrfs import JOURNAL
            if (root / JOURNAL).exists():
                raise UpdateError("Finish the interrupted Btrfs root-layout repair before upgrading packages.")
        prune_payloads(root)
        check_installed_system(preparing=True)
        before = rpm_fingerprint()
        state = dict(job=uuid.uuid4().hex, started=False, early_upgrade=True, installer=installer)
        job = job_directory(state, root)
        job.mkdir(parents=True, mode=0o700)
        write_state(root, state, "preparing")
        try:
            LOG.info("Checking early upgrades for %s before preparing the system update.", " and ".join(EARLY_PACKAGES))
            result = prepare_early_transaction(job, EARLY_PACKAGES)
            state["package_origins"] = result["package_origins"]
            if not result["empty"]:
                replay(job, test=True)
                if rpm_fingerprint() != before:
                    raise UpdateError("Installed packages changed during early upgrade preparation. Try again.")
                write_state(root, state, "installing-live", started=True)
                replay(job)
        except Exception as error:
            # Repository/solver failures with no RPM writes may be retried.
            # Partial installs (or an unreadable database) need explicit repair.
            changed = True
            try:
                changed = rpm_fingerprint() != before
            except Exception:
                pass
            write_state(root, state, "failed", started=state["started"] and changed,
                        error="Early updater/helper upgrade failed: " + str(error),
                        package_conflicts=getattr(error, "package_conflicts", []))
            raise UpdateError(state["error"]) from error
        # Preserve installer kernel-selection expectations and any confirmed
        # recovery cleanup state until the fresh worker makes the main plan.
        write_state(root, previous, previous["status"])


def prepare(root: Path = STATE_DIR, *, codecs: bool = False, installer: bool = False, packages: list[str] | None = None) -> None:
    from .update_plan import prepare_transaction
    packages = package_selection(packages)
    with update_lock(root):
        previous = preparation_state(root, installer=installer)
        if not installer:
            if not maintain_btrfs_root(root, previous):
                from .update_btrfs import JOURNAL
                if (root / JOURNAL).exists():
                    raise UpdateError("An interrupted Btrfs root-layout repair must finish before preparing another update. Review the preceding root-layout error and retry.")
            cleanup_kernel_retention(previous)
        settings = policy()
        prune_payloads(root)
        for attempt in range(1, settings["prepare_attempts"] + 1):
            state = {"job": uuid.uuid4().hex, "started": False, "attempt": attempt, "installer": installer, "selection": packages}
            if installer and previous.get("status") == "installer-complete" and previous.get("boot_selection"):
                # Calamares may run updates followed by a separate codec job.
                # Preserve the first job's kernel expectation across both.
                state["boot_selection"] = previous["boot_selection"]
            job = job_directory(state, root)
            job.mkdir(parents=True, mode=0o700)
            write_state(root, state, "preparing")
            try:
                check_installed_system(preparing=True)
                selection = {"packages": packages} if packages else {}
                result = prepare_transaction(job, codecs=codecs, **selection,
                                             allow_live=settings["live_updates"] and not settings["require_recovery"])
                state["package_origins"] = result.get("package_origins", [])
                shutil.rmtree(job / "cache", ignore_errors=True)
                if result["empty"]:
                    if installer and previous.get("status") == "installer-complete":
                        # Calamares can call cli then install-codecs. A no-op
                        # second command must retain first-boot confirmation
                        # of the transaction that actually changed the target.
                        write_state(root, previous, "installer-complete", codecs=result.get("codecs", False))
                    else:
                        write_state(root, state, "unchanged", **result)
                    return
                # Exercise the saved transaction with the exact native replay
                # path before promising installation on restart. RPM's test
                # flag checks it without running scriptlets or installing RPMs.
                replay(job, test=True)
                if result["fingerprint"] != rpm_fingerprint():
                    raise UpdateError("Installed packages changed during validation. Prepare the update again.")
                live = result.get("execution", {}).get("mode") == "live"
                if installer or live:
                    recovery = {"available": False, "reason": "Installer-owned target root." if installer else "Application update installed during this session."}
                else:
                    recovery = probe_recovery()
                if not installer and (settings["require_recovery"] or (not live and settings["require_offline_recovery"])) and not recovery["available"]:
                    raise UpdateError(recovery["reason"])
                if not installer and not live and not recovery["available"]:
                    LOG.warning("Automatic rollback is unavailable: %s", recovery["reason"])
                elif not installer and not live:
                    check_recovery_space(recovery, result.get("boot_space", {}))
                    LOG.info("Automatic rollback is available for this system layout.")
                    if recovery.get("shared_mounts"):
                        LOG.info("Recovery will preserve current container data at: %s",
                                 ", ".join(mount["target"] for mount in recovery["shared_mounts"]))
                engine = job / "engine" / "nobara_updater"
                engine.mkdir(parents=True)
                for filename in ENGINE_FILES:
                    shutil.copy2(Path(__file__).with_name(filename), engine / filename)
                (engine / "__init__.py").touch()
                shutil.copy2(Path("/usr/libexec/nobara-update-worker"), job / "engine" / "worker.py")
                for path in (job / "engine").rglob("*"):
                    if path.is_file():
                        result["files"][str(path.relative_to(job))] = file_digest(path)
                # Payload and metadata must survive power loss before READY.
                for relative in result["files"]:
                    with (job / relative).open("rb") as stream:
                        os.fsync(stream.fileno())
                atomic_json(job / "plan.json", dict(result, recovery=recovery))
                result["files"]["plan.json"] = file_digest(job / "plan.json")
                os.sync()
                write_state(root, state, "ready", recovery=recovery, **result)
                LOG.info("%s", status_message(state))
                return
            except Exception as error:
                error = annotate_failure(error, state.get("package_origins", []))
                write_state(root, state, "failed", error=str(error),
                            package_conflicts=getattr(error, "package_conflicts", []))
                # Retry only preparation. Never rerun an RPM transaction on an
                # exception. Each retry gets fresh metadata and a separate job.
                if isinstance(error, UpdateError):
                    LOG.error("Preparation attempt %s failed: %s", attempt, error)
                else:
                    LOG.exception("Preparation attempt %s failed", attempt)
                if isinstance(error, UpdateError) or attempt == settings["prepare_attempts"]:
                    raise error
                # No package installation has occurred. Discard this incomplete
                # download so retries cannot exhaust disk with duplicate RPMs.
                shutil.rmtree(job)
                time.sleep(2)


def schedule(root: Path = STATE_DIR, *, reboot: bool = False) -> None:
    with update_lock(root):
        state = read_state(root)
        if state.get("installer"):
            raise UpdateError("An installer transaction cannot be scheduled as a desktop offline update.")
        if state.get("started") or state.get("status") not in {"ready", "scheduled"}:
            raise UpdateError("There is no prepared update ready to install.")
        reconcile_pending(root, state)
        if state["status"] == "failed":
            raise UpdateError(state["error"])
        job = job_directory(state, root)
        try:
            verify_files(job, state["files"])
        except UpdateError as error:
            if TRIGGER.is_symlink() and TRIGGER.resolve() == root.resolve():
                TRIGGER.unlink()
            write_state(root, state, "failed", error=str(error))
            raise
        alternate = Path("/etc/system-update")
        if alternate.exists() or alternate.is_symlink():
            raise UpdateError("Another offline updater has an /etc/system-update trigger. Finish or cancel that update first.")
        check_offline_service()
        if TRIGGER.is_symlink():
            if TRIGGER.resolve() != root.resolve():
                raise UpdateError("Another offline updater has already scheduled a transaction.")
        elif TRIGGER.exists():
            raise UpdateError("The system-update trigger is already in use.")
        else:
            TRIGGER.symlink_to(root)
        # If power is lost between symlink creation and this write, execute
        # also accepts READY, but only when our exact trigger is present.
        write_state(root, state, "scheduled")
        os.sync()
    if reboot:
        run(["systemctl", "reboot"])


def check_offline_service() -> None:
    """Refuse a restart promise if the boot target cannot run our service."""
    for unit, property_name, expected in (
        ("nobara-updater-offline.service", "LoadState", "loaded"),
        ("system-update.target", "Wants", "nobara-updater-offline.service"),
    ):
        result = subprocess.run(["systemctl", "show", "--value", "--property=" + property_name, unit],
                                capture_output=True, text=True, check=True, timeout=30)
        if expected not in result.stdout.split():
            raise UpdateError("The Nobara offline update service is missing, masked, or not connected to system-update.target. Reinstall nobara-updater before scheduling an update.")


def cancel(root: Path = STATE_DIR) -> None:
    with update_lock(root):
        state = read_state(root)
        if state.get("started") or state.get("status") not in {"ready", "scheduled", "failed", "unchanged"}:
            raise UpdateError("Only an update that has not started installing can be cancelled.")
        if TRIGGER.is_symlink() and TRIGGER.resolve() == root.resolve():
            TRIGGER.unlink()
        write_state(root, state, "idle")
        prune_payloads(root)


def check_installed_packages() -> None:
    # An installed Obsoletes relationship is not a broken dependency. In
    # particular, obsolete packages can survive a previous transition.
    # Let the transaction planner handle those; do not abort preparation or
    # roll back an otherwise healthy boot solely for an obsoleted leftover.
    run(["dnf5", "--disable-repo=*", "check", "--dependencies", "--duplicates"])


def check_installed_system(*, preparing: bool = False) -> None:
    LOG.info("Checking the installed RPM database before preparing updates.")
    run(["rpm", "--verifydb"])
    if not preparing:
        check_installed_packages()
    # During preparation the planner repairs missing dependencies in the
    # saved transaction and validates the complete final RPM set. Requiring
    # healthy dependencies here would prevent that repair from being planned.


def retry_failed(root: Path = STATE_DIR) -> None:
    """Explicit repair acknowledgement; never replay a partially applied plan."""
    with update_lock(root):
        state = read_state(root)
        if state.get("status") not in {"failed", "interrupted"} or not state.get("started"):
            raise UpdateError("There is no failed installation awaiting repair. Run nobara-sync cli normally.")
        if state.get("recovery", {}).get("created"):
            raise UpdateError("Boot the previous-system recovery first, repair it, then run nobara-sync cli.")
        if TRIGGER.exists() or TRIGGER.is_symlink():
            raise UpdateError("An offline update trigger is still present. Do not retry while an update is scheduled.")
        check_installed_system()
        # Retain the original state and all diagnostic files for support.
        atomic_json(job_directory(state, root) / "failed-state.json", state)
        write_state(root, {"previous_failed_job": state["job"], "repair_acknowledged": True,
                           "started": False}, "idle")
        LOG.info("RPM database and dependency checks passed. Preparing a fresh update is now allowed; the previous failure report is retained.")


def replay(job: Path, *, test: bool = False) -> None:
    # Every payload is local. Keep native DNF5 in its own process so a Python
    # or libdnf5 upgrade cannot replace modules used by an in-flight binding.
    # Disabling a configured repo leaves its object in DNF's sack. In 5.4.3
    # replay reuses it by repo_id, so bundled RPMs are treated as remote and
    # cacheonly fails. Load no repo definitions or plugins: replay then creates
    # COMMANDLINE repos for the saved IDs and reads the saved RPM paths.
    run(["dnf5", "--config=/dev/null", "--setopt=reposdir=", "--no-plugins",
         "--cacheonly", "--setopt=localpkg_gpgcheck=1",
         "--setopt=installonly_limit=0", "--setopt=clean_requirements_on_remove=0",
         "--setopt=tsflags=" + ("test" if test else ""),
         "-y", "replay", str(job)])


def install_in_target(root: Path = STATE_DIR, *, codecs: bool = False) -> None:
    if not in_installer_root():
        raise UpdateError("Installer updates require an actual chroot target.")
    prepare(root, codecs=codecs, installer=True)
    with update_lock(root):
        state = read_state(root)
        if state.get("status") in {"unchanged", "installer-complete"}:
            return
        if state.get("status") != "ready" or not state.get("installer"):
            raise UpdateError("The installer transaction is not ready.")
        if TRIGGER.exists() or TRIGGER.is_symlink():
            raise UpdateError("An offline update is already scheduled in this target.")
        job = job_directory(state, root)
        verify_files(job, state["files"])
        if state["fingerprint"] != rpm_fingerprint():
            raise UpdateError("Installed target packages changed during preparation. Prepare the update again.")
        write_state(root, state, "installing", started=True)
        replay(job)
        write_state(root, state, "validating")
    # No systemd service, snapshot of the live media, /system-update trigger,
    # or reboot. Calamares waits for this fresh interpreter to finish.
    os.execv("/usr/bin/python3", ["/usr/bin/python3", "-I", str(job / "engine/worker.py"), "installer-finalize"])


def execute(root: Path = STATE_DIR) -> None:
    with update_lock(root):
        if not TRIGGER.is_symlink() or TRIGGER.resolve() != root.resolve():
            return
        state = read_state(root)
        if state.get("installer"):
            raise UpdateError("An installer transaction cannot be replayed by the boot service.")
        if state.get("status") not in {"ready", "scheduled"}:
            raise UpdateError("Unexpected offline update state; refusing to replay an interrupted installation.")
        job = job_directory(state, root)
        verify_files(job, state["files"])
        if state["fingerprint"] != rpm_fingerprint():
            TRIGGER.unlink()
            write_state(root, state, "failed", error="Installed packages changed after preparation. Prepare the update again.")
            raise UpdateError(state["error"])
        Path("/run/nobara-updater-offline").touch(mode=0o600)
        TRIGGER.unlink()
        SPLASH.start(release_upgrade=state.get("source_release") != state.get("target_release"))
        announce("Preparing recovery files. Keep the computer powered on.")
        recovery = create_recovery(job, state["recovery"], boot_space=state.get("boot_space", {}))
        write_state(root, state, "installing", started=True, recovery=recovery)
        arm_report(state)
        trial_boot(recovery)
        announce("Installing the Nobara system update. Keep the computer powered on; this can take several minutes.", percent=5)
        # All RPMs are local, signatures remain required, and replay checks the
        # exact saved operations. No --ignore-installed/--skip-broken escape.
        replay(job)
        write_state(root, state, "validating")
    # A major update can replace Python and its libraries. Start the new
    # interpreter, using the frozen job engine, for all post-install work.
    os.execv("/usr/bin/python3", ["/usr/bin/python3", "-I", str(job / "engine/worker.py"), "finalize"])


def execute_live(root: Path = STATE_DIR) -> None:
    """Apply a prepared application-only transaction under service ownership."""
    if in_installer_root():
        raise UpdateError("Use the installer update path inside a target root.")
    with update_lock(root):
        state = read_state(root)
        if (state.get("status") != "ready" or state.get("started") or state.get("installer")
                or state.get("execution", {}).get("mode") != "live"
                or state.get("source_release") != state.get("target_release")
                or state.get("migrations", {}).get("hooks")):
            raise UpdateError("This prepared transaction requires offline installation.")
        if TRIGGER.exists() or TRIGGER.is_symlink():
            raise UpdateError("An offline update is already scheduled. Finish or cancel it first.")
        try:
            job = job_directory(state, root)
            verify_files(job, state["files"])
            if state["fingerprint"] != rpm_fingerprint():
                raise UpdateError("Installed packages changed after preparation. Prepare the update again.")
            write_state(root, state, "installing-live", started=True)
            LOG.info("Installing the verified application updates now.")
            replay(job)
            write_state(root, state, "validating-live")
        except Exception as error:
            write_state(root, state, "failed", error=str(error))
            raise
    try:
        os.execv("/usr/bin/python3", ["/usr/bin/python3", "-I", str(job / "engine/worker.py"), "live-finalize"])
    except Exception as error:
        with update_lock(root):
            write_state(root, state, "failed", error=str(error))
        raise


def apply_hooks(hooks: list[str]) -> None:
    for hook in hooks:
        if hook == "plasma-login":
            for source, target in ((Path("/etc/sddm.conf"), Path("/etc/plasmalogin.conf")),
                                   (Path("/etc/sddm.conf.d"), Path("/etc/plasmalogin.conf.d"))):
                if source.is_dir():
                    target.mkdir(exist_ok=True)
                    for file in source.iterdir():
                        if file.is_file() and not (target / file.name).exists():
                            shutil.copy2(file, target / file.name)
                elif source.is_file() and not target.exists():
                    shutil.copy2(source, target)
            run(["systemctl", "--root=/", "enable", "--force", "plasmalogin.service"])
        elif hook == "enable-falcond":
            run(["systemctl", "--root=/", "enable", "falcond.service"])
        elif hook == "enable-codecs":
            run(["dnf5", "config-manager", "setopt", "nobara-pikaos-additional.enabled=1"])
        elif hook == "migrate-media-repository":
            from .update_repositories import persist_media_migration
            persist_media_migration(run)
        elif hook in {"nvidia", "nvidia-closed"}:
            text = "options nvidia-drm modeset=1 fbdev=1\n"
            if hook == "nvidia-closed":
                config = Path("/etc/nvidia/kernel.conf")
                if not config.is_file():
                    raise UpdateError("The NVIDIA module configuration is missing.")
                config.write_text(config.read_text().replace("MODULE_VARIANT=kernel-open", "MODULE_VARIANT=kernel"))
                text += "options nvidia NVreg_EnableGpuFirmware=0\n"
            Path("/etc/modprobe.d/nvidia-modeset.conf").write_text(text)
        elif hook == "plymouth-rebuild":
            # finalize() rebuilds boot images for every plymouth-* hook. Leave
            # the selected theme alone when only repairing fallback files.
            pass
        elif hook in {"plymouth-steamos", "plymouth-bgrt"}:
            run(["plymouth-set-default-theme", hook.removeprefix("plymouth-")])
            # Preserve the administrator's GRUB menu policy. Hiding recovery
            # entries automatically would undermine the recovery mechanism.
        elif hook != "rocm-transition":
            raise UpdateError(f"Unknown migration hook: {hook}")


def dkms_status_records(text: str) -> list[dict]:
    """Read DKMS package identities and states, excluding unrelated warnings."""
    pattern = re.compile(r"(?P<name>[^/,\s:]+)/(?P<version>[^,\s]+?)"
                         r"(?:,\s*(?P<kernel>[^,\s]+),\s*(?P<arch>[^,:\s]+))?"
                         r":\s*(?P<status>\S.*)")
    return [match.groupdict() for line in text.splitlines()
            if (match := pattern.fullmatch(line.strip()))]


def validate_boot(packages: list[dict], *, rebuild_all: bool = False, preserved_kernels=()) -> None:
    kernels = set()
    incoming_boot_kernels = set()
    for item in packages:
        if item["action"] in {"Install", "Upgrade", "Reinstall", "Downgrade"} and item["name"] in {"kernel", "kernel-core", "kernel-modules", "kernel-modules-core"}:
            # Ask RPM for the installed package version rather than parsing a
            # possibly epoch-qualified NEVRA with ambiguous hyphens.
            query = subprocess.run(["rpm", "-q", "--qf", "%{VERSION}-%{RELEASE}.%{ARCH}\n", item["nevra"]],
                                   capture_output=True, text=True, check=True)
            kernels.update(query.stdout.splitlines())
            if item["name"] in {"kernel", "kernel-core"}:
                incoming_boot_kernels.update(query.stdout.splitlines())
    preserved = set(preserved_kernels)
    required = kernels | (set() if incoming_boot_kernels else {os.uname().release})
    if preserved & required:
        raise UpdateError("Cannot skip module and boot validation for the selected or updated kernel: "
                          + ", ".join(sorted(preserved & required)) + ". Prepare the update again from the kernel you intend to use.")
    modules_changed = any(any(part in item["name"] for part in ("dkms", "akmod", "kmod", "dracut")) for item in packages)
    if modules_changed or rebuild_all:
        # A running kernel can differ from the selected/default kernel.
        # Rebuild installed bootable kernels, except legacy fallbacks whose
        # unavailable development packages were identified during preparation.
        kernels.update(installed_boot_kernels())
    for kernel in sorted(kernels & preserved):
        LOG.warning("Leaving existing modules and boot files unchanged for retained kernel %s, as planned: matching development files are unavailable.", kernel)
    kernels -= preserved
    for kernel in sorted(kernels):
        if not re.fullmatch(r"[A-Za-z0-9_.+~-]+", kernel):
            raise UpdateError("Invalid target kernel version.")
        if shutil.which("dkms"):
            environment = dict(os.environ, LC_ALL="C.UTF-8")
            architecture = os.uname().machine
            before = subprocess.run(["dkms", "status"], capture_output=True, text=True,
                                    check=True, env=environment).stdout
            expected = {record["name"] for record in dkms_status_records(before)}
            if expected and not (Path("/usr/lib/modules") / kernel / "build/Makefile").is_file():
                raise UpdateError(f"Kernel development files are missing for {kernel}.")
            run(["dkms", "autoinstall", "-k", kernel])
            after = subprocess.run(["dkms", "status", "-k", kernel, "-a", architecture],
                                   capture_output=True, text=True, check=True, env=environment).stdout
            detail = after.strip() or "No DKMS status entries were returned."
            LOG.info("DKMS status for %s (%s):\n%s", kernel, architecture, detail)
            # DKMS saves the original in-tree module when a driver replaces it.
            # That annotation is informational; missing/differing module files
            # and unknown annotations must still block boot validation. DKMS
            # itself resolves BUILT_MODULE_NAME/DEST_MODULE_NAME, so compare its
            # package identity rather than guessing a .ko name from the package.
            installed = {record["name"] for record in dkms_status_records(after)
                         if record["kernel"] == kernel and record["arch"] == architecture
                         and record["status"] in {"installed", "installed (Original modules exist)"}}
            if expected - installed:
                raise UpdateError(f"DKMS did not confirm a clean installed state for kernel {kernel}: "
                                  f"{', '.join(sorted(expected - installed))}\nReported DKMS status:\n{detail}")
        if shutil.which("akmods"):
            run(["akmods", "--force", "--kernels", kernel])
        if subprocess.run(["rpm", "-q", "dkms-nvidia"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            run(["modinfo", "-k", kernel, "nvidia"])
        image = Path("/boot") / ("vmlinuz-" + kernel)
        if image.is_file():
            temporary = Path("/boot") / (".nobara-initramfs-" + kernel + ".img")
            run(["dracut", "--force", "--kver", kernel, str(temporary)])
            run(["lsinitrd", str(temporary)])
            os.replace(temporary, Path("/boot") / ("initramfs-" + kernel + ".img"))
            entries = list(Path("/boot/loader/entries").glob("*.conf"))
            if not any(re.search(r"(?m)^version\s+" + re.escape(kernel) + r"\s*$", p.read_text()) for p in entries):
                raise UpdateError(f"No BLS boot entry exists for kernel {kernel}.")
        else:
            image = Path("/usr/lib/modules") / kernel / "vmlinuz"
            if not image.is_file():
                raise UpdateError(f"The boot image for kernel {kernel} is missing.")
            run(["kernel-install", "add", kernel, str(image)])
            ukis = [p for directory in (Path("/boot/EFI/Linux"), Path("/boot/efi/EFI/Linux")) for p in directory.glob(f"*{kernel}*.efi")]
            if not ukis:
                raise UpdateError(f"No unified kernel image was generated for {kernel}.")
            for uki in ukis:
                result = subprocess.run(["bootctl", "kernel-identify", str(uki)], check=True, capture_output=True, text=True)
                if result.stdout.strip() != "uki":
                    raise UpdateError(f"Invalid unified kernel image: {uki}")
    os.sync()


def finalize(root: Path = STATE_DIR, *, installer: bool = False, live: bool = False) -> None:
    if installer and not in_installer_root():
        raise UpdateError("Installer validation requires an actual chroot target.")
    with update_lock(root):
        state = read_state(root)
        if state.get("status") != ("validating-live" if live else "validating"):
            raise UpdateError("The update is not ready for post-install validation.")
        if bool(state.get("installer")) != installer:
            raise UpdateError("The saved transaction belongs to a different update mode.")
        if live and (installer or state.get("execution", {}).get("mode") != "live"
                     or state["source_release"] != state["target_release"] or state["migrations"]["hooks"]):
            raise UpdateError("The saved transaction is not eligible for live installation.")
        try:
            job = job_directory(state, root)
            verify_files(job, state["files"])
            if live:
                LOG.info("Checking the installed application updates.")
            elif installer:
                LOG.info("Checking the updated system and building boot files. Keep the computer powered on.")
            else:
                # execute() execs a fresh interpreter after the transaction.
                # Restore the display state without resetting it to zero.
                SPLASH.start(release_upgrade=state["source_release"] != state["target_release"], percent=90)
                announce("Checking the updated system and building boot files. Keep the computer powered on.")
            apply_hooks(state["migrations"]["hooks"])
            normal_entry = None
            if not live:
                if not installer:
                    # Feed the active root to dracut/kernel-install, including
                    # when this transaction runs from a prior recovery clone.
                    normal_entry = synchronize_boot_root()
                validate_boot(state["packages"], rebuild_all=any(hook.startswith("plymouth-") for hook in state["migrations"]["hooks"]),
                              preserved_kernels=state.get("preserved_kernels", []))
            # A successful DNF exit must actually have installed every planned
            # inbound RPM (including ordinary updates within the same release).
            for item in state["packages"]:
                if item["action"] in {"Install", "Upgrade", "Downgrade", "Reinstall"}:
                    run(["rpm", "-q", item["nevra"]])
            check_installed_packages()
            if os_release() != state["target_release"]:
                raise UpdateError("Installed release does not match the prepared release.")
            if not live:
                kernel = newest_updated_kernel(state["packages"])
                if kernel or normal_entry or state.get("preserved_kernels"):
                    kernel = kernel or os.uname().release
                    # A driver-only update can leave an obsolete fallback
                    # unchanged. Select the kernel we validated, even when
                    # GRUB was previously pinned to that legacy fallback.
                    # Pin only after all installation/driver/boot checks pass.
                    # Save the exact expectation for confirmation next boot.
                    # Keep recovery armed throughout validation. Package hooks
                    # may alter saved_entry, but our independent GRUB fallback
                    # remains active until the updated boot is confirmed.
                    selection = ({"kernel": kernel, "entry": kernel_entry(kernel), "loader": "grub"}
                                 if state.get("recovery", {}).get("created") else pin_kernel(kernel))
                    write_state(root, state, "validating", boot_selection=selection)
                    if normal_entry:
                        state["normal_boot_entry"] = selection["entry"]
            status = "live-complete" if live else ("installer-complete" if installer else "awaiting-boot")
            write_state(root, state, status, installed_fingerprint=rpm_fingerprint())
            if not live and not installer and state.get("recovery", {}).get("created"):
                entry = state.get("boot_selection", {}).get("entry") or Path(state["recovery"]["entry"]).stem
                write_state(root, state, status, trial_entry=entry)
                trial_boot(state["recovery"], entry)
            if live:
                LOG.info("Application installation and validation finished.")
            elif installer:
                LOG.info("Target installation validated. Returning to the installer.")
            else:
                announce("Installation and boot-file validation finished. Restarting…", percent=100)
        except Exception as error:
            if live:
                write_state(root, state, "failed", error=str(error))
            raise


def recover(root: Path = STATE_DIR) -> None:
    with update_lock(root):
        state = read_state(root)
        if state.get("status") in {"complete", "recovered", "live-complete"}:
            # A service timeout during optional cleanup must not roll back a
            # confirmed system, especially after its snapshot was retired.
            LOG.warning("Ignoring recovery request after successful confirmation; inspect the cleanup log instead.")
            return
        if not state.get("started"):
            if TRIGGER.is_symlink() and TRIGGER.resolve() == root.resolve():
                TRIGGER.unlink()
            # A preparation/check failure leaves the original system usable.
            return
        recovery = state.get("recovery", {})
        if recovery.get("created"):
            SPLASH.recovery()
            announce("The update failed. Restarting into the previous system.")
            write_state(root, state, "recovering")
            select_recovery(recovery)
        else:
            write_state(root, state, "interrupted", error=state.get("error", "Offline installation failed."))
            raise UpdateError("Automatic rollback is unavailable. Boot recovery media and inspect /var/lib/nobara-updater/state.json.")


def maintain_btrfs_root(root: Path, state: dict) -> bool:
    """Layout maintenance is optional; failure must not undo a confirmed boot."""
    from .update_btrfs import restore_root_layout
    try:
        restored = restore_root_layout(root, state)
        if restored:
            # A legacy completed job may already have retired all its rollback
            # snapshots. The displaced original root now needs cleanup too.
            recovery = dict(state.get("recovery", {}), created=True, uuid=restored["uuid"])
            write_state(root, state, state["status"], btrfs_root_layout=restored,
                        recovery=recovery, recovery_cleanup_complete=False)
        return True
    except Exception:
        LOG.exception("The system is usable, but its original Btrfs root layout could not be restored; maintenance will retry on the next boot or updater run.")
        return False


def cleanup_confirmed_update(root: Path, state: dict) -> None:
    """Cleanup failures never undo confirmation; retry them on a later boot."""
    if not maintain_btrfs_root(root, state):
        return
    if state.get("recovery_cleanup_complete"):
        cleanup_kernel_retention(state)
        return
    try:
        if state["status"] == "recovered":
            if not state.get("recovery_report"):
                # Do not discard the only failed-boot diagnostics if importing
                # them into the recovered root previously failed.
                report = publish_recovery(state["job"])
                write_state(root, state, "recovered", recovery_report=state["job"], failure_detail=report["error"])
        else:
            resolve_notice()
        prune_recovery(state.get("recovery", {}), state["job"], state_root=root)
        prune_payloads(root)
        write_state(root, state, state["status"], recovery_cleanup_complete=True)
        cleanup_kernel_retention(state)
    except Exception:
        LOG.exception("System boot confirmed, but recovery cleanup could not finish; it will retry on the next boot.")


def prune_kernel_packages(*, obsolete_kernels=()) -> None:
    # execute() must not load libdnf5 into its parent interpreter before the
    # native replay process potentially upgrades that library and Python.
    from .update_retention import prune_kernel_packages as prune
    prune(obsolete_kernels=obsolete_kernels)


def cleanup_kernel_retention(state: dict) -> None:
    """Keep installation/recovery protected; cleanup never invalidates a boot."""
    if state.get("status") not in {"complete", "unchanged", "live-complete"}:
        return
    if state.get("recovery", {}).get("created") and not state.get("recovery_cleanup_complete"):
        return
    try:
        if (TRIGGER.is_symlink() or TRIGGER.exists()
                or Path("/etc/nobara-updater-recovery.json").exists()):
            return
        environment = grub_environment()
        if (any(environment.get(key) for key in ("nobara_fallback", "nobara_trial", "next_entry"))
                or environment.get("saved_entry", "").startswith("nobara-recovery-")):
            return
        prune_kernel_packages(obsolete_kernels=state.get("preserved_kernels", []))
    except Exception as error:
        LOG.warning("Old-kernel cleanup deferred; it will retry on a later boot or updater run: %s", error)


def confirm_boot(root: Path = STATE_DIR) -> None:
    with update_lock(root):
        state = read_state(root)
        from .update_btrfs import JOURNAL
        if ((root / JOURNAL).exists() and state.get("status") not in ACTIVE
                and (not state.get("started") or state.get("status") in {"complete", "recovered", "live-complete"})):
            if not maintain_btrfs_root(root, state):
                return
        marker = Path("/etc/nobara-updater-recovery.json")
        if marker.exists():
            recovery = json.loads(marker.read_text())
            # Only mark the snapshot's original job as recovered. Subsequent
            # updates from the recovered root have their own independent jobs.
            if state.get("job") == recovery["job"]:
                entry = (recovery["restored_entry"] if recovery.get("backend") == "lvm"
                         else "nobara-recovery-" + recovery["job"])
                if recovery.get("backend") != "lvm":
                    entry = synchronize_boot_root(recovery_entry=entry) or entry
                confirm_trial(entry)
                try:
                    report = publish_recovery(recovery["job"])
                    state["failure_detail"] = report["error"]
                    state["recovery_report"] = recovery["job"]
                except Exception:
                    # Lack of report storage must not undo a successful recovery.
                    LOG.exception("System restored, but the recovery report could not be published.")
                write_state(root, state, "recovered", started=False,
                            recovery_boot=recovery, normal_boot_entry=entry, error="The previous system was restored.",
                            # The snapshot captured state before create_recovery
                            # returned and recorded created=True in the origin.
                            recovery=dict(state.get("recovery", {}), created=True,
                                          entry_id="nobara-recovery-" + recovery["job"]))
                marker.unlink()
                cleanup_confirmed_update(root, state)
                return
        if state.get("status") in {"complete", "recovered"}:
            # Upgrade the older behavior which left a confirmed Btrfs root
            # behind a "previous system" entry. Never override another job's
            # armed rollback/trial while repairing an already completed job.
            if not grub_environment().get("nobara_fallback"):
                if not state.get("normal_boot_entry"):
                    entry = synchronize_boot_root()
                    if entry:
                        confirm_trial(entry)
                        write_state(root, state, state["status"], normal_boot_entry=entry, trial_entry=entry)
                cleanup_confirmed_update(root, state)
            return
        if state.get("status") in {"unchanged", "live-complete"}:
            if maintain_btrfs_root(root, state):
                if state.get("btrfs_root_layout") and not state.get("recovery_cleanup_complete"):
                    cleanup_confirmed_update(root, state)
                else:
                    cleanup_kernel_retention(state)
            return
        if state.get("status") in {"ready", "scheduled"} and not state.get("started"):
            reconcile_pending(root, state)
            return
        if state.get("status") in {"installing-live", "validating-live"}:
            phase = "Early updater/helper upgrade" if state.get("early_upgrade") else "Application installation"
            write_state(root, state, "interrupted", error=phase + " was interrupted. Inspect the update log before retrying.")
            # Application-only failure must not start the boot-recovery
            # service or force the otherwise usable desktop into emergency.
            LOG.error("%s", state["error"])
            publish_failure(state)
            return
        if state.get("status") in {"installing", "validating", "recovering"}:
            write_state(root, state, "interrupted", error="The previous update did not finish.")
            if state.get("recovery", {}).get("created"):
                raise UpdateError(state["error"])
        if state.get("status") in {"failed", "interrupted"} and not state.get("recovery", {}).get("created"):
            # With no rollback target, allow normal startup to continue and
            # surface the failure at the desktop. Keep started=True so a
            # partial transaction is not silently retried or called complete.
            publish_failure(state)
            LOG.error("%s", state.get("error", "The update did not complete."))
            return
        if state.get("status") not in {"awaiting-boot", "installer-complete"}:
            return
        if os_release() != state["target_release"]:
            raise UpdateError("The system booted into a different release than expected.")
        expected_kernel = state.get("boot_selection", {}).get("kernel")
        if expected_kernel and os.uname().release != expected_kernel:
            raise UpdateError(f"The system booted kernel {os.uname().release}, but this update selected {expected_kernel}.")
        # finalize() already checked every installed dependency and duplicate
        # before saving this inventory. Repeating DNF's per-package RPM checks
        # on a cold boot can take minutes on an HDD and used to exceed the
        # confirmation service's deadline. Reuse that result only if RPM can
        # still read the same complete inventory (including install times).
        LOG.info("Checking whether the installed package inventory changed since validation.")
        validated = state.get("installed_fingerprint")
        if validated and rpm_fingerprint() == validated:
            LOG.info("Installed packages match the inventory validated before restart.")
        else:
            LOG.info("Package inventory changed or no saved validation is available; checking dependencies again.")
            check_installed_packages()
        if not (Path("/usr/lib/modules") / os.uname().release).is_dir():
            raise UpdateError("Modules for the running kernel are missing.")
        # A desktop install must at least reach its login manager before its
        # recovery protection is retired. This does not certify every GPU or
        # application workload; those still need normal release testing.
        if Path("/etc/systemd/system/display-manager.service").exists():
            run(["systemctl", "is-active", "--quiet", "display-manager.service"])
        if state.get("trial_entry"):
            confirm_trial(state["trial_entry"])
        write_state(root, state, "complete")
        Path("/etc/nobara/newinstall").unlink(missing_ok=True)
        LOG.info("%s", status_message(state))
        cleanup_confirmed_update(root, state)
