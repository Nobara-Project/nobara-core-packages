import logging
import libdnf5.base as dnf5_base
import libdnf5.repo as dnf5_repo
import libdnf5.rpm as dnf5_rpm
import libdnf5.transaction as dnf5_trans
from libdnf5.exception import OptionValueNotSetError
import queue
import threading
import time
import sys
from logging.handlers import QueueHandler
from typing import Any, List
import inspect
import dnf  # type: ignore[import]
import gi  # type: ignore[import]
import subprocess
import os
import contextlib

gi.require_version("Gtk", "3.0")

from gi.repository import Gtk  # type: ignore[import]

logger = logging.getLogger()

class AttributeDict(dict[str, Any]):
    def __init__(self, id: str, metalink: Any, mirrorlist: Any, baseurl: Any) -> None:
        super().__init__()
        self.id = id
        self.metalink = metalink
        self.mirrorlist = mirrorlist
        self.baseurl = baseurl

    def __getattr__(self, attr: str) -> Any:
        try:
            return self[attr]
        except KeyError as err:
            raise AttributeError(
                f"'AttributeDict' object has no attribute '{attr}'"
            ) from err

    def __setattr__(self, attr: str, value: Any) -> None:
        self[attr] = value

    def __delattr__(self, attr: str) -> None:
        try:
            del self[attr]
        except KeyError as err:
            raise AttributeError(
                f"'AttributeDict' object has no attribute '{attr}'"
            ) from err

@contextlib.contextmanager
def mute_loggers(names: list[str], level: int = logging.WARNING):
    saved = []
    for name in names:
        lg = logging.getLogger(name)
        saved.append((lg, lg.level, lg.disabled, lg.propagate, list(lg.handlers)))
        # Make sure nothing prints
        lg.setLevel(level)
        lg.disabled = False
        lg.propagate = False
        lg.handlers = []          # detach handlers that print to console
        lg.addHandler(logging.NullHandler())
    try:
        yield
    finally:
        for lg, old_level, old_disabled, old_propagate, old_handlers in saved:
            lg.setLevel(old_level)
            lg.disabled = old_disabled
            lg.propagate = old_propagate
            lg.handlers = old_handlers

def repoindex(retries: int = 3, delay: int = 5) -> list[AttributeDict]:
    def get_safe_value(option):
        try:
            return option.get_value()
        except (OptionValueNotSetError, RuntimeError, AttributeError):
            return None

    attempt = 0
    while attempt < retries:
        base = dnf5_base.Base()
        try:
            base.load_config()
            base.setup()

            sack = base.get_repo_sack()
            sack.create_repos_from_system_configuration()
            sack.load_repos()
            
            enabled_repos = []
            query = dnf5_repo.RepoQuery(base)

            for repo in query:
                config = repo.get_config()
                enabled = get_safe_value(config.get_enabled_option())
                if enabled:
                    repo_id = repo.get_id()
                    metalink = get_safe_value(config.get_metalink_option())
                    mirrorlist = get_safe_value(config.get_mirrorlist_option())
                    raw_baseurl = get_safe_value(config.get_baseurl_option())
                    baseurl = list(raw_baseurl) if raw_baseurl is not None else None
                    enabled_repos.append(AttributeDict(repo_id, metalink, mirrorlist, baseurl))

            return enabled_repos

        except Exception as e:
            attempt += 1
            logger.error("Attempt %d failed with error: %s. Retrying...", attempt, e)
            if attempt < retries:
                time.sleep(delay)
            else:
                raise Exception(f"Failed to complete operation after {retries} attempts")
        finally:
            del base

def updatechecker(retries: int = 3, delay: int = 5) -> list[str]:
    attempt = 0
    while attempt < retries:
        base = dnf5_base.Base()
        try:
            config = base.get_config()
            config.get_metadata_expire_option().from_string("0")
            config.get_obsoletes_option().from_string("true")

            base.load_config()
            base.setup()

            sack = base.get_repo_sack()
            sack.create_repos_from_system_configuration()
            sack.load_repos()

            goal = dnf5_base.Goal(base)
            goal.add_upgrade("*")

            try:
                install_only_names = config.installonlypkgs
            except AttributeError:
                install_only_names = []

            for name in install_only_names:
                goal.add_upgrade(name)

            transaction = goal.resolve()
            upgrades = []
            t_pkgs = transaction.get_transaction_packages()
            for t_pkg in t_pkgs:
                action = t_pkg.get_action()
                valid_actions = [
                    dnf5_trans.TransactionItemAction_UPGRADE,
                    dnf5_trans.TransactionItemAction_INSTALL
                ]

                if action in valid_actions:
                    upgrades.append(t_pkg.get_package().get_name())

            return list(set(upgrades))

        except Exception as e:
            attempt += 1
            logger.error(f"Update check attempt {attempt} failed: {e}")
            if attempt >= retries:
                raise
            time.sleep(delay)
        finally:
            del base

class CustomTransactionDisplay(dnf.yum.rpmtrans.LoggingTransactionDisplay):
    def __init__(self, total_packages):
        super().__init__()
        self.logger = logging.getLogger(__name__)
        self.scriptlet_progress = {}
        self.performing_cleanup = 0
        self.performing_upgrade = 0
        self.starting_line = 0
        self.total_packages = total_packages
        self.package = ""

    def progress(self, package, action, ti_done, ti_total, ts_done, ts_total):
        super().progress(package, action, ti_done, ti_total, ts_done, ts_total)
        action_str = self._get_action_str(action)
        package_name = str(package)

        match action_str:
            case "Upgraded:" | "Preparing:" | "Reinstalled:" | "Downgraded:" | "Obsoleted:" | "Cleanup:":
                return  # Skip logging for these actions

        if action_str == "Running scriptlet:":
            if self.performing_cleanup == 0:
                self.logger.info("Cleanup...")
                self.performing_cleanup = 1
            else:
                return
        else:
            if self.performing_upgrade == 0:
                if action_str == "Upgrading:":
                    self.logger.info("Upgrading...")
                if action_str == "Removing:":
                    self.logger.info("Removing...")
                if action_str == "Downgrading:":
                    self.logger.info("Downgrading...")
                if action_str == "Installing:":
                    self.logger.info("Installing...")

                self.performing_upgrade = 1

            if self.package != package_name:
                self.starting_line += 1
                if self.starting_line <= self.total_packages:
                    self.package = package_name
                    self.logger.info(f"    ({self.starting_line}/{self.total_packages}) {action_str} {package_name}")
                else:
                    return

    def _get_action_str(self, action):
        action_map = {
            dnf.transaction.PKG_DOWNGRADE: 'Downgrading:',
            dnf.transaction.PKG_DOWNGRADED: 'Downgraded:',
            dnf.transaction.PKG_INSTALL: 'Installing:',
            dnf.transaction.PKG_OBSOLETE: 'Obsoleting:',
            dnf.transaction.PKG_OBSOLETED: 'Obsoleted:',
            dnf.transaction.PKG_REINSTALL: 'Reinstalling:',
            dnf.transaction.PKG_REINSTALLED: 'Reinstalled:',
            dnf.transaction.PKG_REMOVE: 'Removing:',
            dnf.transaction.PKG_UPGRADE: 'Upgrading:',
            dnf.transaction.PKG_UPGRADED: 'Upgraded:',
            dnf.transaction.PKG_CLEANUP: 'Cleanup:',
            dnf.transaction.PKG_VERIFY: 'Verified:',
            dnf.transaction.PKG_SCRIPTLET: 'Running scriptlet:',
            dnf.transaction.TRANS_PREPARATION: 'Preparing:',
        }
        return action_map.get(action, action)

class PackageUpdater:
    def __init__(
        self,
        package_names: list[str],
        action: str,
        liststore: Gtk.ListStore,
        logger: logging.Logger | None = None,
    ):
        self.package_names = package_names
        self.liststore = liststore
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.queue_handler = QueueHandler(self.log_queue)
        self.logger = logger if logger is not None else logging.getLogger()
        self.logger.addHandler(self.queue_handler)
        self.logger.setLevel(logging.INFO)
        # Right now update_packages doesn't provide sufficient logging.
        # It also doesn't correctly log in the dnf history
        # Use DNF command for now
        #self.update_packages(action)
        self.update_packages_dnf_command(action)


    def update_packages_dnf_command(self, action: str, retries: int = 3, delay: int = 5) -> None:
        def _looks_like_dependency_conflict(lines: List[str]) -> bool:
            needles = (
                "Problem ",
                "Skipping packages with conflicts",
                "Skipping packages with broken dependencies",
                "conflicts",
                "broken dependencies",
                "cannot install",
                "Transaction check error",
                "Error:",
            )
            return any(any(n in line for n in needles) for line in lines)

        if not self.package_names:
            raise ValueError("No package names provided")

        action_map = {"upgrade": "update", "install": "install", "remove": "remove"}
        if action not in action_map:
            raise ValueError(f"Invalid action: {action!r}")

        action_log_string = {
            "upgrade": "Upgrading packages:",
            "install": "Installing packages:",
            "remove": "Removing packages:",
        }[action]

        cmd = ["dnf5", action_map[action], "--refresh", "-y", *self.package_names]

        self.logger.info("%s\n%s", action_log_string, "\n".join(self.package_names))

        for attempt in range(1, retries + 1):
            try:
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )

                output_lines: List[str] = []
                assert process.stdout is not None
                for raw in process.stdout:
                    line = raw.rstrip("\n")
                    output_lines.append(line)
                    self.logger.info(line)

                rc = process.wait()

                # Treat "conflict-style" output as failure even if rc == 0 (your example case)
                if _looks_like_dependency_conflict(output_lines):
                    self.logger.error("==================================================")
                    self.logger.error("ERROR: DNF Package update are incomplete or failed due to conflicts/broken dependencies.")
                    self.logger.error("ERROR: Please see ~/.local/share/logs/nobara-sync.log for more details")
                    self.logger.error("ERROR: You can press the 'Open Log File' button on the Update System app to view it.")
                    self.logger.error("==================================================")
                    return  # <-- IMPORTANT: exit normally (0) so GUI can reset buttons

                if rc != 0:
                    self.logger.error("==================================================")
                    self.logger.error("ERROR: DNF Package update are incomplete or failed due to conflicts/broken dependencies.")
                    self.logger.error("ERROR: Please see ~/.local/share/logs/nobara-sync.log for more details")
                    self.logger.error("ERROR: You can press the 'Open Log File' button on the Update System app to view it.")
                    self.logger.error("==================================================")
                    return  # <-- exit normally

                self.logger.info("DNF System Updates complete!")
                return

            except Exception as e:
                self.logger.error("Attempt %d/%d failed: %s", attempt, retries, e)
                if attempt < retries:
                    time.sleep(delay)
                else:

                    return  # <-- exit normally even on final failure
