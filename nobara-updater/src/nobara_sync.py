#!/usr/bin/python3
"""CLI interface used by dnf-app-center and administrators."""
from __future__ import annotations

import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys

from nobara_updater.update_client import install_live_update, prepare_update, worker_action
from nobara_updater.update_state import STATE_DIR, UpdateError, in_installer_root, read_state, read_status, status_message

LOG = logging.getLogger("nobara-sync")
RESULT_PREFIX = "NOBARA_UPDATE_RESULT "
COMMANDS = {"cli", "prepare-update", "install-updates", "install-fixups", "install-codecs",
            "repair", "check-updates", "check-repos", "update-status", "schedule-update",
            "cancel-update", "reboot", "recovery-report", "retry-update"}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Install application updates now; stage core and release updates for restart.")
    commands = parser.add_subparsers(dest="command", required=True)
    cli = commands.add_parser("cli", help="Install application updates now, or schedule core updates for restart.")
    cli.add_argument("username", nargs="?", help="User whose Flatpaks should be updated with --all.")
    cli.add_argument("--all", action="store_true", help="Also update system and user Flatpaks now.")
    for name, help_text in {
        "prepare-update": "Download and validate an update without scheduling it.",
        "install-updates": "Update packages and Flatpaks; stage core updates for restart.",
        "install-fixups": "Update packages including known package migrations.",
        "install-codecs": "Update packages including optional media codecs; restart if required.",
        "repair": "Prepare a distro-sync and migrations; install now or at restart as required.",
        "check-updates": "Check package updates without installing anything.",
        "check-repos": "Refresh and validate enabled repository metadata.",
        "schedule-update": "Install the prepared update on the next restart.",
        "cancel-update": "Cancel an update that has not begun installation.",
        "retry-update": "After manual repairs without rollback, validate and prepare a fresh update.",
        "reboot": "Restart now and install the prepared update.",
    }.items():
        commands.add_parser(name, help=help_text)
    status = commands.add_parser("update-status", help="Show saved update status.")
    status.add_argument("--json", action="store_true", help="Print one JSON object without log messages.")
    report = commands.add_parser("recovery-report", help="Read the last recovery report without administrator privileges.")
    report.add_argument("--save", type=Path, metavar="FILE", help="Copy the offline .tar.gz report to FILE.")
    report.add_argument("--upload", action="store_true", help="Upload the text log to Nobara's paste service using pbcli.")
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        args = ["cli"]
    elif args[0] not in COMMANDS and args[0] not in {"-h", "--help"}:
        # Preserve the historical `nobara-sync username --all` spelling.
        args.insert(0, "cli")
    return parser.parse_args(args)


def original_user(username=None):
    if username:
        return pwd.getpwnam(username)
    for name in ("SUDO_UID", "PKEXEC_UID", "ORIG_USER"):
        value = os.environ.get(name, "")
        if value.isdigit() and int(value) != 0:
            return pwd.getpwuid(int(value))
    if os.getuid() != 0:
        return pwd.getpwuid(os.getuid())
    # dnf-app-center's privileged helper preserves the invoking user's home.
    for name in ("ORIGINAL_USER_HOME", "HOME"):
        home = os.environ.get(name)
        for user in pwd.getpwall():
            if user.pw_uid != 0 and user.pw_dir == home:
                return user
    return None


def elevate():
    if os.geteuid() == 0:
        return
    # The app center already authenticates its helper; terminal users use sudo.
    os.execvp("sudo", ["sudo", "--", str(Path(__file__).resolve()), *sys.argv[1:]])


def initialize_logging():
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.umask(0o077)
    file_handler = RotatingFileHandler(STATE_DIR / "client.log", maxBytes=5 * 1024**2, backupCount=3)
    logging.basicConfig(level=logging.INFO, format="%(message)s",
                        handlers=[logging.StreamHandler(sys.stdout), file_handler], force=True)


def run(command, *, user=None):
    kwargs = {}
    if user is not None:
        env = dict(os.environ, HOME=user.pw_dir, USER=user.pw_name, LOGNAME=user.pw_name,
                   XDG_CACHE_HOME=user.pw_dir + "/.cache", XDG_CONFIG_HOME=user.pw_dir + "/.config",
                   XDG_DATA_HOME=user.pw_dir + "/.local/share", XDG_RUNTIME_DIR=f"/run/user/{user.pw_uid}",
                   DBUS_SESSION_BUS_ADDRESS=f"unix:path=/run/user/{user.pw_uid}/bus")
        kwargs.update(user=user.pw_uid, group=user.pw_gid, extra_groups=os.getgrouplist(user.pw_name, user.pw_gid),
                      cwd=user.pw_dir, env=env)
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding="utf-8", errors="replace", **kwargs)
    assert process.stdout is not None
    for line in process.stdout:
        LOG.info("%s", line.rstrip())
    process.stdout.close()
    if process.wait():
        raise UpdateError(f"{command[0]} exited with status {process.returncode}.")


def update_flatpaks(user):
    run(["flatpak", "--system", "update", "--noninteractive", "-y"])
    if user is not None:
        run(["flatpak", "--user", "update", "--noninteractive", "-y"], user=user)
    else:
        LOG.info("No invoking user was identified; only system Flatpaks were updated.")


def result_message(state):
    status = state.get("status", "idle")
    return dict(status=state.get("status", "idle"), message=status_message(state),
                reboot_required=status in {"scheduled", "awaiting-boot"} or
                (status == "ready" and state.get("execution", {}).get("mode") != "live"),
                automatic_recovery=state.get("recovery", {}).get("available", False))


def dispatch(args):
    if args.command == "retry-update":
        if in_installer_root():
            raise UpdateError("Retry after repair is only available on an installed system.")
        if not worker_action("retry", LOG):
            return False
    if args.command == "check-repos":
        run(["dnf5", "--refresh", "--setopt=skip_if_unavailable=False", "makecache"])
        return True
    if args.command == "check-updates":
        from nobara_updater.dnf import updatechecker
        updates = updatechecker()
        LOG.info("%s", "\n".join(updates) if updates else "No package updates found.")
        LOG.info("%s", status_message(read_status()))
        return True
    if args.command in {"schedule-update", "cancel-update", "reboot"}:
        if in_installer_root():
            raise UpdateError("Offline scheduling and reboot commands are not available inside an installer target.")
        action = {"schedule-update": "schedule", "cancel-update": "cancel", "reboot": "reboot"}[args.command]
        return worker_action(action, LOG)
    if in_installer_root():
        if args.command == "prepare-update":
            raise UpdateError("Use cli or install-codecs to finish installation inside the target root.")
        if args.command == "cli" and args.all:
            raise UpdateError("User Flatpak updates are unavailable in the installer target. Use cli without --all.")
        action = "installer-codecs" if args.command == "install-codecs" else "installer-update"
        LOG.info("Installing into the target root. No live-session service or reboot will be requested.")
        run(["/usr/libexec/nobara-update-worker", action])
        print(RESULT_PREFIX + json.dumps(result_message(read_state())), flush=True)
        return True
    user = original_user(getattr(args, "username", None))
    if not prepare_update(LOG, codecs=args.command == "install-codecs"):
        return False
    if args.command != "prepare-update":
        state = read_state()
        if state.get("status") in {"ready", "scheduled"}:
            if state["status"] == "ready" and state.get("execution", {}).get("mode") == "live":
                if not install_live_update(LOG):
                    return False
            elif not worker_action("schedule", LOG):
                return False
    if args.command == "install-updates" or (args.command == "cli" and args.all):
        # Package installation/staging remains durable if Flatpak fails.
        update_flatpaks(user)
    state = read_state()
    LOG.info("%s", status_message(state))
    # scheduled is distinct from live-complete: the frontend must not infer
    # installation from a successful staging command's exit code alone.
    print(RESULT_PREFIX + json.dumps(result_message(state)), flush=True)
    return True


def main(argv=None):
    args = parse_args(argv)
    if args.command == "recovery-report":
        from nobara_updater.update_report import latest_report, upload_report
        try:
            report = latest_report()
            if not report:
                print("No recovery report is available.")
                return 0
            if args.save:
                # Avoid silently replacing the user's existing report/file.
                with args.save.open("xb") as target, Path(report["bundle"]).open("rb") as source:
                    shutil.copyfileobj(source, target)
                print(str(args.save))
            if args.upload:
                print(upload_report(report))
            if not args.save and not args.upload:
                print(Path(report["log"]).read_text())
                print("Offline report: " + report["bundle"])
            return 0
        except (OSError, UpdateError, subprocess.TimeoutExpired) as error:
            print(str(error), file=sys.stderr)
            return 1
    elevate()
    if args.command == "update-status":
        state = read_status()
        print(json.dumps(dict(state, message=status_message(state))) if args.json else status_message(state))
        return 0
    initialize_logging()
    try:
        return 0 if dispatch(args) else 1
    except (Exception, KeyboardInterrupt) as error:
        LOG.error("Update command failed: %s", error)
        LOG.error("Logs: /var/lib/nobara-updater/client.log and /var/lib/nobara-updater/update.log")
        return 1


if __name__ == "__main__":
    sys.exit(main())
