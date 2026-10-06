#!/usr/bin/python3
"""Root-only service entry point. The frozen copy survives self-updates."""
import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import sys
from pathlib import Path

if (Path(__file__).parent / "nobara_updater").is_dir():
    sys.path.insert(0, str(Path(__file__).parent))

from nobara_updater.update_state import STATE_DIR, TRIGGER, UpdateError, load_proxy_settings, read_state, read_status, status_message, write_state
from nobara_updater import update_backend as backend
from nobara_updater.update_report import attach_log, record_failure, record_service_failure


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "prepare-codecs", "refresh-pending", "schedule", "reboot", "cancel", "retry", "execute", "finalize", "execute-live", "live-finalize", "recover", "confirm", "confirm-stopped", "offline-stopped", "status", "foreign-trigger", "own-trigger", "installer-update", "installer-codecs", "installer-finalize"])
    parser.add_argument("--early-upgrade-complete", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("This worker must run as root.")
    if args.action.startswith("installer-") and not backend.in_installer_root():
        parser.error("Installer actions require an actual chroot target.")
    if args.action.startswith("installer-"):
        # Also inherited by RPM scriptlets. Service configuration belongs to
        # the target filesystem, never to the live ISO's service manager.
        os.environ["SYSTEMD_OFFLINE"] = "1"
    if args.action in {"foreign-trigger", "own-trigger"}:
        owned = TRIGGER.is_symlink() and TRIGGER.resolve() == STATE_DIR.resolve()
        if args.action == "own-trigger":
            return 0 if owned else 1
        return 1 if owned or Path("/run/nobara-updater-offline").exists() else 0
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), RotatingFileHandler(STATE_DIR / "update.log", maxBytes=10 * 1024**2, backupCount=3)])
    if args.action == "status":
        print(json.dumps(read_status()))
        return 0
    if args.action in {"confirm-stopped", "offline-stopped"}:
        # systemd sets these for ExecStopPost, even if the main worker could
        # not run its exception handler. This hook must never block recovery
        # with a second failure or change the saved installation state.
        try:
            record_service_failure(read_state(), "confirm" if args.action == "confirm-stopped" else "execute",
                                   os.environ.get("SERVICE_RESULT", ""), os.environ.get("EXIT_CODE", ""),
                                   os.environ.get("EXIT_STATUS", ""))
        except Exception:
            logging.exception("Could not inspect stopped update service.")
        return 0
    try:
        if args.action in {"prepare", "prepare-codecs"} and not args.early_upgrade_complete:
            # systemd does not pass nobara-sync's environment to the service.
            # The fresh interpreter below inherits these from os.environ.
            proxies = load_proxy_settings()
            if proxies:
                logging.info("Using proxy settings from nobara-sync: %s", ", ".join(proxies))
        if args.action in {"prepare", "prepare-codecs", "installer-update", "installer-codecs"} and not args.early_upgrade_complete:
            backend.upgrade_early(installer=args.action.startswith("installer-"))
            # Run the newly installed worker and modules before planning or
            # freezing an engine. The service remains the owner of this PID.
            os.execv("/usr/bin/python3", ["/usr/bin/python3", "-I", "/usr/libexec/nobara-update-worker",
                                         args.action, "--early-upgrade-complete"])
            return 0
        if args.action in {"finalize", "recover", "confirm"}:
            try:
                attach_log(read_state())
            except Exception:
                logging.exception("Could not attach the persistent update log.")
        if args.action in {"installer-update", "installer-codecs"}:
            backend.install_in_target(codecs=args.action == "installer-codecs")
        elif args.action == "installer-finalize":
            backend.finalize(installer=True)
        elif args.action == "live-finalize":
            backend.finalize(live=True)
        elif args.action == "execute-live":
            backend.execute_live()
        elif args.action == "prepare-codecs":
            backend.prepare(codecs=True)
        elif args.action == "refresh-pending":
            backend.refresh_pending()
        elif args.action in {"schedule", "reboot"}:
            backend.schedule(reboot=args.action == "reboot")
        else:
            {"prepare": backend.prepare, "cancel": backend.cancel, "retry": backend.retry_failed, "execute": backend.execute,
             "finalize": backend.finalize, "recover": backend.recover, "confirm": backend.confirm_boot}[args.action]()
        return 0
    except Exception as error:
        if isinstance(error, UpdateError):
            logging.error("Update %s failed: %s", args.action, error)
        else:
            logging.exception("Update %s failed", args.action)
        if args.action in {"execute", "finalize", "recover", "confirm"}:
            record_failure(read_state(), args.action, error)
        elif args.action in {"prepare", "prepare-codecs"}:
            state = read_state()
            # A rejected concurrent request must not report another job as a
            # failure; prepare records FAILED while holding the update lock.
            if state.get("status") == "failed" and (not state.get("started") or state.get("early_upgrade")) and state.get("error") == str(error):
                record_failure(state, "prepare", error)
        # Never replace another service's state on an ordinary client error
        # (for example scheduling while preparation still holds the lock).
        # Live actions record their failures while still holding the lock;
        # a rejected concurrent invocation must not overwrite their state.
        if args.action in {"execute", "finalize", "confirm", "installer-update", "installer-codecs", "installer-finalize"}:
            state = read_state()
            write_state(STATE_DIR, state, "failed", error=str(error))
        print(str(error), file=sys.stderr)
        if args.action == "confirm" and not state.get("recovery", {}).get("created"):
            # There is no recovery system to select. Preserve FAILED and its
            # report, but let an otherwise bootable desktop show the notice.
            return 0
        return 1


if __name__ == "__main__":
    sys.exit(main())
