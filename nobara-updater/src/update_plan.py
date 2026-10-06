"""Prepare one libdnf5 transaction without changing installed packages."""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import libdnf5.base as base_api
import libdnf5.common as common
import libdnf5.conf as conf
import libdnf5.repo as repo_api
import libdnf5.rpm as rpm_api
import libdnf5.transaction as trans

from .update_migrations import BGRT_FILES, add_codec_migration, add_plymouth_migration, machine_product, plan_migrations
from .update_boot import changes_kernel, check_selection_support, boot_space_requirements, installed_boot_kernels
from .update_policy import execution_policy, format_restart_reasons
from .update_origins import PackageOriginError, protect_local_packages, validate_local_packages, annotate_failure
from .update_state import UpdateError, file_digest, os_release, rpm_fingerprint
from .update_health import PackageHealth
from .update_repositories import migrated_media_urls

LOG = logging.getLogger(__name__)
ESSENTIAL = {"glibc", "rpm", "dnf5", "libdnf5", "systemd", "bash", "coreutils", "python3", "nobara-updater"}


class DownloadProgress(repo_api.DownloadCallbacks):
    # libdnf5 announces every package before librepo starts. Report the
    # running total at this interval (seconds) and once all are complete.
    INTERVAL = 5

    def __init__(self):
        super().__init__()
        self.sizes, self.received = [], []
        self.total = self.downloaded = self.finished = 0
        self.reported = time.monotonic()

    def add_new_download(self, user_data, description, total_to_download):
        LOG.info("Downloading %s (%s bytes)", description, int(total_to_download))
        self.sizes.append(max(0, int(total_to_download)))
        self.received.append(0)
        self.total += self.sizes[-1]
        # Package downloads all carry the same (null) user_data. Return our
        # own 1-based index; libdnf5 passes it back to the other callbacks.
        return len(self.sizes)

    def _index(self, user_cb_data):
        return user_cb_data - 1 if isinstance(user_cb_data, int) and 0 < user_cb_data <= len(self.sizes) else None

    def _update(self, index, received):
        # A retry on another mirror restarts the count; never exceed the size.
        received = min(max(0, int(received)), self.sizes[index])
        self.downloaded += received - self.received[index]
        self.received[index] = received

    def report(self):
        self.reported = time.monotonic()
        percent = 100 * self.downloaded // self.total if self.total else 100
        LOG.info("Downloaded %.1f of %.1f MiB (%d%%), %d of %d packages", self.downloaded / 1024**2,
                 self.total / 1024**2, percent, self.finished, len(self.sizes))

    def progress(self, user_cb_data, total_to_download, downloaded):
        index = self._index(user_cb_data)
        if index is not None:
            self._update(index, downloaded)
            if time.monotonic() - self.reported >= self.INTERVAL:
                self.report()
        return self.OK

    def mirror_failure(self, user_cb_data, msg, url, metadata):
        LOG.warning("Download mirror failed: %s: %s", url, msg)
        return self.OK

    def end(self, user_cb_data, status, msg):
        if status == self.TransferStatus_ERROR:
            LOG.error("Download failed: %s", msg)
            return self.OK
        index = self._index(user_cb_data)
        if index is not None:
            # A package that is already present reports no progress.
            self._update(index, self.sizes[index])
            self.finished += 1
            if self.finished == len(self.sizes):
                self.report()
        return self.OK

    def fastest_mirror(self, user_cb_data, stage, ptr):
        return self.OK


def configure_base(job: Path, release: str | None = None, *, codecs: bool = False):
    base = base_api.Base()
    base.load_config()
    config = base.get_config()
    for name, value in {
        "cachedir": str(job / "cache"), "system_cachedir": str(job / "cache"),
        "logdir": str(job), "destdir": str(job / "packages"),
        "metadata_expire": 0, "obsoletes": True, "best": True,
        "skip_broken": False, "skip_unavailable": False, "keepcache": True,
        "pkg_gpgcheck": True, "localpkg_gpgcheck": True,
        # Keep fallback kernels until boot confirmation. update_retention
        # applies the configured limit afterwards, outside this transaction.
        "installonly_limit": 0, "clean_requirements_on_remove": False,
        "tsflags": [],
    }.items():
        getattr(config, f"get_{name}_option")().set(value)
    config.get_optional_metadata_types_option().set(["comps", "filelists"])
    if release is not None:
        base.get_vars().set("releasever", release, conf.Vars.Priority_COMMANDLINE)
    base.setup()
    # C++ defaults are WRITE and NON_BLOCKING. The 5.4.3 Python bindings
    # reject the explicit utils enum constants in this overloaded method.
    if not base.lock_system_repo():
        raise UpdateError("Another package manager is running. Try again after it finishes.")
    sack = base.get_repo_sack()
    sack.create_repos_from_system_configuration()
    # Existing freeworld packages record a previous codec opt-in, even if a
    # repository-file replacement accidentally disabled the codec repo.
    installed_names = subprocess.run(["rpm", "-qa", "--qf", "%{NAME}\n"],
                                     check=True, capture_output=True, text=True).stdout.splitlines()
    codecs = codecs or any(name.endswith("-freeworld") for name in installed_names)
    base.nobara_codecs = codecs
    base.nobara_enable_codecs = False
    base.nobara_migrate_media = False
    for repo in repo_api.RepoQuery(base):
        if repo.get_id() == "nobara-pikaos-additional":
            enabled = repo.get_config().get_enabled_option().get_value()
            base.nobara_codecs = codecs or enabled
            if codecs and not enabled:
                base.nobara_enable_codecs = True
                repo.get_config().get_enabled_option().set(True)
        if repo.get_config().get_enabled_option().get_value():
            urls = migrated_media_urls(repo, base.get_vars().get_value("basearch"))
            if urls is not None:
                repo.get_config().get_baseurl_option().set(list(urls))
                base.nobara_migrate_media = True
                LOG.info("Using the current Nobara media repository endpoint; the legacy endpoint contains old release packages. Configuration will be saved after installation.")
            repo.get_config().get_skip_if_unavailable_option().set(False)
            repo.get_config().get_pkg_gpgcheck_option().set(True)
            repo.expire()
    sack.load_repos()
    # Do not undo the early updater/initramfs-helper upgrade during distro-sync.
    # A newer local build must also survive older repository metadata.
    installed = rpm_api.PackageQuery(base)
    installed.filter_installed()
    installed.filter_name(["nobara-updater", "drm-awaiter"])
    for package in installed:
        older = rpm_api.PackageQuery(base)
        older.filter_available()
        older.filter_name([package.get_name()])
        older.filter_evr([package.get_evr()], common.QueryCmp_LT)
        if not older.empty():
            base.get_rpm_package_sack().add_user_excludes(older)
            LOG.info("Keeping %s %s or newer; older repository builds are excluded from this transaction.", package.get_name(), package.get_evr())
    return base


def target_release(base, current: str) -> str:
    query = rpm_api.PackageQuery(base)
    query.filter_available()
    query.filter_provides(["system-release(releasever)"])
    query.filter_latest_evr()
    releases = set()
    for pkg in query:
        # Fedora's release package must not replace the Nobara identity.
        if not pkg.get_name().startswith("nobara-release"):
            continue
        for provide in pkg.get_provides():
            match = re.fullmatch(r"system-release\(releasever\) = ([0-9]+(?:\.[0-9]+)*)", str(provide))
            if match:
                releases.add(match[1])
    if len(releases) > 1:
        raise UpdateError("Repositories disagree about the target Nobara release. Try again later.")
    if not releases:
        # Nobara's release-common owns /etc/os-release but does not currently
        # provide system-release(releasever). Its RPM Version is the distro
        # release. Identity subpackages must agree when they are present.
        query = rpm_api.PackageQuery(base)
        query.filter_available()
        query.filter_name(["nobara-release-common"])
        query.filter_latest_evr()
        releases = {p.get_version() for p in query}
        if not releases:
            raise UpdateError("The repositories do not contain nobara-release-common; cannot verify the target release.")
        if len(releases) != 1 or not all(re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", v) for v in releases):
            raise UpdateError("Cannot determine one target release from nobara-release-common.")
    release = releases.pop()
    if tuple(map(int, release.split("."))) < tuple(map(int, current.split("."))):
        raise UpdateError("Repositories advertise an older Nobara release; refusing a system downgrade.")
    return release


def installed_inventory(base) -> list[dict]:
    query = rpm_api.PackageQuery(base)
    query.filter_installed()
    return [dict(name=p.get_name(), arch=p.get_arch(), epoch=p.get_epoch(), version=p.get_version(),
                 release=p.get_release(), repo=p.get_from_repo_id()) for p in query]


def check_resolution(transaction, origins=()) -> None:
    if transaction.get_problems() != base_api.GoalProblem_NO_PROBLEM:
        # Explicit fixups can name packages already installed. Their notices
        # are useful internally, but must not bury the actual solver failure.
        details = [event.to_string() for event in transaction.get_resolve_logs()
                   if event.get_problem() != base_api.GoalProblem_ALREADY_INSTALLED]
        error = UpdateError("Package conflicts prevent this update:\n" + "\n".join(details))
        raise annotate_failure(error, origins, replacements=True)
    if transaction.get_conflicting_packages() or transaction.get_broken_dependency_packages():
        packages = list(transaction.get_conflicting_packages()) + list(transaction.get_broken_dependency_packages())
        error = UpdateError("DNF skipped conflicting or broken packages. The system update was not prepared.\n" +
                            "\n".join(p.get_full_nevra() for p in packages))
        raise annotate_failure(error, origins)


def validate_removals(records: list[dict], allowed: set[str]) -> None:
    inbound = {(item["name"], item["arch"]) for item in records if item["action"] in {"Install", "Upgrade", "Downgrade", "Reinstall"}}
    for item in records:
        if item["name"] == "nobara-updater" and item["action"] == "Downgrade":
            raise UpdateError("The update would downgrade nobara-updater and remove recovery support. A matching or newer updater must be available.")
        if item["action"] == "Replaced" and item["name"] in ESSENTIAL and (item["name"], item["arch"]) not in inbound:
            raise UpdateError(f"The update would replace essential package {item['name']}.{item['arch']} without a matching replacement.")
        if item["action"] != "Remove":
            continue
        name, arch = item["name"], item["arch"]
        if name in ESSENTIAL or name == "kernel" or name.startswith("kernel-"):
            raise UpdateError(f"The update would remove essential package {name}.{arch}.")
        if name not in allowed and f"{name}.{arch}" not in allowed and item.get("nevra") not in allowed:
            raise UpdateError(f"The update would also remove {name}.{arch}. Resolve this package conflict before updating.")


def projected_packages(base, transaction):
    """Return retained installed RPMs and the complete proposed final set."""
    retained = rpm_api.PackageQuery(base)
    retained.filter_installed()
    inbound, outbound = rpm_api.PackageSet(base), rpm_api.PackageSet(base)
    for item in transaction.get_transaction_packages():
        action = item.get_action()
        if action in {trans.TransactionItemAction_INSTALL, trans.TransactionItemAction_UPGRADE,
                      trans.TransactionItemAction_DOWNGRADE, trans.TransactionItemAction_REINSTALL}:
            inbound.add(item.get_package())
        elif action in {trans.TransactionItemAction_REMOVE, trans.TransactionItemAction_REPLACED}:
            outbound.add(item.get_package())
    retained.difference(outbound)
    final = rpm_api.PackageQuery(retained)
    final.update(inbound)
    return retained, final


def add_dnf_inhibitor_migration(base, migrations, origins):
    """Handle the optional plugin split missing from older mirrored DNF builds.

    This is a packaging transition, not a license to erase orphan packages.
    The updater's systemd services provide their own inhibition. All other
    DNF plugins (including actions, which enforces codec protection) remain
    subject to the normal solver and removal checks.
    """
    name = "libdnf5-plugin-systemd-inhibit"
    held = {p["nevra"] for p in origins if p["kind"] in {"local", "unknown"}}
    installed = rpm_api.PackageQuery(base)
    installed.filter_installed()
    installed.filter_name([name])
    for plugin in installed:
        if plugin.get_full_nevra() in held:
            continue
        available = rpm_api.PackageQuery(base)
        available.filter_available()
        available.filter_name([name])
        available.filter_arch([plugin.get_arch()])
        if not available.empty():
            continue  # Let DNF select a matching build, or report a repo conflict.
        for dependency in plugin.get_requires():
            if dependency.get_name().split("(", 1)[0] != "libdnf5" or dependency.get_relation().strip() != "=":
                continue
            libraries = rpm_api.PackageQuery(base)
            libraries.filter_name(["libdnf5"])
            libraries.filter_arch([plugin.get_arch()])
            current = rpm_api.PackageQuery(libraries)
            current.filter_installed()
            current.filter_provides([str(dependency)])
            libraries.filter_available()
            matching = rpm_api.PackageQuery(libraries)
            matching.filter_provides([str(dependency)])
            if current.empty() or libraries.empty() or not matching.empty():
                continue
            migrations.remove.add(plugin.get_full_nevra())
            LOG.info("Will retire optional %s: it requires %s, but the repositories offer neither that library "
                     "version nor a replacement plugin. The DNF packages will synchronize together; "
                     "Nobara Updater provides its own shutdown inhibition.", plugin.get_full_nevra(), dependency)
            break


def obsolete_replacements(base, package, final):
    target = rpm_api.PackageSet(base)
    target.add(package)
    replacements = rpm_api.PackageQuery(final)
    replacements.filter_obsoletes(target)
    replacements.difference(target)  # Never treat a self-Obsoletes as a replacement.
    return replacements


def allow_architecture_cleanup(base, transaction, migrations):
    """DNF can remove a stale noarch/native copy when the other is installed."""
    _, final = projected_packages(base, transaction)
    native = base.get_vars().get_value("basearch")
    installonly = rpm_api.PackageQuery(base)
    installonly.filter_installed()
    installonly.filter_installonly()
    for item in transaction.get_transaction_packages():
        if item.get_action() != trans.TransactionItemAction_REMOVE:
            continue
        package = item.get_package()
        arch = package.get_arch()
        if arch not in {"noarch", native} or installonly.contains(package):
            continue
        replacements = rpm_api.PackageQuery(final)
        replacements.filter_name([package.get_name()])
        replacements.filter_arch([native if arch == "noarch" else "noarch"])
        if replacements.empty():
            continue
        LOG.info("Will retire redundant architecture copy %s; replacement: %s.", package.get_full_nevra(),
                 ", ".join(sorted(p.get_full_nevra() for p in replacements)))
        migrations.remove.add(package.get_full_nevra())


def resolve_obsolete_cleanup(base, goal, transaction, settings, origins, migrations):
    """Retire leftover RPMs using native Obsoletes matching and one saved goal.

    DNF already handles incoming replacements. Fill the gap where a replacing
    package is installed already, using its final version's metadata. Do not
    remove a replacement along with the package whose cleanup it authorizes.
    """
    held = {p["nevra"] for p in origins if p["kind"] in {"local", "unknown"}}
    installonly = rpm_api.PackageQuery(base)
    installonly.filter_installed()
    installonly.filter_installonly()
    cleaned = {}
    reported = set()
    while True:
        retained, final = projected_packages(base, transaction)
        # A later solve must not invalidate an earlier cleanup decision.
        for nevra, package in cleaned.items():
            if retained.contains(package) or obsolete_replacements(base, package, final).empty():
                raise UpdateError(f"Cannot safely retire obsolete package {nevra}: its replacement would not remain installed.")

        obsoleters = rpm_api.PackageQuery(final)
        obsoleters.filter_obsoletes(retained)
        # Narrow candidates using provides, then let native RPM Obsoletes
        # matching decide exact names, epochs, versions, and architectures.
        candidates = rpm_api.PackageQuery(retained)
        candidates.clear()
        for replacement in obsoleters:
            matches = rpm_api.PackageQuery(retained)
            matches.filter_provides(replacement.get_obsoletes())
            candidates.update(matches)
        eligible = {}
        for package in candidates:
            nevra, name = package.get_full_nevra(), package.get_name()
            replacements = obsolete_replacements(base, package, final)
            if replacements.empty():
                continue
            protected = (nevra in held or name in ESSENTIAL or name == "kernel"
                         or name.startswith("kernel-") or installonly.contains(package))
            if protected:
                if nevra not in reported:
                    LOG.warning("Keeping protected obsolete package %s; automatic cleanup cannot remove it.", nevra)
                    reported.add(nevra)
                continue
            eligible[package.get_id().id] = (package, replacements)
        if not eligible:
            return transaction

        # If C obsoletes B and B obsoletes A, first retire B, then re-evaluate
        # A against packages that really remain. Never remove an entire chain
        # solely on the strength of metadata from packages being removed.
        selected = []
        for package, replacements in eligible.values():
            survivors = [p for p in replacements if p.get_id().id not in eligible]
            if survivors:
                selected.append((package, survivors))
        if not selected:
            names = ", ".join(sorted(p.get_full_nevra() for p, _ in eligible.values()))
            raise UpdateError("Ambiguous or cyclic Obsoletes relationships prevent safe cleanup: " + names
                              + ". Resolve which replacement to keep before updating.")
        for package, replacements in sorted(selected, key=lambda entry: entry[0].get_full_nevra()):
            nevra = package.get_full_nevra()
            LOG.info("Will retire obsolete package %s; replacement: %s.", nevra,
                     ", ".join(sorted(p.get_full_nevra() for p in replacements)))
            # Select this exact RPM, not every architecture/version with its name.
            goal.add_rpm_remove(package, settings)
            migrations.remove.add(nevra)
            cleaned[nevra] = package
        transaction = goal.resolve()
        check_resolution(transaction, origins)


def require_space(path: Path, required: int) -> None:
    if shutil.disk_usage(path).free < required:
        raise UpdateError(f"Not enough space on {path}: at least {required // (1024 * 1024)} MiB free is required.")


def kernel_unames(package) -> set[str]:
    return {str(p).split(" = ", 1)[1] for p in package.get_provides()
            if str(p).startswith("kernel-uname-r = ")}


def repair_plymouth_packages(goal, transaction, settings, repairs, origins):
    inbound = {item.get_package().get_name() for item in transaction.get_transaction_packages()
               if item.get_action() in {trans.TransactionItemAction_INSTALL, trans.TransactionItemAction_UPGRADE,
                                        trans.TransactionItemAction_DOWNGRADE, trans.TransactionItemAction_REINSTALL}}
    # A newer repo version already restores the files; do not also ask DNF to
    # reinstall an old version that may no longer exist in rolling repositories.
    reinstalls = repairs - inbound
    for package in sorted(reinstalls):
        LOG.info("Reinstalling %s to restore missing Plymouth files.", package)
        goal.add_reinstall(package, settings)
    if reinstalls:
        transaction = goal.resolve()
        check_resolution(transaction, origins)
    return transaction


def add_kernel_development(base, goal, transaction, settings, hooks, origins):
    """Request development providers and record legacy kernels left unchanged.

    LTO/LTS variants may use different package names. Installing an unqualified
    kernel-devel can select every retained version or the wrong kernel family.
    """
    inbound = {trans.TransactionItemAction_INSTALL, trans.TransactionItemAction_UPGRADE,
               trans.TransactionItemAction_DOWNGRADE, trans.TransactionItemAction_REINSTALL}
    items = list(transaction.get_transaction_packages())
    targets = set()
    incoming_boot_kernels = set()
    rebuild = any(h.startswith("plymouth-") for h in hooks)
    for item in items:
        rebuild |= any(part in item.get_package().get_name() for part in ("dkms", "akmod", "kmod", "dracut"))
        if item.get_action() not in inbound:
            continue
        package = item.get_package()
        targets.update(kernel_unames(package))
        if package.get_name() in {"kernel", "kernel-core"}:
            incoming_boot_kernels.update(kernel_unames(package))
    # These must build successfully, including a reinstalled/updated module
    # package for an existing kernel. Never excuse a missing new dependency.
    required = set(targets)
    preserved = []
    if rebuild:
        root = Path(base.get_config().get_installroot_option().get_value())
        retained = installed_boot_kernels(root)
        installed = rpm_api.PackageQuery(base)
        installed.filter_installed()
        installed_versions = set()
        for package in installed:
            installed_versions.update(kernel_unames(package))
        for kernel in sorted(installed_versions - retained - targets):
            LOG.info("Skipping development files for leftover kernel %s: no installed boot image.", kernel)
        targets.update(retained)
        running = os.uname().release
        if not incoming_boot_kernels:
            # finalize() selects the running kernel when no new kernel is
            # installed. In a chroot it may not be one of the target's kernels;
            # keep all targets mandatory when we cannot identify the next boot.
            required.update({running} if running in retained else retained)
    for kernel in sorted(targets):
        spec = "kernel-devel-uname-r = " + kernel
        providers = rpm_api.PackageQuery(base)
        providers.filter_provides([spec])
        if providers.empty():
            if kernel not in required:
                LOG.warning("Preserving existing modules and boot files for retained kernel %s: matching development files are unavailable. This kernel will not be selected for the updated system.", kernel)
                preserved.append(kernel)
                continue
            message = f"Matching kernel development files are unavailable: {spec}. Check the enabled kernel repository and its excludes."
            raise UpdateError(message)
        goal.add_install(spec, settings)
    if targets:
        transaction = goal.resolve()
        check_resolution(transaction, origins)
    return transaction, preserved


def store_transaction(transaction, incoming: list, job: Path) -> None:
    # The 5.4 Python bindings do not wrap std::filesystem::path. Use the
    # native serializer's defaults and attach local paths in its replay
    # format; Group/Environment.serialize accept ordinary strings.
    serialized = json.loads(transaction.serialize())
    payloads = {p.get_nevra(): Path(p.get_location()).name for p in incoming}
    for item in serialized.get("rpms", []):
        if item["action"] in {"Install", "Upgrade", "Downgrade", "Reinstall"}:
            filename = payloads[item["nevra"]]
            relative = "packages/" + filename
            if not (job / relative).is_file():
                raise UpdateError(f"A prepared package is missing: {relative}")
            item["package_path"] = relative
    for kind, items, getter, id_getter in (
        ("groups", transaction.get_transaction_groups(), "get_group", "get_groupid"),
        ("environments", transaction.get_transaction_environments(), "get_environment", "get_environmentid"),
    ):
        definitions = {getattr(getattr(item, getter)(), id_getter)(): getattr(item, getter)() for item in items}
        for item in serialized.get(kind, []):
            identifier = item["id"]
            if not re.fullmatch(r"[A-Za-z0-9_.+-]+", identifier):
                raise UpdateError(f"Unsupported comps identifier: {identifier}")
            relative = f"comps/{kind}/{identifier}.xml"
            path = job / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            definitions[identifier].serialize(str(path))
            item["group_path" if kind == "groups" else "environment_path"] = relative
    (job / "transaction.json").write_text(json.dumps(serialized, indent=2) + "\n")


def prepare_early_transaction(job: Path, names: tuple[str, ...]) -> dict:
    """Resolve only the installed updater/helpers, preserving normal safeguards.

    Do not run migrations, kernel retention, release synchronization, or boot
    space estimates here: the updated helper must be present before those.
    """
    base = configure_base(job, os_release())
    origins = []
    try:
        origins = protect_local_packages(base)
        held = {p["name"] for p in origins if p["kind"] in {"local", "unknown"}}
        installed = {p["name"] for p in installed_inventory(base)}
        targets = [name for name in names if name in installed and name not in held]
        if not targets:
            return dict(empty=True, package_origins=origins)
        settings = base_api.GoalJobSettings()
        settings.set_best(True)
        settings.set_skip_broken(False)
        settings.set_skip_unavailable(False)
        base.get_config().get_install_weak_deps_option().set(False)
        goal = base_api.Goal(base)
        for name in targets:
            goal.add_upgrade(name, settings)
        transaction = goal.resolve()
        check_resolution(transaction, origins)
        if transaction.empty():
            return dict(empty=True, package_origins=origins)
        records, incoming = [], []
        for item in transaction.get_transaction_packages():
            package = item.get_package()
            action = trans.transaction_item_action_to_string(item.get_action())
            records.append(dict(name=package.get_name(), arch=package.get_arch(),
                                nevra=package.get_full_nevra(), action=action))
            if action in {"Install", "Upgrade", "Reinstall"}:
                incoming.append(package)
            elif action not in {"Replaced", "Reason Change"}:
                raise UpdateError("The early updater/helper upgrade would remove or downgrade "
                                  + package.get_full_nevra() + ". Repair this dependency conflict before updating.")
        validate_local_packages(records, origins)
        validate_removals(records, set())
        for record in records:
            LOG.info("Early upgrade: %s %s", record["action"], record["nevra"])
        (job / "packages").mkdir(exist_ok=True)
        callback = DownloadProgress()
        base.set_download_callbacks(repo_api.DownloadCallbacksUniquePtr(callback))
        transaction.download()
        if not transaction.check_gpg_signatures():
            raise UpdateError("Early upgrade package signature verification failed:\n"
                              + "\n".join(transaction.get_gpg_signature_problems()))
        store_transaction(transaction, incoming, job)
        return dict(empty=False, package_origins=origins)
    except UpdateError as error:
        raise annotate_failure(error, origins) from error
    finally:
        base.unlock_system_repo()


def prepare_transaction(job: Path, *, codecs: bool = False, allow_live: bool = True) -> dict:
    current = os_release()
    base = configure_base(job, current, codecs=codecs)
    release = target_release(base, current)
    if release != current:
        base.unlock_system_repo()
        del base
        base = configure_base(job, release, codecs=codecs)
        if target_release(base, current) != release:
            raise UpdateError("The repository release changed during preparation. Try again.")
    origins = []
    try:
        origins = protect_local_packages(base)
        health = PackageHealth(base)
        inventory = installed_inventory(base)
        try:
            nvidia_closed = "MODULE_VARIANT=kernel\n" in Path("/etc/nvidia/kernel.conf").read_text()
        except OSError:
            nvidia_closed = False
        migrations = plan_migrations(inventory, product=machine_product(), nvidia_closed=nvidia_closed)
        add_dnf_inhibitor_migration(base, migrations, origins)
        if getattr(base, "nobara_migrate_media", False):
            migrations.hooks.append("migrate-media-repository")
        # Retire only known old ROCm packages whose exact names disappeared.
        # Surviving names are distro-synced instead of removed/reinstalled.
        from .update_migrations import OLD_ROCM
        for installed in inventory:
            if installed.get("repo") != "nobara-rocm-official" or installed["name"] not in OLD_ROCM:
                continue
            available = rpm_api.PackageQuery(base)
            available.filter_available()
            available.filter_name([installed["name"]])
            available.filter_arch([installed["arch"]])
            if available.empty():
                migrations.remove.add(f'{installed["name"]}.{installed["arch"]}')
        codecs = getattr(base, "nobara_codecs", codecs)
        if codecs:
            add_codec_migration(migrations, inventory)
            if not getattr(base, "nobara_enable_codecs", True):
                migrations.hooks.remove("enable-codecs")
        module_builders = any(p["name"] in {"dkms", "akmods"} or p["name"].startswith(("dkms-", "akmod-")) for p in inventory)
        gaming = {p["name"] for p in inventory} & {"gamescope-htpc-common", "gamescope-session-common"}
        theme = None
        if gaming:
            theme = subprocess.run(["plymouth-set-default-theme"], capture_output=True, text=True, check=True).stdout.strip()
        plymouth_repairs = add_plymouth_migration(
            migrations, inventory, current_theme=theme, root=Path(base.get_config().get_installroot_option().get_value()))
        settings = base_api.GoalJobSettings()
        settings.set_best(True)
        settings.set_skip_broken(False)
        settings.set_skip_unavailable(False)
        settings.set_clean_requirements_on_remove(False)
        goal = base_api.Goal(base)
        goal.add_rpm_distro_sync(settings)
        for spec in sorted(migrations.remove):
            goal.add_remove(spec, settings)
        for spec in sorted(migrations.install):
            goal.add_install(spec, settings)
        # Installed group members are already covered by distro-sync. Nobara
        # mirrors Fedora comps unchanged; upgrading those definitions would
        # add Fedora defaults (e.g. plasma-setup) that conflict with Nobara's
        # desktop. New required Nobara packages belong in explicit migrations.
        transaction = goal.resolve()
        check_resolution(transaction, origins)
        transaction = repair_plymouth_packages(goal, transaction, settings, plymouth_repairs, origins)
        transaction = resolve_obsolete_cleanup(base, goal, transaction, settings, origins, migrations)
        _, projected = projected_packages(base, transaction)
        requirements = health.repair_requirements(transaction, projected)
        if requirements:
            for requirement in sorted(requirements):
                goal.add_provide_install(requirement, settings)
            transaction = goal.resolve()
            try:
                check_resolution(transaction, origins)
            except UpdateError as error:
                raise annotate_failure(UpdateError("Cannot repair missing dependencies of these installed packages:\n- "
                    + "\n- ".join(health.repair_packages) + "\n\n" + str(error)), origins) from error
            transaction = resolve_obsolete_cleanup(base, goal, transaction, settings, origins, migrations)
        # Inspect the resolved dependency repairs too: they can introduce a
        # kernel or module builder which was not in the initial transaction.
        module_builders |= any(item.get_package().get_name() in {"dkms", "akmods"}
                               or item.get_package().get_name().startswith(("dkms-", "akmod-"))
                               for item in transaction.get_transaction_packages())
        preserved_kernels = []
        if module_builders:
            transaction, preserved_kernels = add_kernel_development(
                base, goal, transaction, settings, migrations.hooks, origins)
            transaction = resolve_obsolete_cleanup(base, goal, transaction, settings, origins, migrations)
        allow_architecture_cleanup(base, transaction, migrations)
        actions = {
            trans.TransactionItemAction_INSTALL: "Install", trans.TransactionItemAction_UPGRADE: "Upgrade",
            trans.TransactionItemAction_DOWNGRADE: "Downgrade", trans.TransactionItemAction_REINSTALL: "Reinstall",
            trans.TransactionItemAction_REMOVE: "Remove", trans.TransactionItemAction_REPLACED: "Replaced",
            trans.TransactionItemAction_REASON_CHANGE: "Reason Change",
        }
        records = []
        footprints = []
        incoming = []
        incoming_footprints = []
        for item in transaction.get_transaction_packages():
            package = item.get_package()
            action = actions[item.get_action()]
            records.append(dict(name=package.get_name(), arch=package.get_arch(), nevra=package.get_full_nevra(), action=action))
            footprints.append(dict(name=package.get_name(), action=action, files=list(package.get_files())))
            if action in {"Install", "Upgrade", "Downgrade", "Reinstall"}:
                incoming.append(package)
                incoming_footprints.append(footprints[-1])
        validate_local_packages(records, origins)
        validate_removals(records, migrations.remove)
        if changes_kernel(records) or preserved_kernels:
            check_selection_support()
        if release != current:
            replacement_versions = {p.get_name(): p.get_version() for p in incoming}
            for installed in inventory:
                name = installed["name"]
                if name == "nobara-release-common" or name.startswith("nobara-release-identity-"):
                    if replacement_versions.get(name, installed["version"]) != release:
                        message = f"The release identity package {name} is not ready for Nobara {release}."
                        held = [p for p in origins if p["name"] == name and p["kind"] in {"local", "unknown"}]
                        raise PackageOriginError(message, held) if held else UpdateError(message)
        for record in records:
            LOG.info("%s %s", record["action"], record["nevra"])
        if transaction.empty() and not migrations.hooks:
            health.validate(transaction, [])
            return {"empty": True, "source_release": current, "target_release": release, "codecs": codecs,
                    "package_origins": origins}
        download_size = sum(p.get_download_size() for p in incoming)
        install_size = sum(p.get_install_size() for p in incoming)
        # Reserve enough for unpacking and retained boot images; do not rely
        # solely on the final net size reported by the solver.
        require_space(job, download_size + 512 * 1024**2)
        require_space(Path("/"), install_size + 512 * 1024**2 + (download_size if job.stat().st_dev == Path("/").stat().st_dev else 0))
        (job / "packages").mkdir(exist_ok=True)
        callback = DownloadProgress()
        callback_ptr = repo_api.DownloadCallbacksUniquePtr(callback)
        base.set_download_callbacks(callback_ptr)
        LOG.info("Downloading the complete transaction before changing installed packages…")
        transaction.download()
        if not transaction.check_gpg_signatures():
            raise UpdateError("Package signature verification failed:\n" + "\n".join(transaction.get_gpg_signature_problems()))
        health.validate(transaction, [job / "packages" / Path(p.get_location()).name for p in incoming])
        # Classify signed payload contents, even if a repository supplies
        # incomplete filelists. Outgoing inventories come from the RPM DB.
        for package, footprint in zip(incoming, incoming_footprints):
            payload = job / "packages" / Path(package.get_location()).name
            footprint["files"] = subprocess.run(
                ["rpm", "-qp", "--qf", "[%{FILENAMES}\\n]", str(payload)],
                check=True, capture_output=True, text=True).stdout.splitlines()
            required = BGRT_FILES.get(package.get_name())
            if required and required not in footprint["files"] and any(h.startswith("plymouth-") for h in migrations.hooks):
                raise UpdateError(f"The prepared {package.get_name()} package is missing required Plymouth file {required}.")
        execution = execution_policy(footprints, current, release, migrations.hooks)
        if health.repair_packages:
            execution = {"mode": "offline", "reasons": [*execution["reasons"], "Repairing installed package dependencies"]}
        if not allow_live:
            execution = {"mode": "offline", "reasons": ["Administrator requires offline installation"]}
        boot_space = {}
        if execution["mode"] == "offline":
            boot_space = boot_space_requirements(records, migrations.hooks)
            for path, required in boot_space.items():
                require_space(Path(path), required)
            LOG.info("%s", format_restart_reasons(execution["reasons"]))
        else:
            LOG.info("This transaction contains application updates eligible for installation now.")
        result = transaction.test()
        if result != base_api.Transaction.TransactionRunResult_SUCCESS:
            details = [base_api.Transaction.transaction_result_to_string(result)]
            details.extend(transaction.get_transaction_problems())
            details.extend(str(message) for message in transaction.get_rpm_messages())
            raise UpdateError("Transaction validation failed:\n" + "\n".join(details))
        (job / "comps").mkdir(exist_ok=True)
        store_transaction(transaction, incoming, job)
        # Serialize only after testing; every inbound package must actually
        # exist in the durable destination, outside the evictable DNF cache.
        serialized = json.loads((job / "transaction.json").read_text())
        for item in serialized.get("rpms", []):
            if "package_path" in item and not (job / item["package_path"]).is_file():
                raise UpdateError(f"A prepared package is missing: {item['package_path']}")
        manifest = {str(p.relative_to(job)): file_digest(p) for directory in (job / "packages", job / "comps") for p in directory.rglob("*") if p.is_file()}
        manifest["transaction.json"] = file_digest(job / "transaction.json")
        return dict(empty=False, source_release=current, target_release=release, codecs=codecs, fingerprint=rpm_fingerprint(),
                    execution=execution, boot_space=boot_space, package_origins=origins, preserved_kernels=preserved_kernels,
                    files=manifest, packages=records, migrations=migrations.as_dict())
    except UpdateError as error:
        raise annotate_failure(error, origins)
    finally:
        base.unlock_system_repo()
