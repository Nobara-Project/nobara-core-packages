"""Real RPM/libdnf5 preparation against disposable local repositories.

No host packages, services, boot files, or remote repositories are used.
The fixtures are deliberately unsigned; production signature policy is
covered separately and is never disabled by a user-facing option.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import types
import unittest
from pathlib import Path
from unittest.mock import patch

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
from nobara_updater.update_migrations import MigrationPlan
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
    def build_rpm(cls, name, version, script="", *, headers="", payload=None):
        payload = payload or f"/usr/share/{name}/version"
        spec = cls.directory / f"{name}-{version}.spec"
        spec.write_text(f'''Name: {name}
Version: {version}
Release: 1
Summary: Disposable offline updater test
License: MIT
BuildArch: noarch
{headers}
%description
Disposable offline updater test.
%install
mkdir -p %{{buildroot}}{Path(payload).parent}
echo {version} > %{{buildroot}}{payload}
%files
{payload}
{script}
''')
        result = subprocess.run(["rpmbuild", "--define", f"_topdir {cls.top}", "--define", f"_tmppath {cls.directory}", "-bb", str(spec)], capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
        return cls.top / "RPMS/noarch" / f"{name}-{version}-1.noarch.rpm"

    def set_origin(self, name, repo):
        # Model a genuine recorded repository install, instead of conflating
        # the fixture's rpm --justdb setup with a manual user installation.
        nevra = subprocess.run(["rpm", "--root", str(self.root), "-q", name, "--qf", "%{NAME}-%{VERSION}-%{RELEASE}.%{ARCH}"],
                               check=True, capture_output=True, text=True).stdout
        path = self.root / "usr/lib/sysimage/libdnf5/nevras.toml"
        values = tomllib.loads(path.read_text()).get("nevras", {}) if path.exists() else {}
        values[nevra] = {"from_repo": repo}
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

    def prepare(self):
        with patch.object(planner, "configure_base", side_effect=self.base), \
             patch.object(planner, "os_release", return_value=getattr(self, "current_release", "44")), \
             patch.object(planner, "plan_migrations", return_value=getattr(self, "migrations", MigrationPlan())), \
             patch.object(planner, "rpm_fingerprint", return_value="fixture"), \
             patch.object(planner, "require_space"):
            return planner.prepare_transaction(self.job)

    def test_real_upgrade_is_saved_with_local_payload_and_no_installed_changes(self):
        result = self.prepare()
        self.assertFalse(result["empty"])
        self.assertTrue(any(p["action"] == "Upgrade" for p in result["packages"]))
        verify_files(self.job, result["files"])
        installed = subprocess.run(["rpm", "--root", str(self.root), "-q", "nobara-offline-fixture", "--qf", "%{VERSION}"], check=True, capture_output=True, text=True)
        self.assertEqual(installed.stdout, "1")

    def updater_fixture(self, repository_version):
        installed = self.build_rpm("nobara-updater", "2")
        available = self.build_rpm("nobara-updater", repository_version)
        subprocess.run(["rpm", "--root", str(self.root), "--justdb", "--nodeps", "--noscripts", "--noplugins",
                        "--ignoresize", "-i", str(installed)], check=True, capture_output=True)
        self.set_origin("nobara-updater", "nobara")
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

    def test_local_rpm_is_held_while_unrelated_repository_package_updates(self):
        self.local_desktop_fixture()
        result = self.prepare()
        self.assertFalse(any(p["name"] == "labwc" for p in result["packages"]))
        self.assertTrue(any(p["action"] == "Upgrade" for p in result["packages"]))
        self.assertEqual(result["package_origins"][0]["repo"], "@commandline")

    def test_local_rpm_is_not_downgraded_by_repository(self):
        self.local_desktop_fixture(available_version="4")
        self.assertFalse(any(p["name"] == "labwc" for p in self.prepare()["packages"]))

    def test_rpmfusion_repo_rpms_can_update_while_local_desktop_stays_protected(self):
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
        self.assertTrue(set(names) <= upgraded)
        self.assertFalse(any(p["name"] == "labwc" for p in result["packages"]))
        self.assertFalse(any(p["name"] in names for p in result["package_origins"]))

    def test_unknown_origin_direct_rpm_install_is_also_preserved(self):
        self.local_desktop_fixture(origin="<unknown>")
        result = self.prepare()
        self.assertFalse(any(p["name"] == "labwc" for p in result["packages"]))
        self.assertEqual(next(p for p in result["package_origins"] if p["name"] == "labwc")["kind"], "unknown")

    def test_obsoletes_cannot_replace_local_package_under_a_different_name(self):
        self.local_desktop_fixture()
        replacement = self.build_rpm("replacement-desktop", "1", headers="Obsoletes: labwc < 99\nProvides: labwc = 6")
        shutil.copy2(replacement, self.repo)
        subprocess.run(["createrepo_c", str(self.repo)], check=True, capture_output=True)
        result = self.prepare()
        self.assertFalse(any(p["name"] in {"labwc", "replacement-desktop"} for p in result["packages"]))

    def test_migration_cannot_remove_local_package_even_when_explicitly_allowed(self):
        self.local_desktop_fixture()
        self.migrations = MigrationPlan()
        self.migrations.remove.add("labwc")
        with self.assertRaises(UpdateError) as caught:
            self.prepare()
        self.assertEqual(caught.exception.package_conflicts[0]["name"], "labwc")
        self.assertIn("Manually installed RPM", str(caught.exception))
        self.assertFalse((self.job / "transaction.json").exists())

    def test_local_dependency_conflict_names_held_package_and_nobara_counterpart(self):
        self.local_desktop_fixture()
        requiring = self.build_rpm("nobara-offline-fixture", "2", headers="Requires: labwc >= 6")
        shutil.copy2(requiring, self.repo)
        subprocess.run(["createrepo_c", str(self.repo)], check=True, capture_output=True)
        with self.assertRaises(UpdateError) as caught:
            self.prepare()
        self.assertIn("Manually installed RPM", str(caught.exception))
        self.assertEqual(caught.exception.package_conflicts[0]["name"], "labwc")
        self.assertEqual(caught.exception.package_conflicts[0]["nobara_repos"], ["nobara"])
        self.assertFalse((self.job / "transaction.json").exists())

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

    def test_native_file_conflict_identifies_preserved_local_package(self):
        self.local_desktop_fixture()
        conflicting = self.build_rpm("nobara-offline-fixture", "2", payload="/usr/share/labwc/version")
        shutil.copy2(conflicting, self.repo)
        subprocess.run(["createrepo_c", str(self.repo)], check=True, capture_output=True)
        with self.assertRaises(UpdateError) as caught:
            self.prepare()
        error = caught.exception
        self.assertEqual(error.package_conflicts[0]["name"], "labwc")
        self.assertIn("Manually installed RPM", str(error))

    def test_major_release_reports_a_manually_installed_release_identity(self):
        old_release = self.build_rpm("nobara-release-common", "43")
        subprocess.run(["rpm", "--root", str(self.root), "--justdb", "--nodeps", "--noscripts", "--noplugins",
                        "--ignoresize", "-i", str(old_release)], check=True, capture_output=True)
        self.set_origin("nobara-release-common", "@commandline")
        self.current_release = "43"
        with self.assertRaises(UpdateError) as caught:
            self.prepare()
        self.assertEqual(caught.exception.package_conflicts[0]["name"], "nobara-release-common")
        self.assertIn("Manually installed RPM", str(caught.exception))

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
