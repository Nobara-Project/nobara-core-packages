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

def _add_resolvable_installonly_upgrades(
    base: dnf5_base.Base,
    goal: dnf5_base.Goal,
    install_only_names,
) -> None:
    settings = dnf5_base.GoalJobSettings()
    try:
        installed_query = dnf5_rpm.PackageQuery(base)
        installed_query.filter_installed()
        installed_set = {pkg.get_name() for pkg in installed_query}
    except Exception:
        installed_set = set()
    for name in install_only_names:
        if name not in installed_set:
            continue
        query = dnf5_rpm.PackageQuery(base)
        try:
            query.resolve_pkg_spec(name, settings, False)
        except Exception:
            continue
        if any(True for _ in query):
            goal.add_upgrade(name)

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

            _add_resolvable_installonly_upgrades(base, goal, install_only_names)

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

def _log_transaction_resolve_problems(transaction, logger: logging.Logger) -> None:
    for problem in transaction.get_resolve_logs_as_strings():
        logger.warning(problem)

    for package in transaction.get_conflicting_packages():
        logger.error("Package skipped due to conflicts: %s", package.get_nevra())

    for package in transaction.get_broken_dependency_packages():
        logger.error(
            "Package skipped due to broken dependencies: %s", package.get_nevra()
        )


def _transaction_has_errors(transaction, logger: logging.Logger) -> bool:
    has_errors = False
    for package in transaction.get_transaction_packages():
        if package.get_state() == dnf5_trans.TransactionItemState_ERROR:
            logger.error(
                "The transaction contains package %s in error state.",
                package.get_package().get_full_nevra(),
            )
            has_errors = True
    return has_errors


def _log_transaction_packages(transaction, logger: logging.Logger) -> None:
    packages = transaction.get_transaction_packages()
    total = transaction.get_transaction_packages_count()
    logger.info("Transaction contains %s packages.", total)

    for index, item in enumerate(packages, start=1):
        package = item.get_package()
        action = {
            dnf5_trans.TransactionItemAction_INSTALL: "Installing",
            dnf5_trans.TransactionItemAction_UPGRADE: "Upgrading",
            dnf5_trans.TransactionItemAction_DOWNGRADE: "Downgrading",
            dnf5_trans.TransactionItemAction_REINSTALL: "Reinstalling",
            dnf5_trans.TransactionItemAction_REMOVE: "Removing",
            dnf5_trans.TransactionItemAction_REPLACED: "Replacing",
        }.get(item.get_action(), "Processing")

        logger.info("    (%s/%s) %s %s", index, total, action, package.get_nevra())


def _format_nevra(nevra) -> str:
    """libdnf5.rpm.Nevra has no useful __str__/to_string(): printing it
    directly (e.g. via %s) yields a SWIG proxy repr like
    "<libdnf5.rpm.Nevra; proxy of ...>" instead of the package name, which
    makes scriptlet log lines impossible to attribute to a package. Build
    the NEVRA string from its getters instead."""
    epoch = nevra.get_epoch()
    name = nevra.get_name()
    version = nevra.get_version()
    release = nevra.get_release()
    arch = nevra.get_arch()
    if epoch and epoch != "0":
        return f"{name}-{epoch}:{version}-{release}.{arch}"
    return f"{name}-{version}-{release}.{arch}"


class _UpgradeTransactionCallbacks(dnf5_rpm.TransactionCallbacks):
    """Heartbeat logging for transaction.run().

    Without this, an rpm transaction reports nothing for its entire
    duration -- no percentage, no package names, no heartbeat -- which on
    a major-version upgrade can mean 20-40 minutes of total silence with
    no way to tell a working transaction from a hung one. That silence is
    exactly what has led users to kill an apparently-frozen upgrade
    mid-transaction, which can leave the system with thousands of
    duplicate/orphaned packages that a re-run cannot repair.
    """

    def __init__(self, logger: logging.Logger, total_packages: int) -> None:
        super().__init__()
        self.logger = logger
        self.total_packages = total_packages

    def transaction_start(self, total: int) -> None:
        self.logger.info("Preparing transaction (%s items)...", total)

    def elem_progress(self, item, amount: int, total: int) -> None:
        action = {
            dnf5_trans.TransactionItemAction_INSTALL: "Installing",
            dnf5_trans.TransactionItemAction_UPGRADE: "Upgrading",
            dnf5_trans.TransactionItemAction_DOWNGRADE: "Downgrading",
            dnf5_trans.TransactionItemAction_REINSTALL: "Reinstalling",
            dnf5_trans.TransactionItemAction_REMOVE: "Removing",
            dnf5_trans.TransactionItemAction_REPLACED: "Replacing",
        }.get(item.get_action(), "Processing")
        self.logger.info(
            "    (%s/%s) %s %s", amount + 1, total, action, item.get_package().get_nevra()
        )

    def script_start(self, item, nevra, type) -> None:
        self.logger.info(
            "    Running %s scriptlet for %s...",
            self.script_type_to_string(type),
            _format_nevra(nevra),
        )

    def script_error(self, item, nevra, type, return_code: int) -> None:
        self.logger.warning(
            "    Scriptlet for %s exited with code %s", _format_nevra(nevra), return_code
        )


def run_system_upgrade_transaction(logger: logging.Logger | None = None) -> bool:
    tx_logger = logger if logger is not None else logging.getLogger()
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

        _add_resolvable_installonly_upgrades(base, goal, install_only_names)

        transaction = goal.resolve()
        _log_transaction_resolve_problems(transaction, tx_logger)

        if transaction.get_conflicting_packages():
            return False

        if transaction.get_broken_dependency_packages():
            return False

        if transaction.empty():
            tx_logger.info("Nothing to do.")
            return True

        _log_transaction_packages(transaction, tx_logger)

        tx_logger.info("Downloading packages...")
        transaction.download()

        tx_logger.info("Running transaction...")
        transaction.set_callbacks(
            dnf5_rpm.TransactionCallbacksUniquePtr(
                _UpgradeTransactionCallbacks(tx_logger, transaction.get_transaction_packages_count())
            )
        )
        result = transaction.run()
        if (
            result != dnf5_base.Transaction.TransactionRunResult_SUCCESS
            or _transaction_has_errors(transaction, tx_logger)
        ):
            tx_logger.error(
                "DNF transaction failed: %s",
                dnf5_base.Transaction.transaction_result_to_string(result),
            )
            for problem in transaction.get_transaction_problems():
                tx_logger.error(problem)
            return False

        tx_logger.info("DNF System Updates complete!")
        return True

    except Exception as e:
        tx_logger.error("DNF transaction failed: %s", e)
        return False
    finally:
        del base


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
        self.success = self.update_packages_dnf_command(action)


    def update_packages_dnf_command(self, action: str, retries: int = 3, delay: int = 5) -> bool:
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
            
        installed_set = set()
        try:
            temp_base = dnf5_base.Base()
            temp_base.load_config()
            temp_base.setup()
            sack = temp_base.get_repo_sack()
            sack.create_repos_from_system_configuration()
            sack.load_repos() 
            installed_query = dnf5_rpm.PackageQuery(temp_base)
            installed_query.filter_installed()
            installed_set = {pkg.get_name() for pkg in installed_query}
            del installed_query
            del sack
            del temp_base
        except Exception as e:
            self.logger.warning("Could not pre-filter installed packages: %s", e)
        if action == "upgrade":
            targets = [p for p in self.package_names if p in installed_set]
            if not targets:
                targets = self.package_names
        else:
            targets = self.package_names

        action_log_string = {
            "upgrade": "Upgrading packages:",
            "install": "Installing packages:",
            "remove": "Removing packages:",
        }[action]

        cmd = ["dnf5", action_map[action], "--refresh", "-y", *targets]

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
                    bufsize=1,
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
                    self.logger.error("ERROR: Please see ~/.local/share/nobara-updater/nobara-sync.log for more details")
                    self.logger.error("ERROR: You can press the 'Open Log File' button on the Update System app to view it.")
                    self.logger.error("==================================================")
                    return False  # Exit normally so GUI can reset buttons.

                if rc != 0:
                    self.logger.error("==================================================")
                    self.logger.error("ERROR: DNF Package update are incomplete or failed due to conflicts/broken dependencies.")
                    self.logger.error("ERROR: Please see ~/.local/share/nobara-updater/nobara-sync.log for more details")
                    self.logger.error("ERROR: You can press the 'Open Log File' button on the Update System app to view it.")
                    self.logger.error("==================================================")
                    return False

                self.logger.info("DNF System Updates complete!")
                return True

            except Exception as e:
                self.logger.error("Attempt %d/%d failed: %s", attempt, retries, e)
                if attempt < retries:
                    time.sleep(delay)
                else:
                    return False

        return False
