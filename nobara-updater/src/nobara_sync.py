#!/usr/bin/python3
import argparse
import html
import importlib
import json
import logging
import multiprocessing
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
import requests
from nobara_updater.quirks import QuirkFixup  # type: ignore[import]
from nobara_updater.run_as import run_as_user

gi.require_version("Gtk", "3.0")
gi.require_version("GLib", "2.0")
gi.require_version("Flatpak", "1.0")

from typing import Any

from gi.repository import Flatpak, GLib, Gtk  # type: ignore[import]
from yumex.constants import BACKEND  # type: ignore[import]

if BACKEND == "DNF5":
    from nobara_updater.dnf5 import (  # type: ignore[import]
        AttributeDict,
        PackageUpdater,
        repoindex,
        updatechecker,
    )
else:
    from nobara_updater.dnf4 import (  # type: ignore[import]
        AttributeDict,
        PackageUpdater,
        repoindex,
        updatechecker,
    )

package_names = updatechecker()

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
        response = session.get(mirrorlist_url, headers=headers)
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
    global package_names

    updates_available = 0

    sys_update_text = None
    fp_user_update_text = None
    fp_sys_update_text = None

    # Get our system updates
    package_names = updatechecker()
    if package_names:
        updates_available = 1
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
        raise ValueError("ORIG_USER environment variable is not set.")
    orig_user_uid = int(orig_user)
    pw_record = pwd.getpwuid(orig_user_uid)
    orig_user_gid = pw_record.pw_gid

    # Flatpak User Updates window
    fp_user_updates = run_as_user(
        orig_user_uid, orig_user_gid, "fp_get_user_updates"
    )
    if fp_user_updates:
        updates_available = 1
        fp_user_update_text = "\n".join(fp_user_updates)

    # Flatpak System Updates window
    fp_system_updates = fp_get_system_updates()
    if fp_system_updates:
        updates_available = 1
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
    system_installation = Flatpak.Installation.new_system(None)
    flatpak_system_updates = system_installation.list_installed_refs_for_update(None)
    del system_installation
    if flatpak_system_updates != []:
        return flatpak_system_updates
    return []


def install_system_flatpak_updates() -> None:
    # System installation updates
    system_installation = Flatpak.Installation.new_system(None)
    flatpak_sys_updates = system_installation.list_installed_refs_for_update(None)
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
    logger.info("Running quirk fixup -- first pass")
    quirk_fixup = QuirkFixup(logger)
    (
        perform_kernel_actions,
        perform_reboot_request,
        fixups_available,
        perform_refresh,
    ) = quirk_fixup.system_quirk_fixup()

    # Perform final refresh after making core fixes before updating the rest of the packages.
    if perform_refresh == 1:
        try:
            logger.info("Running quirk fixup -- second pass")
            # Reload the quirks module to apply any changes made to quirks.py
            import nobara_updater.quirks

            importlib.reload(nobara_updater.quirks)

            # Create a new instance of QuirkFixup after reloading the module
            quirk_fixup = nobara_updater.quirks.QuirkFixup(logger)
            (
                perform_kernel_actions,
                perform_reboot_request,
                fixups_available,
                perform_refresh,
            ) = quirk_fixup.system_quirk_fixup()
        except Exception as e:
            logger.error("Error during install_fixups: %s", e)

    # Check for media codec fixup first
    if fixups_available == 1:
        logger.info("Problems with Media Packages detected, asking user for repair...")
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
        raise ValueError("ORIG_USER environment variable is not set.")
    orig_user_uid = int(orig_user)
    pw_record = pwd.getpwuid(orig_user_uid)
    orig_user_gid = pw_record.pw_gid

    # Send update refresh request to systray service
    run_as_user(
        orig_user_uid, orig_user_gid, "yumex_sync_updates"
    )

def install_updates() -> None:
    global perform_kernel_actions
    global perform_reboot_request
    global package_names

    logger.info("Starting package updates, please do not turn off your computer...\n")
    action = "upgrade"
    if package_names:
        # Now update our system packages
        PackageUpdater(package_names, action, None, logger)
    # Perform akmods and dracut if kmods or kernel were updated.
    if perform_kernel_actions == 1:
        logger.info(
            "Kernel or kernel module updates were performed. Running required 'akmods' and 'dracut -f'...\n"
        )

        # Run the commands
        subprocess.run(["akmods"], check=True)
        subprocess.run(["dracut", "-f"], check=True)

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
        raise ValueError("ORIG_USER environment variable is not set.")
    orig_user_uid = int(orig_user)
    pw_record = pwd.getpwuid(orig_user_uid)
    orig_user_gid = pw_record.pw_gid

    # Now update our flatpaks
    # system
    install_system_flatpak_updates()
    # user
    run_as_user(
        orig_user_uid, orig_user_gid, "install_user_flatpak_updates"
    )

    # Send update refresh request to systray service
    run_as_user(
        orig_user_uid, orig_user_gid, "yumex_sync_updates"
    )

    # Remove newinstall needs-update tracker
    if Path.exists(Path("/etc/nobara/newinstall")):
        try:
            # Remove the file
            Path("/etc/nobara/newinstall").unlink()
        except OSError as e:
            logger.error("Error: %s", e.strerror)

    if perform_reboot_request == 1:
        logger.info("Kernel, kernel module, or desktop compositor update performed. Reboot required.")
        prompt_reboot()


def prompt_media_fixup() -> None:
    global media_fixup_event
    media_fixup_event.set()
    if is_running_with_sudo_or_pkexec() == 2:
        dialog = Gtk.MessageDialog(
            modal=True,
            destroy_with_parent=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text="We have found some missing optional video and/or audio codec packages. Do you want to install them now (recommended)?",
        )
        response = dialog.run()
        dialog.destroy()  # Close the dialog immediately after getting the response

        if response == Gtk.ResponseType.YES:
            logger.info("User chose to repair media packages.")
            media_fixup()
            media_fixup_event.wait()
        else:
            logger.info("User chose not to repair media packages.")
    else:
        while True:
            response = (
                input(
                    "We have found some missing optional video and/or audio codec packages. Do you want to install them now (recommended)? (yes/no): "
                )
                .strip()
                .lower()
            )
            if response in ["yes", "y"]:
                logger.info("User chose to repair media packages.")
                media_fixup()
                media_fixup_event.wait()
                break
            if response in ["no", "n"]:
                logger.info("User chose not to repair media packages.")
                break
            logger.error("Invalid input. Please enter 'y' or 'yes' or 'n' or 'no'.")

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
        "mesa-va-drivers.x86_64",
        "mesa-vdpau-drivers.x86_64",
        "mesa-va-drivers-freeworld.x86_64",
        "mesa-vdpau-drivers-freeworld.x86_64",
        "noopenh264.x86_64",
        "noopenh264.i686",
        "x264.x86_64",
        "x264-libs.x86_64",
        "x264-libs.i686",
        "x265.x86_64",
        "x265-libs.x86_64",
        "x265-libs.i686",
    ]
    soft_removal = ["mozilla-openh264", "qt5-qtwebengine-freeworld"]

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

    install = [
        "mesa-va-drivers-freeworld.x86_64",
        "mesa-vdpau-drivers-freeworld.x86_64",
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
        "mozilla-openh264.x86_64",
        "x264-libs.x86_64",
        "x264-libs.i686",
        "x265-libs.x86_64",
        "x265-libs.i686",
        "libavcodec-freeworld.x86_64",
        "libavcodec-freeworld.i686",
    ]

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
    fixups_available = 0

def prompt_reboot() -> None:
    if "gamescope" in os.environ.get('XDG_CURRENT_DESKTOP', '').lower():
        subprocess.run(["reboot"], check=True)
    if is_running_with_sudo_or_pkexec() == 2:
        dialog = Gtk.MessageDialog(
            modal=True,
            destroy_with_parent=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text="System updates require a reboot. Do you want to reboot now?",
        )
        response = dialog.run()
        dialog.destroy()  # Close the dialog immediately after getting the response

        if response == Gtk.ResponseType.YES:
            logger.info("User chose to reboot the system.")
            subprocess.run(["reboot"], check=True)
        else:
            logger.info("User chose to reboot later.")
    else:
        while True:
            response = (
                input(
                    "System updates require a reboot. Do you want to reboot now? (yes/no): "
                )
                .strip()
                .lower()
            )
            if response in ["yes", "y"]:
                logger.info("User chose to reboot the system.")
                subprocess.run(["reboot"], check=True)
                break
            if response in ["no", "n"]:
                logger.info("User chose to reboot later.")
                break
            logger.error("Invalid input. Please enter 'y' or 'yes' or 'n' or 'no'.")

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update System")

    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser(
        "install-updates",
        help="Performs check-updates, install-fixups, then installs any updates available.",
    )
    subparsers.add_parser("check-updates", help="Check for new updates and fixups.")
    subparsers.add_parser(
        "install-fixups", help="Performs a series of known problem fixes."
    )
    cli_parser = subparsers.add_parser("cli", help="Run in CLI mode, defaults to install-updates")
    cli_parser.add_argument("username", help="Specify the username", nargs='?')  # Optional positional argument
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

            # Check if the command line contains /usr/bin/nobara-updater-gamescope-gui
            if any("/usr/bin/nobara-updater-gamescope-gui" in arg for arg in parent_cmdline):
                # Get user information using the pwd module
                user_info = pwd.getpwnam(args.username)
                os.environ['ORIGINAL_USER_HOME'] = user_info.pw_dir  # User's home directory
                os.environ['ORIG_USER'] = str(user_info.pw_uid)      # User's UID
                os.environ['SUDO_USER'] = str(user_info.pw_uid)      # User's UID (as SUDO_USER)
                os.environ['XDG_CURRENT_DESKTOP'] = "gamescope"      # User's UID (as SUDO_USER)
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

    logger.info("Finished known problem checking and repair")

    # Check the same flag once more in case they declined prompt_media_fixup
    # Use else statement this time to finalize the check
    if fixups_available == 1:
        have_updates = 1
        logger.info("Fixups Available.")
    else:
        logger.info("No Fixups Available.")

    if updates_available == 1:
        have_updates = 1
        logger.info("Updates Available.")
    else:
        logger.info("No Updates Available.")

    if have_updates != 1:
        logger.info("All Updates complete!")


def main() -> None:

    args = parse_args()
    check_root_privileges(args)

    if args.command and os.geteuid() == 0:
        initialize_logging()
        logger.info("Running CLI mode...")
        if args.command == "install-updates" or args.command == "cli":
            check_repos()
            check_updates()
            install_fixups()
            install_updates()
            check_updates()
            request_update_status()
            exit(0)
        if args.command == "install-fixups":
            check_updates()
            install_fixups()
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
        update_window = UpdateWindow()
        update_window.connect("destroy", Gtk.main_quit)
        update_window.show_all()
        update_window.present()  # Ensure the window is presented in a normal state
        Gtk.main()
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

        self.main_context = GLib.MainContext.default()
        self.set_border_width(10)
        self.set_default_size(900, 600)  # Set default window size

        # Create the update text view and its scrolled window
        self.update_textview = Gtk.TextView()
        self.update_textview.set_editable(False)
        self.update_textview.set_wrap_mode(Gtk.WrapMode.WORD)
        update_scrolled_window = Gtk.ScrolledWindow()
        update_scrolled_window.set_policy(
            Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC
        )  # Disable horizontal scrolling, enable vertical scrolling
        update_scrolled_window.add(self.update_textview)
        update_scrolled_window.set_size_request(300, 150)
        update_scrolled_window.set_vexpand(True)  # Allow vertical expansion

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

        # Create the button to install updates
        self.install_button = Gtk.Button(label="No Updates Available")
        GLib.idle_add(button_ensure_sensitivity, self.install_button, False)
        self.install_button.connect("clicked", self.on_install_button_clicked)

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

        # Create a grid to arrange the labels, text views
        grid = Gtk.Grid()
        grid.set_column_spacing(6)
        grid.set_row_spacing(6)

        # Attach labels and text views to the grid
        grid.attach(update_label, 0, 0, 1, 1)  # Column 0, Row 0
        grid.attach(flatpak_user_label, 1, 0, 1, 1)  # Column 1, Row 0
        grid.attach(flatpak_system_label, 2, 0, 1, 1)  # Column 2, Row 0

        grid.attach(update_scrolled_window, 0, 1, 1, 1)  # Column 0, Row 1
        grid.attach(flatpak_user_scrolled_window, 1, 1, 1, 1)  # Column 1, Row 1
        grid.attach(flatpak_system_scrolled_window, 2, 1, 1, 1)  # Column 2, Row 1

        grid.attach(self.status_label, 0, 2, 3, 1)  # Column 0, Row 2
        grid.attach(
            status_scrolled_window, 0, 3, 3, 1
        )  # Column 0, Row 3, spanning 3 columns

        # Add the buttons to the grid
        # Column 0, Row 4, spanning 3 columns
        grid.attach(
            self.install_button, 0, 4, 3, 1
        )
        # Column 0, Row 5, spanning 3 columns
        grid.attach(
            self.fixups_button, 0, 5, 3, 1
        )
        # Column 0, Row 6, spanning 3 columns
        grid.attach(
            self.check_updates_button, 0, 6, 3, 1
        )
        # Column 0, Row 7, spanning 3 columns
        grid.attach(self.open_log_button, 0, 7, 3, 1)
        # Column 0, Row 8, spanning 3 columns
        grid.attach(
            self.open_log_button_dir, 0, 8, 3, 1
        )
        # Column 0, Row 9, spanning 3 columns
        grid.attach(
            self.open_package_man_button, 0, 9, 3, 1
        )

        self.add(grid)

        # Initialize the logger
        self.logger = initialize_logging(self.status_textview)

        logger.info("Running GUI mode...")
        updater_thread = threading.Thread(target=self.run_updater)
        updater_thread.start()

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
            GLib.idle_add(
                button_ensure_sensitivity, self.check_updates_button, False
            )
            GLib.idle_add(
                self.check_updates_button.set_label, "Performing tasks, please wait..."
            )
            GLib.idle_add(button_ensure_sensitivity, self.install_button, False)
            GLib.idle_add(
                self.install_button.set_label, "Performing tasks, please wait..."
            )
            GLib.idle_add(button_ensure_sensitivity, self.fixups_button, False)
            GLib.idle_add(
                self.fixups_button.set_label, "Performing tasks, please wait..."
            )
        else:
            GLib.idle_add(
                button_ensure_sensitivity, self.check_updates_button, True
            )
            GLib.idle_add(self.check_updates_button.set_label, "Check for Updates/Fixups")
            if get_updates_available() == 1:
                GLib.idle_add(button_ensure_sensitivity, self.install_button, True)
                GLib.idle_add(self.install_button.set_label, "Install Updates")
            else:
                GLib.idle_add(button_ensure_sensitivity, self.install_button, False)
                GLib.idle_add(self.install_button.set_label, "No Updates Available")
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
    main()
