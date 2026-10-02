"""Durable, root-owned state shared by the updater CLI and offline worker."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

STATE_DIR = Path("/var/lib/nobara-updater")
TRIGGER = Path("/system-update")
ACTIVE = {"preparing", "ready", "scheduled", "installing", "validating", "installing-live", "validating-live", "awaiting-boot", "recovering"}


class UpdateError(RuntimeError):
    pass


def in_installer_root() -> bool:
    """Recognize an actual chroot, never infer one from a missing system bus."""
    result = subprocess.run(["systemd-detect-virt", "--chroot"], capture_output=True, text=True)
    if result.returncode not in {0, 1}:
        raise UpdateError("Could not determine whether this is an installer target root: " + result.stderr.strip())
    return result.returncode == 0


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_state(root: Path = STATE_DIR) -> dict:
    try:
        return json.loads((root / "state.json").read_text())
    except FileNotFoundError:
        return {"status": "idle"}


def read_status(root: Path = STATE_DIR) -> dict:
    """Read-only view: a saved label alone cannot promise an offline boot."""
    state = read_state(root)
    if state.get("status") == "scheduled" and not state.get("started"):
        if not (TRIGGER.is_symlink() and TRIGGER.resolve() == root.resolve()):
            return dict(state, status="ready", scheduling_error="The offline boot trigger is missing or belongs to another updater. Run nobara-sync cli again.")
    return state


def write_state(root: Path, state: dict, status: str, **fields) -> dict:
    state.update(fields, status=status, updated=time.time())
    atomic_json(root / "state.json", state)
    return state


@contextlib.contextmanager
def update_lock(root: Path = STATE_DIR):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / "lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise UpdateError("Another Nobara update operation is running.") from error
        yield


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inventory_digest(lines: str) -> str:
    return hashlib.sha256("\n".join(sorted(lines.splitlines())).encode()).hexdigest()


def rpm_fingerprint() -> str:
    result = subprocess.run(
        ["rpm", "-qa", "--qf", "%{NAME}|%{EPOCHNUM}|%{VERSION}|%{RELEASE}|%{ARCH}|%{INSTALLTIME}\n"],
        capture_output=True, text=True, check=True,
    )
    return inventory_digest(result.stdout)


def verify_files(job: Path, manifest: dict[str, str]) -> None:
    if not manifest or "transaction.json" not in manifest:
        raise UpdateError("The prepared transaction has no integrity manifest.")
    for relative, checksum in manifest.items():
        path = job / relative
        if not path.resolve().is_relative_to(job.resolve()) or path.is_symlink():
            raise UpdateError("Invalid path in the prepared transaction.")
        if not path.is_file() or file_digest(path) != checksum:
            raise UpdateError(f"Prepared update file is missing or changed: {relative}")


def os_release(path: Path = Path("/etc/os-release")) -> str:
    for line in path.read_text().splitlines():
        key, separator, value = line.partition("=")
        if key == "VERSION_ID" and separator:
            value = value.strip().strip('\"\'')
            if re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", value):
                return value
    raise UpdateError(f"Cannot determine the installed release from {path}.")


def status_message(state: dict) -> str:
    if state.get("scheduling_error"):
        return state["scheduling_error"]
    status = state.get("status", "idle")
    messages = {
        "idle": "Check for system updates.",
        "preparing": "Preparing and downloading the complete system update…",
        "ready": ("Application updates are ready to install now." if state.get("execution", {}).get("mode") == "live"
                  else "System update is prepared. Run nobara-sync cli to schedule installation."),
        "scheduled": "System update will install on the next restart.",
        "installing": "Installing the prepared system update. Please keep the computer powered on.",
        "validating": "Checking the updated system and preparing its boot files…",
        "awaiting-boot": "Installation finished. Restart to verify the updated system.",
        "installer-complete": "Updates installed and validated in the installer target. The installer can continue.",
        "installing-live": "Installing application updates. Please keep the computer powered on.",
        "validating-live": "Checking the installed application updates…",
        "live-complete": "Updates installed. Restart updated applications to use their new versions.",
        "complete": "System update completed and startup checks passed.",
        "unchanged": "System packages are up to date.",
        "failed": "System update failed: " + state.get("error", "See the update log."),
        "interrupted": ("A system update was interrupted. Boot recovery before another update."
                        if state.get("recovery", {}).get("created") else
                        "A system update was interrupted. Read nobara-sync recovery-report; after repairing the problem, run sudo nobara-sync retry-update."),
        "recovering": "Restoring the previous system boot selection.",
        "recovered": "The previous system was restored. The update did not complete.",
    }
    return messages.get(status, "Unknown update state; see the update log.")
