"""Apply installonly retention after startup and recovery have been confirmed."""
from __future__ import annotations

import logging
import os
from pathlib import Path
import re

import libdnf5.base as base_api
import libdnf5.repo as repo_api
import libdnf5.rpm as rpm_api
import libdnf5.transaction as trans

from .update_state import UpdateError

LOG = logging.getLogger(__name__)


def kernel_evr(package):
    return (package.get_epoch(), package.get_version(), package.get_release(), package.get_arch())


def package_kernel(package) -> str:
    return f"{package.get_version()}-{package.get_release()}.{package.get_arch()}"


def uname_provides(package) -> set[str]:
    return {value for dependency in package.get_provides()
            for name, separator, value in [str(dependency).partition(" = ")]
            if separator and name.startswith("kernel-") and name.endswith("uname-r")}


def versioned_kmod_kernel(package) -> str | None:
    """Identify generated per-kernel RPMs, never generic kmod tools/metapackages."""
    name = package.get_name()
    if not name.startswith("kmod-"):
        return None
    match = re.search(r"-([0-9][A-Za-z0-9_.+~]*-[A-Za-z0-9_.+~]+\."
                      + re.escape(package.get_arch()) + r")$", name)
    return match[1] if match else None


def plan_cleanup(base, *, obsolete_kernels=()):
    """Remove obsolete kernel sets and excess versions after a confirmed boot."""
    limit = base.get_config().get_installonly_limit_option().get_value()
    installed = rpm_api.PackageQuery(base)
    installed.filter_installed()
    installonly = rpm_api.PackageQuery(installed)
    installonly.filter_installonly()
    installonly_ids = {p.get_id().id for p in installonly}
    candidates = rpm_api.PackageQuery(installonly)
    # libdnf sorts RPM EVRs per name/architecture, including epochs/releases.
    if limit:
        candidates.filter_latest_evr(-limit)
    candidates = {p.get_id().id: p for p in candidates} if limit else {}
    kernel_packages = {}
    core_packages = set()
    core_versions = set()
    runtime_versions = set()
    kmods = {}
    for package in installed:
        name = package.get_name()
        identifier = package.get_id().id
        kernel = versioned_kmod_kernel(package)
        if kernel:
            kmods[identifier] = (package, {kernel})
        if name != "kernel" and not name.startswith("kernel-"):
            continue
        versions = uname_provides(package)
        if not versions and identifier not in installonly_ids:
            # kernel-headers/tools and unrelated similarly named packages
            # are not a kernel version's removable package set.
            continue
        versions.add(package_kernel(package))
        kernel_packages[identifier] = (package, versions)
        core = name.endswith("-core") and "modules" not in name
        if name == "kernel":
            # Older layouts can put the image in kernel itself instead of
            # kernel-core. The modern kernel metapackage has no image.
            core = any(path in {f"/boot/vmlinuz-{k}", f"/usr/lib/modules/{k}/vmlinuz"}
                       for k in versions for path in package.get_files())
        if core:
            core_packages.add(identifier)
            core_versions.update(versions)
        if core or "modules" in name or name == "kernel":
            runtime_versions.update(versions)
    root = Path(base.get_config().get_installroot_option().get_value())
    # Missing repo metadata alone is never grounds for removal. Explicit
    # obsolete versions come from the successfully validated update plan.
    # Orphans need both a missing core RPM and a missing actual boot image.
    orphans = runtime_versions | {kernel for _, versions in kmods.values() for kernel in versions}
    orphans -= core_versions
    orphans = {kernel for kernel in orphans
               if not (root / "boot" / ("vmlinuz-" + kernel)).is_file()
               and not (root / "usr/lib/modules" / kernel / "vmlinuz").is_file()}
    if not candidates and not obsolete_kernels and not orphans:
        return None
    running = base.get_rpm_package_sack().get_running_kernel()
    if running.get_id().id <= 0:
        raise UpdateError("Cannot identify the running kernel's RPM; keeping all kernels.")
    running_evr = kernel_evr(running)
    protected = uname_provides(running) | {package_kernel(running), os.uname().release}
    # Never interpret a package preinstalled for a newer kernel as old debris.
    obsolete = set(obsolete_kernels) | {kernel for kernel in orphans
                                       if rpm_api.rpmvercmp(kernel, package_kernel(running)) < 0}
    for identifier, (package, versions) in kernel_packages.items():
        if versions & obsolete:
            candidates[identifier] = package
    for identifier, (package, versions) in kmods.items():
        if versions & obsolete:
            candidates[identifier] = package
    if not candidates:
        return None
    for identifier, package in list(candidates.items()):
        versions = kernel_packages.get(identifier, kmods.get(identifier, (None, set())))[1]
        if kernel_evr(package) == running_evr or versions & protected:
            del candidates[identifier]
    # Generated kmod RPMs often use @commandline and a driver EVR rather
    # than the kernel EVR. Retire them only when their exact kernel goes.
    remaining_core = set()
    for identifier in core_packages:
        if identifier not in candidates:
            remaining_core.update(kernel_packages[identifier][1])
    retired = set(obsolete)
    for identifier in candidates:
        if identifier in kernel_packages:
            retired.update((kernel_packages[identifier][1] & core_versions) - remaining_core)
    for identifier, (package, versions) in kmods.items():
        if versions & retired and not versions & protected:
            candidates[identifier] = package
    if not candidates:
        return None
    settings = base_api.GoalJobSettings()
    settings.set_clean_requirements_on_remove(False)
    goal = base_api.Goal(base)
    for package in candidates.values():
        goal.add_rpm_remove(package, settings)
    # The candidates already implement retention and preserve the running
    # kernel's whole EVRA set. A second implicit limit pass can otherwise
    # remove its matching devel/modules RPMs to reach the numeric limit.
    option = base.get_config().get_installonly_limit_option()
    option.set(0)
    try:
        transaction = goal.resolve()
    finally:
        option.set(limit)
    if transaction.get_problems() != base_api.GoalProblem_NO_PROBLEM:
        raise UpdateError("Old-kernel cleanup could not be resolved:\n" +
                          "\n".join(transaction.get_resolve_logs_as_strings()))
    allowed = {package.get_full_nevra() for package in candidates.values()}
    for item in transaction.get_transaction_packages():
        if item.get_action() != trans.TransactionItemAction_REMOVE or item.get_package().get_full_nevra() not in allowed:
            raise UpdateError("Old-kernel cleanup would also change " + item.get_package().get_full_nevra() +
                              "; keeping the existing kernels.")
    return transaction


class CleanupCallbacks(rpm_api.TransactionCallbacks):
    def __init__(self):
        super().__init__()
        self.errors = []

    def script_error(self, item, nevra, script_type, return_code):
        self.errors.append(f"{nevra.get_name()} scriptlet exited with code {return_code}")


def prune_kernel_packages(*, obsolete_kernels=()) -> None:
    """Use the installed RPM database only; keep the DNF lock through removal."""
    base = base_api.Base()
    base.load_config()
    config = base.get_config()
    config.get_clean_requirements_on_remove_option().set(False)
    config.get_protect_running_kernel_option().set(True)
    # Deliberately leave installonly_limit at the user's configured value.
    base.setup()
    if not base.lock_system_repo():
        raise UpdateError("Another package manager is running; old-kernel cleanup will retry later.")
    try:
        base.get_repo_sack().load_repos(repo_api.Repo.Type_SYSTEM)
        transaction = plan_cleanup(base, obsolete_kernels=obsolete_kernels)
        if transaction is None or transaction.empty():
            return
        for item in transaction.get_transaction_packages():
            LOG.info("Removing obsolete or excess kernel package: %s", item.get_package().get_full_nevra())
        transaction.set_description("Nobara confirmed-boot kernel retention")
        callbacks = CleanupCallbacks()
        callbacks_ptr = rpm_api.TransactionCallbacksUniquePtr(callbacks)
        transaction.set_callbacks(callbacks_ptr)
        # The worker's private state uses 077; DNF's system state and RPM
        # scriptlets must retain normal package-manager file permissions.
        previous_umask = os.umask(0o022)
        try:
            result = transaction.run()
        finally:
            os.umask(previous_umask)
        for message in transaction.get_rpm_messages():
            LOG.info("Kernel cleanup: %s", message)
        if result != base_api.Transaction.TransactionRunResult_SUCCESS or callbacks.errors:
            details = [base_api.Transaction.transaction_result_to_string(result),
                       *transaction.get_transaction_problems(), *callbacks.errors]
            raise UpdateError("Old-kernel cleanup did not finish: " + "; ".join(details))
        LOG.info("Kernel retention cleanup finished (installonly_limit=%s).",
                 config.get_installonly_limit_option().get_value())
    finally:
        base.unlock_system_repo()
