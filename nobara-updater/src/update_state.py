"""Durable, root-owned state shared by the updater CLI and offline worker."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import time
import urllib.parse
from pathlib import Path

STATE_DIR = Path("/var/lib/nobara-updater")
TRIGGER = Path("/system-update")
ACTIVE = {"preparing", "ready", "scheduled", "installing", "validating", "installing-live", "validating-live", "awaiting-boot", "recovering"}
# The variables curl reads when dnf.conf sets no proxy=. curl ignores
# upper-case HTTP_PROXY on purpose, so it is not passed on either.
PROXY_VARIABLES = ("http_proxy", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY", "no_proxy", "NO_PROXY")
PROXY_SCHEMES = {"http", "https", "socks4", "socks4a", "socks5", "socks5h"}
# Proxy URLs can hold credentials: keep the handover on tmpfs, root-only.
PROXY_FILE = Path("/run/nobara-updater-proxy.json")


class UpdateError(RuntimeError):
    pass


def valid_proxy_setting(name: str, value: str) -> bool:
    """Accept a proxy URL or a no_proxy host list; refuse anything else."""
    if not isinstance(value, str) or not 0 < len(value) <= 2048:
        return False
    if name.lower() == "no_proxy":
        return re.fullmatch(r"[A-Za-z0-9.:*/%_\[\], -]+", value) is not None
    if not re.fullmatch(r"[!-~]+", value):
        return False
    try:
        # Like curl, read a proxy without a scheme as http://.
        url = urllib.parse.urlsplit(value if "://" in value else "http://" + value)
        port = url.port
    except ValueError:
        return False
    return (url.scheme in PROXY_SCHEMES and re.fullmatch(r"[A-Za-z0-9._:%-]+", url.hostname or "") is not None
            and port != 0 and url.path in {"", "/"} and not url.query and not url.fragment)


def proxy_settings(environ) -> tuple[dict[str, str], list[str]]:
    """Return the valid proxy variables in environ, and the names refused."""
    settings, refused = {}, []
    for name in PROXY_VARIABLES:
        value = environ.get(name)
        if value and valid_proxy_setting(name, value):
            settings[name] = value
        elif value:
            refused.append(name)
    return settings, refused


def load_proxy_settings(path: Path = PROXY_FILE) -> list[str]:
    """Apply the proxy settings nobara-sync handed over; return their names.

    systemd starts the preparation services with the manager's environment,
    not the caller's. Only a regular file this user owns and nobody else
    can write is read, and only valid settings from PROXY_VARIABLES are used.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return []
    except OSError as error:
        raise UpdateError(f"Refusing proxy settings in {path}: {error.strerror}.") from error
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        os.close(fd)
        raise UpdateError(f"Refusing proxy settings in {path}: not a private regular file.")
    with os.fdopen(fd) as stream:
        try:
            data = json.load(stream)
        except ValueError as error:
            raise UpdateError(f"Refusing proxy settings in {path}: {error}") from error
    if not isinstance(data, dict):
        raise UpdateError(f"Refusing proxy settings in {path}: unexpected format.")
    settings, _ = proxy_settings(data)
    os.environ.update(settings)
    return sorted(settings)


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
