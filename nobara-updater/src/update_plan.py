"""Prepare one libdnf5 transaction without changing installed packages."""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from pathlib import Path

import libdnf5.base as base_api
import libdnf5.common as common
import libdnf5.conf as conf
import libdnf5.repo as repo_api
import libdnf5.rpm as rpm_api
import libdnf5.transaction as trans

from .update_migrations import add_codec_migration, machine_product, plan_migrations
from .update_boot import changes_kernel, check_selection_support
from .update_policy import execution_policy, format_restart_reasons
from .update_origins import PackageOriginError, protect_local_packages, validate_local_packages, annotate_failure
from .update_state import UpdateError, file_digest, os_release, rpm_fingerprint

LOG = logging.getLogger(__name__)
ESSENTIAL = {"glibc", "rpm", "dnf5", "libdnf5", "systemd", "bash", "coreutils", "python3", "nobara-updater"}


class DownloadProgress(repo_api.DownloadCallbacks):
    def add_new_download(self, user_data, description, total_to_download):
        LOG.info("Downloading %s (%s bytes)", description, int(total_to_download))
        return user_data

    def progress(self, user_cb_data, total_to_download, downloaded):
        return self.OK

    def mirror_failure(self, user_cb_data, msg, url, metadata):
        LOG.warning("Download mirror failed: %s: %s", url, msg)
        return self.OK

    def end(self, user_cb_data, status, msg):
        if status == self.TransferStatus_ERROR:
            LOG.error("Download failed: %s", msg)
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
    for repo in repo_api.RepoQuery(base):
        if repo.get_id() == "nobara-pikaos-additional":
            enabled = repo.get_config().get_enabled_option().get_value()
            base.nobara_codecs = codecs or enabled
            if codecs and not enabled:
                base.nobara_enable_codecs = True
                repo.get_config().get_enabled_option().set(True)
        if repo.get_config().get_enabled_option().get_value():
            repo.get_config().get_skip_if_unavailable_option().set(False)
            repo.get_config().get_pkg_gpgcheck_option().set(True)
            repo.expire()
    sack.load_repos()
    # A newer locally installed updater must not be downgraded out from
    # under its frozen worker and boot-confirmation protocol by distro-sync.
    installed = rpm_api.PackageQuery(base)
    installed.filter_installed()
    installed.filter_name(["nobara-updater"])
    for package in installed:
        older = rpm_api.PackageQuery(base)
        older.filter_available()
        older.filter_name(["nobara-updater"])
        older.filter_evr([package.get_evr()], common.QueryCmp_LT)
        if not older.empty():
            base.get_rpm_package_sack().add_user_excludes(older)
            LOG.info("Keeping nobara-updater %s or newer; older repository builds are excluded from this transaction.", package.get_evr())
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
        if name not in allowed and f"{name}.{arch}" not in allowed:
            raise UpdateError(f"The update would also remove {name}.{arch}. Resolve this package conflict before updating.")


def require_space(path: Path, required: int) -> None:
    if shutil.disk_usage(path).free < required:
        raise UpdateError(f"Not enough space on {path}: at least {required // (1024 * 1024)} MiB free is required.")


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
        inventory = installed_inventory(base)
        try:
            nvidia_closed = "MODULE_VARIANT=kernel\n" in Path("/etc/nvidia/kernel.conf").read_text()
        except OSError:
            nvidia_closed = False
        migrations = plan_migrations(inventory, product=machine_product(), nvidia_closed=nvidia_closed)
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
        module_builders = any(p["name"] == "dkms" or p["name"].startswith(("dkms-", "akmod-")) for p in inventory)
        if module_builders or "dkms-nvidia" in migrations.install:
            migrations.install.add("kernel-devel")
        gaming = {p["name"] for p in inventory} & {"gamescope-htpc-common", "gamescope-session-common"}
        if gaming:
            theme = subprocess.run(["plymouth-set-default-theme"], capture_output=True, text=True, check=True).stdout.strip()
            wanted = "steamos" if len(gaming) == 2 else "bgrt"
            if theme != wanted:
                migrations.install.add("plymouth-plugin-script")
                migrations.hooks.append("plymouth-" + wanted)
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
        if changes_kernel(records):
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
        if module_builders or "dkms-nvidia" in migrations.install:
            development = {(p["version"], p["release"], p["arch"]) for p in inventory if p["name"] == "kernel-devel"}
            development.update((p.get_version(), p.get_release(), p.get_arch()) for p in incoming if p.get_name() == "kernel-devel")
            for kernel in incoming:
                if kernel.get_name() == "kernel-core" and (kernel.get_version(), kernel.get_release(), kernel.get_arch()) not in development:
                    message = f"Matching kernel-devel is unavailable for {kernel.get_full_nevra()}."
                    held = [p for p in origins if p["name"] == "kernel-devel" and p["kind"] in {"local", "unknown"}]
                    raise PackageOriginError(message, held) if held else UpdateError(message)
        for record in records:
            LOG.info("%s %s", record["action"], record["nevra"])
        if transaction.empty() and not migrations.hooks:
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
        # Classify signed payload contents, even if a repository supplies
        # incomplete filelists. Outgoing inventories come from the RPM DB.
        for package, footprint in zip(incoming, incoming_footprints):
            payload = job / "packages" / Path(package.get_location()).name
            footprint["files"] = subprocess.run(
                ["rpm", "-qp", "--qf", "[%{FILENAMES}\\n]", str(payload)],
                check=True, capture_output=True, text=True).stdout.splitlines()
        execution = execution_policy(footprints, current, release, migrations.hooks)
        if not allow_live:
            execution = {"mode": "offline", "reasons": ["Administrator requires offline installation"]}
        if execution["mode"] == "offline":
            # Budget both retained recovery images and temporary replacement
            # initramfs/UKI images, using the current largest image as a floor.
            boot_images = [p.stat().st_size for folder in (Path("/boot"), Path("/boot/efi/EFI/Linux"), Path("/boot/EFI/Linux"))
                           for pattern in ("initramfs-*.img", "vmlinuz-*", "*.efi") for p in folder.glob(pattern) if p.is_file()]
            image_budget = max(boot_images, default=128 * 1024**2)
            new_kernels = sum(p.get_name() == "kernel-core" for p in incoming)
            rebuild_images = new_kernels or module_builders or any("dracut" in p.get_name() for p in incoming) or any(h.startswith("plymouth-") for h in migrations.hooks)
            uki_layout = any(p.is_file() for directory in (Path("/boot/EFI/Linux"), Path("/boot/efi/EFI/Linux")) for p in directory.glob("*.efi"))
            efi_update = any(p.get_name().startswith(("shim", "grub2-efi", "systemd-boot")) for p in incoming)
            for boot in (Path("/boot"), Path("/boot/efi")):
                if boot.is_mount():
                    if boot == Path("/boot/efi") and not uki_layout:
                        # Split initramfs images never go onto the ESP. A small
                        # ESP must not block ordinary application updates.
                        if efi_update:
                            require_space(boot, 32 * 1024**2)
                    else:
                        require_space(boot, (new_kernels + 1 + bool(rebuild_images)) * image_budget + 64 * 1024**2)
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
                    execution=execution, package_origins=origins,
                    files=manifest, packages=records, migrations=migrations.as_dict())
    except UpdateError as error:
        raise annotate_failure(error, origins)
    finally:
        base.unlock_system_repo()
