import os
import pwd
import subprocess

import gi  # type: ignore[import]

gi.require_version("Flatpak", "1.0")

from pathlib import Path
from typing import Any

from gi.repository import Flatpak  # type: ignore[import]


def fp_get_user_updates(
    uid: int, gid: int, log_queue: Any, update_queue: Any
) -> list[str]:

    # Get the user's home directory and other details
    pw_record = pwd.getpwuid(uid)
    user_home = Path(pw_record.pw_dir)
    # Update environment variables
    os.environ["HOME"] = str(user_home)
    os.environ["USER"] = pw_record.pw_name
    os.environ["LOGNAME"] = pw_record.pw_name
    os.environ["SHELL"] = pw_record.pw_shell
    os.environ["XDG_CACHE_HOME"] = str(user_home / ".cache")
    os.environ["XDG_CONFIG_HOME"] = str(user_home / ".config")
    os.environ["XDG_DATA_HOME"] = str(user_home / ".local" / "share")
    os.environ["XDG_RUNTIME_DIR"] = f"/run/user/{uid}"

    # Add Flatpak export directory to XDG_DATA_DIRS
    flatpak_export_dir = (
        user_home / ".local" / "share" / "flatpak" / "exports" / "share"
    )
    os.environ["XDG_DATA_DIRS"] = (
        f"{flatpak_export_dir}:{os.environ.get('XDG_DATA_DIRS', '/usr/local/share:/usr/share')}"
    )

    # Ensure the runtime directory exists
    runtime_dir = Path(os.environ["XDG_RUNTIME_DIR"])
    if not runtime_dir.exists():
        runtime_dir.mkdir(parents=True)
        os.chown(runtime_dir, uid, gid)

    # Change working directory to the user's home directory
    os.chdir(user_home)

    # Get our flatpak updates
    user_installation = Flatpak.Installation.new_user(None)
    flatpak_user_updates = user_installation.list_installed_refs_for_update(None)

    # Convert InstalledRef objects to a list of strings
    update_list = [
        fp_user_update.get_appdata_name()
        for fp_user_update in flatpak_user_updates
        if fp_user_update.get_appdata_name()
    ]
    del user_installation
    if update_list:
        return update_list
    return []


def install_user_flatpak_updates(
    uid: int, gid: int, log_queue: Any, update_queue: Any
) -> None:
    # Get the user's home directory and other details
    pw_record = pwd.getpwuid(uid)
    user_home = Path(pw_record.pw_dir)

    # Update environment variables
    os.environ["HOME"] = str(user_home)
    os.environ["USER"] = pw_record.pw_name
    os.environ["LOGNAME"] = pw_record.pw_name
    os.environ["SHELL"] = pw_record.pw_shell
    os.environ["XDG_CACHE_HOME"] = str(user_home / ".cache")
    os.environ["XDG_CONFIG_HOME"] = str(user_home / ".config")
    os.environ["XDG_DATA_HOME"] = str(user_home / ".local" / "share")
    os.environ["XDG_RUNTIME_DIR"] = f"/run/user/{uid}"

    # Add Flatpak export directory to XDG_DATA_DIRS
    flatpak_export_dir = (
        user_home / ".local" / "share" / "flatpak" / "exports" / "share"
    )
    os.environ["XDG_DATA_DIRS"] = (
        f"{flatpak_export_dir}:{os.environ.get('XDG_DATA_DIRS', '/usr/local/share:/usr/share')}"
    )

    # Ensure the runtime directory exists
    runtime_dir = Path(os.environ["XDG_RUNTIME_DIR"])
    if not runtime_dir.exists():
        runtime_dir.mkdir(parents=True)
        os.chown(runtime_dir, uid, gid)

    # Change working directory to the user's home directory
    os.chdir(user_home)

    # User installation updates
    user_installation = Flatpak.Installation.new_user(None)
    flatpak_user_updates = user_installation.list_installed_refs_for_update(None)

    if flatpak_user_updates:
        transaction = Flatpak.Transaction.new_for_installation(user_installation)
        for ref in flatpak_user_updates:
            try:
                appdata_name = ref.get_appdata_name()
                if appdata_name:
                    log_queue.put(f"Updating {appdata_name} for user installation...")

                # Perform the update
                transaction.add_update(ref.format_ref(), None, None)
            except Exception as e:
                if appdata_name:
                    log_queue.put(f"Error updating {appdata_name}: {e}")
                else:
                    log_queue.put(f"Error updating ref: {e}")
        transaction.run()
        log_queue.put("Flatpak User Updates complete!")

    del user_installation


def on_open_log_button_clicked(
    uid: int, gid: int, log_queue: Any, update_queue: Any
) -> None:
    # Get the user's home directory and other details
    pw_record = pwd.getpwuid(uid)
    user_home = Path(pw_record.pw_dir)

    # Update environment variables
    os.environ["HOME"] = str(user_home)
    os.environ["USER"] = pw_record.pw_name
    os.environ["LOGNAME"] = pw_record.pw_name
    os.environ["SHELL"] = pw_record.pw_shell
    os.environ["XDG_CACHE_HOME"] = str(user_home / ".cache")
    os.environ["XDG_CONFIG_HOME"] = str(user_home / ".config")
    os.environ["XDG_DATA_HOME"] = str(user_home / ".local" / "share")
    os.environ["XDG_RUNTIME_DIR"] = f"/run/user/{uid}"

    # Add Flatpak export directory to XDG_DATA_DIRS
    flatpak_export_dir = (
        user_home / ".local" / "share" / "flatpak" / "exports" / "share"
    )
    os.environ["XDG_DATA_DIRS"] = (
        f"{flatpak_export_dir}:{os.environ.get('XDG_DATA_DIRS', '/usr/local/share:/usr/share')}"
    )

    # Ensure the runtime directory exists
    runtime_dir = Path(os.environ["XDG_RUNTIME_DIR"])
    if not runtime_dir.exists():
        runtime_dir.mkdir(parents=True)
        os.chown(runtime_dir, uid, gid)

    # Change working directory to the user's home directory
    os.chdir(user_home)
    subprocess.Popen(
        [
            "xdg-open",
            str(user_home / ".local" / "share" / "nobara-updater" / "nobara-sync.log"),
        ]
    )


def on_open_log_button_dir_clicked(
    uid: int, gid: int, log_queue: Any, update_queue: Any
) -> None:
    # Get the user's home directory and other details
    pw_record = pwd.getpwuid(uid)
    user_home = Path(pw_record.pw_dir)

    # Update environment variables
    os.environ["HOME"] = str(user_home)
    os.environ["USER"] = pw_record.pw_name
    os.environ["LOGNAME"] = pw_record.pw_name
    os.environ["SHELL"] = pw_record.pw_shell
    os.environ["XDG_CACHE_HOME"] = str(user_home / ".cache")
    os.environ["XDG_CONFIG_HOME"] = str(user_home / ".config")
    os.environ["XDG_DATA_HOME"] = str(user_home / ".local" / "share")
    os.environ["XDG_RUNTIME_DIR"] = f"/run/user/{uid}"

    # Add Flatpak export directory to XDG_DATA_DIRS
    flatpak_export_dir = (
        user_home / ".local" / "share" / "flatpak" / "exports" / "share"
    )
    os.environ["XDG_DATA_DIRS"] = (
        f"{flatpak_export_dir}:{os.environ.get('XDG_DATA_DIRS', '/usr/local/share:/usr/share')}"
    )

    # Ensure the runtime directory exists
    runtime_dir = Path(os.environ["XDG_RUNTIME_DIR"])
    if not runtime_dir.exists():
        runtime_dir.mkdir(parents=True)
        os.chown(runtime_dir, uid, gid)

    # Change working directory to the user's home directory
    os.chdir(user_home)
    subprocess.Popen(
        ["xdg-open", str(user_home / ".local" / "share" / "nobara-updater")]
    )
