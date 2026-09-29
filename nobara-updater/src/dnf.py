import logging
import libdnf5.base as dnf5_base
import libdnf5.repo as dnf5_repo
import libdnf5.rpm as dnf5_rpm
import libdnf5.transaction as dnf5_trans
from libdnf5.exception import OptionValueNotSetError
import time
import sys
from typing import Any
import subprocess
import os
import contextlib
import uuid
from dataclasses import dataclass



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


def _expire_enabled_repositories(
    base: dnf5_base.Base, log: logging.Logger | None = None
) -> None:
    expired_repo_ids = []
    for repo in dnf5_repo.RepoQuery(base):
        try:
            enabled = repo.get_config().get_enabled_option().get_value()
        except (OptionValueNotSetError, RuntimeError, AttributeError):
            enabled = False
        if not enabled:
            continue
        repo.expire()
        expired_repo_ids.append(repo.get_id())

    if log is not None and expired_repo_ids:
        log.info("Refreshing repository metadata before resolving transaction...")
        log.debug("Expired repositories: %s", ", ".join(expired_repo_ids))


def updatechecker(retries: int = 3, delay: int = 5) -> list[str]:
    attempt = 0
    while attempt < retries:
        base = dnf5_base.Base()
        try:
            base.load_config()
            config = base.get_config()
            config.get_metadata_expire_option().set(0)
            config.get_obsoletes_option().set(True)
            config.get_skip_if_unavailable_option().set(False)
            base.setup()

            sack = base.get_repo_sack()
            sack.create_repos_from_system_configuration()
            for repo in dnf5_repo.RepoQuery(base):
                if repo.get_config().get_enabled_option().get_value():
                    repo.get_config().get_skip_if_unavailable_option().set(False)
                    repo.expire()
            sack.load_repos()

            goal = dnf5_base.Goal(base)
            goal.add_rpm_distro_sync()

            try:
                install_only_names = config.installonlypkgs
            except AttributeError:
                install_only_names = []

            _add_resolvable_installonly_upgrades(base, goal, install_only_names)

            transaction = goal.resolve()
            if transaction.get_problems() != dnf5_base.GoalProblem_NO_PROBLEM:
                problems = transaction.get_resolve_logs_as_strings()
                raise RuntimeError("Could not resolve updates: " + "; ".join(problems))
            upgrades = []
            t_pkgs = transaction.get_transaction_packages()
            for t_pkg in t_pkgs:
                action = t_pkg.get_action()
                valid_actions = [
                    dnf5_trans.TransactionItemAction_UPGRADE,
                    dnf5_trans.TransactionItemAction_INSTALL,
                    dnf5_trans.TransactionItemAction_DOWNGRADE,
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


def _log_transaction_failure_details(
    transaction, logger: logging.Logger, log_generic_fallback: bool = True
) -> None:
    logged_details = False

    for getter_name in (
        "get_transaction_problems",
        "get_gpg_signature_problems",
        "get_problems",
    ):
        getter = getattr(transaction, getter_name, None)
        if getter is None:
            continue
        try:
            problems = list(getter() or [])
        except Exception as e:
            logger.debug("Could not read %s: %s", getter_name, e)
            continue
        for problem in problems:
            problem_text = str(problem)
            if "NO_PROBLEM" in problem_text:
                continue
            logger.error("%s: %s", getter_name, problem_text)
            logged_details = True

    if _transaction_has_errors(transaction, logger):
        logged_details = True

    if not logged_details and log_generic_fallback:
        logger.error(
            "libdnf5 did not provide package-specific failure details. "
            "Try rerunning with `dnf5 upgrade --downloadonly --refresh` to identify the failed package or mirror."
        )


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


class _DownloadCallbacks(dnf5_repo.DownloadCallbacks):
    """Capture package download failures reported through libdnf5/librepo."""

    def __init__(self, logger: logging.Logger) -> None:
        super().__init__()
        self.logger = logger
        self.had_failure = False
        self._download_labels: dict[str, str] = {}
        self._mirror_failures: dict[str, list[str]] = {}

    def add_new_download(self, user_data, description: str, total_to_download: float):
        self._download_labels[str(user_data)] = (
            str(description) if description else str(user_data)
        )
        return user_data

    def _download_label(self, user_cb_data) -> str:
        return self._download_labels.get(str(user_cb_data), str(user_cb_data))

    def progress(
        self, user_cb_data, total_to_download: float, downloaded: float
    ) -> int:
        return self.OK

    def mirror_failure(self, user_cb_data, msg: str, url: str, metadata: str) -> int:
        label = self._download_label(user_cb_data)
        detail = f"{url}: {msg}" if url else msg
        if metadata:
            detail = f"{metadata}: {detail}"
        self._mirror_failures.setdefault(label, []).append(detail)
        return self.OK

    def end(self, user_cb_data, status: int, msg: str) -> int:
        if status == self.TransferStatus_ERROR:
            self.had_failure = True
            label = self._download_label(user_cb_data)
            self.logger.error(
                "Download failed for %s: %s", label, msg or "unknown error"
            )
            for failure in self._mirror_failures.get(label, [])[-3:]:
                self.logger.error("    Mirror failure: %s", failure)
        return self.OK

    def fastest_mirror(self, user_cb_data, stage: int, ptr: str) -> int:
        return self.OK


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
        self.script_errors: list[str] = []

    @property
    def had_script_error(self) -> bool:
        return bool(self.script_errors)

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
        failure = f"Scriptlet for {_format_nevra(nevra)} exited with code {return_code}"
        self.script_errors.append(failure)
        self.logger.error(
            "    %s",
            failure,
        )


@dataclass(frozen=True)
class DnfTransactionOutcome:
    success: bool
    transaction_id: int | None = None
    packages: tuple[str, ...] = ()
    error: str | None = None
    changed: bool = False
    interrupted: bool = False


def _transaction_package_names(transaction) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                item.get_package().get_name()
                for item in transaction.get_transaction_packages()
            }
        )
    )


def _installed_package_specs(base: dnf5_base.Base) -> frozenset[str]:
    installed_query = dnf5_rpm.PackageQuery(base)
    installed_query.filter_installed()
    specs: set[str] = set()
    for package in installed_query:
        specs.add(package.get_name())
        specs.add(f"{package.get_name()}.{package.get_arch()}")
    return frozenset(specs)


def _latest_history_id(base: dnf5_base.Base) -> int:
    try:
        return int(base.get_transaction_history().get_latest_transaction().get_id())
    except Exception:
        return 0


def _find_history_transaction_id(
    previous_id: int, description: str, tx_logger: logging.Logger
) -> int | None:
    """Find the exact history entry created by a named transaction.

    Never use ``history undo last`` here: another package-management client
    could otherwise make "last" refer to a transaction that nobara-updater did
    not create.
    """

    history_base = dnf5_base.Base()
    try:
        history_base.load_config()
        history_base.setup()
        history = history_base.get_transaction_history()
        latest_id = _latest_history_id(history_base)
        if latest_id <= previous_id:
            return None

        transactions = history.list_transactions(previous_id + 1, latest_id)
        for index in range(len(transactions) - 1, -1, -1):
            history_transaction = transactions[index]
            if history_transaction.get_description() == description:
                return int(history_transaction.get_id())
    except Exception as error:
        tx_logger.error("Could not identify the DNF history transaction: %s", error)
    finally:
        del history_base
    return None


def _prepare_transaction_base(
    tx_logger: logging.Logger, *, refresh_metadata: bool
) -> dnf5_base.Base:
    base = dnf5_base.Base()
    config = base.get_config()
    if refresh_metadata:
        config.get_metadata_expire_option().set(0)
    config.get_obsoletes_option().set(True)
    # Exact history records are the rollback boundary for each package group.
    # Force recording even if the local dnf.conf disables it.
    config.get_history_record_option().set(True)

    base.load_config()
    base.setup()
    sack = base.get_repo_sack()
    sack.create_repos_from_system_configuration()
    if refresh_metadata:
        _expire_enabled_repositories(base, tx_logger)
    sack.load_repos()
    return base


def _resolve_failure_reason(
    transaction,
    *,
    strict_logs: bool = False,
    allowed_log_problems: set[int] | None = None,
) -> str | None:
    conflicts = [
        package.get_nevra() for package in transaction.get_conflicting_packages()
    ]
    if conflicts:
        return "Package conflicts: " + ", ".join(conflicts)

    broken = [
        package.get_nevra()
        for package in transaction.get_broken_dependency_packages()
    ]
    if broken:
        return "Broken package dependencies: " + ", ".join(broken)

    if strict_logs:
        nonfatal_problems = {
            dnf5_base.GoalProblem_NO_PROBLEM,
            dnf5_base.GoalProblem_ALREADY_INSTALLED,
            dnf5_base.GoalProblem_INSTALLED_LOWEST_VERSION,
            dnf5_base.GoalProblem_HINT_ALTERNATIVES,
            dnf5_base.GoalProblem_HINT_ICASE,
        }
        if allowed_log_problems:
            nonfatal_problems.update(allowed_log_problems)
        for resolve_log in transaction.get_resolve_logs():
            if resolve_log.get_problem() not in nonfatal_problems:
                return f"Package resolution failed: {resolve_log.to_string()}"
    return None


def _execute_resolved_transaction(
    base: dnf5_base.Base,
    transaction,
    tx_logger: logging.Logger,
    description: str,
) -> DnfTransactionOutcome:
    packages = _transaction_package_names(transaction)
    if transaction.empty():
        tx_logger.info("Nothing to do.")
        return DnfTransactionOutcome(True, packages=packages)

    _log_transaction_packages(transaction, tx_logger)
    download_callbacks = _DownloadCallbacks(tx_logger)
    download_callbacks_ptr = dnf5_repo.DownloadCallbacksUniquePtr(download_callbacks)
    base.set_download_callbacks(download_callbacks_ptr)

    tx_logger.info("Downloading packages...")
    try:
        transaction.download()
    except Exception as error:
        download_failure_logged = download_callbacks.had_failure
        tx_logger.error("DNF package download failed: %s", error)
        _log_transaction_failure_details(
            transaction,
            tx_logger,
            log_generic_fallback=not download_failure_logged,
        )
        return DnfTransactionOutcome(
            False,
            packages=packages,
            error=f"Package download failed: {error}",
        )

    previous_history_id = _latest_history_id(base)
    transaction.set_description(description)
    tx_logger.info("Running transaction...")

    # Keep both Python SWIG directors alive until transaction.run() returns.
    # Inline UniquePtr construction drops the Python proxy and causes callback
    # dispatch to abort with Swig::DirectorMethodException.
    callbacks = _UpgradeTransactionCallbacks(
        tx_logger, transaction.get_transaction_packages_count()
    )
    callbacks_ptr = dnf5_rpm.TransactionCallbacksUniquePtr(callbacks)
    transaction.set_callbacks(callbacks_ptr)

    try:
        result = transaction.run()
    except KeyboardInterrupt:
        transaction_id = _find_history_transaction_id(
            previous_history_id, description, tx_logger
        )
        tx_logger.error("DNF transaction was interrupted.")
        return DnfTransactionOutcome(
            False,
            transaction_id=transaction_id,
            packages=packages,
            error="DNF transaction was interrupted",
            changed=True,
            interrupted=True,
        )
    except Exception as error:
        transaction_id = _find_history_transaction_id(
            previous_history_id, description, tx_logger
        )
        tx_logger.error("DNF transaction failed: %s", error)
        return DnfTransactionOutcome(
            False,
            transaction_id=transaction_id,
            packages=packages,
            error=f"DNF transaction raised an exception: {error}",
            # Once rpm execution starts, conservatively require rollback even
            # if libdnf5 failed before it could persist a history record.
            changed=True,
        )

    transaction_id = _find_history_transaction_id(
        previous_history_id, description, tx_logger
    )
    item_errors = _transaction_has_errors(transaction, tx_logger)
    if (
        result != dnf5_base.Transaction.TransactionRunResult_SUCCESS
        or item_errors
        or callbacks.had_script_error
    ):
        result_text = dnf5_base.Transaction.transaction_result_to_string(result)
        error_parts = [f"DNF transaction result: {result_text}"]
        if callbacks.script_errors:
            error_parts.extend(callbacks.script_errors)
        tx_logger.error("DNF transaction failed: %s", "; ".join(error_parts))
        for problem in transaction.get_transaction_problems():
            tx_logger.error(problem)
        return DnfTransactionOutcome(
            False,
            transaction_id=transaction_id,
            packages=packages,
            error="; ".join(error_parts),
            changed=True,
        )

    tx_logger.info("DNF transaction complete.")
    return DnfTransactionOutcome(
        True,
        transaction_id=transaction_id,
        packages=packages,
        changed=True,
    )


def run_package_upgrade_transaction(
    package_names: list[str] | tuple[str, ...],
    logger: logging.Logger | None = None,
    *,
    description: str | None = None,
    refresh_metadata: bool = False,
) -> DnfTransactionOutcome:
    """Upgrade only the supplied package specs in one DNF transaction."""

    tx_logger = logger if logger is not None else logging.getLogger()
    targets = tuple(dict.fromkeys(name for name in package_names if name))
    if not targets:
        return DnfTransactionOutcome(True)

    transaction_description = (
        f"{description or 'nobara-updater package transaction'} [{uuid.uuid4()}]"
    )
    base: dnf5_base.Base | None = None
    try:
        base = _prepare_transaction_base(
            tx_logger, refresh_metadata=refresh_metadata
        )
        goal = dnf5_base.Goal(base)
        job_settings = dnf5_base.GoalJobSettings()
        job_settings.set_best(True)
        job_settings.set_skip_broken(False)
        job_settings.set_skip_unavailable(False)
        installed_specs = _installed_package_specs(base)
        for target in targets:
            if target == "*" or target in installed_specs:
                goal.add_upgrade(target, job_settings)
            else:
                # updatechecker also reports INSTALL items introduced by the
                # all-upgrades solution (new dependencies and obsoleting
                # replacement packages).  They are legitimate targets because
                # they came from the pending list, even though no package with
                # that name is installed yet.
                goal.add_install(target, job_settings)

        if targets == ("*",):
            try:
                install_only_names = base.get_config().installonlypkgs
            except AttributeError:
                install_only_names = []
            _add_resolvable_installonly_upgrades(base, goal, install_only_names)

        transaction = goal.resolve()
        _log_transaction_resolve_problems(transaction, tx_logger)
        resolve_failure = _resolve_failure_reason(transaction, strict_logs=True)
        if resolve_failure is not None:
            return DnfTransactionOutcome(
                False,
                packages=_transaction_package_names(transaction),
                error=resolve_failure,
            )

        return _execute_resolved_transaction(
            base, transaction, tx_logger, transaction_description
        )
    except KeyboardInterrupt:
        tx_logger.error("DNF transaction preparation was interrupted.")
        return DnfTransactionOutcome(
            False,
            error="DNF transaction preparation was interrupted",
            interrupted=True,
        )
    except Exception as error:
        tx_logger.error("DNF transaction failed: %s", error)
        return DnfTransactionOutcome(False, error=f"DNF transaction failed: {error}")
    finally:
        if base is not None:
            del base


def revert_package_transaction(
    transaction_id: int,
    logger: logging.Logger | None = None,
    *,
    description: str | None = None,
) -> DnfTransactionOutcome:
    """Revert one exact DNF history transaction, including its dependencies."""

    tx_logger = logger if logger is not None else logging.getLogger()
    transaction_description = (
        f"{description or f'nobara-updater revert transaction {transaction_id}'} "
        f"[{uuid.uuid4()}]"
    )
    base: dnf5_base.Base | None = None
    try:
        base = _prepare_transaction_base(tx_logger, refresh_metadata=False)
        history_transactions = base.get_transaction_history().list_transactions(
            transaction_id, transaction_id
        )
        if len(history_transactions) != 1:
            error = f"DNF history transaction {transaction_id} was not found"
            tx_logger.error(error)
            return DnfTransactionOutcome(False, error=error)

        history_transaction = history_transactions[0]
        incomplete_history = (
            history_transaction.get_state() != dnf5_trans.TransactionState_OK
        )
        settings = dnf5_base.GoalJobSettings()
        if incomplete_history:
            # A failed/interrupted rpm transaction can contain history items
            # that never reached the installed state.  Skip those mismatches
            # while reverting the items that did complete.
            settings.set_ignore_installed(True)
            tx_logger.info(
                "Transaction %s is incomplete; reverting its applied subset.",
                transaction_id,
            )

        goal = dnf5_base.Goal(base)
        # Match dnf5 history undo: dependency packages installed as part of the
        # original transaction may need to be erased by the inverse goal.
        goal.set_allow_erasing(True)
        goal.add_revert_transactions(history_transactions, settings)
        transaction = goal.resolve()
        _log_transaction_resolve_problems(transaction, tx_logger)
        allowed_replay_mismatches = set()
        if incomplete_history:
            allowed_replay_mismatches.update(
                {
                    dnf5_base.GoalProblem_INSTALLED_IN_DIFFERENT_VERSION,
                    dnf5_base.GoalProblem_NOT_INSTALLED,
                    dnf5_base.GoalProblem_NOT_INSTALLED_FOR_ARCHITECTURE,
                }
            )
        resolve_failure = _resolve_failure_reason(
            transaction,
            strict_logs=True,
            allowed_log_problems=allowed_replay_mismatches,
        )
        if resolve_failure is not None:
            return DnfTransactionOutcome(False, error=resolve_failure)

        return _execute_resolved_transaction(
            base, transaction, tx_logger, transaction_description
        )
    except KeyboardInterrupt:
        tx_logger.error("Rollback of DNF transaction %s was interrupted.", transaction_id)
        return DnfTransactionOutcome(
            False,
            error=f"Rollback of transaction {transaction_id} was interrupted",
            interrupted=True,
        )
    except Exception as error:
        tx_logger.error("Could not revert DNF transaction %s: %s", transaction_id, error)
        return DnfTransactionOutcome(
            False, error=f"Could not revert transaction {transaction_id}: {error}"
        )
    finally:
        if base is not None:
            del base


def run_system_upgrade_transaction(logger: logging.Logger | None = None) -> bool:
    """Backward-compatible all-package upgrade entry point."""

    return run_package_upgrade_transaction(
        ["*"],
        logger,
        description="nobara-updater system upgrade",
        refresh_metadata=True,
    ).success
