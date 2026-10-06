"""Bounded diagnostics that survive rollback; publishing never uploads them."""
from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import time

from .update_state import STATE_DIR, UpdateError, atomic_json
from .update_origins import annotate_failure

BOOT_REPORTS = Path("/boot/nobara-updater")
REPORTS = Path("/var/lib/nobara-updater-reports")
DKMS_LOG_ROOT = Path("/var/lib/dkms")
LIMIT = 1024 * 1024
LOG = logging.getLogger(__name__)
PACKAGE_GUIDANCE = """How packages are handled
The updater uses DNF distro-sync, which can upgrade or downgrade packages
according to repository priorities, available versions, and dependencies.

Locally installed RPMs follow normal repository updates, including RPMs
installed from local files or URLs and packages with no recorded repository
origin. Distro-sync can upgrade, downgrade, or replace them through RPM
Obsoletes. A custom build with the same version as a repository package is
not automatically reinstalled. A local-only package is not removed merely
because no repository provides it. Repository priorities, explicit exclusions
and versionlocks, codec protections, and dependency/removal checks still apply.
Source installations outside the RPM database are not managed or automatically
updated by DNF.

Third-party repository packages remain eligible for replacement by a Nobara
build. Repository priorities and dependencies still apply; a newer version
alone does not override repository priority. Conflicts can occur regardless
of which repository has higher priority.

When failure evidence identifies an affected local or third-party package,
this report names it and its recorded origin. Where an enabled Nobara
repository supplies a counterpart, the report also names that repository.
An unknown origin is reported as unverified. An unrelated local or third-party
package is not blamed merely because it is installed.
"""
SHARING_GUIDANCE = """Sharing logs
Save the .tar.gz report even when offline. Developers can extract README.txt
and update.log with an archive manager. When online, Upload logs sends the
text report to paste.nobaraproject.org using pbcli and gives you a link to
share. Nothing is uploaded automatically. Review the log before sharing;
it can contain package names, repository URLs and local file paths.
"""


def valid_job(job: str) -> str:
    if not isinstance(job, str) or not re.fullmatch(r"[a-f0-9]{32}", job):
        raise UpdateError("Invalid recovery report identifier.")
    return job


def report_directory(state: dict) -> Path:
    job = valid_job(state["job"])
    return (BOOT_REPORTS / job if state.get("recovery", {}).get("created")
            else STATE_DIR / "jobs" / job / "diagnostics")


def instructions(recovered: bool, started: bool) -> str:
    if recovered:
        text = """The system update failed and the previous system was restored.
You can continue using this recovered system. The update did not complete.

What to do next
1. Read the error and update log below. System Update Recovery in the
   application menu reopens this report. You can also use:
   nobara-sync recovery-report
2. Fix the reported problem in this recovered system. It is now your active
   system, and your repairs remain part of it.
3. Open DNF App Center and run the updater again to prepare a fresh
   transaction. You can also run: sudo nobara-sync cli
4. Restart if the updater asks you to. The system automatically selects
   offline update mode and attempts installation.
5. If installation succeeds, it restarts into the updated system using the
   selected kernel. If installation or startup validation fails again and
   automatic rollback is available, it returns to the previous system.
6. If the same error persists, save this report or upload the logs and share
   them with Nobara support. Resolve the cause before repeating the update.

You normally do not need to select a boot entry manually. The normal entry
uses your restored system after recovery. Once boot is confirmed, unused
updater rollback snapshots and their recovery entries are removed; your
active system remains in place.
"""
    elif started:
        text = """The system update failed. No automatic rollback was performed.
Some packages may have changed; reaching the desktop does not confirm that the update completed.

What to do next
1. Read the error and update log below before making repairs.
2. Run sudo dnf5 check --dependencies --duplicates to check installed
   dependencies, conflicts, and duplicate versions without changing packages.
3. Repair the reported problem; save this report and ask Nobara support if
   you need help. After repairs, run: sudo nobara-sync retry-update
   This checks the RPM database and dependencies, preserves the failure
   report, and prepares a fresh transaction. It does not replay the old one.
4. Restart only if the updater asks you to. Rebooting alone does not repair
   or retry the failed installation. Do not delete state.json or manually
   create /system-update to bypass the failure.
"""
    else:
        text = """The system update stopped before package installation began. No rollback was needed.

What to do next
1. Read the error and update log below. Preparation can detect conflicts
   before packages change, so entering recovery is not necessary.
2. Fix the reported problem, then open DNF App Center and run the updater
   again. You can also run: sudo nobara-sync cli
3. Restart only if the updater asks you to after preparing the new update.
   If the same failure persists, save this report or upload the logs and
   share them with Nobara support.
"""
    return text + "\n" + PACKAGE_GUIDANCE + "\n" + SHARING_GUIDANCE


def tail(path: Path) -> str:
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - LIMIT))
            return stream.read(LIMIT).decode("utf-8", errors="replace")
    except FileNotFoundError:
        return ""


def redact(text: str) -> str:
    # Keep useful diagnostics without publishing URL credentials or common
    # query-string secrets. This is not a guarantee of anonymity.
    text = re.sub(r"(https?://)[^/\s@]+@", r"\1[redacted]@", text)
    return re.sub(r"(?i)([?&](?:token|password|secret|key|api_key|access_token)=)[^&\s]+", r"\1[redacted]", text)


def arm_report(state: dict) -> None:
    """Called before RPM writes; use separate /boot when root can roll back."""
    directory = report_directory(state)
    atomic_json(directory / "failure.json", dict(
        job=state["job"], time=time.time(), phase="installation or startup",
        error="The update did not reach successful startup confirmation. It may have been interrupted.",
        source_release=state.get("source_release"), target_release=state.get("target_release"),
        package_origins=state.get("package_origins", [])))
    attach_log(state)


def attach_log(state: dict) -> None:
    if not state.get("job") or not state.get("started"):
        return
    path = report_directory(state) / "failure.log"
    if not path.parent.is_dir():
        return
    logger = logging.getLogger()
    if any(getattr(handler, "baseFilename", None) == str(path) for handler in logger.handlers):
        return
    handler = RotatingFileHandler(path, maxBytes=LIMIT, backupCount=1)
    os.chmod(path, 0o600)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)


def capture_build_logs(directory: Path) -> None:
    """Save only referenced DKMS make.log files before rollback loses them."""
    evidence = "\n".join(tail(directory / name) for name in ("failure.log.1", "failure.log"))
    if not evidence.strip():
        evidence = tail(STATE_DIR / "update.log")
    pattern = re.escape(str(DKMS_LOG_ROOT)) + r"/[A-Za-z0-9_.+/@:-]+/make\.log\b"
    paths = list(dict.fromkeys(re.findall(pattern, evidence)))[-8:]
    if not paths:
        return
    contents = bytearray()
    for filename in paths:
        path = Path(filename)
        try:
            # Never follow a build-directory symlink into unrelated files.
            if not path.resolve().is_relative_to(DKMS_LOG_ROOT.resolve()) or not path.is_file():
                continue
            text = tail(path)
        except OSError as error:
            text = f"Could not read build log: {error}\n"
        header = f"\nDKMS build log: {filename}\n".encode("utf-8")
        budget = LIMIT // len(paths) - len(header)
        if budget > 0:
            contents.extend(header)
            contents.extend(text.encode("utf-8")[-budget:])
    if contents:
        path = directory / "failure-build.log"
        path.write_bytes(contents)
        os.chmod(path, 0o600)


def capture_journal(path: Path) -> None:
    units = ["nobara-updater-prepare.service", "nobara-updater-prepare-codecs.service", "nobara-updater-live.service",
             "nobara-updater-offline.service", "nobara-updater-recovery.service", "nobara-updater-confirm.service"]
    command = ["journalctl", "-b", "--no-pager", "--no-hostname", "-o", "short-iso", "-n", "2000"]
    for unit in units:
        command.extend(["-u", unit])
    note = ""
    with tempfile.TemporaryFile() as output:
        try:
            subprocess.run(command, stdout=output, stderr=subprocess.STDOUT, timeout=15, check=False)
        except subprocess.TimeoutExpired:
            note = "Journal collection timed out after 15 seconds; any partial output is included below.\n"
        except OSError as error:
            note = f"Journal collection was unavailable: {error}\n"
        output.seek(0, os.SEEK_END)
        output.seek(max(0, output.tell() - (LIMIT - len(note.encode("utf-8")))))
        journal = output.read(LIMIT).decode("utf-8", errors="replace")
    path.write_text(note + journal)
    os.chmod(path, 0o600)


def record_failure(state: dict, phase: str, error: Exception) -> None:
    """Best effort only: diagnostics must never prevent recovery."""
    try:
        if not state.get("job"):
            return
        directory = report_directory(state)
        path = directory / "failure.json"
        report = json.loads(path.read_text()) if path.exists() else {"job": state["job"]}
        # Booting the interrupted installation can fail confirmation before
        # rollback succeeds. Preserve the original failure and its journal.
        secondary = phase in {"recover", "confirm"} and report.get("phase") not in {None, "installation or startup"}
        if secondary:
            report["recovery_error" if phase == "recover" else "confirmation_error"] = str(error)
        else:
            error = annotate_failure(error, state.get("package_origins", []))
            report.update(phase=phase, error=str(error), time=time.time(),
                          package_origins=state.get("package_origins", report.get("package_origins", [])),
                          package_conflicts=getattr(error, "package_conflicts", state.get("package_conflicts", [])))
        atomic_json(path, report)
        if not secondary and state.get("started"):
            try:
                capture_build_logs(directory)
            except Exception:
                LOG.warning("Could not collect DKMS build logs; preserving the update failure report.", exc_info=True)
        journal_name = f"failure-{phase}-journal.log" if secondary else "failure-journal.log"
        try:
            capture_journal(directory / journal_name)
        except Exception:
            LOG.warning("Could not collect the journal; preserving the update failure report.", exc_info=True)
        if not state.get("recovery", {}).get("created"):
            publish_failure(state)
        os.sync()
    except Exception:
        LOG.exception("Could not preserve update failure diagnostics.")


def record_service_failure(state: dict, phase: str, result: str, exit_code: str, exit_status: str) -> None:
    """ExecStopPost also runs after a timeout, signal, OOM kill or exec failure.

    Python's exception handler cannot record those failures. Save systemd's
    result and journal outside the root snapshot before OnFailure rolls back.
    Never replace a more useful error already recorded by the worker.
    """
    if result in {"", "success", "exec-condition"} or not state.get("job") or not state.get("started"):
        return
    if state.get("status") in {"complete", "recovered", "live-complete", "installer-complete"}:
        LOG.warning("Update already confirmed; service stopped during cleanup: %s (%s/%s).",
                    result, exit_code, exit_status)
        return
    try:
        directory = report_directory(state)
        path = directory / "failure.json"
        report = json.loads(path.read_text()) if path.exists() else {}
        unit = "nobara-updater-confirm.service" if phase == "confirm" else "nobara-updater-offline.service"
        message = f"{unit} stopped before completion: systemd result={result}, exit code={exit_code}, exit status={exit_status}."
        if result == "timeout":
            message += " The service exceeded its time limit; this does not by itself prove a package or kernel failure."
        LOG.error("%s", message)
        if report.get("phase") in {None, "installation or startup"}:
            record_failure(state, phase, UpdateError(message))
        else:
            # Preserve both the primary error and its journal, including when
            # finalization already reported a real package/boot-file failure.
            report["service_failure"] = dict(unit=unit, result=result, exit_code=exit_code, exit_status=exit_status)
            atomic_json(path, report)
            capture_journal(directory / f"failure-{phase}-journal.log")
            if not state.get("recovery", {}).get("created"):
                publish_failure(state)
            os.sync()
    except Exception:
        LOG.exception("Could not preserve service failure diagnostics.")


def publish_recovery(job: str) -> dict:
    """Import from separate /boot into the restored system for desktop users."""
    return publish_report(job, BOOT_REPORTS / valid_job(job), recovered=True, started=True)


def publish_failure(state: dict) -> dict:
    return publish_report(state["job"], report_directory(state), recovered=False,
                          started=bool(state.get("started")), error=state.get("error"))


def publish_report(job: str, directory: Path, *, recovered: bool, started: bool, error: str | None = None) -> dict:
    job = valid_job(job)
    path = directory / "failure.json"
    report = json.loads(path.read_text()) if path.exists() else dict(
        error=error or "Detailed failure information was not saved by the previous updater.", phase="unknown")
    report.update(job=job, recovered=recovered, installation_started=started, active=True)
    detail = "\n".join(tail(directory / name) for name in (
        "failure.log.1", "failure.log", "failure-build.log", "failure-journal.log",
        "failure-confirm-journal.log", "failure-execute-journal.log", "failure-recover-journal.log"))
    if not detail.strip():
        detail = "No separate failed-boot log was preserved. This updater log may be incomplete.\n" + tail(STATE_DIR / "update.log")
    guidance = instructions(recovered, started)
    # A hard crash may bypass record_failure. Use the saved pre-update origin
    # inventory only for exact packages identified in the surviving error log.
    if not report.get("package_conflicts") and report.get("package_origins"):
        evidence = "\n".join(line for line in detail.splitlines()
                             if re.search(r"(?i)conflict|requires |nothing provides|scriptlet.*fail|error:", line))
        error = annotate_failure(UpdateError(report["error"] + "\n" + evidence), report["package_origins"])
        report["package_conflicts"] = getattr(error, "package_conflicts", [])
        if report["package_conflicts"]:
            from .update_origins import format_origins
            report["error"] += "\n\n" + format_origins(report["package_conflicts"])
    # The complete inventory is private context, not a list of culprits.
    report.pop("package_origins", None)
    text = redact(guidance + "\nFailure details\n" + json.dumps(report, indent=2) + "\n\nUpdate log\n" + detail)
    # These intentionally user-readable reports contain updater diagnostics,
    # not the private transaction state, full journal, or credentials/configs.
    REPORTS.mkdir(mode=0o755, parents=True, exist_ok=True)
    os.chmod(REPORTS, 0o755)
    destination = REPORTS / job
    destination.mkdir(mode=0o755, exist_ok=True)
    os.chmod(destination, 0o755)
    for name, contents in (("README.txt", guidance), ("update.log", text)):
        (destination / name).write_text(contents)
        os.chmod(destination / name, 0o644)
    bundle = destination / "nobara-update-report.tar.gz"
    fd, temporary = tempfile.mkstemp(dir=destination)
    os.close(fd)
    try:
        with tarfile.open(temporary, "w:gz") as archive:
            for name in ("README.txt", "update.log"):
                archive.add(destination / name, arcname=name)
        os.chmod(temporary, 0o644)
        os.replace(temporary, bundle)
    finally:
        Path(temporary).unlink(missing_ok=True)
    atomic_json(REPORTS / "latest.json", json.loads(redact(json.dumps(report))))
    os.chmod(REPORTS / "latest.json", 0o644)
    os.sync()
    return report


def latest_report() -> dict | None:
    try:
        report = json.loads((REPORTS / "latest.json").read_text())
    except FileNotFoundError:
        return None
    job = valid_job(report["job"])
    directory = REPORTS / job
    return dict(report, log=str(directory / "update.log"), bundle=str(directory / "nobara-update-report.tar.gz"))


def resolve_notice() -> None:
    report = latest_report()
    if report and report.get("active"):
        report["active"] = False
        atomic_json(REPORTS / "latest.json", report)
        os.chmod(REPORTS / "latest.json", 0o644)


def upload_report(report: dict) -> str:
    """Only called following an explicit Upload action or CLI --upload."""
    result = subprocess.run(["pbcli", "--host", "https://paste.nobaraproject.org", "--timeout", "30"],
                            input=Path(report["log"]).read_text(), capture_output=True, text=True, timeout=90)
    if result.returncode:
        raise UpdateError("Log upload failed. Save the report and share it when you have a connection.\n" + result.stderr[-1000:])
    urls = re.findall(r"https://(?:pb|paste)\.nobaraproject\.org/[^\s]+", result.stdout)
    if not urls:
        raise UpdateError("The upload did not return a Nobara log link. Your saved report is still available.")
    return urls[-1]
