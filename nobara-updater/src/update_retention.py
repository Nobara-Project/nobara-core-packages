"""Apply Nobara's kernel retention policy while no update is pending.

Nobara keeps the two newest standard kernels and one Easy Anti-Cheat kernel:
the EAC build of the booted kernel's version, or the newest EAC kernel when
there is none. /boot then holds three kernels plus the rescue image. The
running kernel is always kept as well.

The policy runs before every preparation, against the booted kernel, and
again after an update's boot has been confirmed, against the new kernel. It
never runs inside an update transaction: until the new kernel has booted
(including its DKMS/akmods/kmod modules), the old ones remain the fallback.

An update that installs new kernels briefly needs room for them and for the
recovery copy of the booted kernel beside every kept kernel, which a 1 GiB
/boot often lacks. When it does, free_boot_space() retires, before the
update, only as many of the kernels it would replace as the shortfall needs:
older standard kernels first, then EAC kernels with a newer EAC build
inbound. The booted kernel is always kept, and the recovery entry boots it.
"""
from __future__ import annotations

from functools import cmp_to_key
import logging
import os
from pathlib import Path
import re

import libdnf5.base as base_api
import libdnf5.repo as repo_api
import libdnf5.rpm as rpm_api
import libdnf5.transaction as trans

from .update_boot import eac_kernel
from .update_state import UpdateError

LOG = logging.getLogger(__name__)

KERNEL_LIMIT = 2
EAC_KERNEL_LIMIT = 1


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


def core_kernel(package) -> bool:
    """Whether this RPM provides a kernel's boot image (kernel-core, kernel-eac-core...)."""
    name = package.get_name()
    if name == "kernel":
        # Older layouts can put the image in kernel itself instead of
        # kernel-core. The modern kernel metapackage has no image.
        versions = uname_provides(package) | {package_kernel(package)}
        return any(path in {f"/boot/vmlinuz-{k}", f"/usr/lib/modules/{k}/vmlinuz"}
                   for k in versions for path in package.get_files())
    return name.startswith("kernel-") and name.endswith("-core") and "modules" not in name


def core_uname(package) -> str:
    """The kernel release a core RPM boots: its kernel-uname-r, else its EVRA."""
    provided = uname_provides(package)
    return next(iter(provided)) if len(provided) == 1 else package_kernel(package)


def compare_kernels(first, second) -> int:
    return (int(first.get_epoch() or 0) - int(second.get_epoch() or 0)
            or rpm_api.rpmvercmp(first.get_version(), second.get_version())
            or rpm_api.rpmvercmp(first.get_release(), second.get_release()))


def same_build(first: str, second: str) -> bool:
    """7.3.1-202.eac.nobara.fc44.x86_64 is the EAC build of 7.3.1-202.nobara.fc44.x86_64."""
    return first.replace(".eac.", ".") == second.replace(".eac.", ".")


def excess_kernels(cores, *, running: str, keep=()) -> set[str]:
    """Kernel versions beyond the two newest standard kernels and one EAC kernel.

    cores maps each kernel version to its core RPM. Each kernel package name
    and architecture is counted separately, so LTS or other variants never
    displace each other. The running kernel and versions in keep are never
    excess.
    """
    groups = {}
    for kernel, package in cores.items():
        name = "kernel-core" if package.get_name() == "kernel" else package.get_name()
        groups.setdefault((name, package.get_arch(), eac_kernel(kernel)), []).append((kernel, package))
    excess = set()
    for (_, _, eac), members in groups.items():
        members.sort(key=cmp_to_key(lambda a, b: compare_kernels(b[1], a[1])))
        kernels = [kernel for kernel, _ in members]
        if eac:
            # Follow the booted kernel, so an update's new EAC kernel only
            # replaces the old one after the new kernel has booted.
            kept = [k for k in kernels if same_build(k, running)][:EAC_KERNEL_LIMIT] or kernels[:EAC_KERNEL_LIMIT]
        else:
            kept = kernels[:KERNEL_LIMIT]
        excess.update(set(kernels) - set(kept))
    return excess - {running, *keep}


def kernel_sets(installed, installonly_ids):
    """Group installed kernel RPMs and generated kmod RPMs by kernel version."""
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
        core = core_kernel(package)
        if core:
            core_packages.add(identifier)
            core_versions.update(versions)
        if core or "modules" in name or name == "kernel":
            runtime_versions.update(versions)
    return kernel_packages, kmods, core_packages, core_versions, runtime_versions


def plan_cleanup(base, *, obsolete_kernels=()):
    """Remove obsolete kernel sets and excess versions while no update is pending."""
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
    kernel_packages, kmods, core_packages, core_versions, runtime_versions = kernel_sets(installed, installonly_ids)
    # installonly_limit=0 keeps every kernel; otherwise Nobara's per-flavor
    # limits apply, which are stricter than DNF's default of three per name.
    # Explicitly obsolete kernels are removed anyway; they never take one of
    # the policy's slots from a kernel that is kept.
    cores = {core_uname(kernel_packages[i][0]): kernel_packages[i][0] for i in core_packages}
    policy = excess_kernels({k: v for k, v in cores.items() if k not in set(obsolete_kernels)},
                            running=os.uname().release) if limit else set()
    root = Path(base.get_config().get_installroot_option().get_value())
    # Missing repo metadata alone is never grounds for removal. Explicit
    # obsolete versions come from the successfully validated update plan.
    # Orphans need both a missing core RPM and a missing actual boot image.
    orphans = runtime_versions | {kernel for _, versions in kmods.values() for kernel in versions}
    orphans -= core_versions
    orphans = {kernel for kernel in orphans
               if not (root / "boot" / ("vmlinuz-" + kernel)).is_file()
               and not (root / "usr/lib/modules" / kernel / "vmlinuz").is_file()}
    if not candidates and not obsolete_kernels and not orphans and not policy:
        return None
    running = base.get_rpm_package_sack().get_running_kernel()
    if running.get_id().id <= 0:
        raise UpdateError("Cannot identify the running kernel's RPM; keeping all kernels.")
    running_evr = kernel_evr(running)
    protected = uname_provides(running) | {package_kernel(running), os.uname().release}
    # Never interpret a package preinstalled for a newer kernel as old debris.
    obsolete = set(obsolete_kernels) | policy | {kernel for kernel in orphans
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


class InboundKernel:
    """A kernel core RPM an update installs, from its planned NEVRA."""

    def __init__(self, name: str, nevra: str):
        match = re.fullmatch(re.escape(name) + r"-(?:(\d+):)?([^-]+)-(.+)\.([^.]+)", nevra)
        if not match:
            raise UpdateError(f"Cannot identify the planned kernel {nevra}.")
        self.name, self.epoch = name, match[1] or "0"
        self.version, self.release, self.arch = match[2], match[3], match[4]

    def get_name(self): return self.name
    def get_epoch(self): return self.epoch
    def get_version(self): return self.version
    def get_release(self): return self.release
    def get_arch(self): return self.arch


def inbound_kernels(packages) -> dict[str, InboundKernel]:
    """Kernel versions whose boot image the update installs, from (name, nevra) pairs."""
    kernels = {}
    for name, nevra in packages:
        if name.startswith("kernel") and name.endswith("-core") and "modules" not in name:
            package = InboundKernel(name, nevra)
            kernels[package_kernel(package)] = package
    return kernels


def boot_room_candidates(cores, incoming, *, running: str) -> list[str]:
    """Installed kernels the update replaces, in the order to retire them early.

    These are the versions the policy removes once the update's kernel has
    booted, which in turn is the newest inbound kernel of the booted kind
    (standard or EAC). Standard kernels come first, oldest first; EAC kernels
    follow, as the inbound EAC build replaces them. Never the booted kernel.
    """
    kind = [kernel for kernel in incoming if eac_kernel(kernel) == eac_kernel(running)]
    future = max(kind, key=cmp_to_key(lambda a, b: compare_kernels(incoming[a], incoming[b])), default=running)
    excess = excess_kernels({**cores, **incoming}, running=future, keep={running}) - set(incoming)
    return sorted(excess, key=cmp_to_key(lambda a, b: (eac_kernel(a) - eac_kernel(b))
                                         or compare_kernels(cores[a], cores[b])))


def kernel_boot_bytes(kernel: str, boot: Path = Path("/boot")) -> int:
    """Space on /boot that removing a kernel version gives back."""
    total = 0
    for name in (f"vmlinuz-{kernel}", f".vmlinuz-{kernel}.hmac", f"initramfs-{kernel}.img",
                 f"initramfs-{kernel}kdump.img", f"System.map-{kernel}", f"config-{kernel}"):
        try:
            info = (boot / name).lstat()
        except OSError:
            continue
        if not os.path.islink(boot / name):
            total += info.st_blocks * 512 or info.st_size
    return total


def kernels_to_retire(candidates, shortfall: int, boot: Path = Path("/boot")) -> list[str]:
    """The shortest prefix of candidates that frees shortfall bytes, or none."""
    chosen, freed = [], 0
    for kernel in candidates:
        if freed >= shortfall:
            break
        chosen.append(kernel)
        freed += kernel_boot_bytes(kernel, boot)
    if freed < shortfall:
        LOG.warning("Retiring the kernels this update replaces would free only %d of the %d MiB it needs on /boot; "
                    "keeping them.", freed // 1024**2, -(-shortfall // 1024**2))
        return []
    return chosen


def installed_cores(base) -> dict:
    installed = rpm_api.PackageQuery(base)
    installed.filter_installed()
    installonly = rpm_api.PackageQuery(installed)
    installonly.filter_installonly()
    kernel_packages, _, core_packages, _, _ = kernel_sets(installed, {p.get_id().id for p in installonly})
    return {core_uname(kernel_packages[i][0]): kernel_packages[i][0] for i in core_packages}


def free_boot_space(packages, shortfall: int, *, boot: Path = Path("/boot")) -> list[str]:
    """Retire just enough of the kernels an update replaces to fit it on /boot.

    packages are the update's inbound (name, nevra) pairs. Returns the kernel
    versions removed; nothing is removed when even all candidates would not
    free enough, since the update still could not be prepared.
    """
    incoming = inbound_kernels(packages)
    if not incoming:
        return []
    base = base_api.Base()
    base.load_config()
    if not base.get_config().get_installonly_limit_option().get_value():
        # installonly_limit=0: the administrator keeps every kernel.
        return []
    base.setup()
    base.get_repo_sack().load_repos(repo_api.Repo.Type_SYSTEM)
    candidates = boot_room_candidates(installed_cores(base), incoming, running=os.uname().release)
    chosen = kernels_to_retire(candidates, shortfall, boot)
    if not chosen:
        return []
    LOG.info("/boot needs %d MiB more for this update. Retiring %s now, which this update replaces; "
             "the booted kernel %s is kept and remains the recovery kernel.",
             -(-shortfall // 1024**2), ", ".join(chosen), os.uname().release)
    prune_kernel_packages(obsolete_kernels=chosen)
    return chosen
