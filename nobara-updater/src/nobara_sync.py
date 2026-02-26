#!/usr/bin/python3
import argparse
import html
import logging
import os
import platform
import pwd
import re
import subprocess
import sys
import threading
import xml.etree.ElementTree as ElementTree
from argparse import Namespace
from pathlib import Path

import gi  # type: ignore[import]
import psutil
import shutil
import requests
from nobara_updater.quirks import QuirkFixup  # type: ignore[import]
from nobara_updater.run_as import run_as_user

gi.require_version("Gtk", "3.0")
gi.require_version("GLib", "2.0")
gi.require_version("Flatpak", "1.0")

from gi.repository import Flatpak, GLib, Gtk  # type: ignore[import]

from nobara_updater.dnf import (  # type: ignore[import]
    AttributeDict,
    PackageUpdater,
    repoindex,
    updatechecker,
)

# Force UTF-8 locale for all child processes spawned from this Python process
os.environ.setdefault("LANG", "C.UTF-8")
os.environ.setdefault("LC_ALL", "C.UTF-8")

original_user_home = Path("~").expanduser()

if "ORIGINAL_USER_HOME" in os.environ:
    original_user_home = Path(os.environ["ORIGINAL_USER_HOME"])

log_file_path = original_user_home / ".local/share/nobara-updater/"

if not log_file_path.exists():
    log_file_path.mkdir(parents=True)

log_file = log_file_path / "nobara-sync.log"

class Color:
    """A class for terminal color codes."""

    GREEN = "\033[92m"
    RED = "\033[91m"
    END = "\033[0m"


class ColorLogFormatter(logging.Formatter):
    """A class for formatting colored logs."""

    def format(self, record):
        log_entry = super().format(record)
        if record.levelno == logging.INFO:
            if "✔" in log_entry:
                log_entry = log_entry.replace(
                    "<span foreground='#00FF00'>✔</span>",
                    f"{Color.GREEN}✔{Color.END}",
                )
            if "✘" in log_entry:
                log_entry = log_entry.replace(
                    "<span foreground='#FF0000'>✘</span>",
                    f"{Color.RED}✘{Color.END}",
                )
        return log_entry


class TextViewHandler(logging.Handler):
    def __init__(self, textview: Gtk.TextView) -> None:
        super().__init__()
        self.textview = textview

    def emit(self, record: logging.LogRecord) -> None:
        log_entry = self.format(record)
        GLib.idle_add(self.update_textview, log_entry)

    def update_textview(self, log_entry: str) -> bool:
        buffer = self.textview.get_buffer()
        # Create a mark at the end of the buffer
        mark = buffer.create_mark(None, buffer.get_end_iter(), False)
        # Insert the log entry text
        buffer.insert_markup(buffer.get_end_iter(), log_entry + "\n", -1)
        # Scroll to the mark
        self.textview.scroll_to_mark(mark, 0.0, True, 0.0, 1.0)
        return False  # Stop the idle_add loop


def rotate_log_files(log_file: str) -> None:
    log_file_path = Path(log_file)
    log_dir = log_file_path.parent
    log_pattern = f"{log_file_path.name}.*"

    log_files = sorted(log_dir.glob(log_pattern), key=os.path.getmtime)

    # Rotate log files
    for i in range(len(log_files), 0, -1):
        old_log = log_dir / f"{log_file_path.name}.{i}"
        new_log = log_dir / f"{log_file_path.name}.{i+1}"
        if i == 5:
            old_log.unlink()
        elif old_log.exists():
            old_log.rename(new_log)

    # Rename the current log file if it exists
    if Path(log_file).exists():
        Path(log_file).rename(Path(f"{log_file}.1"))


# Initialize the logger with a basic configuration
logger = logging.getLogger()
logger.setLevel(logging.INFO)


def initialize_logging(textview: Gtk.TextView = None) -> logging.Logger:
    global rotate_log_files
    global logger

    # Clear existing handlers
    logger.handlers = []

    # CONSOLE/TERMINAL
    # Create console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    # Create formatter for console handler
    console_formatter = ColorLogFormatter(
        "%(asctime)s - %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    console_handler.setFormatter(console_formatter)
    # Add the console handler to the logger
    logger.addHandler(console_handler)

    # LOG FILE
    # Rotate log files before writing the new log fo;e
    rotate_log_files(str(log_file))

    # Create file handler
    file_handler = logging.FileHandler(log_file, mode="w")
    file_handler.setLevel(logging.INFO)
    # Create formatter for the file handler
    file_formatter = logging.Formatter(
        "%(asctime)s - %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    file_handler.setFormatter(file_formatter)
    # Add the file handler to the logger
    logger.addHandler(file_handler)

    # GUI STATUS WINDOW
    # Optionally create textview handler for GUI
    if textview is not None:
        textview_handler = TextViewHandler(textview)
        textview_handler.setLevel(logging.INFO)
        # Add the textview handler to the logger
        logger.addHandler(textview_handler)

    return logger


def is_running_with_sudo_or_pkexec() -> int:
    # Check environment variables first
    if "SUDO_USER" in os.environ:
        return 1
    if "PKEXEC_UID" in os.environ:
        return 2

    return 0


# Read VERSION_ID from /etc/os-release
def get_os_release_version() -> str:
    with Path("/etc/os-release").open() as f:
        for line in f:
            if line.startswith("VERSION_ID"):
                return line.strip().split("=")[1].strip('"')
    return "40"


# Get system architecture
def get_basearch() -> str:
    return platform.machine()


VERSION_ID = get_os_release_version()
BASEARCH = get_basearch()


def normalize_url(url: str) -> str:
    # Remove any trailing slashes
    return url.rstrip("/")


def remove_double_slashes(url: str) -> str:
    # Use regex to replace double slashes after the protocol part
    return re.sub(r"(?<!:)/{2,}", "/", url)


# Define the Repo class with the required attributes
class Repo:
    def __init__(
        self,
        metalink: str | None = None,
        mirrorlist: str | None = None,
        baseurl: list[str] | None = None,
        repo_id: str | None = None,
    ):
        self.metalink = metalink
        self.mirrorlist = mirrorlist
        self.baseurl = baseurl
        self.id = repo_id


# Function to convert list of AttributeDict to dictionary of Repo objects
def convert_to_repo_dict(repo_list: list[AttributeDict]) -> dict[str | None, Repo]:
    repo_dict = {}
    for repo_info in repo_list:
        repo = Repo(
            metalink=repo_info.get("metalink"),
            mirrorlist=repo_info.get("mirrorlist"),
            baseurl=repo_info.get("baseurl"),
            repo_id=repo_info.get("id"),
        )
        repo_dict[repo.id] = repo
    return repo_dict

def get_repolist() -> tuple[
    list[str],
    list[str],
    list[str],
    dict[str, str | None],
    dict[str, str | None],
    dict[str, str | None],
]:
    metalinks = []
    mirrorlists = []
    baseurls = []

    metalink_repos: dict[str, str | None] = {}
    mirrorlist_repos: dict[str, str | None] = {}
    baseurl_repos: dict[str, str | None] = {}

    repositories: dict[str | None, Repo] = convert_to_repo_dict(repoindex())

    if repositories is not None:
        for repo in repositories.values():
            if repo.metalink:
                metalinks.append(str(repo.metalink))
                metalink_repos[str(repo.metalink)] = repo.id
            if repo.mirrorlist:
                mirrorlist_url = (
                    str(repo.mirrorlist)
                    .replace("$releasever", VERSION_ID)
                    .replace("$basearch", BASEARCH)
                )
                mirrorlist_url = normalize_url(
                    mirrorlist_url
                )  # Remove trailing slashes
                mirrorlist_url = remove_double_slashes(
                    mirrorlist_url
                )  # Remove any double slashes
                mirrorlists.append(mirrorlist_url)
                mirrorlist_repos[mirrorlist_url] = repo.id
            if repo.baseurl:
                for url in repo.baseurl:
                    if url:  # Check if url is not None, not empty, and not blank
                        url = (
                            str(url)
                            .replace("$releasever", VERSION_ID)
                            .replace("$basearch", BASEARCH)
                        )
                        url = normalize_url(url)  # Remove trailing slashes
                        url = url + "/repodata/repomd.xml"  # Append the required path
                        url = remove_double_slashes(url)  # Remove any double slashes
                        baseurls.append(url)
                        baseurl_repos[url] = repo.id
    return (
        metalinks,
        mirrorlists,
        baseurls,
        metalink_repos,
        mirrorlist_repos,
        baseurl_repos,
    )


def validate_metalink(
    metalink_url: str, session: requests.Session, headers: dict[str, str]
) -> bool:
    try:
        response = session.get(metalink_url, headers=headers, timeout=5)
        if response.status_code == 200:
            try:
                root = ElementTree.fromstring(response.content)
                return bool(root.tag.endswith("metalink"))
            except ElementTree.ParseError:
                return False
        return False
    except Exception:
        return False


def validate_mirrorlist(
    mirrorlist_url: str, session: requests.Session, headers: dict[str, str]
) -> bool:
    try:
        response = session.get(mirrorlist_url, headers=headers, timeout=5)
        if response.status_code == 200:
            mirrors = response.text.splitlines()
            mirrors = [mirror for mirror in mirrors if mirror.strip()]
            mirrors = [
                str(mirror)
                .replace("$releasever", VERSION_ID)
                .replace("$basearch", BASEARCH)
                for mirror in mirrors
            ]
            mirrors = [
                normalize_url(mirror) for mirror in mirrors
            ]  # Remove trailing slashes
            mirrors = [
                mirror + "/repodata/repomd.xml" for mirror in mirrors
            ]  # Append the required path
            mirrors = [
                remove_double_slashes(mirror) for mirror in mirrors
            ]  # Remove any double slashes
            if validate_baseurls(mirrors, session, headers):
                return True
        return False
    except Exception:
        return False


def validate_baseurls(
    baseurl_list: list[str], session: requests.Session, headers: dict[str, str]
) -> bool:
    for url in baseurl_list:
        try:
            response = session.head(url, headers=headers, timeout=5)
            if response.status_code == 200:
                return True
        except Exception:
            return False
    return False

def check_repos() -> None:
    green = "#00FF00"
    red = "#FF0000"
    check_mark = f"<span foreground='{green}'>✔</span>"
    red_x = f"<span foreground='{red}'>✘</span>"

    logger.info("Checking repositories...\n")
    (
        metalinks,
        mirrorlists,
        baseurls,
        metalink_repos,
        mirrorlist_repos,
        baseurl_repos,
    ) = get_repolist()
    log_messages = []

    # Create a session
    session = requests.Session()

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/98.0.4758.82 Safari/537.36"
    }
    # Example: Validate the URLs and print the corresponding repo names
    for metalink in metalinks:
        escaped_metalink = html.escape(metalink)
        if validate_metalink(metalink, session, headers):
            log_messages.append(
                f"{check_mark} {metalink_repos[metalink]}: metalink: {escaped_metalink}\n"
            )
        else:
            log_messages.append(
                f"{red_x} {metalink_repos[metalink]}: metalink: {escaped_metalink}\n"
            )

    for mirrorlist in mirrorlists:
        escaped_mirrorlist = html.escape(mirrorlist)
        if validate_mirrorlist(mirrorlist, session, headers):
            log_messages.append(
                f"{check_mark} {mirrorlist_repos[mirrorlist]}: mirrorlist: {escaped_mirrorlist}\n"
            )
        else:
            log_messages.append(
                f"{red_x} {mirrorlist_repos[mirrorlist]}: mirrorlist: {escaped_mirrorlist}\n"
            )

    for url in baseurls:
        escaped_url = html.escape(url)
        if validate_baseurls([url], session, headers):
            log_messages.append(
                f"{check_mark} {baseurl_repos[url]}: baseurl: {escaped_url}\n"
            )
        else:
            log_messages.append(
                f"{red_x} {baseurl_repos[url]}: baseurl: {escaped_url}\n"
            )

    for message in log_messages:
        logger.info(message)


updates_available = 0
fixups_available = 0
perform_kernel_actions = 0
perform_reboot_request = 0
perform_refresh = 0
is_refreshing = 0
media_fixup_event = threading.Event()

def get_system_updates_available() -> int:
    global system_updates_available
    return system_updates_available

def get_flatpak_updates_available() -> int:
    global flatpak_updates_available
    return flatpak_updates_available

def toggle_refresh() -> None:
    global is_refreshing

    is_refreshing = 1 if is_refreshing == 0 else 0

def get_refresh() -> int:
    global is_refreshing
    return is_refreshing

def get_updates_available() -> int:
    global updates_available
    return updates_available

def get_fixups_available() -> int:
    global fixups_available
    return fixups_available

def check_updates(return_texts: bool = False) -> None | tuple[str | None, str | None, str | None]:
    global updates_available
    global system_updates_available
    global flatpak_updates_available

    updates_available = 0
    system_updates_available = 0
    flatpak_updates_available = 0

    sys_update_text = None
    fp_user_update_text = None
    fp_sys_update_text = None

    # Get our system updates
    package_names = updatechecker()
    if package_names:
        updates_available = 1
        system_updates_available = 1
        sys_update_text = "\n".join(package_names)

    if is_running_with_sudo_or_pkexec() == 1:
        sudo_user = os.environ.get('SUDO_USER', '')
        if sudo_user and not sudo_user.isdigit():
            try:
                orig_user_uid = pwd.getpwnam(sudo_user).pw_uid
                os.environ['ORIG_USER'] = str(orig_user_uid)

                original_user_home = pwd.getpwnam(sudo_user).pw_dir
                os.environ['ORIGINAL_USER_HOME'] = str(original_user_home)
            except KeyError:
                print(f"User {sudo_user} not found")

    # Get the original user's UID and GID
    orig_user = os.environ.get("ORIG_USER")
    if orig_user is None:
        orig_user = "0"
    orig_user_uid = int(orig_user)
    pw_record = pwd.getpwuid(orig_user_uid)
    orig_user_gid = pw_record.pw_gid

    # Flatpak User Updates window
    fp_user_updates = run_as_user(orig_user_uid, orig_user_gid, "fp_get_user_updates")
    if fp_user_updates:
        updates_available = 1
        flatpak_updates_available = 1
        fp_user_update_text = "\n".join(fp_user_updates)

    # Flatpak System Updates window
    fp_system_updates = fp_get_system_updates()
    if fp_system_updates:
        updates_available = 1
        flatpak_updates_available = 1
        fp_sys_update_texts = [
            fp_system_update.get_appdata_name()
            for fp_system_update in fp_system_updates
            if fp_system_update.get_appdata_name() is not None
        ]
        fp_sys_update_text = "\n".join(fp_sys_update_texts)

    if is_running_with_sudo_or_pkexec() == 1:
        if sys_update_text:
            logger.info("")
            logger.info("System Updates:")
            logger.info("\n%s", sys_update_text)
        if fp_user_update_text:
            logger.info("")
            logger.info("Flatpak User Updates:")
            logger.info("\n%s", fp_user_update_text)
        if fp_sys_update_text:
            logger.info("")
            logger.info("Flatpak System Updates:")
            logger.info("\n%s", fp_sys_update_text)
        logger.info("")

    if return_texts:
        return sys_update_text, fp_user_update_text, fp_sys_update_text
    return None

def fp_get_system_updates() -> list[Flatpak.Ref] | None:
    # Get our flatpak updates
    with fp_system_installation_list(Flatpak.Installation.new_system(None)) as flatpak_sys_updates:
        if flatpak_sys_updates != []:
            return flatpak_sys_updates
        return []

def get_orig_user_ids() -> tuple[int, int]:
    if is_running_with_sudo_or_pkexec() == 1:
        sudo_user = os.environ.get("SUDO_USER", "")
        if sudo_user and not sudo_user.isdigit():
            try:
                orig_user_uid = pwd.getpwnam(sudo_user).pw_uid
                os.environ["ORIG_USER"] = str(orig_user_uid)
                original_user_home = pwd.getpwnam(sudo_user).pw_dir
                os.environ["ORIGINAL_USER_HOME"] = str(original_user_home)
            except KeyError:
                print(f"User {sudo_user} not found")

    orig_user = os.environ.get("ORIG_USER") or "0"
    orig_user_uid = int(orig_user)
    pw_record = pwd.getpwuid(orig_user_uid)
    orig_user_gid = pw_record.pw_gid
    return orig_user_uid, orig_user_gid

def install_system_updates_only() -> None:
    global perform_kernel_actions
    global perform_reboot_request

    package_names = updatechecker()

    logger.info("Starting SYSTEM package updates, please do not turn off your computer...\n")
    action = "upgrade"
    if package_names:
        PackageUpdater(package_names, action, None, logger)

    # Perform dracut if kernel was updated.
    if perform_kernel_actions == 1:
        logger.info(
            "Kernel or kernel module updates were performed. Running required 'dracut -f'...\n"
        )
        try:
            result = subprocess.run(
                "ls /boot/ | grep vmlinuz | grep -v rescue",
                shell=True,
                capture_output=True,
                text=True,
                check=True,
            )
            lines = result.stdout.strip().split("\n")
            versions = [line.replace("vmlinuz-", "") for line in lines if line.startswith("vmlinuz-")]

            result = subprocess.run(["ls", "/lib/modules"], capture_output=True, text=True, check=True)
            modules = result.stdout.strip().split()

            filtered_modules = [module for module in modules if module not in versions]
            for directory in filtered_modules:
                if directory:
                    dir_path = os.path.join("/lib/modules", directory)
                    if os.path.exists(dir_path):
                        shutil.rmtree(dir_path)
        except subprocess.CalledProcessError as e:
            print(f"An error occurred: {e}")

        subprocess.run(["dracut", "-f", "--regenerate-all"], check=True)
        perform_reboot_request = 1

    # Send update refresh request to systray service
    orig_user_uid, orig_user_gid = get_orig_user_ids()
    run_as_user(orig_user_uid, orig_user_gid, "yumex_sync_updates")

    # Remove newinstall needs-update tracker
    if Path.exists(Path("/etc/nobara/newinstall")):
        try:
            Path("/etc/nobara/newinstall").unlink()
        except OSError as e:
            logger.error("Error: %s", e.strerror)

    if perform_reboot_request == 1:
        logger.info("Kernel, kernel module, or desktop compositor update performed. Reboot required.")
        prompt_reboot()

def install_flatpak_updates_only() -> None:
    logger.info("Starting FLATPAK updates, please do not turn off your computer...\n")

    orig_user_uid, orig_user_gid = get_orig_user_ids()

    # system flatpaks
    install_system_flatpak_updates()

    # user flatpaks
    run_as_user(orig_user_uid, orig_user_gid, "install_user_flatpak_updates")

    # refresh systray
    run_as_user(orig_user_uid, orig_user_gid, "yumex_sync_updates")

    logger.info("Flatpak updates complete!\n")


def install_system_flatpak_updates() -> None:
    # System installation updates
    system_installation = Flatpak.Installation.new_system(None)
    with fp_system_installation_list(system_installation) as flatpak_sys_updates:
        if flatpak_sys_updates is not None:
            transaction = Flatpak.Transaction.new_for_installation(system_installation)
            for ref in flatpak_sys_updates:
                logger.info(
                    "Updating %s for system installation...", ref.get_appdata_name()
                )
                try:
                    # Perform the update
                    transaction.add_update(ref.format_ref(), None, None)
                except Exception as e:
                    logger.error("Error updating %s: %s", ref.get_appdata_name(), e)
            transaction.run()
            logger.info("Flatpak System Updates complete!")
    del system_installation

class fp_system_installation_list(object):
    # Generates flatpak_system_updates for other functions with error handling
    def __init__(self, system_installation):
        self.system_installation = system_installation


    def __enter__(self):
        flatpak_system_updates = None
        error = True # No do-while in Python so init to true to run loop once
        while error:
            try:
                flatpak_system_updates = self.system_installation.list_installed_refs_for_update(None)
            except gi.repository.GLib.GError as e:
                # Expected, see #43
                logger.error(e)
            except:
                raise
            else:
                error = False
        return flatpak_system_updates


    def __exit__(self, *args):
        del self.system_installation

def button_ensure_sensitivity(
    widget: Gtk.Widget, desired_state: bool
) -> None:
    # Check the current sensitivity
    current_state = widget.get_sensitive()
    # Only set the sensitivity if it is not already set to the desired state
    if current_state != desired_state:
        widget.set_sensitive(desired_state)

def install_fixups() -> None:
    global perform_kernel_actions
    global perform_reboot_request
    global fixups_available
    global media_fixup_event

    logger.info(
        "Checking for various known problems to repair, please do not turn off your computer...\n"
    )

    # Run quirks.py and get the values
    logger.info("Running quirk fixup")
    quirk_fixup = QuirkFixup(logger)
    (
        perform_kernel_actions,
        perform_reboot_request,
        fixups_available,
        perform_refresh,
    ) = quirk_fixup.system_quirk_fixup()

    # Perform final refresh after making core fixes before updating the rest of the packages.
    if perform_refresh == 1:
        logger.info("Re-launching after critical update to continue update process...")
        try:
            os.execv(sys.executable, [sys.executable] + sys.argv)
            # Log success
            logger.info("Command scheduled successfully.")
        except Exception as e:
            logger.error(f"Failed to relaunch script: {e}")

    if fixups_available == 1:
        logger.info("Problems with Media Packages detected, repairing...")
        prompt_media_fixup()

    if is_running_with_sudo_or_pkexec() == 1:
        sudo_user = os.environ.get('SUDO_USER', '')
        if sudo_user and not sudo_user.isdigit():
            try:
                orig_user_uid = pwd.getpwnam(sudo_user).pw_uid
                os.environ['ORIG_USER'] = str(orig_user_uid)

                original_user_home = pwd.getpwnam(sudo_user).pw_dir
                os.environ['ORIGINAL_USER_HOME'] = str(original_user_home)
            except KeyError:
                print(f"User {sudo_user} not found")

    # Get the original user's UID and GID
    orig_user = os.environ.get("ORIG_USER")
    if orig_user is None:
        orig_user = "0"
    orig_user_uid = int(orig_user)
    pw_record = pwd.getpwuid(orig_user_uid)
    orig_user_gid = pw_record.pw_gid

    # Send update refresh request to systray service
    run_as_user(
        orig_user_uid, orig_user_gid, "yumex_sync_updates"
    )


def install_updates() -> None:
    install_system_updates_only()
    install_flatpak_updates_only()

def attempt_distro_sync() -> None:
    # Run dnf distro-sync first
    try:
        logger.info("Running dnf distro-sync --refresh...")

        # Run dnf distro-sync with output capture
        result = subprocess.run(
            ["dnf", "distro-sync", "--refresh", "-y"],
            capture_output=True,
            text=True,
            check=True
        )

        # Display the output in the status window
        if result.stdout:
            logger.info("dnf distro-sync output:\n" + result.stdout)

    except subprocess.CalledProcessError as e:
        logger.error(f"dnf distro-sync failed: {e}")
        return
    # Cleanup old modules
    try:
        # Run the command and capture the output
        result = subprocess.run(
            "ls /boot/ | grep vmlinuz | grep -v rescue",
            shell=True,
            capture_output=True,
            text=True,
            check=True
        )
        logger.info("Found kernel versions:\n" + result.stdout)

        # Split the output into lines
        lines = result.stdout.strip().split('\n')

        # Extract version numbers by removing 'vmlinuz-' prefix
        versions = [line.replace('vmlinuz-', '') for line in lines if line.startswith('vmlinuz-')]
        logger.info("Kernel versions to keep: " + ", ".join(versions))

        # Run the ls command and capture the output
        result = subprocess.run(
            ['ls', '/lib/modules'],
            capture_output=True,
            text=True,
            check=True
        )
        logger.info("Found module directories:\n" + result.stdout)

        # Split the output into entries
        modules = result.stdout.strip().split()

        # Filter modules that do not match the kernel versions
        filtered_modules = [module for module in modules if module not in versions]
        logger.info("Modules to remove: " + ", ".join(filtered_modules))

        # Remove filtered modules
        for directory in filtered_modules:
            if directory:  # Check if directory is not None or empty
                dir_path = os.path.join('/lib/modules', directory)
                if os.path.exists(dir_path):  # Check if the path exists
                    shutil.rmtree(dir_path)
                    logger.info(f"Removed module directory: {dir_path}")

    except subprocess.CalledProcessError as e:
        logger.error(f"An error occurred: {e}")
        self.status_label_updates(f"Error during distro-sync: {str(e)}")
        return

    # Run the commands
    try:
        result = subprocess.run(
            ["dracut", "-f","--regenerate-all"],
            capture_output=True,
            text=True,
            check=True
        )
        logger.info("dracut output:\n" + result.stdout)
        logger.info("Distro-sync completed successfully")

    except subprocess.CalledProcessError as e:
        logger.error(f"Error running dracut: {e}")
        return

    try:
        os.execv(sys.executable, [sys.executable] + sys.argv)
        # Log success
        logger.info("Command scheduled successfully.")
    except Exception as e:
        logger.error(f"Failed to relaunch script: {e}")
        self.status_label_updates("Failed to relaunch script")

def prompt_media_fixup() -> None:
    global media_fixup_event
    media_fixup_event.set()
    media_fixup()
    media_fixup_event.wait()

def media_fixup() -> None:
    global fixups_available
    global media_fixup_event
    hard_removal = [
        "ffmpeg.x86_64",
        "ffmpeg-libs.x86_64",
        "ffmpeg-libs.i686",
        "libavcodec-freeworld.x86_64",
        "libavcodec-freeworld.i686",
        "libavdevice.x86_64",
        "libavdevice.i686",
        "obs-studio-gstreamer-vaapi.x86_64",
        "openh264.x86_64",
        "openh264.i686",
        "mesa-libgallium.x86_64",
        "mesa-libgallium.i686",
        "mesa-va-drivers.x86_64",
        "mesa-va-drivers.i686",
        "mesa-libgallium-freeworld.x86_64",
        "mesa-libgallium-freeworld.i686",
        "mesa-va-drivers-freeworld.x86_64",
        "mesa-va-drivers-freeworld.i686",
        "noopenh264.x86_64",
        "noopenh264.i686",
        "x264.x86_64",
        "x264-libs.x86_64",
        "x264-libs.i686",
        "x265.x86_64",
        "x265-libs.x86_64",
        "x265-libs.i686",
        "libheif-freeworld.x86_64",
        "libheif-freeworld.i686",
        "pipewire-codec-aptx",
    ]
    soft_removal = ["mozilla-openh264", "qt5-qtwebengine-freeworld"]

    vulkan_standard = [
        "mesa-vulkan-drivers.x86_64",
        "mesa-vulkan-drivers.i686",
    ]

    vulkan_git = [
        "mesa-vulkan-drivers-git.x86_64",
        "mesa-vulkan-drivers-git.i686",
    ]

    action_log_string = "Purging media packages for a clean slate..."
    combined_removal = hard_removal + soft_removal
    indented_combined_removal = ["    " + line for line in combined_removal]
    logger.info("%s\n\n%s\n", action_log_string, chr(10).join(indented_combined_removal))
    for package in hard_removal:
        subprocess.run(
            ["rpm", "-e", "--nodeps", package], capture_output=True, text=True
        )

    soft_removal_list = []
    for package in soft_removal:
        removal_check = subprocess.run(
            ["rpm", "-q", package], capture_output=True, text=True
        )
        if removal_check.returncode == 0:
            soft_removal_list.append(package)
    if soft_removal_list:
        PackageUpdater(soft_removal_list, "remove", None)

    vulkan_standard_installed = 0
    vulkan_git_installed = 0
    for package in vulkan_standard:
        removal_check_vulkan = subprocess.run(
            ["rpm", "-q", package], capture_output=True, text=True
        )
        if removal_check_vulkan.returncode == 0:
            subprocess.run(
                ["rpm", "-e", "--nodeps", package], capture_output=True, text=True
            )
            vulkan_standard_installed = 1

    for package in vulkan_git:
        removal_check_vulkan_git = subprocess.run(
            ["rpm", "-q", package], capture_output=True, text=True
        )
        if removal_check_vulkan_git.returncode == 0:
            subprocess.run(
                ["rpm", "-e", "--nodeps", package], capture_output=True, text=True
            )
            vulkan_git_installed = 1

    install = [
        "mesa-libgallium-freeworld.x86_64",
        "mesa-libgallium-freeworld.i686",
        "ffmpeg-free.x86_64",
        "libavcodec-free.x86_64",
        "libavcodec-free.i686",
        "libavutil-free.x86_64",
        "libavutil-free.i686",
        "libswresample-free.x86_64",
        "libswresample-free.i686",
        "libavformat-free.x86_64",
        "libavformat-free.i686",
        "libpostproc-free.x86_64",
        "libpostproc-free.i686",
        "libswscale-free.x86_64",
        "libswscale-free.i686",
        "libavfilter-free.x86_64",
        "libavfilter-free.i686",
        "libavdevice-free.x86_64",
        "libavdevice-free.i686",
        "gstreamer1-plugins-bad-free-extras.x86_64",
        "gstreamer1-plugins-bad-free-extras.i686",
        "openh264.x86_64",
        "openh264.i686",
        "mozilla-openh264.x86_64",
        "x264-libs.x86_64",
        "x264-libs.i686",
        "x265-libs.x86_64",
        "x265-libs.i686",
        "libavcodec-freeworld.x86_64",
        "libavcodec-freeworld.i686",
        "libheif-freeworld.x86_64",
        "libheif-freeworld.i686",
        "pipewire-codec-aptx",
    ]

    # enable the nobara-pikaos-additional repo first
    subprocess.run(
            ["dnf", "config-manager", "setopt", "nobara-pikaos-additional.enabled=1"], capture_output=True, text=True
        )

    subprocess.run(
        ["sed", "-i", "s/enabled=0/enabled=1/g", "/etc/yum.repos.d/nobara-pikaos-additional.repo"],
        capture_output=True,
        text=True
    )

    action_log_string = "Performing clean media package installation..."
    indented_install = ["    " + line for line in install]
    logger.info("%s\n\n%s\n", action_log_string, chr(10).join(indented_install))
    install_list = []
    for package in install:
        install_check = subprocess.run(
            ["rpm", "-q", package], capture_output=True, text=True
        )
        if install_check.returncode != 0:
            install_list.append(package)

    if install_list:
        PackageUpdater(install_list, "install", None)

    if vulkan_standard_installed == 1:
        vulkan_standard_freeworld = [
            "mesa-vulkan-drivers-freeworld.x86_64",
            "mesa-vulkan-drivers-freeworld.i686",
        ]
        PackageUpdater(vulkan_standard_freeworld, "install", None)

    if vulkan_git_installed == 1:
        vulkan_git_freeworld = [
            "mesa-vulkan-drivers-git-freeworld.x86_64",
            "mesa-vulkan-drivers-git-freeworld.i686",
        ]
        PackageUpdater(vulkan_git_freeworld, "install", None)

    vulkan_install_check_standard_freeworld = subprocess.run(
        ["rpm", "-q", "mesa-vulkan-drivers-freeworld"], capture_output=True, text=True
    )
    vulkan_install_check_git_freeworld = subprocess.run(
        ["rpm", "-q", "mesa-vulkan-drivers-git-freeworld"], capture_output=True, text=True
    )
    if vulkan_standard_installed == 0 and vulkan_git_installed == 0 and vulkan_install_check_standard_freeworld.returncode != 0 and vulkan_install_check_git_freeworld.returncode != 0:
        vulkan_standard_freeworld = [
            "mesa-vulkan-drivers-freeworld.x86_64",
            "mesa-vulkan-drivers-freeworld.i686",
        ]
        PackageUpdater(vulkan_standard_freeworld, "install", None)

    fixups_available = 0

def prompt_reboot() -> None:
    logger.info("UPDATES COMPLETE. A REBOOT IS REQUIRED. PLEASE REBOOT WHEN POSSIBLE.")

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update System")

    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser(
        "install-updates",
        help="Performs check-updates, install-fixups, then installs any updates available.",
    )
    subparsers.add_parser("check-updates", help="Check for new updates and fixups.")
    subparsers.add_parser("repair", help="Attempts repair using distro-sync.")
    subparsers.add_parser(
        "install-fixups", help="Performs a series of known problem fixes."
    )
    subparsers.add_parser(
        "install-codecs",
        help="Performs media codec installation.",
    )
    cli_parser = subparsers.add_parser("cli", help="Run in CLI mode, defaults to --all")
    cli_parser.add_argument("username", help="Specify the username", nargs="?")
    mode_group = cli_parser.add_mutually_exclusive_group()
    mode_group.add_argument("--system", action="store_true", help="Install only system package updates")
    mode_group.add_argument("--flatpak", action="store_true", help="Install only flatpak updates")
    mode_group.add_argument("--all", action="store_true", help="Install both system and flatpak updates (default)")

    subparsers.add_parser("check-repos", help="list enabled repo information")

    return parser.parse_args()

def check_root_privileges(args: Namespace) -> None:
    if args.command == "cli" and args.username:
        try:
            # Get the parent process ID (PPID)
            ppid = os.getppid()

            # Get the parent process using psutil
            parent_process = psutil.Process(ppid)

            # Log the parent process information for debugging
            logger.info(f"Parent process info: {parent_process}")

            # Get the executable path of the parent process
            parent_cmdline = parent_process.cmdline()

            # Log the command line for debugging
            logger.info(f"Parent process command line: {parent_cmdline}")

        except psutil.NoSuchProcess:
            logger.info("Error: Process does not exist.")
            sys.exit(1)
        except Exception as e:
            logger.info(f"Error: {e}")
            sys.exit(1)

    if is_running_with_sudo_or_pkexec() == 0:
        # Relaunch the script with pkexec or sudo
        script_path = Path(__file__).resolve()
        if "DISPLAY" not in os.environ or args.command is not None:
            ouid = os.getuid()
            os.execvp(
                "sudo",
                [
                    "sudo",
                    "-E",
                    "env",
                    f"XAUTHORITY={os.environ.get('XAUTHORITY', '')}",
                    f"ORIGINAL_USER_HOME={Path('~').expanduser()!s}",
                    f"ORIG_USER={int(ouid)}",
                    f"SUDO_USER={int(ouid)}",
                    sys.executable,
                    str(script_path),
                ]
                + sys.argv[1:],
            )
        else:
            try:
                subprocess.run(["xhost", "si:localuser:root"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
                )
            except Exception:
                pass
            os.execvp(
                "pkexec",
                [
                    "pkexec",
                    "--disable-internal-agent",
                    "env",
                    f"DISPLAY={os.environ['DISPLAY']}",
                    f"XAUTHORITY={os.environ.get('XAUTHORITY', '')}",
                    f"XDG_CURRENT_DESKTOP={os.environ.get('XDG_CURRENT_DESKTOP', '').lower()}",
                    f"ORIGINAL_USER_HOME={Path('~').expanduser()!s}",
                    f"ORIG_USER={os.getuid()!s}",
                    f"PKEXEC_UID={os.getuid()!s}",
                    "NO_AT_BRIDGE=1",
                    "G_MESSAGES_DEBUG=none",
                    sys.executable,
                    str(script_path),
                ]
                + sys.argv[1:],
            )
def request_update_status() -> None:
    global updates_available
    global fixups_available
    have_updates = 0

    if updates_available == 1:
        have_updates = 1
        logger.info("Updates Available.")
    else:
        logger.info("No Updates Available.")

    if have_updates != 1:
        logger.info("All Updates complete!")

def cleanup_xhost():
    """Cleanup function to run xhost on exit"""
    args = parse_args()
    if "DISPLAY" not in os.environ or args.command is not None:
        try:
            subprocess.run(["xhost", "-si:localuser:root"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
            )
        except Exception:
            pass

def main() -> None:

    args = parse_args()
    check_root_privileges(args)

    if args.command and os.geteuid() == 0:
        initialize_logging()
        logger.info("Running CLI mode...")
        # Display updates.txt content
        try:
            response = requests.get("https://updates.nobaraproject.org/updates.txt", timeout=5)
            if response.status_code == 200:
                content = response.text
                print("\n" + "="*50)
                print("Important Notices:")
                print("="*50)
                print(content)
                print("="*50)
            else:
                error_message = f"Failed to fetch updates.nobaraproject.org/updates.txt (Status code: {response.status_code})"
                print(error_message)
        except Exception as e:
            error_message = f"Error fetching updates: {str(e)}"
            print(error_message)
        if args.command == "install-updates":
            check_repos()
            check_updates()
            install_fixups()
            install_updates()  # all (system + flatpak)
            check_updates()
            request_update_status()
            exit(0)
        if args.command == "cli":
            # default to --all if none specified
            if not (args.system or args.flatpak or args.all):
                args.all = True

            do_system = args.system or args.all
            do_flatpak = args.flatpak or args.all

            check_repos()
            check_updates()

            # Only run fixups if we're doing system (fixups are system/RPM-oriented)
            if do_system:
                install_fixups()

            if do_system:
                install_system_updates_only()

            if do_flatpak:
                install_flatpak_updates_only()

            check_updates()
            request_update_status()
            exit(0)
        if args.command == "install-codecs":
            prompt_media_fixup()
            exit(0)
        if args.command == "install-fixups":
            check_updates()
            install_fixups()
            check_updates()
            request_update_status()
            exit(0)
        if args.command == "repair":
            check_updates()
            attempt_distro_sync()
            check_updates()
            request_update_status()
            exit(0)
        if args.command == "check-updates":
            check_updates()
            request_update_status()
            exit(0)
        if args.command == "check-repos":
            check_repos()
            exit(0)
        if args.command and os.geteuid() == 0:
            initialize_logging()
            logger.info("Running CLI mode...")
    elif "DISPLAY" in os.environ and os.geteuid() == 0:
        try:
            if not Gtk.init_check():
                logger.error("Failed to initialize GTK")
                return 1
            update_window = UpdateWindow()
            update_window.connect("destroy", Gtk.main_quit)
            update_window.show_all()
            update_window.present()  # Ensure the window is presented in a normal state
            Gtk.main()
        except RuntimeError as e:
            logger.error(f"GTK initialization error: {e}")
    else:
        logger.info(
            "No valid options provided. Use -h or --help for usage information."
        )
        exit(1)


class UpdateWindow(Gtk.Window):  # type: ignore[misc]
    def __init__(self) -> None:
        super().__init__(title="Update System")
        if os.geteuid() == 0:
            if is_running_with_sudo_or_pkexec() == 1:
                sudo_user = os.environ.get('SUDO_USER', '')
                if sudo_user and not sudo_user.isdigit():
                    try:
                        orig_user_uid = pwd.getpwnam(sudo_user).pw_uid
                        os.environ['ORIG_USER'] = str(orig_user_uid)

                        original_user_home = pwd.getpwnam(sudo_user).pw_dir
                        os.environ['ORIGINAL_USER_HOME'] = str(original_user_home)
                    except KeyError:
                        print(f"User {sudo_user} not found")

            # Get the original user's UID and GID to pass to root mode
            self.orig_user = os.environ.get("ORIG_USER")
            self.orig_user_uid = os.getuid() if self.orig_user is None else int(self.orig_user)
            self.pw_record = pwd.getpwuid(self.orig_user_uid)
            self.orig_user_gid = self.pw_record.pw_gid
            self.perform_kernel_actions = perform_kernel_actions
            self.perform_reboot_request = perform_reboot_request
            self.fp_system_updates: list[Flatpak.Ref] | None = None
            self.fp_user_updates: list[Flatpak.Ref] | None = None

        settings = Gtk.Settings.get_default()
        settings.set_property("gtk-theme-name", "adw-gtk3-dark")  # Replace with the exact theme name if different
        settings.set_property("gtk-application-prefer-dark-theme", True)


        self.main_context = GLib.MainContext.default()
        self.set_border_width(10)
        self.set_default_size(900, 900)  # Set default window size

        # Create the notices text view and its scrolled window
        self.notices_textview = Gtk.TextView()
        self.notices_textview.set_editable(False)
        self.notices_textview.set_wrap_mode(Gtk.WrapMode.WORD)
        notices_scrolled_window = Gtk.ScrolledWindow()
        notices_scrolled_window.set_policy(
            Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC
        )  # Disable horizontal scrolling, enable vertical scrolling
        notices_scrolled_window.add(self.notices_textview)
        notices_scrolled_window.set_size_request(300, 150)
        notices_scrolled_window.set_vexpand(True)  # Allow vertical expansion

        # Create the update text view and its scrolled window
        self.update_textview = Gtk.TextView()
        self.update_textview.set_editable(False)
        self.update_textview.set_wrap_mode(Gtk.WrapMode.WORD)
        update_scrolled_window = Gtk.ScrolledWindow()
        update_scrolled_window.set_policy(
            Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC
        )
        update_scrolled_window.add(self.update_textview)
        update_scrolled_window.set_size_request(300, 150)
        update_scrolled_window.set_vexpand(True)

        # Create the status text view and its scrolled window
        self.status_textview = Gtk.TextView()
        self.status_textview.set_editable(False)
        self.status_textview.set_wrap_mode(Gtk.WrapMode.WORD)
        status_scrolled_window = Gtk.ScrolledWindow()
        status_scrolled_window.set_policy(
            Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC
        )  # Disable horizontal scrolling, enable vertical scrolling
        status_scrolled_window.add(self.status_textview)
        status_scrolled_window.set_hexpand(
            True
        )  # Make the status_textview take the remaining 3/4 of the width
        status_scrolled_window.set_vexpand(True)  # Allow vertical expansion

        # Connect to the insert-text signal to auto-scroll
        status_buffer = self.status_textview.get_buffer()
        status_buffer.connect("insert-text", self.on_status_text_inserted)

        # Create the flatpak user updates text view and its scrolled window
        self.flatpak_user_textview = Gtk.TextView()
        self.flatpak_user_textview.set_editable(False)
        self.flatpak_user_textview.set_wrap_mode(Gtk.WrapMode.WORD)
        flatpak_user_scrolled_window = Gtk.ScrolledWindow()
        flatpak_user_scrolled_window.set_policy(
            Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC
        )  # Disable horizontal scrolling, enable vertical scrolling
        flatpak_user_scrolled_window.add(self.flatpak_user_textview)
        flatpak_user_scrolled_window.set_size_request(300, 150)
        flatpak_user_scrolled_window.set_vexpand(True)  # Allow vertical expansion

        # Create the flatpak system updates text view and its scrolled window
        self.flatpak_system_textview = Gtk.TextView()
        self.flatpak_system_textview.set_editable(False)
        self.flatpak_system_textview.set_wrap_mode(Gtk.WrapMode.WORD)
        flatpak_system_scrolled_window = Gtk.ScrolledWindow()
        flatpak_system_scrolled_window.set_policy(
            Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC
        )  # Disable horizontal scrolling, enable vertical scrolling
        flatpak_system_scrolled_window.add(self.flatpak_system_textview)
        flatpak_system_scrolled_window.set_size_request(300, 150)
        flatpak_system_scrolled_window.set_vexpand(True)  # Allow vertical expansion

        # Create labels for the text views
        update_label = Gtk.Label(label="System Updates:")
        update_label.set_halign(Gtk.Align.START)  # Align label to the left
        self.status_label = Gtk.Label(label="STATUS:")
        self.status_label.set_halign(Gtk.Align.START)  # Align label to the left
        flatpak_user_label = Gtk.Label(label="Flatpak User Updates:")
        flatpak_user_label.set_halign(Gtk.Align.START)  # Align label to the left
        flatpak_system_label = Gtk.Label(label="Flatpak System Updates:")
        flatpak_system_label.set_halign(Gtk.Align.START)  # Align label to the left

        # Create the new updates.nobaraproject.org text view and its scrolled window
        self.nobara_notices_textview = Gtk.TextView()
        self.nobara_notices_textview.set_editable(False)
        self.nobara_notices_textview.set_wrap_mode(Gtk.WrapMode.WORD)
        nobara_notices_scrolled_window = Gtk.ScrolledWindow()
        nobara_notices_scrolled_window.set_policy(
            Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC
        )
        nobara_notices_scrolled_window.add(self.nobara_notices_textview)
        nobara_notices_scrolled_window.set_hexpand(True)
        nobara_notices_scrolled_window.set_vexpand(True)

        # Create label for the nobara updates
        nobara_notices_label = Gtk.Label(label="Important Notices:")
        nobara_notices_label.set_halign(Gtk.Align.START)

        # Create the button to install updates
        self.install_button = Gtk.Button(label="No System Updates Available")
        GLib.idle_add(button_ensure_sensitivity, self.install_button, False)
        self.install_button.connect("clicked", self.on_install_system_button_clicked)

        self.install_flatpak_button = Gtk.Button(label="No Flatpak Updates Available")
        GLib.idle_add(button_ensure_sensitivity, self.install_flatpak_button, False)
        self.install_flatpak_button.connect("clicked", self.on_install_flatpak_button_clicked)

        # Create the button to refresh updates
        self.check_updates_button = Gtk.Button(label="Check for Updates/Fixups")
        self.check_updates_button.connect(
            "clicked", self.on_check_updates_button_clicked
        )

        # Create the button to install fixups
        self.fixups_button = Gtk.Button(label="No Fixups Available")
        GLib.idle_add(button_ensure_sensitivity, self.fixups_button, False)
        self.fixups_button.connect("clicked", self.on_fixups_button_clicked)

        # Create the button to open the log file
        self.open_log_button = Gtk.Button(label="Open Log File")
        self.open_log_button.connect("clicked", self.on_open_log_button_clicked)

        # Create the button to open the log directory
        self.open_log_button_dir = Gtk.Button(label="Open Log Directory")
        self.open_log_button_dir.connect("clicked", self.on_open_log_button_dir_clicked)

        # Create the button to open the package manager
        self.open_package_man_button = Gtk.Button(label="Open Package Manager")
        self.open_package_man_button.connect("clicked", self.on_open_package_man_button_clicked)

        # Create the button to open the package manager
        self.open_repair_button = Gtk.Button(label="Repair System Packages")
        self.open_repair_button.connect("clicked", self.on_repair_button_clicked)

        # Create a grid to arrange the labels, text views
        grid = Gtk.Grid()
        grid.set_column_spacing(6)
        grid.set_row_spacing(6)

        # Attach labels and text views to the grid
        grid.attach(nobara_notices_label, 0, 0, 3, 1)  # Span all columns
        grid.attach(nobara_notices_scrolled_window, 0, 1, 3, 1)  # Span all columns

        # Attach labels and text views to the grid
        grid.attach(update_label, 0, 2, 1, 1)  # Column 0, Row 0
        grid.attach(flatpak_user_label, 1, 2, 1, 1)  # Column 1, Row 0
        grid.attach(flatpak_system_label, 2, 2, 1, 1)  # Column 2, Row 0

        grid.attach(update_scrolled_window, 0, 3, 1, 1)  # Column 0, Row 1
        grid.attach(flatpak_user_scrolled_window, 1, 3, 1, 1)  # Column 1, Row 1
        grid.attach(flatpak_system_scrolled_window, 2, 3, 1, 1)  # Column 2, Row 1

        grid.attach(self.status_label, 0, 4, 3, 1)  # Column 0, Row 2
        grid.attach(
            status_scrolled_window, 0, 5, 3, 1
        )  # Column 0, Row 3, spanning 3 columns

        grid.attach(self.install_button, 0, 6, 3, 1)
        grid.attach(self.install_flatpak_button, 0, 7, 3, 1)

        grid.attach(self.fixups_button, 0, 8, 3, 1)
        grid.attach(self.check_updates_button, 0, 9, 3, 1)
        grid.attach(self.open_repair_button, 0, 10, 3, 1)
        grid.attach(self.open_log_button, 0, 11, 3, 1)
        grid.attach(self.open_log_button_dir, 0, 12, 3, 1)
        grid.attach(self.open_package_man_button, 0, 13, 3, 1)
        self.add(grid)

        # Initialize the logger
        self.logger = initialize_logging(self.status_textview)

        # Add method to update the nobara updates text view
        self.update_nobara_notices()

        logger.info("Running GUI mode...")
        updater_thread = threading.Thread(target=self.run_updater)
        updater_thread.start()

    def on_install_system_button_clicked_async(self):
        toggle_refresh()
        GLib.idle_add(self.toggle_buttons_during_refresh)
        self.status_label_updates("Starting SYSTEM package updates, please do not turn off your computer...")
        self.textview_updates()
        install_system_updates_only()
        self.textview_updates()
        self.status_label_updates("System updates complete!")
        toggle_refresh()
        GLib.idle_add(self.toggle_buttons_during_refresh)
        request_update_status()

    def on_install_system_button_clicked(self, widget):
        threading.Thread(target=self.on_install_system_button_clicked_async).start()

    def on_install_flatpak_button_clicked_async(self):
        toggle_refresh()
        GLib.idle_add(self.toggle_buttons_during_refresh)
        self.status_label_updates("Starting FLATPAK updates, please do not turn off your computer...")
        self.textview_updates()
        install_flatpak_updates_only()
        self.textview_updates()
        self.status_label_updates("Flatpak updates complete!")
        toggle_refresh()
        GLib.idle_add(self.toggle_buttons_during_refresh)
        request_update_status()

    def on_install_flatpak_button_clicked(self, widget):
        threading.Thread(target=self.on_install_flatpak_button_clicked_async).start()


    def update_nobara_notices(self):
        try:
            response = requests.get("https://updates.nobaraproject.org/updates.txt", timeout=5)
            if response.status_code == 200:
                content = response.text
                buffer = self.nobara_notices_textview.get_buffer()
                buffer.set_text(content)

                # Log the content with a clear separator
                logger.info("\n" + "="*50)
                logger.info("Important Notices:")
                logger.info("="*50)
                logger.info(content)
                logger.info("="*50)
            else:
                error_message = f"Failed to fetch updates.nobaraproject.org/updates.txt (Status code: {response.status_code})"
                buffer = self.nobara_notices_textview.get_buffer()
                buffer.set_text(error_message)
                logger.error(error_message)
        except Exception as e:
            error_message = f"Error fetching updates: {str(e)}"
            buffer = self.nobara_notices_textview.get_buffer()
            buffer.set_text(error_message)
            logger.error(error_message)

    def textview_updates(self) -> None:
        result = check_updates(return_texts=True)
        if result is None:
            result = "", "", ""
        sys_update_text, fp_user_update_text, fp_sys_update_text = result

        # Function to clear and insert text into a buffer
        def clear_and_insert_text(buffer, text):
            buffer.set_text("")  # Clear the buffer
            if text:
                buffer.insert(buffer.get_end_iter(), text + "\n")

        if result:
            # Clear and insert text into the update buffer
            update_buffer = self.update_textview.get_buffer()
            GLib.idle_add(clear_and_insert_text, update_buffer, sys_update_text)

            # Clear and insert text into the flatpak user buffer
            user_buffer = self.flatpak_user_textview.get_buffer()
            GLib.idle_add(clear_and_insert_text, user_buffer, fp_user_update_text)

            # Clear and insert text into the flatpak system buffer
            system_buffer = self.flatpak_system_textview.get_buffer()
            GLib.idle_add(clear_and_insert_text, system_buffer, fp_sys_update_text)

    def status_label_updates(self, message: str) -> None:
        GLib.idle_add(
            self.status_label.set_label,
            f"STATUS: {message}",
        )


    def on_install_button_clicked_async(self):
        toggle_refresh()
        GLib.idle_add(self.toggle_buttons_during_refresh)
        self.status_label_updates("Starting package updates, please do not turn off your computer...")
        self.textview_updates()
        install_updates()
        self.textview_updates()
        self.status_label_updates("All Updates complete!")
        toggle_refresh()
        GLib.idle_add(self.toggle_buttons_during_refresh)
        request_update_status()

    def on_install_button_clicked(self, widget):
        threading.Thread(target=self.on_install_button_clicked_async).start()

    def on_check_updates_button_clicked_async(self):
        toggle_refresh()
        GLib.idle_add(self.toggle_buttons_during_refresh)
        self.textview_updates()
        install_fixups()
        self.textview_updates()
        toggle_refresh()
        GLib.idle_add(self.toggle_buttons_during_refresh)
        request_update_status()

    def on_check_updates_button_clicked(self, widget):
        threading.Thread(target=self.on_check_updates_button_clicked_async).start()

    def on_fixups_updates_button_clicked_async(self):
        toggle_refresh()
        GLib.idle_add(self.toggle_buttons_during_refresh)
        self.status_label_updates("Checking for various known problems to repair, please do not turn off your computer...")
        self.textview_updates()
        install_fixups()
        self.textview_updates()
        toggle_refresh()
        GLib.idle_add(self.toggle_buttons_during_refresh)
        request_update_status()
        self.status_label_updates("Finished known problem checking and repair")

    def on_fixups_button_clicked(self, widget):
        threading.Thread(target=self.on_fixups_updates_button_clicked_async).start()

    def on_open_log_button_clicked(self, widget):
        threading.Thread(target=self.button_popen_async, args=("log_file",)).start()

    def on_open_log_button_dir_clicked(self, widget):
        threading.Thread(target=self.button_popen_async, args=("log_dir",)).start()

    def on_open_package_man_button_clicked(self, widget):
        threading.Thread(target=self.button_popen_async, args=("pac_man",)).start()

    def on_repair_button_clicked(self, widget):
        threading.Thread(target=self.on_repair_button_clicked_async).start()

    def on_repair_button_clicked_async(self):
        toggle_refresh()
        GLib.idle_add(self.toggle_buttons_during_refresh)
        self.status_label_updates("Attempting repair using distro-sync...")
        self.textview_updates()
        attempt_distro_sync()
        self.textview_updates()
        self.status_label_updates("Process complete!")
        toggle_refresh()
        GLib.idle_add(self.toggle_buttons_during_refresh)
        request_update_status()

    def button_popen_async(self, option: str) -> None:
        run_as_user(
            self.orig_user_uid, self.orig_user_gid, "on_button_popen_async", option
        )

    def on_status_text_inserted(self, buffer, iter, text, length):
        # Create a mark at the end of the buffer
        mark = buffer.create_mark(None, buffer.get_end_iter(), False)
        # Scroll to the mark after the text has been inserted
        GLib.idle_add(self.status_textview.scroll_to_mark, mark, 0.0, True, 0.0, 1.0)

    def toggle_buttons_during_refresh(self):
        if get_refresh() == 1:
            GLib.idle_add(button_ensure_sensitivity, self.check_updates_button, False)
            GLib.idle_add(
                self.check_updates_button.set_label, "Performing tasks, please wait..."
            )

            # System updates button (renamed behavior)
            GLib.idle_add(button_ensure_sensitivity, self.install_button, False)
            GLib.idle_add(
                self.install_button.set_label, "Performing tasks, please wait..."
            )

            # Flatpak updates button (NEW)
            GLib.idle_add(button_ensure_sensitivity, self.install_flatpak_button, False)
            GLib.idle_add(
                self.install_flatpak_button.set_label, "Performing tasks, please wait..."
            )

            GLib.idle_add(button_ensure_sensitivity, self.fixups_button, False)
            GLib.idle_add(
                self.fixups_button.set_label, "Performing tasks, please wait..."
            )

            GLib.idle_add(button_ensure_sensitivity, self.open_repair_button, False)
            GLib.idle_add(
                self.open_repair_button.set_label, "Performing tasks, please wait..."
            )

        else:
            GLib.idle_add(button_ensure_sensitivity, self.check_updates_button, True)
            GLib.idle_add(
                self.check_updates_button.set_label, "Check for Updates/Fixups"
            )

            GLib.idle_add(button_ensure_sensitivity, self.open_repair_button, True)
            GLib.idle_add(self.open_repair_button.set_label, "Repair")

            # System install button
            if get_system_updates_available() == 1:
                GLib.idle_add(button_ensure_sensitivity, self.install_button, True)
                GLib.idle_add(self.install_button.set_label, "Install System Updates")
            else:
                GLib.idle_add(button_ensure_sensitivity, self.install_button, False)
                GLib.idle_add(self.install_button.set_label, "No System Updates Available")

            # Flatpak install button
            if get_flatpak_updates_available() == 1:
                GLib.idle_add(button_ensure_sensitivity, self.install_flatpak_button, True)
                GLib.idle_add(self.install_flatpak_button.set_label, "Install Flatpak Updates")
            else:
                GLib.idle_add(button_ensure_sensitivity, self.install_flatpak_button, False)
                GLib.idle_add(self.install_flatpak_button.set_label, "No Flatpak Updates Available")

            # Fixups button remains based on fixups availability
            if get_fixups_available() == 1:
                GLib.idle_add(button_ensure_sensitivity, self.fixups_button, True)
                GLib.idle_add(self.fixups_button.set_label, "Install Fixups")
            else:
                GLib.idle_add(button_ensure_sensitivity, self.fixups_button, False)
                GLib.idle_add(self.fixups_button.set_label, "No Fixups Available")


    def run_updater(self) -> None:
        toggle_refresh() # turn on perform-task toggle
        GLib.idle_add(self.toggle_buttons_during_refresh) # disable buttons
        check_repos()
        self.status_label_updates("Checking for various known problems to repair, please do not turn off your computer...")
        self.textview_updates()
        install_fixups()
        self.textview_updates()
        toggle_refresh() # turn  off perform-task toggle
        GLib.idle_add(self.toggle_buttons_during_refresh) # enable buttons
        request_update_status()
        self.status_label_updates("Finished known problem checking and repair")

if __name__ == "__main__":
    try:
        # Your main application code here
        main()
    finally:
        # This ensures cleanup runs even if main() throws an exception
        cleanup_xhost()
