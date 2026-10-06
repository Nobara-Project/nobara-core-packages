"""Real RPM/libdnf5 preparation against disposable local repositories.

No host packages, services, boot files, or remote repositories are used.
The fixtures are deliberately unsigned; production signature policy is
covered separately and is never disabled by a user-facing option.
"""
from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1] / "src"
if "nobara_updater" not in sys.modules:
    package = types.ModuleType("nobara_updater")
    package.__path__ = [str(SOURCE)]
    sys.modules["nobara_updater"] = package

import libdnf5.base as b
import libdnf5.comps as comps
import libdnf5.repo as r
from nobara_updater import update_plan as planner
from nobara_updater import update_backend as backend
from nobara_updater import update_origins as origins
from nobara_updater import update_retention as retention
import libdnf5.rpm as rpm_api
from nobara_updater.update_migrations import MigrationPlan, plan_migrations
from nobara_updater.update_plan import configure_base
from nobara_updater.update_state import UpdateError, verify_files


def configure_fixture_base(root, repos, job, release="44", **kwargs):
    """Run production configuration with paths redirected to a disposable root."""
    config_path = root / "dnf.conf"
    config_path.write_text("[main]\n")
    base = b.Base()
    config = base.get_config()
    for name, value in {"installroot": str(root), "config_file_path": str(config_path),
                        "reposdir": [str(repos)], "varsdir": [str(root / "vars")],
                        "plugins": False, "use_host_config": True}.items():
        getattr(config, f"get_{name}_option")().set(value)
    native_run = subprocess.run

    def target_inventory(command, **options):
        # configure_base normally runs inside the real target root. Redirect
        # its RPM inventory query too; never opt in based on host codecs.
        if command[:2] != ["rpm", "-qa"]:
            raise AssertionError(f"Unexpected setup subprocess: {command}")
        return native_run(["rpm", "--root", str(root), *command[1:]], **options)

    with patch.object(planner.base_api, "Base", return_value=base), \
         patch.object(planner.subprocess, "run", side_effect=target_inventory):
        return configure_base(job, release, **kwargs)


class BaseConfigurationTests(unittest.TestCase):
    """Exercise the SWIG setup/locking calls even without a user namespace."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="nobara-base-configuration-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "root"
        self.repos = Path(self.temp.name) / "repos"
        self.job = Path(self.temp.name) / "job"
        for path in (self.root, self.repos, self.job):
            path.mkdir()

    def test_production_setup_uses_real_bindings_and_keeps_signature_checks(self):
        base = configure_fixture_base(self.root, self.repos, self.job)
        self.addCleanup(base.unlock_system_repo)
        self.assertTrue(base.is_initialized())
        self.assertTrue(base.get_config().get_pkg_gpgcheck_option().get_value())
        self.assertTrue(base.get_config().get_localpkg_gpgcheck_option().get_value())
        self.assertEqual(planner.installed_inventory(base), [])
        self.assertFalse(base.nobara_codecs)

    def test_lock_excludes_other_processes_without_waiting_and_can_be_released(self):
        base = configure_fixture_base(self.root, self.repos, self.job)
        self.addCleanup(base.unlock_system_repo)
        probe = '''
import sys
import libdnf5.base as b
base = b.Base()
config = base.get_config()
config.get_installroot_option().set(sys.argv[1])
config.get_plugins_option().set(False)
base.get_vars().set("releasever", "44")
base.setup()
print(base.lock_system_repo())
base.unlock_system_repo()
'''
        command = [sys.executable, "-B", "-c", probe, str(self.root)]
        blocked = subprocess.run(command, capture_output=True, text=True, check=True, timeout=5)
        self.assertEqual(blocked.stdout.strip(), "False")
        base.unlock_system_repo()
        acquired = subprocess.run(command, capture_output=True, text=True, check=True, timeout=5)
        self.assertEqual(acquired.stdout.strip(), "True")


@unittest.skipUnless(all(shutil.which(tool) for tool in ("rpmbuild", "createrepo_c", "rpm", "dnf5")), "RPM integration tools unavailable")
@unittest.skipUnless(os.geteuid() == 0, "Run these tests inside unshare --user --map-root-user for RPM's test chroot")
class OfflineIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="nobara-offline-integration-")
        cls.directory = Path(cls.temp.name)
        cls.top = cls.directory / "build"
        cls.repo = cls.directory / "repo"
        cls.repo.mkdir()
        cls.old = cls.build_rpm("nobara-offline-fixture", "1")
        cls.new = cls.build_rpm("nobara-offline-fixture", "2")
        shutil.copy2(cls.new, cls.repo)
        shutil.copy2(cls.build_rpm("nobara-release-common", "44"), cls.repo)
        cls.comps = cls.directory / "comps.xml"
        cls.comps.write_text('''<comps><group><id>fixture-group</id><name>Fixture</name><description>Fixture</description><default>false</default><uservisible>true</uservisible><packagelist><packagereq type="mandatory">nobara-offline-fixture</packagereq></packagelist></group><environment><id>fixture-environment</id><name>Fixture environment</name><description>Fixture</description><grouplist><groupid>fixture-group</groupid></grouplist></environment></comps>''')
        subprocess.run(["createrepo_c", "-g", str(cls.comps), str(cls.repo)], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    @classmethod
    def build_rpm(cls, name, version, script="", *, headers="", payload=None, arch="noarch", content=None):
        payload = payload or f"/usr/share/{name}/version"
        source = cls.directory / f"{name}-{version}-{arch}.payload"
        source.write_text(content if content is not None else version + "\n")
        spec = cls.directory / f"{name}-{version}.spec"
        spec.write_text(f'''Name: {name}
Version: {version}
Release: 1
Summary: Disposable offline updater test
License: MIT
{"BuildArch: noarch" if arch == "noarch" else ""}
{headers}
%description
Disposable offline updater test.
%install
mkdir -p %{{buildroot}}{Path(payload).parent}
cp {shlex.quote(str(source))} %{{buildroot}}{payload}
%files
{payload}
{script}
''')
        result = subprocess.run(["rpmbuild", *(["--target", arch] if arch != "noarch" else []),
                                 "--define", f"_topdir {cls.top}", "--define", f"_tmppath {cls.directory}",
                                 # Identical fixtures must have identical headers. A later
                                 # build timestamp otherwise makes distro-sync reinstall
                                 # the same NEVRA and hides the already-installed case.
                                 "--define", "use_source_date_epoch_as_buildtime 1",
                                 "--define", "clamp_mtime_to_source_date_epoch 1",
                                 "-bb", str(spec)], capture_output=True, text=True,
                                env=dict(os.environ, SOURCE_DATE_EPOCH="1700000000"))
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
        return cls.top / "RPMS" / arch / f"{name}-{version}-1.{arch}.rpm"

    def set_origin(self, name, repo):
        # Model a genuine recorded repository install, instead of conflating
        # the fixture's rpm --justdb setup with a manual user installation.
        inventory = subprocess.run(["rpm", "--root", str(self.root), "-q", name, "--qf",
                                    "%{NAME}|%{EPOCHNUM}|%{VERSION}|%{RELEASE}|%{ARCH}\n"],
                                   check=True, capture_output=True, text=True).stdout
        path = self.root / "usr/lib/sysimage/libdnf5/nevras.toml"
        values = tomllib.loads(path.read_text()).get("nevras", {}) if path.exists() else {}
        for line in inventory.splitlines():
            pkgname, epoch, version, release, arch = line.split("|")
            evr = (epoch + ":" if epoch != "0" else "") + version + "-" + release
            values[f"{pkgname}-{evr}.{arch}"] = {"from_repo": repo}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('version = "1.0"\n[nevras]\n' + '\n'.join(json.dumps(k) + ' = {from_repo = ' + json.dumps(v["from_repo"]) + '}' for k, v in values.items()) + '\n')

    def setUp(self):
        self.case = tempfile.TemporaryDirectory(dir=self.directory)
        self.addCleanup(self.case.cleanup)
        self.root = Path(self.case.name) / "root"
        self.root.mkdir()
        self.job = Path(self.case.name) / "job"
        self.job.mkdir()
        subprocess.run(["rpm", "--root", str(self.root), "--initdb"], check=True, capture_output=True)
        dbpath = self.root / "usr/lib/sysimage/rpm"
        result = subprocess.run(["rpm", "--dbpath", str(dbpath), "--justdb", "--nodeps", "--noscripts", "--noplugins", "--ignoresize", "-i", str(self.old)], capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
        self.set_origin("nobara-offline-fixture", "fixture")

    def base(self, job, release=None, **kwargs):
        repos = self.root / "fixture-repos"
        repos.mkdir(exist_ok=True)
        repo_id = getattr(self, "repo_id", "fixture")
        (repos / "fixture.repo").write_text(f"[{repo_id}]\nname=Fixture\nbaseurl={self.repo.as_uri()}\nenabled=1\n")
        base = configure_fixture_base(self.root, repos, job, release or "44", **kwargs)
        self.addCleanup(base.unlock_system_repo)
        # Only these deliberately unsigned test fixtures bypass signatures.
        # Production configuration, including its real SWIG lock, ran above.
        required = getattr(self, "require_signatures", False)
        base.get_config().get_pkg_gpgcheck_option().set(required)
        base.get_config().get_localpkg_gpgcheck_option().set(False)
        for repo in r.RepoQuery(base):
            repo.get_config().get_pkg_gpgcheck_option().set(required)
        return base

    def prepare(self, **options):
        with patch.object(planner, "configure_base", side_effect=self.base), \
             patch.object(planner, "os_release", return_value=getattr(self, "current_release", "44")), \
             patch.object(planner, "plan_migrations", return_value=getattr(self, "migrations", MigrationPlan())), \
             patch.object(planner, "rpm_fingerprint", return_value="fixture"), \
             patch.object(planner, "require_space"):
            return planner.prepare_transaction(self.job, **options)

    def test_real_upgrade_is_saved_with_local_payload_and_no_installed_changes(self):
        result = self.prepare()
        self.assertFalse(result["empty"])
        self.assertTrue(any(p["action"] == "Upgrade" for p in result["packages"]))
        verify_files(self.job, result["files"])
        installed = subprocess.run(["rpm", "--root", str(self.root), "-q", "nobara-offline-fixture", "--qf", "%{VERSION}"], check=True, capture_output=True, text=True)
        self.assertEqual(installed.stdout, "1")

    def selection_fixture(self):
        private = Path(self.case.name) / "selection-repo"
        shutil.copytree(self.repo, private)
        self.repo = private
        for name in ("selected-app", "required-library", "unselected-app"):
            self.install_health_fixture(name, origin="fixture")
            headers = "Requires: required-library >= 2" if name == "selected-app" else ""
            shutil.copy2(self.build_rpm(name, "2", headers=headers), private)
        shutil.copy2(self.build_rpm("mandatory-fixup", "1"), private)
        subprocess.run(["createrepo_c", str(private)], check=True, capture_output=True)

    def test_selected_update_keeps_unrelated_packages_but_includes_dependencies_and_fixups(self):
        self.selection_fixture()
        self.migrations = MigrationPlan(install={"mandatory-fixup"})
        result = self.prepare(packages=["selected-app"])
        changed = {p["name"] for p in result["packages"]}
        self.assertEqual(changed, {"selected-app", "required-library", "mandatory-fixup"})
        verify_files(self.job, result["files"])
        replay = subprocess.run(self.replay_command(test=False), capture_output=True, text=True)
        self.assertEqual(replay.returncode, 0, replay.stdout + replay.stderr)
        self.check_fixture_health()
        version = subprocess.run(["rpm", "--root", str(self.root), "-q", "unselected-app", "--qf", "%{VERSION}"], check=True, capture_output=True, text=True)
        self.assertEqual(version.stdout, "1")

    def test_selected_library_also_updates_required_consumers(self):
        private = Path(self.case.name) / "consumer-repo"
        shutil.copytree(self.repo, private)
        self.repo = private
        self.install_health_fixture("abi-library", origin="fixture")
        self.install_health_fixture("abi-client", headers="Requires: abi-library = 1-1", origin="fixture")
        shutil.copy2(self.build_rpm("abi-library", "2"), private)
        shutil.copy2(self.build_rpm("abi-client", "2", headers="Requires: abi-library = 2-1"), private)
        subprocess.run(["createrepo_c", str(private)], check=True, capture_output=True)
        result = self.prepare(packages=["abi-library"])
        self.assertEqual({p["name"] for p in result["packages"]}, {"abi-library", "abi-client"})

    def test_selected_release_upgrade_expands_to_full_sync(self):
        self.selection_fixture()
        self.current_release = "43"
        with self.assertLogs(planner.LOG, level="INFO") as logs:
            result = self.prepare(packages=["selected-app"])
        self.assertIn("unselected-app", {p["name"] for p in result["packages"]})
        self.assertEqual(result["execution"]["mode"], "offline")
        self.assertTrue(any("requires a complete system update" in line for line in logs.output))

    def test_real_download_callbacks_and_replay_emit_package_identity(self):
        from nobara_updater import update_progress as progress
        events = []
        with patch.object(progress, "emit_progress", side_effect=lambda event, **fields: events.append(dict(event=event, **fields))):
            result = self.prepare()
            self.assertTrue(any(e.get("phase") == "downloaded" for e in events), events)
            with self.assertLogs(backend.LOG, level="INFO") as logs:
                backend.run(self.replay_command(test=False))
        # Must report this package from the real DNF line, BEFORE replay's
        # final successful return marks the complete transaction as applied.
        self.assertTrue(any(e.get("phase") == "applying" and e["fraction"] == 1 for e in events), logs.output)
        self.assertTrue(any(e.get("phase") == "applied" for e in events))
        self.assertEqual({e["id"] for e in events if e.get("phase") == "downloaded"},
                         {p["action"] + ":" + p["nevra"] for p in result["packages"] if p["action"] == "Upgrade"})

    def test_repository_endpoint_migration_is_saved_for_offline_finalization(self):
        fixture_base = self.base

        def migrate(job, release=None, **kwargs):
            base = fixture_base(job, release, **kwargs)
            base.nobara_migrate_media = True
            return base

        with patch.object(self, "base", side_effect=migrate):
            result = self.prepare()
        self.assertIn("migrate-media-repository", result["migrations"]["hooks"])
        self.assertEqual(result["execution"]["mode"], "offline")

    def install_health_fixture(self, name, version="1", *, headers="", origin="nobara-updates", arch="noarch", payload=None, content=None):
        package = self.build_rpm(name, version, headers=headers, arch=arch, payload=payload, content=content)
        subprocess.run(["rpm", "--root", str(self.root), "--justdb", "--nodeps", "--noscripts", "--noplugins",
                        "--ignoresize", "-i", str(package)], check=True, capture_output=True)
        self.set_origin(name + "." + arch, origin)

    def install_retention_fixtures(self):
        cls = type(self)
        if not hasattr(cls, "retention_rpms"):
            cls.retention_rpms = []
            for version in ("7.2.3", "7.2.4", "7.2.8", "7.2.10"):
                for name in ("kernel", "kernel-core", "kernel-modules", "kernel-devel"):
                    headers = "Provides: installonlypkg(kernel)"
                    if name != "kernel-core":
                        headers += f"\nRequires: kernel-core = {version}-1"
                    cls.retention_rpms.append(cls.build_rpm(name, version, headers=headers,
                        payload=f"/usr/share/retention/{name}/{version}"))
        subprocess.run(["rpm", "--root", str(self.root), "--justdb", "--nodeps", "--noscripts", "--noplugins",
                        "--ignoresize", "-i", *map(str, cls.retention_rpms)], check=True, capture_output=True)

    def retention_base(self, limit=3):
        self.install_retention_fixtures()
        base = self.base(self.job)
        base.get_config().get_installonly_limit_option().set(limit)
        return base

    def running_fixture(self, base, version="7.2.10"):
        query = rpm_api.PackageQuery(base)
        query.filter_installed()
        query.filter_name(["kernel-core"])
        query.filter_version([version])
        return next(iter(query))

    def test_retention_removes_oldest_complete_kernel_set_with_rpm_version_order(self):
        base = self.retention_base()
        with patch.object(rpm_api.PackageSackWeakPtr, "get_running_kernel", return_value=self.running_fixture(base)):
            transaction = retention.plan_cleanup(base)
        removed = {(item.get_package().get_name(), item.get_package().get_version())
                   for item in transaction.get_transaction_packages()}
        self.assertEqual(removed, {(name, "7.2.3") for name in
                                  ("kernel", "kernel-core", "kernel-modules", "kernel-devel")})

    def test_retention_respects_custom_limits_and_preserves_entire_running_kernel_set(self):
        base = self.retention_base(2)
        with patch.object(rpm_api.PackageSackWeakPtr, "get_running_kernel", return_value=self.running_fixture(base, "7.2.3")):
            transaction = retention.plan_cleanup(base)
        self.assertEqual({item.get_package().get_version() for item in transaction.get_transaction_packages()}, {"7.2.4"})

    def test_unlimited_retention_does_not_remove_anything(self):
        base = self.retention_base(0)
        with patch.object(rpm_api.PackageSackWeakPtr, "get_running_kernel") as running:
            self.assertIsNone(retention.plan_cleanup(base))
        running.assert_not_called()

    def test_unknown_running_kernel_prevents_retention_removals(self):
        base = self.retention_base()
        with self.assertRaisesRegex(UpdateError, "Cannot identify the running kernel"):
            retention.plan_cleanup(base)

    def test_retention_refuses_to_remove_non_kernel_dependents(self):
        self.install_health_fixture("user-driver", headers="Requires: kernel-core = 7.2.3-1")
        base = self.retention_base()
        with patch.object(rpm_api.PackageSackWeakPtr, "get_running_kernel", return_value=self.running_fixture(base)):
            with self.assertRaises(UpdateError):
                retention.plan_cleanup(base)

    def install_versioned_kmod(self, kernel, *, requires=""):
        name = "kmod-v4l2loopback-" + kernel
        self.install_health_fixture(name, "0.13.2", origin="@commandline", headers=requires)
        return name

    def cleanup_names(self, base, *, obsolete=(), running="7.2.10"):
        with patch.object(rpm_api.PackageSackWeakPtr, "get_running_kernel",
                          return_value=self.running_fixture(base, running)):
            transaction = retention.plan_cleanup(base, obsolete_kernels=obsolete)
        return {(item.get_package().get_name(), item.get_package().get_version())
                for item in transaction.get_transaction_packages()} if transaction else set()

    def test_retention_removes_explicit_obsolete_set_and_generated_kmods_below_limit(self):
        old = "7.2.8-1.noarch"
        kmod = self.install_versioned_kmod(old, requires="Requires: kernel-core = 7.2.8-1")
        unrelated = self.install_versioned_kmod("7.2.4-1.noarch")
        self.install_health_fixture("labwc", "7.2.8", origin="@commandline")
        self.install_health_fixture("kernel-headers", "7.2.8")
        base = self.retention_base(0)
        removed = self.cleanup_names(base, obsolete=[old])
        self.assertEqual(removed, {(name, "7.2.8") for name in
                                  ("kernel", "kernel-core", "kernel-modules", "kernel-devel")} | {(kmod, "0.13.2")})
        self.assertNotIn((unrelated, "0.13.2"), removed)

    def test_retention_removes_module_only_and_kmod_only_legacy_remnants(self):
        old = "6.12.6-1.noarch"
        self.install_health_fixture("kernel-modules-core", "6.12.6", origin="<unknown>",
                                    headers=f"Provides: kernel-uname-r = {old}\nProvides: installonlypkg(kernel-module)")
        self.install_health_fixture("kernel-devel", "6.12.6", headers="Provides: installonlypkg(kernel)",
                                    payload="/usr/src/kernels/6.12.6/Makefile")
        kmods = {self.install_versioned_kmod(old), self.install_versioned_kmod("6.12.9-1.noarch")}
        base = self.retention_base(0)
        removed = self.cleanup_names(base)
        self.assertEqual(removed, {("kernel-modules-core", "6.12.6"), ("kernel-devel", "6.12.6")}
                         | {(name, "0.13.2") for name in kmods})

    def test_retention_preserves_entire_running_set_even_if_marked_obsolete(self):
        current = "7.2.10-1.noarch"
        self.install_versioned_kmod(current)
        base = self.retention_base(0)
        self.assertEqual(self.cleanup_names(base, obsolete=[current]), set())

    def test_retention_cleans_generated_kmods_with_normal_limit_removal(self):
        old_kmod = self.install_versioned_kmod("7.2.3-1.noarch", requires="Requires: kernel-core = 7.2.3-1")
        current_kmod = self.install_versioned_kmod("7.2.10-1.noarch")
        base = self.retention_base()
        removed = self.cleanup_names(base)
        self.assertIn((old_kmod, "0.13.2"), removed)
        self.assertNotIn((current_kmod, "0.13.2"), removed)

    def test_retention_keeps_other_boot_images_and_future_kernel_remnants(self):
        old = "6.12.6-1.noarch"
        self.install_health_fixture("kernel-modules-core", "6.12.6",
                                    headers=f"Provides: kernel-uname-r = {old}")
        self.install_versioned_kmod(old)
        (self.root / "boot").mkdir()
        (self.root / "boot" / ("vmlinuz-" + old)).write_text("manually managed boot image")
        self.install_health_fixture("kernel-modules-extra", "9.0.0",
                                    headers="Provides: kernel-uname-r = 9.0.0-1.noarch")
        self.install_versioned_kmod("9.0.0-1.noarch")
        base = self.retention_base(0)
        # The installed fixture core RPMs also have no on-disk boot images:
        # missing files alone must never identify a kernel as an orphan.
        self.assertEqual(self.cleanup_names(base), set())

    def test_retention_obsolete_cleanup_refuses_unrelated_dependency_removals(self):
        self.install_health_fixture("local-tool", origin="@commandline", headers="Requires: kernel-core = 7.2.8-1")
        base = self.retention_base(0)
        with self.assertRaises(UpdateError):
            self.cleanup_names(base, obsolete=["7.2.8-1.noarch"])

    def test_retention_keeps_generic_kmod_packages_and_standalone_development_files(self):
        for name in ("kmod", "kmod-libs", "kmod-v4l2loopback", "akmod-v4l2loopback"):
            self.install_health_fixture(name, "6.12.6")
        self.install_health_fixture("kernel-lto-devel", "6.12.6",
                                    headers="Provides: kernel-devel-uname-r = 6.12.6-1.noarch\nProvides: installonlypkg(kernel)")
        base = self.retention_base(0)
        self.assertEqual(self.cleanup_names(base), set())

    def test_retention_runs_real_removal_only_in_disposable_root(self):
        old_kmod = self.install_versioned_kmod("7.2.8-1.noarch", requires="Requires: kernel-core = 7.2.8-1")
        current_kmod = self.install_versioned_kmod("7.2.10-1.noarch")
        installed = self.retention_base()
        running = self.running_fixture(installed)
        installed.unlock_system_repo()
        base = b.Base()
        config_path = self.root / "cleanup.conf"
        config_path.write_text("[main]\ninstallonly_limit=3\n")
        config = base.get_config()
        for name, value in {"installroot": str(self.root), "config_file_path": str(config_path),
                            "reposdir": [], "plugins": False, "use_host_config": True}.items():
            getattr(config, f"get_{name}_option")().set(value)
        base.get_vars().set("releasever", "44")
        with patch.object(retention.base_api, "Base", return_value=base), \
             patch.object(rpm_api.PackageSackWeakPtr, "get_running_kernel", return_value=running):
            retention.prune_kernel_packages(obsolete_kernels=["7.2.8-1.noarch"])
        inventory = subprocess.run(["rpm", "--root", str(self.root), "-qa", "--qf", "%{NAME}|%{VERSION}\n"],
                                   check=True, capture_output=True, text=True).stdout
        self.assertNotIn("|7.2.3", inventory)
        self.assertNotIn("|7.2.8", inventory)
        self.assertNotIn(old_kmod, inventory)
        self.assertIn(current_kmod, inventory)
        for version in ("7.2.4", "7.2.10"):
            for name in ("kernel", "kernel-core", "kernel-modules", "kernel-devel"):
                self.assertIn(f"{name}|{version}\n", inventory)

    def health_check_command(self, *options):
        return ["dnf5", "--installroot=" + str(self.root), "--releasever=44", "--config=/dev/null",
                "--setopt=reposdir=", "--no-plugins", "check", *options]

    def check_fixture_health(self):
        native_run = backend.run

        def target_run(command, **kwargs):
            if command[0] == "rpm":
                command = ["rpm", "--root", str(self.root), *command[1:]]
            elif command[0] == "dnf5":
                # This root has no repo definitions at all. DNF rejects a
                # --disable-repo glob that matches none; reposdir= already
                # provides the same installed-only check in the fixture.
                command = [*self.health_check_command()[:-1],
                           *(arg for arg in command[1:] if arg != "--disable-repo=*")]
            else:
                raise AssertionError(command)
            return native_run(command, **kwargs)

        with patch.object(backend, "run", side_effect=target_run):
            backend.check_installed_system()

    def packagekit_leftover_fixture(self, *, obsoletes=True, origin="nobara-updates"):
        self.install_health_fixture("PackageKit", "1.3.6", headers=(
            "Obsoletes: dnf4-plugin-notify-PackageKit < 1.3.6-1" if obsoletes else ""))
        self.install_health_fixture("dnf4-plugin-notify-PackageKit", "1.3.4", origin=origin)

    def plymouth_fixture(self, *, installed=True, repository_version="1", healthy=False, valid_payload=True):
        self.install_health_fixture("plymouth")
        private = Path(self.case.name) / "plymouth-repo"
        shutil.copytree(self.repo, private)
        self.repo = private
        for name, filename in planner.BGRT_FILES.items():
            content = "[Plymouth Theme]\nName=BGRT\nModuleName=two-step\n" if name == "plymouth-theme-spinner" else "fixture plugin\n"
            headers = "Requires: plymouth-plugin-two-step = {}-1" if name == "plymouth-theme-spinner" else ""
            if installed:
                self.install_health_fixture(name, payload=filename, content=content, headers=headers.format("1"))
            payload = filename if valid_payload else filename + ".wrong"
            shutil.copy2(self.build_rpm(name, repository_version, headers=headers.format(repository_version),
                                        payload=payload, content=content), self.repo)
            if healthy:
                path = self.root / filename.lstrip("/")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
        subprocess.run(["createrepo_c", str(self.repo)], check=True, capture_output=True)

    def test_missing_bgrt_packages_are_included_in_prepared_transaction(self):
        self.plymouth_fixture(installed=False)
        result = self.prepare()
        changed = {(p["name"], p["action"]) for p in result["packages"] if p["name"] in planner.BGRT_FILES}
        self.assertEqual(changed, {(name, "Install") for name in planner.BGRT_FILES})
        self.assertEqual(result["execution"]["mode"], "offline")
        self.assertIn("plymouth-rebuild", result["migrations"]["hooks"])

    @unittest.skipUnless(shutil.which("plymouth-set-default-theme"), "Plymouth theme selector unavailable")
    def test_deleted_bgrt_payload_is_reinstalled_and_real_theme_selection_succeeds(self):
        self.plymouth_fixture()
        environment = dict(os.environ, PLYMOUTH_DATADIR=str(self.root / "usr/share"),
                           PLYMOUTH_CONFDIR=str(self.root / "etc/plymouth"),
                           PLYMOUTH_POLICYDIR=str(self.root / "usr/share/plymouth"),
                           PLYMOUTH_PLUGIN_PATH=str(self.root / "usr/lib64/plymouth") + "/")
        before = subprocess.run(["plymouth-set-default-theme", "bgrt"], env=environment, capture_output=True, text=True)
        self.assertNotEqual(before.returncode, 0)
        self.assertIn("bgrt.plymouth does not exist", before.stderr)
        result = self.prepare()
        changed = {(p["name"], p["action"]) for p in result["packages"] if p["name"] in planner.BGRT_FILES}
        self.assertTrue({(name, "Reinstall") for name in planner.BGRT_FILES} <= changed)
        self.assertFalse((self.root / "usr/share/plymouth/themes/bgrt/bgrt.plymouth").exists())
        replay = subprocess.run(self.replay_command(test=False), capture_output=True, text=True)
        self.assertEqual(replay.returncode, 0, replay.stdout + replay.stderr)
        after = subprocess.run(["plymouth-set-default-theme", "bgrt"], env=environment, capture_output=True, text=True)
        self.assertEqual(after.returncode, 0, after.stdout + after.stderr)

    def test_missing_bgrt_payload_uses_upgrade_when_old_rpm_is_no_longer_available(self):
        self.plymouth_fixture(repository_version="2")
        result = self.prepare()
        actions = {p["action"] for p in result["packages"] if p["name"] in planner.BGRT_FILES}
        self.assertEqual(actions - {"Reason Change"}, {"Upgrade", "Replaced"})
        replay = subprocess.run(self.replay_command(test=False), capture_output=True, text=True)
        self.assertEqual(replay.returncode, 0, replay.stdout + replay.stderr)
        for filename in planner.BGRT_FILES.values():
            self.assertTrue((self.root / filename.lstrip("/")).is_file())

    def test_healthy_bgrt_does_not_force_reinstall_or_boot_rebuild(self):
        self.plymouth_fixture(healthy=True)
        result = self.prepare()
        self.assertFalse(any(p["name"] in planner.BGRT_FILES and p["action"] != "Reason Change"
                             for p in result["packages"]), result["packages"])
        self.assertFalse(any(hook.startswith("plymouth-") for hook in result["migrations"]["hooks"]))

    def test_invalid_bgrt_repository_payload_stops_preparation(self):
        self.plymouth_fixture(installed=False, valid_payload=False)
        with self.assertRaisesRegex(UpdateError, "missing required Plymouth file"):
            self.prepare()

    def test_bgrt_repair_can_reinstall_a_locally_installed_theme_package(self):
        self.plymouth_fixture()
        self.set_origin("plymouth-theme-spinner", "@commandline")
        result = self.prepare()
        self.assertTrue(any(p["name"] == "plymouth-theme-spinner" and p["action"] == "Reinstall"
                            for p in result["packages"]))

    def test_obsoleted_packagekit_plugin_passes_preflight_and_is_removed_in_saved_transaction(self):
        self.packagekit_leftover_fixture()
        original = subprocess.run(self.health_check_command(), capture_output=True, text=True)
        self.assertNotEqual(original.returncode, 0)
        self.assertIn("obsoleted", original.stdout + original.stderr)
        self.check_fixture_health()
        result = self.prepare()
        self.assertTrue(any(p["name"] == "dnf4-plugin-notify-PackageKit" and p["action"] == "Remove"
                            for p in result["packages"]))
        self.assertFalse(any(p["name"] == "PackageKit" for p in result["packages"]))
        # Preparation itself must not remove anything from the installed root.
        subprocess.run(["rpm", "--root", str(self.root), "-q", "dnf4-plugin-notify-PackageKit"],
                       check=True, capture_output=True)
        replay = subprocess.run(self.replay_command(test=False), capture_output=True, text=True)
        self.assertEqual(replay.returncode, 0, replay.stdout + replay.stderr)
        checked = subprocess.run(self.health_check_command(), capture_output=True, text=True)
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        self.check_fixture_health()

    def test_packagekit_plugin_is_preserved_without_installed_obsoletes(self):
        self.packagekit_leftover_fixture(obsoletes=False)
        result = self.prepare()
        self.assertFalse(any(p["name"] == "dnf4-plugin-notify-PackageKit" for p in result["packages"]))

    def test_packagekit_cleanup_includes_locally_installed_obsolete_plugin(self):
        self.packagekit_leftover_fixture(origin="@commandline")
        result = self.prepare()
        self.assertTrue(any(p["name"] == "dnf4-plugin-notify-PackageKit" and p["action"] == "Remove"
                            for p in result["packages"]))

    def add_obsolete_repo_package(self, name, version, *, headers="", arch="noarch", payload=None):
        if not getattr(self, "_obsolete_repo", False):
            private = Path(self.case.name) / "obsoletes-repo"
            shutil.copytree(self.repo, private)
            self.repo = private
            self._obsolete_repo = True
        shutil.copy2(self.build_rpm(name, version, headers=headers, arch=arch, payload=payload), self.repo)
        subprocess.run(["createrepo_c", str(self.repo)], check=True, capture_output=True)

    def dnf_plugin_fixture(self, *, plugin="libdnf5-plugin-systemd-inhibit", compatible=False):
        # The newer Fedora build split out an optional, exact-version plugin
        # which is absent from the older DNF build in the rolling mirror.
        family = ("libdnf5", "libdnf5-cli", "dnf5", "python3-libdnf5", "libdnf5-plugin-actions")
        for name in family:
            requirement = "Requires: libdnf5(x86-64) = {}-1" if name != "libdnf5" else ""
            self.install_health_fixture(name, "5.4.5.0", arch="x86_64", headers=requirement.format("5.4.5.0"))
            self.add_obsolete_repo_package(name, "5.4.3.0", arch="x86_64", headers=requirement.format("5.4.3.0"))
        self.install_health_fixture(plugin, "5.4.5.0", arch="x86_64",
                                    headers="Requires: libdnf5(x86-64) = 5.4.5.0-1")
        if compatible:
            self.add_obsolete_repo_package(plugin, "5.4.3.0", arch="x86_64",
                                           headers="Requires: libdnf5(x86-64) = 5.4.3.0-1")
        return family

    def test_unavailable_optional_dnf_inhibitor_does_not_block_family_sync(self):
        family = self.dnf_plugin_fixture()
        result = self.prepare()
        self.assertEqual({(p["name"], p["action"]) for p in result["packages"] if p["name"] in family},
                         {(name, action) for name in family for action in ("Downgrade", "Replaced")})
        self.assertEqual([p["name"] for p in result["packages"] if p["action"] == "Remove"],
                         ["libdnf5-plugin-systemd-inhibit"])
        self.assertEqual(result["execution"]["mode"], "offline")
        # Preparation is read-only; removal is saved alongside the matching
        # DNF family and only takes effect when that transaction is replayed.
        subprocess.run(["rpm", "--root", str(self.root), "-q", "libdnf5-plugin-systemd-inhibit"],
                       check=True, capture_output=True)
        replay = subprocess.run(self.replay_command(test=False), capture_output=True, text=True)
        self.assertEqual(replay.returncode, 0, replay.stdout + replay.stderr)
        self.check_fixture_health()

    def test_compatible_dnf_inhibitor_build_is_synchronized_instead_of_removed(self):
        self.dnf_plugin_fixture(compatible=True)
        result = self.prepare()
        self.assertEqual({p["action"] for p in result["packages"] if p["name"] == "libdnf5-plugin-systemd-inhibit"},
                         {"Downgrade", "Replaced"})

    def test_dnf_inhibitor_cleanup_includes_locally_installed_plugin(self):
        self.dnf_plugin_fixture()
        self.set_origin("libdnf5-plugin-systemd-inhibit", "@commandline")
        result = self.prepare()
        self.assertTrue(any(p["name"] == "libdnf5-plugin-systemd-inhibit" and p["action"] == "Remove"
                            for p in result["packages"]))

    def test_dnf_inhibitor_cleanup_cannot_remove_dependent_application(self):
        self.dnf_plugin_fixture()
        self.install_health_fixture("user-application", headers="Requires: libdnf5-plugin-systemd-inhibit")
        with self.assertRaisesRegex(UpdateError, "user-application"):
            self.prepare()
        self.assertFalse((self.job / "transaction.json").exists())

    def test_dnf_fixup_does_not_remove_other_unavailable_plugins(self):
        self.dnf_plugin_fixture(plugin="custom-dnf-plugin")
        with self.assertRaisesRegex(UpdateError, "custom-dnf-plugin"):
            self.prepare()
        self.assertFalse((self.job / "transaction.json").exists())

    def test_unavailable_dnf_inhibitor_is_kept_when_matching_library_is_available(self):
        self.install_health_fixture("libdnf5", arch="x86_64")
        self.install_health_fixture("libdnf5-plugin-systemd-inhibit", arch="x86_64",
                                    headers="Requires: libdnf5(x86-64) = 1-1")
        self.add_obsolete_repo_package("libdnf5", "1", arch="x86_64")
        result = self.prepare()
        self.assertFalse(any(p["name"].startswith("libdnf5") for p in result["packages"]))

    def test_unavailable_dnf_inhibitor_is_kept_without_repository_library(self):
        self.install_health_fixture("libdnf5", arch="x86_64")
        self.install_health_fixture("libdnf5-plugin-systemd-inhibit", arch="x86_64",
                                    headers="Requires: libdnf5(x86-64) = 1-1")
        result = self.prepare()
        self.assertFalse(any(p["name"].startswith("libdnf5") for p in result["packages"]))

    def test_incompatible_available_dnf_inhibitor_build_is_reported_not_erased(self):
        self.dnf_plugin_fixture()
        self.add_obsolete_repo_package("libdnf5-plugin-systemd-inhibit", "5.4.5.0", arch="x86_64",
                                       headers="Requires: libdnf5(x86-64) = 5.4.5.0-1")
        with self.assertRaisesRegex(UpdateError, "libdnf5-plugin-systemd-inhibit"):
            self.prepare()
        self.assertFalse((self.job / "transaction.json").exists())

    def test_sddm_settings_retire_with_display_manager_in_one_transaction(self):
        names = ["sddm", "sddm-wayland-plasma", "kde-settings-sddm", "dnf-app-center",
                 "inputplumber", "falcond", "mesa-vulkan-drivers"]
        for name in names:
            self.install_health_fixture(name, headers="Requires: sddm" if name in {
                "sddm-wayland-plasma", "kde-settings-sddm"} else "")
        self.add_obsolete_repo_package("kde-settings-sddm", "2", headers="Requires: sddm")
        self.add_obsolete_repo_package("plasma-login-manager", "1", headers="Conflicts: sddm")
        self.migrations = plan_migrations([dict(name=name, arch="noarch") for name in names])
        result = self.prepare()
        self.assertEqual({p["name"] for p in result["packages"] if p["action"] == "Remove"},
                         {"sddm", "sddm-wayland-plasma", "kde-settings-sddm"})
        self.assertIn("plasma-login", result["migrations"]["hooks"])
        replay = subprocess.run(self.replay_command(test=False), capture_output=True, text=True)
        self.assertEqual(replay.returncode, 0, replay.stdout + replay.stderr)
        self.check_fixture_health()

    def test_redundant_noarch_copy_is_removed_when_native_package_remains(self):
        for arch in ("noarch", "x86_64"):
            self.install_health_fixture("emacs-filesystem", arch=arch)
        self.add_obsolete_repo_package("emacs-filesystem", "1", arch="x86_64")
        result = self.prepare()
        self.assertEqual([(p["arch"], p["action"]) for p in result["packages"] if p["name"] == "emacs-filesystem"],
                         [("noarch", "Remove")])
        self.assertIn("emacs-filesystem-0:1-1.noarch", result["migrations"]["remove"])
        replay = subprocess.run(self.replay_command(test=False), capture_output=True, text=True)
        self.assertEqual(replay.returncode, 0, replay.stdout + replay.stderr)
        remaining = subprocess.check_output(["rpm", "--root", str(self.root), "-q", "emacs-filesystem", "--qf", "%{ARCH}"], text=True)
        self.assertEqual(remaining, "x86_64")
        self.check_fixture_health()

    def test_architecture_cleanup_does_not_authorize_removing_32_bit_library(self):
        for arch, path in (("i686", "/usr/lib/library"), ("x86_64", "/usr/lib64/library")):
            self.install_health_fixture("multilib", arch=arch, payload=path)
        base = self.base(self.job)
        goal = b.Goal(base)
        goal.add_remove("multilib.i686")
        transaction = goal.resolve()
        planner.check_resolution(transaction)
        migrations = MigrationPlan()
        planner.allow_architecture_cleanup(base, transaction, migrations)
        self.assertEqual(migrations.remove, set())

    def test_redundant_local_noarch_copy_is_retired(self):
        self.install_health_fixture("emacs-filesystem", arch="noarch", origin="@commandline")
        self.install_health_fixture("emacs-filesystem", arch="x86_64")
        self.add_obsolete_repo_package("emacs-filesystem", "1", arch="x86_64")
        result = self.prepare()
        self.assertEqual([(p["arch"], p["action"]) for p in result["packages"] if p["name"] == "emacs-filesystem"],
                         [("noarch", "Remove")])

    def test_missing_multilib_dependency_is_restored_in_offline_plan_and_replay(self):
        self.install_health_fixture("libchromaprint", arch="i686",
                                    headers="Requires: libavutil.so.60\nRequires: libavutil.so.60(LIBAVUTIL_60)")
        for arch, suffix, libdir in (("i686", "", "lib"), ("x86_64", "(64bit)", "lib64")):
            self.add_obsolete_repo_package("libavutil-free", "1", arch=arch, payload=f"/usr/{libdir}/libavutil.so.60",
                headers=f"Provides: libavutil.so.60{suffix}\nProvides: libavutil.so.60(LIBAVUTIL_60){suffix}")
        result = self.prepare()
        self.assertEqual([(p["arch"], p["action"]) for p in result["packages"] if p["name"] == "libavutil-free"],
                         [("i686", "Install")])
        self.assertFalse(any(p["name"] == "libchromaprint" for p in result["packages"]))
        self.assertEqual(result["execution"]["mode"], "offline")
        with self.assertRaises(UpdateError):
            self.check_fixture_health()  # Preparation has not changed the system.
        replay = subprocess.run(self.replay_command(test=False), capture_output=True, text=True)
        self.assertEqual(replay.returncode, 0, replay.stdout + replay.stderr)
        self.check_fixture_health()

    def test_unavailable_missing_dependency_reports_consumer_before_download(self):
        self.install_health_fixture("libchromaprint", headers="Requires: missing-libavutil", origin="@commandline")
        with self.assertRaises(UpdateError) as caught, patch.object(b.Transaction, "download") as download:
            self.prepare()
        self.assertIn("missing-libavutil", str(caught.exception))
        self.assertIn("libchromaprint", str(caught.exception))
        self.assertEqual(caught.exception.package_conflicts[0]["name"], "libchromaprint")
        download.assert_not_called()

    def test_repo_upgrade_replaces_local_consumer_without_pulling_its_old_dependencies(self):
        self.install_health_fixture("labwc", headers="Requires: custom-runtime", origin="@commandline")
        self.add_obsolete_repo_package("custom-runtime", "1")
        self.add_obsolete_repo_package("labwc", "2")
        result = self.prepare()
        self.assertTrue(any(p["name"] == "labwc" and p["action"] == "Upgrade" for p in result["packages"]))
        self.assertFalse(any(p["name"] == "custom-runtime" for p in result["packages"]))

    def competing_dependency_providers(self, *, selection="migration", rich=False):
        requirement = "(shared-codec-abi and retained-helper)" if rich else "shared-codec-abi"
        self.install_health_fixture("media-client", headers=f"Requires: {requirement}\nRequires: missing-runtime")
        if rich:
            self.install_health_fixture("retained-helper")
        if selection == "migration":
            self.install_health_fixture("alternate-codec", headers="Provides: shared-codec-abi = 1")
        elif selection == "retained":
            self.install_health_fixture("selected-codec", headers="Provides: shared-codec-abi = 1")
        self.add_obsolete_repo_package("selected-codec", "1", headers="Provides: shared-codec-abi = 1")
        self.add_obsolete_repo_package("alternate-codec", "2",
                                       headers="Provides: shared-codec-abi = 2\nConflicts: selected-codec")
        media = Path(self.case.name) / "media"
        media.mkdir()
        shutil.copy2(self.top / "RPMS/noarch/alternate-codec-2-1.noarch.rpm", media)
        subprocess.run(["createrepo_c", str(media)], check=True, capture_output=True)
        (self.root / "fixture-repos").mkdir()
        (self.root / "fixture-repos/media.repo").write_text(
            f"[media]\nname=Media\nbaseurl={media.as_uri()}\nenabled=1\npriority=25\n")
        self.add_obsolete_repo_package("missing-runtime", "1")
        if selection != "retained":
            self.migrations = MigrationPlan(remove={"alternate-codec"} if selection == "migration" else set(),
                                            install={"selected-codec"})

    def assert_dependency_repair_keeps_selected_provider(self):
        result = self.prepare()
        self.assertTrue(any(p["name"] == "missing-runtime" and p["action"] == "Install" for p in result["packages"]))
        self.assertFalse(any(p["name"] == "alternate-codec" and p["action"] != "Remove" for p in result["packages"]))
        self.assertFalse(any(p["name"] == "selected-codec" and p["action"] in {"Remove", "Replaced"}
                             for p in result["packages"]))
        replay = subprocess.run(self.replay_command(test=False), capture_output=True, text=True)
        self.assertEqual(replay.returncode, 0, replay.stdout + replay.stderr)
        self.check_fixture_health()

    def test_dependency_repair_preserves_migrated_provider_for_satisfied_requirement(self):
        self.competing_dependency_providers()
        self.assert_dependency_repair_keeps_selected_provider()

    def test_dependency_repair_preserves_retained_provider_for_satisfied_requirement(self):
        self.competing_dependency_providers(selection="retained")
        self.assert_dependency_repair_keeps_selected_provider()

    def test_dependency_repair_uses_missing_provider_already_in_transaction(self):
        self.competing_dependency_providers(selection="new")
        self.assert_dependency_repair_keeps_selected_provider()

    def test_dependency_repair_preserves_satisfied_rich_requirement(self):
        self.competing_dependency_providers(rich=True)
        self.assert_dependency_repair_keeps_selected_provider()

    def test_dependency_repair_checks_selected_providers_with_older_dnf5(self):
        self.competing_dependency_providers()
        with patch.object(rpm_api.PackageQuery, "is_dep_satisfied", None, create=True):
            self.assert_dependency_repair_keeps_selected_provider()

    def test_rich_dependencies_are_repaired_by_native_solver(self):
        self.install_health_fixture("client", headers="Requires: (addon-a or addon-b)")
        self.add_obsolete_repo_package("addon-b", "1")
        result = self.prepare()
        self.assertTrue(any(p["name"] == "addon-b" and p["action"] == "Install" for p in result["packages"]))

    def test_rich_dependencies_are_repaired_with_older_dnf5(self):
        with patch.object(rpm_api.PackageQuery, "is_dep_satisfied", None, create=True):
            self.test_rich_dependencies_are_repaired_by_native_solver()

    def test_dependency_repair_does_not_use_provider_being_removed(self):
        self.install_health_fixture("client", headers="Requires: shared-abi\nRequires: missing-runtime")
        self.install_health_fixture("old-provider", headers="Provides: shared-abi")
        self.add_obsolete_repo_package("new-provider", "1", headers="Provides: shared-abi")
        self.add_obsolete_repo_package("missing-runtime", "1")
        self.migrations = MigrationPlan(remove={"old-provider"})
        result = self.prepare()
        self.assertTrue(any(p["name"] == "new-provider" and p["action"] == "Install" for p in result["packages"]))
        replay = subprocess.run(self.replay_command(test=False), capture_output=True, text=True)
        self.assertEqual(replay.returncode, 0, replay.stdout + replay.stderr)
        self.check_fixture_health()

    def test_replacing_broken_consumer_does_not_pull_obsolete_abi(self):
        self.install_health_fixture("kpipewire", "1", headers="Requires: ffmpeg7-abi")
        self.add_obsolete_repo_package("kpipewire", "2", headers="Requires: ffmpeg8-abi")
        self.add_obsolete_repo_package("ffmpeg8", "1", headers="Provides: ffmpeg8-abi")
        result = self.prepare()
        self.assertTrue(any(p["name"] == "kpipewire" and p["action"] == "Upgrade" for p in result["packages"]))
        self.assertTrue(any(p["name"] == "ffmpeg8" and p["action"] == "Install" for p in result["packages"]))

    def test_older_desktop_can_retain_abi_using_available_compatibility_provider(self):
        self.install_health_fixture("kpipewire", headers="Requires: ffmpeg7-abi")
        self.install_health_fixture("plasma-desktop", headers="Requires: kpipewire")
        self.add_obsolete_repo_package("compat-ffmpeg7", "1", headers="Provides: ffmpeg7-abi")
        result = self.prepare()
        self.assertFalse(any(p["name"] in {"kpipewire", "plasma-desktop"} for p in result["packages"]))
        self.assertTrue(any(p["name"] == "compat-ffmpeg7" and p["action"] == "Install" for p in result["packages"]))

    def test_final_check_rejects_unresolved_installed_conflict(self):
        self.install_health_fixture("conflicting-fixture", headers="Conflicts: nobara-offline-fixture")
        with self.assertRaisesRegex(UpdateError, "conflicting-fixture"):
            self.prepare()
        self.assertFalse((self.job / "transaction.json").exists())

    def test_final_check_rejects_unresolved_duplicate_versions(self):
        self.install_health_fixture("orphan", "1", payload="/usr/share/orphan/1")
        self.install_health_fixture("orphan", "2", payload="/usr/share/orphan/2")
        with self.assertRaisesRegex(UpdateError, "Duplicate installed versions.*orphan"):
            self.prepare()
        self.assertFalse((self.job / "transaction.json").exists())

    def test_final_check_allows_multiple_installonly_kernel_versions(self):
        for version in ("1", "2"):
            self.install_health_fixture("retained-image", version, headers="Provides: installonlypkg(kernel)",
                                        payload=f"/boot/retained-{version}")
        result = self.prepare()
        self.assertFalse(any(p["name"] == "retained-image" for p in result["packages"]))

    def test_final_check_detects_dependencies_broken_by_removing_rich_provider(self):
        self.install_health_fixture("client", headers="Requires: (missing-addon or condition)")
        self.install_health_fixture("condition")
        self.migrations = MigrationPlan(remove={"condition"})
        with self.assertRaisesRegex(UpdateError, "missing-addon|client"):
            self.prepare()
        self.assertFalse((self.job / "transaction.json").exists())

    def test_generic_obsoletes_respects_epoch_version_ranges_and_leaves_orphans(self):
        self.install_health_fixture("legacy-alpha", "99", headers="Epoch: 1")
        self.install_health_fixture("legacy-beta", "3")
        self.install_health_fixture("unrelated-orphan")
        self.install_health_fixture("media-replacement", headers=(
            "Obsoletes: legacy-alpha < 2:1-1\nObsoletes: legacy-beta < 3"))
        result = self.prepare()
        removed = {p["name"] for p in result["packages"] if p["action"] == "Remove"}
        self.assertEqual(removed, {"legacy-alpha"})
        self.assertIn("legacy-alpha-1:99-1.noarch", result["migrations"]["remove"])
        replay = subprocess.run(self.replay_command(test=False), capture_output=True, text=True)
        self.assertEqual(replay.returncode, 0, replay.stdout + replay.stderr)
        self.check_fixture_health()

    def test_generic_obsoletes_removes_only_matching_multilib_version(self):
        self.install_health_fixture("legacy-lib", "1", arch="i686", payload="/usr/lib/legacy-lib.so")
        self.install_health_fixture("legacy-lib", "3", arch="x86_64", payload="/usr/lib64/legacy-lib.so")
        self.install_health_fixture("media-replacement", headers="Obsoletes: legacy-lib < 2")
        result = self.prepare()
        changed = {(p["arch"], p["action"]) for p in result["packages"] if p["name"] == "legacy-lib"}
        self.assertEqual(changed, {("i686", "Remove")})

    def test_generic_obsoletes_uses_replacement_metadata_after_its_upgrade(self):
        self.install_health_fixture("legacy-tool")
        self.install_health_fixture("replacement-tool", "1", headers="Obsoletes: legacy-tool")
        self.add_obsolete_repo_package("replacement-tool", "2")
        result = self.prepare()
        self.assertTrue(any(p["name"] == "replacement-tool" and p["action"] == "Upgrade" for p in result["packages"]))
        self.assertFalse(any(p["name"] == "legacy-tool" for p in result["packages"]))

    def test_generic_obsoletes_preserves_old_package_when_replacement_is_removed(self):
        self.install_health_fixture("legacy-tool")
        self.install_health_fixture("replacement-tool", headers="Obsoletes: legacy-tool")
        self.migrations = MigrationPlan(remove={"replacement-tool"})
        result = self.prepare()
        self.assertFalse(any(p["name"] == "legacy-tool" for p in result["packages"]))

    def test_generic_obsoletes_does_not_erase_package_being_upgraded_past_cutoff(self):
        self.install_health_fixture("legacy-tool", "1")
        self.install_health_fixture("replacement-tool", headers="Obsoletes: legacy-tool < 2")
        self.add_obsolete_repo_package("legacy-tool", "2")
        result = self.prepare()
        changed = {p["action"] for p in result["packages"] if p["name"] == "legacy-tool"}
        self.assertEqual(changed, {"Upgrade", "Replaced"})

    def test_generic_obsoletes_retires_local_and_unknown_origin_packages(self):
        for name, origin in (("local-tool", "@commandline"), ("unknown-tool", "")):
            self.install_health_fixture(name, origin=origin)
        self.install_health_fixture("replacement-tool", headers="Obsoletes: local-tool\nObsoletes: unknown-tool")
        result = self.prepare()
        self.assertEqual({p["name"] for p in result["packages"] if p["action"] == "Remove"},
                         {"local-tool", "unknown-tool"})

    def test_generic_obsoletes_preserves_essential_and_installonly_packages(self):
        self.install_health_fixture("bash")
        self.install_health_fixture("retained-image", headers="Provides: installonlypkg(kernel)")
        self.install_health_fixture("replacement-tool", headers="Obsoletes: bash\nObsoletes: retained-image")
        result = self.prepare()
        self.assertFalse(any(p["name"] in {"bash", "retained-image"} for p in result["packages"]))

    def test_generic_obsoletes_does_not_allow_cascading_application_removal(self):
        self.install_health_fixture("legacy-library")
        self.install_health_fixture("replacement-library", headers="Obsoletes: legacy-library")
        self.install_health_fixture("user-application", headers="Requires: legacy-library")
        with self.assertRaisesRegex(UpdateError, "user-application"), patch.object(b.Transaction, "download") as download:
            self.prepare()
        download.assert_not_called()

    def test_obsoletes_chain_keeps_packages_without_a_surviving_direct_replacement(self):
        self.install_health_fixture("old-tool")
        self.install_health_fixture("middle-tool", headers="Obsoletes: old-tool")
        self.install_health_fixture("new-tool", headers="Obsoletes: middle-tool")
        result = self.prepare()
        removed = {p["name"] for p in result["packages"] if p["action"] == "Remove"}
        self.assertEqual(removed, {"middle-tool"})

    def test_obsoletes_cycle_stops_before_downloading(self):
        self.install_health_fixture("tool-a", headers="Obsoletes: tool-b")
        self.install_health_fixture("tool-b", headers="Obsoletes: tool-a")
        with self.assertRaisesRegex(UpdateError, "cyclic Obsoletes"), patch.object(b.Transaction, "download") as download:
            self.prepare()
        download.assert_not_called()

    def test_self_obsoletes_does_not_remove_the_package_itself(self):
        self.install_health_fixture("self-replacer", headers="Obsoletes: self-replacer")
        result = self.prepare()
        self.assertFalse(any(p["name"] == "self-replacer" for p in result["packages"]))

    def test_obsolete_cleanup_cannot_remove_its_own_replacement_as_a_dependent(self):
        self.install_health_fixture("old-tool")
        self.install_health_fixture("replacement-tool", headers="Obsoletes: old-tool\nRequires: old-tool")
        with self.assertRaisesRegex(UpdateError, "replacement-tool"), \
             patch.object(b.Transaction, "download") as download:
            self.prepare()
        download.assert_not_called()

    def test_generic_obsoletes_respects_installed_package_excludes(self):
        self.install_health_fixture("old-tool")
        self.install_health_fixture("replacement-tool", headers="Obsoletes: old-tool")
        native_collect = planner.collect_package_origins

        def exclude(base):
            result = native_collect(base)
            packages = planner.rpm_api.PackageQuery(base)
            packages.filter_installed()
            packages.filter_name(["old-tool"])
            base.get_rpm_package_sack().add_user_excludes(packages)
            return result

        with patch.object(planner, "collect_package_origins", side_effect=exclude):
            result = self.prepare()
        self.assertFalse(any(p["name"] == "old-tool" for p in result["packages"]))

    def test_preflight_still_rejects_missing_installed_dependencies(self):
        self.install_health_fixture("broken-fixture", headers="Requires: missing-fixture-dependency")
        with self.assertRaisesRegex(UpdateError, "missing-fixture-dependency"):
            self.check_fixture_health()

    def test_preflight_still_rejects_installed_conflicts(self):
        self.install_health_fixture("conflicting-fixture", headers="Conflicts: nobara-offline-fixture")
        with self.assertRaisesRegex(UpdateError, "conflict"):
            self.check_fixture_health()

    def test_preflight_still_rejects_duplicate_versions(self):
        # Keep the previously installed version 1 in the database too.
        subprocess.run(["rpm", "--root", str(self.root), "--justdb", "--nodeps", "--noscripts", "--noplugins",
                        "--ignoresize", "-i", str(self.new)], check=True, capture_output=True)
        with self.assertRaisesRegex(UpdateError, "duplicate"):
            self.check_fixture_health()

    def updater_fixture(self, repository_version, name="nobara-updater"):
        installed = self.build_rpm(name, "2")
        available = self.build_rpm(name, repository_version)
        subprocess.run(["rpm", "--root", str(self.root), "--justdb", "--nodeps", "--noscripts", "--noplugins",
                        "--ignoresize", "-i", str(installed)], check=True, capture_output=True)
        self.set_origin(name, "nobara")
        private_repo = Path(self.case.name) / "updater-repo"
        shutil.copytree(self.repo, private_repo)
        shutil.copy2(available, private_repo)
        subprocess.run(["createrepo_c", str(private_repo)], check=True, capture_output=True)
        self.repo = private_repo

    def test_distro_sync_preserves_newer_installed_updater(self):
        self.updater_fixture("1")
        result = self.prepare()
        self.assertTrue(any(p["name"] == "nobara-offline-fixture" and p["action"] == "Upgrade" for p in result["packages"]))
        self.assertFalse(any(p["name"] == "nobara-updater" for p in result["packages"]))

    def test_distro_sync_still_upgrades_updater_when_repository_is_newer(self):
        self.updater_fixture("3")
        result = self.prepare()
        self.assertTrue(any(p["name"] == "nobara-updater" and p["action"] == "Upgrade" for p in result["packages"]))
        # Remove access to all repositories. Native DNF5 must still be able to
        # validate the exact saved transaction using only its bundled RPMs.
        replay = subprocess.run(self.replay_command(test=True), capture_output=True, text=True)
        self.assertEqual(replay.returncode, 0, replay.stdout + replay.stderr)

    def test_distro_sync_preserves_newer_installed_drm_awaiter(self):
        self.updater_fixture("1", "drm-awaiter")
        result = self.prepare()
        self.assertFalse(any(p["name"] == "drm-awaiter" for p in result["packages"]))

    def prepare_early(self):
        with patch.object(planner, "configure_base", side_effect=self.base), \
             patch.object(planner, "os_release", return_value="44"), \
             patch.object(planner, "boot_space_requirements", side_effect=AssertionError("Too early for boot-space checks")), \
             patch.object(planner, "plan_migrations", side_effect=AssertionError("Too early for main fixups")):
            return planner.prepare_early_transaction(self.job, backend.EARLY_PACKAGES)

    def test_early_upgrade_selects_both_helpers_but_leaves_unrelated_updates(self):
        self.updater_fixture("3")
        self.install_health_fixture("drm-awaiter", "1")
        shutil.copy2(self.build_rpm("drm-awaiter", "2"), self.repo)
        subprocess.run(["createrepo_c", str(self.repo)], check=True, capture_output=True)
        result = self.prepare_early()
        self.assertFalse(result["empty"])
        saved = json.loads((self.job / "transaction.json").read_text())
        # The native serialized NEVRA omits epoch 0; identify via package paths.
        payloads = {Path(p["package_path"]).name for p in saved["rpms"] if "package_path" in p}
        self.assertEqual(payloads, {"nobara-updater-3-1.noarch.rpm", "drm-awaiter-2-1.noarch.rpm"})
        replay = subprocess.run(self.replay_command(test=False), capture_output=True, text=True)
        self.assertEqual(replay.returncode, 0, replay.stdout + replay.stderr)
        installed = subprocess.run(["rpm", "--root", str(self.root), "-q", "nobara-updater", "drm-awaiter",
                                    "nobara-offline-fixture", "--qf", "%{NAME}=%{VERSION}\n"],
                                   check=True, capture_output=True, text=True).stdout
        self.assertEqual(set(installed.splitlines()), {"nobara-updater=3", "drm-awaiter=2", "nobara-offline-fixture=1"})

    def test_early_upgrade_does_not_downgrade_or_install_absent_helper(self):
        self.updater_fixture("1")
        shutil.copy2(self.build_rpm("drm-awaiter", "4"), self.repo)
        subprocess.run(["createrepo_c", str(self.repo)], check=True, capture_output=True)
        self.assertTrue(self.prepare_early()["empty"])
        self.assertFalse((self.job / "transaction.json").exists())

    def test_early_upgrade_updates_locally_installed_helpers(self):
        self.updater_fixture("3")
        self.set_origin("nobara-updater", "@commandline")
        self.install_health_fixture("drm-awaiter", "1", origin="@commandline")
        self.add_obsolete_repo_package("drm-awaiter", "2")
        self.assertFalse(self.prepare_early()["empty"])
        saved = json.loads((self.job / "transaction.json").read_text())
        payloads = {Path(p["package_path"]).name for p in saved["rpms"] if "package_path" in p}
        self.assertEqual(payloads, {"nobara-updater-3-1.noarch.rpm", "drm-awaiter-2-1.noarch.rpm"})

    def test_early_upgrade_can_replace_a_local_dependency(self):
        self.install_health_fixture("drm-awaiter", "1")
        self.install_health_fixture("labwc", "1", origin="@commandline")
        private_repo = Path(self.case.name) / "early-repo"
        shutil.copytree(self.repo, private_repo)
        for package in (self.build_rpm("drm-awaiter", "2", headers="Requires: labwc >= 2"),
                        self.build_rpm("labwc", "2")):
            shutil.copy2(package, private_repo)
        subprocess.run(["createrepo_c", str(private_repo)], check=True, capture_output=True)
        self.repo = private_repo
        self.assertFalse(self.prepare_early()["empty"])
        saved = json.loads((self.job / "transaction.json").read_text())
        payloads = {Path(p["package_path"]).name for p in saved["rpms"] if "package_path" in p}
        self.assertEqual(payloads, {"drm-awaiter-2-1.noarch.rpm", "labwc-2-1.noarch.rpm"})

    def test_higher_priority_repo_wins_over_newer_fedora_candidate(self):
        upstream = Path(self.case.name) / "upstream"
        upstream.mkdir()
        shutil.copy2(self.build_rpm("nobara-offline-fixture", "9"), upstream)
        shutil.copy2(self.top / "RPMS/noarch/nobara-release-common-44-1.noarch.rpm", upstream)
        subprocess.run(["createrepo_c", str(upstream)], check=True, capture_output=True)
        # The original repository has version 2; Fedora's stand-in has 9.
        repos = self.root / "fixture-repos"
        repos.mkdir()
        (repos / "baseos.repo").write_text(
            f"[baseos]\nname=Base OS\nbaseurl={self.repo.as_uri()}\nenabled=1\npriority=50\n")
        self.repo = upstream
        result = self.prepare()
        package = next(p for p in result["packages"] if p["name"] == "nobara-offline-fixture" and p["action"] == "Upgrade")
        self.assertEqual(package["nevra"], "nobara-offline-fixture-0:2-1.noarch")
        saved = json.loads((self.job / "transaction.json").read_text())
        self.assertTrue(any(p["action"] == "Upgrade" and p.get("repo_id") == "baseos" for p in saved["rpms"]))

    def test_resolution_error_reports_missing_packages_without_installed_notices(self):
        base = self.base(self.job)
        goal = b.Goal(base)
        goal.add_install("nobara-offline-fixture-1-1.noarch")
        settings = b.GoalJobSettings()
        settings.set_skip_unavailable(False)
        goal.add_install("nobara-missing-fixture", settings)
        transaction = goal.resolve()
        self.assertTrue(any(event.get_problem() == b.GoalProblem_ALREADY_INSTALLED for event in transaction.get_resolve_logs()))
        with self.assertRaises(UpdateError) as error:
            planner.check_resolution(transaction)
        self.assertIn("nobara-missing-fixture", str(error.exception))
        self.assertNotIn("already installed", str(error.exception))

    def test_updater_preserves_installed_groups_without_installing_new_fedora_defaults(self):
        # Record a real installed environment/group in the disposable DNF
        # state, with the package already present in this fixture's RPMDB.
        base = self.base(self.job)
        goal = b.Goal(base)
        settings = b.GoalJobSettings()
        settings.set_group_search_environments(True)
        settings.set_group_search_groups(False)
        settings.set_group_no_packages(True)
        goal.add_group_install("fixture-environment", planner.trans.TransactionItemReason_USER, settings)
        transaction = goal.resolve()
        planner.check_resolution(transaction)
        self.assertEqual(transaction.get_transaction_packages_count(), 0)
        self.assertEqual(transaction.run(), b.Transaction.TransactionRunResult_SUCCESS)
        base.unlock_system_repo()

        # Model Nobara's workspace obsoleting Fedora's appearance package.
        old_workspace = self.build_rpm("nobara-plasma-fixture", "1", headers="Obsoletes: nobara-fedora-lookandfeel")
        subprocess.run(["rpm", "--dbpath", str(self.root / "usr/lib/sysimage/rpm"), "--justdb", "--nodeps",
                        "--noscripts", "--noplugins", "--ignoresize", "-i", str(old_workspace)], check=True, capture_output=True)
        self.set_origin("nobara-plasma-fixture", "nobara")
        baseos = Path(self.case.name) / "baseos"
        baseos.mkdir()
        shutil.copy2(self.build_rpm("nobara-plasma-fixture", "2", headers="Obsoletes: nobara-fedora-lookandfeel"), baseos)
        subprocess.run(["createrepo_c", str(baseos)], check=True, capture_output=True)
        (self.root / "fixture-repos/baseos.repo").write_text(
            f"[baseos]\nname=Base OS\nbaseurl={baseos.as_uri()}\nenabled=1\npriority=50\n")
        upstream = Path(self.case.name) / "upstream"
        shutil.copytree(self.repo, upstream)
        shutil.copy2(self.build_rpm("nobara-plasma-fixture", "9"), upstream)
        shutil.copy2(self.build_rpm("nobara-fedora-lookandfeel", "1", headers="Requires: nobara-plasma-fixture = 9-1"), upstream)
        shutil.copy2(self.build_rpm("nobara-plasma-setup", "1", headers="Requires: nobara-fedora-lookandfeel"), upstream)
        new_comps = Path(self.case.name) / "new-comps.xml"
        new_comps.write_text(self.comps.read_text().replace("</packagelist>", '<packagereq type="mandatory">nobara-plasma-setup</packagereq></packagelist>'))
        subprocess.run(["createrepo_c", "-g", str(new_comps), str(upstream)], check=True, capture_output=True)
        self.repo = upstream

        result = self.prepare()
        self.assertTrue(any(p["nevra"] == "nobara-plasma-fixture-0:2-1.noarch" and p["action"] == "Upgrade" for p in result["packages"]))
        self.assertFalse(any(p["name"] in {"nobara-plasma-setup", "nobara-fedora-lookandfeel"} for p in result["packages"]))
        saved = json.loads((self.job / "transaction.json").read_text())
        self.assertFalse(saved.get("groups"))
        self.assertFalse(saved.get("environments"))
        base = self.base(self.job)
        for query_class, getter, identifier in ((comps.GroupQuery, "get_groupid", "fixture-group"),
                                                (comps.EnvironmentQuery, "get_environmentid", "fixture-environment")):
            query = query_class(base)
            query.filter_installed(True)
            self.assertIn(identifier, [getattr(item, getter)() for item in query])

    def replay_command(self, *, test):
        empty_repos = self.root / "etc/yum.repos.d"
        empty_repos.mkdir(parents=True, exist_ok=True)
        (empty_repos / "offline.repo").write_text("[unavailable]\nname=Must never be accessed\nbaseurl=file:///nonexistent-offline-test\nenabled=1\n")
        # Use the production command; redirect only the root and unsigned
        # fixture policy. Keeping a second replay command hid a real failure.
        with patch.object(backend, "run") as run:
            backend.replay(self.job, test=test)
        command = run.call_args.args[0]
        return [*command[:1], "--installroot=" + str(self.root), "--releasever=44",
                *command[1:-3], "--setopt=logdir=" + str(self.job),
                "--setopt=localpkg_gpgcheck=0", "--setopt=pkg_gpgcheck=0", *command[-3:]]

    def test_offline_replay_ignores_configured_repo_with_same_id_as_saved_payload(self):
        self.prepare()
        repos = self.root / "etc/yum.repos.d"
        repos.mkdir(parents=True, exist_ok=True)
        (repos / "fixture.repo").write_text(
            "[fixture]\nname=Original remote repo\nbaseurl=https://unavailable.invalid/\nenabled=1\n")
        shutil.rmtree(self.job / "cache")
        command = self.replay_command(test=True)
        # DNF5 5.4.3 reuses a disabled remote repo object instead of making a
        # local replay repo. Reproduce the production failure before the fix.
        broken = subprocess.run([c if c != "--setopt=reposdir=" else "--disable-repo=*" for c in command], capture_output=True, text=True)
        self.assertNotEqual(broken.returncode, 0)
        self.assertIn("cacheonly option is activated", broken.stdout + broken.stderr)
        fixed = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(fixed.returncode, 0, fixed.stdout + fixed.stderr)
        installed = subprocess.check_output(["rpm", "--root", str(self.root), "-q", "nobara-offline-fixture", "--qf", "%{VERSION}"], text=True)
        self.assertEqual(installed, "1")
        # The same saved transaction really installs with all repos absent.
        applied = subprocess.run(self.replay_command(test=False), capture_output=True, text=True)
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        self.assertEqual((self.root / "usr/share/nobara-offline-fixture/version").read_text().strip(), "2")

    def test_group_and_environment_definitions_can_be_replayed_without_repos(self):
        base = self.base(self.job)
        goal = b.Goal(base)
        settings = b.GoalJobSettings()
        settings.set_group_search_environments(True)
        settings.set_group_search_groups(False)
        goal.add_group_install("fixture-environment", planner.trans.TransactionItemReason_USER, settings)
        transaction = goal.resolve()
        planner.check_resolution(transaction)
        transaction.download()
        incoming = [item.get_package() for item in transaction.get_transaction_packages()
                    if item.get_action() in {planner.trans.TransactionItemAction_INSTALL, planner.trans.TransactionItemAction_UPGRADE}]
        planner.store_transaction(transaction, incoming, self.job)
        saved = json.loads((self.job / "transaction.json").read_text())
        self.assertTrue(saved["groups"])
        self.assertTrue(saved["environments"])
        self.assertTrue((self.job / saved["groups"][0]["group_path"]).is_file())
        base.unlock_system_repo()
        replay = subprocess.run(self.replay_command(test=True), capture_output=True, text=True)
        self.assertEqual(replay.returncode, 0, replay.stdout + replay.stderr)

    def test_scriptlet_failure_is_not_reported_as_success(self):
        broken_repo = Path(self.case.name) / "broken-repo"
        broken_repo.mkdir()
        broken = self.build_rpm("nobara-offline-fixture", "3", '%post -p <lua>\nerror("intentional fixture scriptlet failure")')
        shutil.copy2(broken, broken_repo)
        shutil.copy2(self.top / "RPMS/noarch/nobara-release-common-44-1.noarch.rpm", broken_repo)
        subprocess.run(["createrepo_c", str(broken_repo)], check=True, capture_output=True)
        self.repo = broken_repo
        self.prepare()
        with self.assertLogs(backend.LOG, level="INFO") as logs:
            with self.assertRaises(UpdateError):
                backend.run(self.replay_command(test=False))
        self.assertIn("intentional fixture scriptlet failure", "\n".join(logs.output))

    def test_missing_download_never_changes_installed_packages(self):
        with patch.object(b.Transaction, "download", side_effect=RuntimeError("RPM disappeared")):
            with self.assertRaisesRegex(RuntimeError, "disappeared"):
                self.prepare()
        self.assertFalse((self.job / "transaction.json").exists())
        installed = subprocess.run(["rpm", "--root", str(self.root), "-q", "nobara-offline-fixture", "--qf", "%{VERSION}"], check=True, capture_output=True, text=True)
        self.assertEqual(installed.stdout, "1")

    def test_major_release_is_staged_without_live_release_package_install(self):
        old_release = self.build_rpm("nobara-release-common", "43")
        subprocess.run(["rpm", "--dbpath", str(self.root / "usr/lib/sysimage/rpm"), "--justdb", "--nodeps",
                        "--noscripts", "--noplugins", "--ignoresize", "-i", str(old_release)], check=True, capture_output=True)
        self.set_origin("nobara-release-common", "nobara")
        self.current_release = "43"
        result = self.prepare()
        self.assertEqual((result["source_release"], result["target_release"]), ("43", "44"))
        self.assertEqual(result["execution"]["mode"], "offline")
        self.assertTrue(any(p["name"] == "nobara-release-common" and p["action"] == "Upgrade" for p in result["packages"]))
        installed = subprocess.run(["rpm", "--root", str(self.root), "-q", "nobara-release-common", "--qf", "%{VERSION}"],
                                   check=True, capture_output=True, text=True)
        self.assertEqual(installed.stdout, "43")
        replay = subprocess.run(self.replay_command(test=True), capture_output=True, text=True)
        self.assertEqual(replay.returncode, 0, replay.stdout + replay.stderr)

    def test_unsigned_payload_is_rejected_when_signature_policy_is_enabled(self):
        self.require_signatures = True
        with self.assertRaisesRegex(UpdateError, "signature verification failed"):
            self.prepare()
        self.assertFalse((self.job / "transaction.json").exists())

    def test_unsupported_kernel_boot_layout_stops_before_downloading_or_installing(self):
        old = self.build_rpm("kernel-core", "1")
        new = self.build_rpm("kernel-core", "2")
        subprocess.run(["rpm", "--root", str(self.root), "--justdb", "--nodeps", "--noscripts", "--noplugins",
                        "--ignoresize", "-i", str(old)], check=True, capture_output=True)
        self.set_origin("kernel-core", "nobara-kernel-mainline")
        private_repo = Path(self.case.name) / "kernel-repo"
        shutil.copytree(self.repo, private_repo)
        shutil.copy2(new, private_repo)
        subprocess.run(["createrepo_c", str(private_repo)], check=True, capture_output=True)
        self.repo = private_repo
        with patch.object(planner, "check_selection_support", side_effect=UpdateError("Unsupported boot layout")) as check, \
             patch.object(b.Transaction, "download") as download:
            with self.assertRaisesRegex(UpdateError, "Unsupported boot layout"):
                self.prepare()
        check.assert_called_once()
        download.assert_not_called()
        installed = subprocess.run(["rpm", "--root", str(self.root), "-q", "kernel-core", "--qf", "%{VERSION}"],
                                   check=True, capture_output=True, text=True)
        self.assertEqual(installed.stdout, "1")

    def install_current_release(self):
        subprocess.run(["rpm", "--root", str(self.root), "--nodeps", "--noscripts", "--noplugins", "-i",
                        str(self.top / "RPMS/noarch/nobara-release-common-44-1.noarch.rpm")], check=True, capture_output=True)
        self.set_origin("nobara-release-common", "nobara")

    def local_desktop_fixture(self, available_version="6", origin="@commandline", dependency=""):
        installed = self.build_rpm("labwc", "5", headers=dependency)
        subprocess.run(["rpm", "--root", str(self.root), "--justdb", "--nodeps", "--noscripts", "--noplugins",
                        "--ignoresize", "-i", str(installed)], check=True, capture_output=True)
        self.set_origin("labwc", origin)
        private = Path(self.case.name) / "desktop-repo"
        shutil.copytree(self.repo, private)
        shutil.copy2(self.build_rpm("labwc", available_version), private)
        subprocess.run(["createrepo_c", str(private)], check=True, capture_output=True)
        self.repo, self.repo_id = private, "nobara"

    def test_local_rpm_is_upgraded_by_repository_and_replay(self):
        self.local_desktop_fixture()
        result = self.prepare()
        self.assertTrue(any(p["name"] == "labwc" and p["action"] == "Upgrade" for p in result["packages"]))
        self.assertEqual(next(p for p in result["package_origins"] if p["name"] == "labwc")["repo"], "@commandline")
        replay = subprocess.run(self.replay_command(test=False), capture_output=True, text=True)
        self.assertEqual(replay.returncode, 0, replay.stdout + replay.stderr)
        installed = subprocess.run(["rpm", "--root", str(self.root), "-q", "labwc", "--qf", "%{VERSION}"],
                                   check=True, capture_output=True, text=True)
        self.assertEqual(installed.stdout, "6")

    def test_local_rpm_still_obeys_explicit_repository_excludes(self):
        self.local_desktop_fixture()
        native_base = self.base
        def excluding_base(*args, **kwargs):
            base = native_base(*args, **kwargs)
            packages = rpm_api.PackageQuery(base)
            packages.filter_available()
            packages.filter_name(["labwc"])
            base.get_rpm_package_sack().add_user_excludes(packages)
            return base
        with patch.object(self, "base", side_effect=excluding_base):
            self.assertFalse(any(p["name"] == "labwc" for p in self.prepare()["packages"]))

    def test_unavailable_local_rpm_is_not_removed_merely_for_being_local(self):
        self.install_health_fixture("custom-tool", origin="@commandline")
        self.assertFalse(any(p["name"] == "custom-tool" for p in self.prepare()["packages"]))

    def test_local_rpm_is_downgraded_to_repository_version(self):
        self.local_desktop_fixture(available_version="4")
        self.assertTrue(any(p["name"] == "labwc" and p["action"] == "Downgrade" for p in self.prepare()["packages"]))

    def test_equal_version_local_rpm_is_not_forcibly_reinstalled(self):
        self.local_desktop_fixture(available_version="5")
        self.assertFalse(any(p["name"] == "labwc" for p in self.prepare()["packages"]))

    def test_selected_update_includes_commandline_origin_without_at_prefix(self):
        self.local_desktop_fixture(origin="commandline")
        result = self.prepare(packages=["labwc"])
        self.assertTrue(any(p["name"] == "labwc" and p["action"] == "Upgrade" for p in result["packages"]))
        self.assertFalse(any(p["name"] == "nobara-offline-fixture" for p in result["packages"]))

    def test_rpmfusion_repo_rpms_and_local_desktop_follow_normal_updates(self):
        self.local_desktop_fixture()
        names = ("rpmfusion-free-release", "rpmfusion-nonfree-release")
        for name, origin in zip(names, ("@commandline", "<unknown>")):
            installed = self.build_rpm(name, "43")
            subprocess.run(["rpm", "--root", str(self.root), "--justdb", "--nodeps", "--noscripts",
                            "--noplugins", "--ignoresize", "-i", str(installed)], check=True, capture_output=True)
            self.set_origin(name, origin)
            shutil.copy2(self.build_rpm(name, "44"), self.repo)
        subprocess.run(["createrepo_c", str(self.repo)], check=True, capture_output=True)
        result = self.prepare()
        upgraded = {p["name"] for p in result["packages"] if p["action"] == "Upgrade"}
        self.assertTrue(set(names) | {"labwc"} <= upgraded)

    def test_unknown_origin_direct_rpm_install_is_also_upgraded(self):
        self.local_desktop_fixture(origin="<unknown>")
        result = self.prepare()
        self.assertTrue(any(p["name"] == "labwc" and p["action"] == "Upgrade" for p in result["packages"]))
        self.assertEqual(next(p for p in result["package_origins"] if p["name"] == "labwc")["kind"], "unknown")

    def test_missing_history_for_distribution_signed_rpm_does_not_hold_upgrade(self):
        self.local_desktop_fixture(origin="<unknown>")
        with patch.object(origins, "distribution_signed_packages", return_value={"labwc-0:5-1.noarch"}):
            result = self.prepare()
        self.assertTrue(any(p["name"] == "labwc" and p["action"] == "Upgrade" for p in result["packages"]))

    def test_explicit_local_origin_is_upgraded_with_distribution_signature(self):
        self.local_desktop_fixture(origin="@commandline")
        with patch.object(origins, "distribution_signed_packages", return_value={"labwc-0:5-1.noarch"}):
            self.assertTrue(any(p["name"] == "labwc" and p["action"] == "Upgrade" for p in self.prepare()["packages"]))

    def kernel_development_fixture(self, *, matching=True):
        self.local_desktop_fixture()
        for name in ("dkms", "kernel-core"):
            headers = "Provides: kernel-uname-r = 1-1.noarch" if name == "kernel-core" else ""
            installed = self.build_rpm(name, "1", headers=headers)
            subprocess.run(["rpm", "--root", str(self.root), "--justdb", "--nodeps", "--noscripts",
                            "--noplugins", "--ignoresize", "-i", str(installed)], check=True, capture_output=True)
            self.set_origin(name, "nobara-kernel-mainline")
        shutil.copy2(self.build_rpm("kernel-core", "2", headers="Provides: kernel-uname-r = 2-1.noarch"), self.repo)
        if matching:
            shutil.copy2(self.build_rpm("kernel-lto-devel", "2", headers=(
                "Provides: kernel-devel-uname-r = 2-1.noarch\nProvides: installonlypkg(kernel)"),
                payload="/usr/src/kernels/2-1.noarch/Makefile"), self.repo)
        subprocess.run(["createrepo_c", str(self.repo)], check=True, capture_output=True)

    def test_kernel_development_uses_matching_lto_provider_without_generic_devel(self):
        self.kernel_development_fixture()
        with patch.object(planner, "check_selection_support"):
            result = self.prepare()
        self.assertTrue(any(p["name"] == "kernel-lto-devel" and p["action"] == "Install" for p in result["packages"]))
        self.assertFalse(any(p["name"] == "kernel-devel" for p in result["packages"]))

    def test_missing_matching_development_provider_stops_before_download(self):
        self.kernel_development_fixture(matching=False)
        with patch.object(b.Transaction, "download") as download:
            with self.assertRaisesRegex(UpdateError, "kernel-devel-uname-r = 2-1.noarch"):
                self.prepare()
            download.assert_not_called()

    def kernel_rebuild_fixture(self, *, new_kernel=True):
        if new_kernel:
            self.kernel_development_fixture()
        else:
            self.local_desktop_fixture()
            self.install_health_fixture("dkms")
        # A driver/tool update causes rebuilding retained bootable kernels.
        shutil.copy2(self.build_rpm("dkms", "2"), self.repo)
        subprocess.run(["createrepo_c", str(self.repo)], check=True, capture_output=True)

    def retained_kernel_fixture(self, kernel, *, image=True):
        directory = self.root / "usr/lib/modules" / kernel
        directory.mkdir(parents=True, exist_ok=True)
        if image:
            (self.root / "boot").mkdir(exist_ok=True)
            (self.root / "boot" / ("vmlinuz-" + kernel)).write_text("fixture kernel")

    def test_kernel_development_ignores_local_module_only_remnants(self):
        self.kernel_rebuild_fixture()
        old = "6.12.6-200.fsync.fc41.x86_64"
        self.install_health_fixture("kernel-modules-core", "6.12.6",
                                    headers=f"Provides: kernel-uname-r = {old}", origin="<unknown>")
        self.install_health_fixture("kmod-v4l2loopback-" + old, origin="@commandline")
        self.retained_kernel_fixture(old, image=False)
        with patch.object(planner, "check_selection_support"), self.assertLogs(planner.LOG, level="INFO") as logs:
            result = self.prepare()
        self.assertTrue(any(old in line and "no installed boot image" in line for line in logs.output))
        self.assertTrue(any(p["name"] == "kernel-lto-devel" for p in result["packages"]))
        self.assertFalse(any(p["name"] == "kernel-modules-core" or p["name"].startswith("kmod-v4l2loopback")
                             for p in result["packages"]))
        self.assertEqual(result["preserved_kernels"], [])

    def test_kernel_development_preserves_old_running_kernel_when_new_kernel_will_boot(self):
        self.kernel_rebuild_fixture()
        self.retained_kernel_fixture("1-1.noarch")
        with patch.object(planner, "check_selection_support"), \
             patch.object(planner.os, "uname", return_value=Mock(release="1-1.noarch")):
            result = self.prepare()
        self.assertEqual(result["preserved_kernels"], ["1-1.noarch"])
        self.assertTrue(any(p["name"] == "kernel-lto-devel" for p in result["packages"]))
        self.assertFalse(any(p["name"] == "kernel-core" and p["action"] == "Remove" for p in result["packages"]))

    def test_kernel_development_preserves_old_fallback_during_driver_only_update(self):
        self.kernel_rebuild_fixture(new_kernel=False)
        for kernel in ("1-1.noarch", "2-1.noarch"):
            self.retained_kernel_fixture(kernel)
        self.install_health_fixture("kernel-lto-devel", "2", headers="Provides: kernel-devel-uname-r = 2-1.noarch")
        with patch.object(planner.os, "uname", return_value=Mock(release="2-1.noarch")), \
             patch.object(planner, "check_selection_support") as check:
            result = self.prepare()
        check.assert_called_once()
        self.assertEqual(result["preserved_kernels"], ["1-1.noarch"])
        self.assertFalse(any(p["name"].startswith("kernel") and p["action"] != "Reason Change"
                             for p in result["packages"]))

    def test_kernel_development_does_not_skip_current_kernel_without_replacement(self):
        self.kernel_rebuild_fixture(new_kernel=False)
        self.retained_kernel_fixture("1-1.noarch")
        with patch.object(planner.os, "uname", return_value=Mock(release="1-1.noarch")), \
             patch.object(b.Transaction, "download") as download:
            with self.assertRaisesRegex(UpdateError, "kernel-devel-uname-r = 1-1.noarch"):
                self.prepare()
        download.assert_not_called()

    def test_kernel_development_does_not_guess_chroot_boot_target(self):
        self.kernel_rebuild_fixture(new_kernel=False)
        self.retained_kernel_fixture("1-1.noarch")
        with patch.object(planner.os, "uname", return_value=Mock(release="host-kernel")), \
             patch.object(b.Transaction, "download") as download:
            with self.assertRaisesRegex(UpdateError, "kernel-devel-uname-r = 1-1.noarch"):
                self.prepare()
        download.assert_not_called()

    def test_kernel_development_still_rebuilds_retained_kernel_with_available_headers(self):
        self.kernel_rebuild_fixture()
        self.retained_kernel_fixture("1-1.noarch")
        shutil.copy2(self.build_rpm("kernel-lto-devel", "1", headers=(
            "Provides: kernel-devel-uname-r = 1-1.noarch\nProvides: installonlypkg(kernel)"),
            payload="/usr/src/kernels/1-1.noarch/Makefile"), self.repo)
        subprocess.run(["createrepo_c", str(self.repo)], check=True, capture_output=True)
        with patch.object(planner, "check_selection_support"):
            result = self.prepare()
        self.assertEqual(result["preserved_kernels"], [])
        self.assertEqual(len([p for p in result["packages"] if p["name"] == "kernel-lto-devel"]), 2)

    def test_obsoletes_can_replace_local_package_under_a_different_name(self):
        self.local_desktop_fixture()
        replacement = self.build_rpm("replacement-desktop", "1", headers="Obsoletes: labwc < 99\nProvides: labwc = 6")
        shutil.copy2(replacement, self.repo)
        subprocess.run(["createrepo_c", str(self.repo)], check=True, capture_output=True)
        result = self.prepare()
        self.assertTrue(any(p["name"] == "replacement-desktop" and p["action"] == "Install" for p in result["packages"]))
        self.assertTrue(any(p["name"] == "labwc" and p["action"] == "Replaced" for p in result["packages"]))

    def test_migration_can_remove_local_package_when_explicitly_allowed(self):
        self.local_desktop_fixture()
        self.migrations = MigrationPlan(remove={"labwc"})
        result = self.prepare()
        self.assertTrue(any(p["name"] == "labwc" and p["action"] == "Remove" for p in result["packages"]))

    def test_local_dependency_conflict_is_resolved_by_repository_upgrade(self):
        self.local_desktop_fixture()
        requiring = self.build_rpm("nobara-offline-fixture", "2", headers="Requires: labwc >= 6")
        shutil.copy2(requiring, self.repo)
        subprocess.run(["createrepo_c", str(self.repo)], check=True, capture_output=True)
        result = self.prepare()
        self.assertTrue(any(p["name"] == "labwc" and p["action"] == "Upgrade" for p in result["packages"]))

    def test_third_party_repo_conflict_is_identified_without_holding_repo_packages(self):
        self.local_desktop_fixture(origin="copr:test:desktop", dependency="Requires: nobara-offline-fixture = 1")
        third_party = Path(self.case.name) / "third-party"
        third_party.mkdir()
        shutil.copy2(self.top / "RPMS/noarch/labwc-5-1.noarch.rpm", third_party)
        subprocess.run(["createrepo_c", str(third_party)], check=True, capture_output=True)
        repos = self.root / "fixture-repos"
        repos.mkdir()
        (repos / "third-party.repo").write_text(f"[copr:test:desktop]\nname=Third Party\nbaseurl={third_party.as_uri()}\nenabled=1\npriority=10\n")
        with self.assertRaises(UpdateError) as caught:
            self.prepare()
        self.assertIn("third-party repository 'copr:test:desktop'", str(caught.exception))
        self.assertEqual(caught.exception.package_conflicts[0]["nobara_repos"], ["nobara"])

    def test_nobara_can_replace_third_party_repo_build_when_resolution_succeeds(self):
        self.local_desktop_fixture(origin="copr:test:desktop")
        self.assertTrue(any(p["name"] == "labwc" and p["action"] == "Upgrade" for p in self.prepare()["packages"]))

    def test_native_file_conflict_identifies_remaining_local_package(self):
        self.local_desktop_fixture()
        # Without a repository replacement the local RPM remains installed;
        # its file conflict still needs provenance diagnostics.
        (self.repo / "labwc-6-1.noarch.rpm").unlink()
        conflicting = self.build_rpm("nobara-offline-fixture", "2", payload="/usr/share/labwc/version")
        shutil.copy2(conflicting, self.repo)
        subprocess.run(["createrepo_c", str(self.repo)], check=True, capture_output=True)
        with self.assertRaises(UpdateError) as caught:
            self.prepare()
        error = caught.exception
        self.assertEqual(error.package_conflicts[0]["name"], "labwc")
        self.assertIn("Manually installed RPM", str(error))

    def test_major_release_can_replace_a_manually_installed_release_identity(self):
        old_release = self.build_rpm("nobara-release-common", "43")
        subprocess.run(["rpm", "--root", str(self.root), "--justdb", "--nodeps", "--noscripts", "--noplugins",
                        "--ignoresize", "-i", str(old_release)], check=True, capture_output=True)
        self.set_origin("nobara-release-common", "@commandline")
        self.current_release = "43"
        result = self.prepare()
        self.assertEqual(result["target_release"], "44")
        self.assertTrue(any(p["name"] == "nobara-release-common" and p["action"] == "Upgrade" for p in result["packages"]))

    def test_replay_under_private_worker_umask_keeps_dnf_system_state_readable(self):
        self.prepare()
        old_mask = os.umask(0o077)
        try:
            backend.run(self.replay_command(test=False))
            private = Path(self.case.name) / "private-updater-state"
            private.write_text("private")
        finally:
            os.umask(old_mask)
        self.assertEqual(private.stat().st_mode & 0o777, 0o600)
        files = list((self.root / "usr/lib/sysimage/libdnf5").glob("*.toml"))
        self.assertTrue(files)
        for path in files:
            self.assertEqual(path.stat().st_mode & 0o777, 0o644, path.name)

    def test_real_application_only_update_installs_and_validates_live_without_remote_repos(self):
        from nobara_updater.update_state import read_state, write_state
        self.install_current_release()
        result = self.prepare()
        self.assertEqual(result["execution"], {"mode": "live", "reasons": []})
        state_root = Path(self.case.name) / "state"
        write_state(state_root, dict(result, job="a" * 32, started=False), "ready")
        native_run = backend.run

        def run_in_fixture(command):
            if command[0] == "rpm":
                command = ["rpm", "--root", str(self.root), *command[1:]]
            elif "replay" in command:
                command = self.replay_command(test=False)
            else:
                command = ["dnf5", "--installroot=" + str(self.root), "--releasever=44", "--no-plugins",
                           "--setopt=logdir=" + str(self.job), *command[1:]]
            native_run(command)

        with patch.object(backend, "TRIGGER", state_root / "system-update"), \
             patch.object(backend, "job_directory", return_value=self.job), \
             patch.object(backend, "in_installer_root", return_value=False), \
             patch.object(backend, "rpm_fingerprint", return_value="fixture"), \
             patch.object(backend, "os_release", return_value="44"), \
             patch.object(backend, "run", side_effect=run_in_fixture), \
             patch.object(backend.os, "execv") as execute, \
             patch.object(backend, "validate_boot") as boot:
            backend.execute_live(state_root)
            self.assertEqual(execute.call_args.args[1][-1], "live-finalize")
            backend.finalize(state_root, live=True)
        boot.assert_not_called()
        self.assertEqual(read_state(state_root)["status"], "live-complete")
        self.assertEqual((self.root / "usr/share/nobara-offline-fixture/version").read_text().strip(), "2")

    def test_payload_library_forces_offline_even_if_metadata_omits_its_path(self):
        self.install_current_release()
        library_repo = Path(self.case.name) / "library-repo"
        library_repo.mkdir()
        library = self.build_rpm("nobara-offline-fixture", "3", payload="/usr/lib64/libfixture.so.1")
        shutil.copy2(library, library_repo)
        shutil.copy2(self.top / "RPMS/noarch/nobara-release-common-44-1.noarch.rpm", library_repo)
        subprocess.run(["createrepo_c", str(library_repo)], check=True, capture_output=True)
        self.repo = library_repo
        with patch.object(planner.rpm_api.Package, "get_files", return_value=["/usr/share/ordinary-data"]):
            result = self.prepare()
        self.assertEqual(result["execution"]["mode"], "offline")
        self.assertIn("Shared system library: nobara-offline-fixture", result["execution"]["reasons"])


if __name__ == "__main__":
    unittest.main()
