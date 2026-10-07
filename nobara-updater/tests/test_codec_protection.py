"""Codec protection must cover dependencies without freezing ordinary upgrades."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import types
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "src"
if "nobara_updater" not in sys.modules:
    package = types.ModuleType("nobara_updater")
    package.__path__ = [str(SOURCE)]
    sys.modules["nobara_updater"] = package

from nobara_updater.update_codecs import validate_codec_changes, installed_codec_dependencies
from nobara_updater.update_state import UpdateError


def record(name, action, arch="x86_64"):
    return dict(name=name, action=action, arch=arch)


class CodecProtectionTests(unittest.TestCase):
    def test_same_provider_upgrade_is_allowed(self):
        validate_codec_changes([record("ffmpeg-free", "O"), record("ffmpeg-free", "U")], {("ffmpeg-free", "x86_64")})

    def test_removal_swap_downgrade_and_reinstall_are_blocked(self):
        for action in ("E", "O", "D", "R"):
            with self.subTest(action=action), self.assertRaisesRegex(UpdateError, "Codec Wizard"):
                validate_codec_changes([record("libmedia", action)], {("libmedia", "x86_64")})

    def test_remaining_other_arch_does_not_allow_multilib_removal(self):
        with self.assertRaises(UpdateError):
            validate_codec_changes([record("libmedia", "E", "i686"), record("libmedia", "U")],
                                   {("libmedia", "x86_64"), ("libmedia", "i686")})

    def test_manual_codec_install_is_directed_to_wizard(self):
        with self.assertRaisesRegex(UpdateError, "ffmpeg-free"):
            validate_codec_changes([record("ffmpeg-free", "I")], set())
        validate_codec_changes([record("text-editor", "I")], set())

    def test_vulkan_driver_variants_allow_install_and_swap_for_both_architectures(self):
        names = ("mesa-vulkan-drivers", "mesa-vulkan-drivers-freeworld",
                 "mesa-vulkan-drivers-git", "mesa-vulkan-drivers-git-freeworld")
        for source in names:
            for target in names:
                if source == target:
                    continue
                with self.subTest(source=source, target=target):
                    packages = [p for arch in ("x86_64", "i686") for p in
                                (record(source, "E", arch), record(target, "I", arch))]
                    validate_codec_changes(packages, {("libavcodec-free", "x86_64"), ("libavcodec-free", "i686")})

    def test_vulkan_swap_does_not_authorize_codec_or_shared_dependency_changes(self):
        drivers = [record("mesa-vulkan-drivers", "E"), record("mesa-vulkan-drivers-freeworld", "I")]
        for name in ("mesa-va-drivers", "mesa-va-drivers-freeworld", "mesa-libgallium",
                     "mesa-libgallium-freeworld", "ffmpeg-free", "shared-media-library"):
            for arch in ("x86_64", "i686"):
                with self.subTest(name=name, arch=arch), self.assertRaisesRegex(UpdateError, name):
                    validate_codec_changes(drivers + [record(name, "E", arch)], {(name, arch)})
        for name in ("mesa-va-drivers-freeworld", "mesa-libgallium-freeworld", "ffmpeg-free"):
            with self.subTest(install=name), self.assertRaisesRegex(UpdateError, name):
                validate_codec_changes(drivers + [record(name, "I")], set())


PLUGIN_DIR = Path(os.environ.get("NOBARA_ACTIONS_PLUGIN_DIR", "/usr/lib64/libdnf5/plugins"))
import test_offline_integration as fixtures


@unittest.skipUnless(os.geteuid() == 0 and (PLUGIN_DIR / "actions.so").is_file(),
                     "Requires a root user namespace and the DNF5 actions plugin")
class CodecGuardIntegrationTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.OfflineIntegrationTests.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.OfflineIntegrationTests.tearDownClass.__func__)
    build_rpm = classmethod(fixtures.OfflineIntegrationTests.build_rpm.__func__)
    set_origin = fixtures.OfflineIntegrationTests.set_origin

    def setUp(self):
        fixtures.OfflineIntegrationTests.setUp(self)
        self.repo = Path(self.case.name) / "codecs-repo"
        self.repo.mkdir()
        specs = (("codec-left", "", None), ("codec-right", "", None),
                 ("codec-conditional", "", None),
                 ("codec-leaf", "", "/usr/lib64/libcodec-leaf.so.1"),
                 ("codec-support", "Requires: /usr/lib64/libcodec-leaf.so.1\nRequires: (codec-left and codec-right)\nRequires: (codec-conditional if codec-left)", None),
                 ("ffmpeg", "Requires: codec-support >= 1", None))
        for name, headers, payload in specs:
            package = self.build_rpm(name, "1", headers=headers, payload=payload)
            subprocess.run(["rpm", "--root", str(self.root), "--justdb", "--nodeps", "--noscripts", "--noplugins",
                            "--ignoresize", "-i", str(package)], check=True, capture_output=True)
        for name, headers in (("ffmpeg", "Requires: codec-support >= 1"),
                              ("ffmpeg-free", "Obsoletes: ffmpeg\nProvides: ffmpeg\nRequires: new-codec-support"),
                              ("new-codec-support", "")):
            shutil.copy2(self.build_rpm(name, "2", headers=headers), self.repo)
        subprocess.run(["createrepo_c", str(self.repo)], check=True, capture_output=True)
        self.config = Path(self.case.name) / "plugins"
        (self.config / "actions.d").mkdir(parents=True)
        (self.config / "actions.conf").write_text("[main]\nenabled=1\n")
        helper = Path(self.case.name) / "guard.py"
        helper.write_text("import sys, types\np=types.ModuleType('nobara_updater')\np.__path__=[" + repr(str(SOURCE)) + "]\nsys.modules['nobara_updater']=p\nfrom nobara_updater.update_codecs import guard_main\nraise SystemExit(guard_main())\n")
        action = (SOURCE.parent / "data/nobara-codecs.actions").read_text().replace(
            "/usr/libexec/nobara-codec-guard", sys.executable + " -B " + str(helper))
        (self.config / "actions.d/nobara-codecs.actions").write_text(action)
        self.repos = Path(self.case.name) / "repos"
        self.repos.mkdir()
        (self.repos / "codec.repo").write_text(f"[fixture]\nname=Fixture\nbaseurl={self.repo.as_uri()}\nenabled=1\ngpgcheck=0\n")

    def command(self, *args, plugins=True):
        return ["dnf5", "-y", "--installroot=" + str(self.root), "--releasever=44", "--config=/dev/null",
                "--setopt=use_host_config=1", "--setopt=reposdir=" + str(self.repos),
                "--setopt=pluginpath=" + str(PLUGIN_DIR), "--setopt=pluginconfpath=" + str(self.config),
                "--setopt=logdir=" + str(self.job), "--setopt=pkg_gpgcheck=0", "--setopt=localpkg_gpgcheck=0",
                *([] if plugins else ["--no-plugins"]), *args]

    def run_dnf(self, *args, plugins=True):
        return subprocess.run(self.command(*args, plugins=plugins), capture_output=True, text=True)

    def install_vulkan_fixture(self):
        cls = type(self)
        if not hasattr(cls, "vulkan_rpms"):
            names = ("mesa-vulkan-drivers", "mesa-vulkan-drivers-freeworld",
                     "mesa-vulkan-drivers-git", "mesa-vulkan-drivers-git-freeworld")
            cls.vulkan_rpms = {}
            for name in names:
                headers = "Provides: fixture-vulkan-driver\nRequires: driver-private\nRequires: shared-media-support\n"
                headers += "\n".join("Conflicts: " + other for other in names if other != name)
                for arch in ("x86_64", "i686"):
                    cls.vulkan_rpms[name, arch] = cls.build_rpm(name, "1", headers=headers, arch=arch)
            for name, headers in (("driver-private", ""), ("shared-media-support", ""),
                                  ("libheif", "Requires: fixture-vulkan-driver\nRequires: shared-media-support")):
                cls.vulkan_rpms[name, "noarch"] = cls.build_rpm(name, "1", headers=headers)
        for (name, arch), package in cls.vulkan_rpms.items():
            shutil.copy2(package, self.repo)
            if name == "mesa-vulkan-drivers" or arch == "noarch":
                subprocess.run(["rpm", "--root", str(self.root), "--justdb", "--nodeps", "--noscripts", "--noplugins",
                                "--ignoresize", "-i", str(package)], check=True, capture_output=True)
        subprocess.run(["createrepo_c", str(self.repo)], check=True, capture_output=True)

    def test_vulkan_dependency_boundary_preserves_actual_codec_dependencies(self):
        self.install_vulkan_fixture()
        base = fixtures.configure_fixture_base(self.root, self.repos, self.job)
        self.addCleanup(base.unlock_system_repo)
        protected = installed_codec_dependencies(base)
        for arch in ("x86_64", "i686"):
            self.assertNotIn(("mesa-vulkan-drivers", arch), protected)
        self.assertNotIn(("driver-private", "noarch"), protected)
        self.assertIn(("shared-media-support", "noarch"), protected)
        self.assertIn(("libheif", "noarch"), protected)
        base.unlock_system_repo()
        result = self.run_dnf("remove", "shared-media-support")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Nobara manages", result.stdout + result.stderr)

    def test_helium_bundled_libraries_do_not_prevent_browser_removal(self):
        graphics = "Provides: libEGL.so()(64bit)\nProvides: libvulkan.so.1()(64bit)"
        specs = (
            ("helium-bin", graphics + "\nRequires: helium-private\nRequires: shared-media-support", "x86_64"),
            ("helium-private", "", "noarch"),
            ("system-graphics", graphics + "\nRequires: shared-media-support", "x86_64"),
            ("shared-media-support", "", "noarch"),
            ("libheif", "Requires: libEGL.so()(64bit)\nRequires: libvulkan.so.1()(64bit)", "x86_64"),
        )
        for name, headers, arch in specs:
            package = self.build_rpm(name, "1", headers=headers, arch=arch)
            subprocess.run(["rpm", "--root", str(self.root), "--justdb", "--nodeps", "--noscripts", "--noplugins",
                            "--ignoresize", "-i", str(package)], check=True, capture_output=True)
        base = fixtures.configure_fixture_base(self.root, self.repos, self.job)
        self.addCleanup(base.unlock_system_repo)
        protected = installed_codec_dependencies(base)
        self.assertNotIn(("helium-bin", "x86_64"), protected)
        self.assertNotIn(("helium-private", "noarch"), protected)
        for identity in (("libheif", "x86_64"), ("system-graphics", "x86_64"), ("shared-media-support", "noarch")):
            self.assertIn(identity, protected)
        base.unlock_system_repo()

        # Exercise the actual DNF5 pre-transaction guard, not just its inventory.
        result = self.run_dnf("remove", "helium-bin")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = subprocess.run(["rpm", "--root", str(self.root), "-q", "helium-bin"], capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        result = self.run_dnf("remove", "shared-media-support")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Nobara manages", result.stdout + result.stderr)

    def test_dnf_swaps_all_vulkan_variants_with_multilib_and_protection_enabled(self):
        self.install_vulkan_fixture()
        for target in ("mesa-vulkan-drivers-freeworld", "mesa-vulkan-drivers-git-freeworld",
                       "mesa-vulkan-drivers-git", "mesa-vulkan-drivers"):
            with self.subTest(target=target):
                result = self.run_dnf("install", target + ".x86_64", target + ".i686", "--allowerasing")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                installed = subprocess.check_output(["rpm", "--root", str(self.root), "-q", "--whatprovides",
                                                     "fixture-vulkan-driver", "--qf", "%{NAME}.%{ARCH}\n"], text=True)
                self.assertEqual(set(installed.splitlines()), {target + ".x86_64", target + ".i686"})
        # A Vulkan replacement must not act as a bypass for another package.
        result = self.run_dnf("swap", "ffmpeg", "mesa-vulkan-drivers-freeworld", "--allowerasing")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Nobara manages", result.stdout + result.stderr)
        self.assertIn("ffmpeg", result.stdout + result.stderr)

    def test_driver_manager_can_install_after_its_separate_rpm_removal(self):
        self.install_vulkan_fixture()
        for source, target in (("mesa-vulkan-drivers", "mesa-vulkan-drivers-freeworld"),
                               ("mesa-vulkan-drivers-freeworld", "mesa-vulkan-drivers-git-freeworld"),
                               ("mesa-vulkan-drivers-git-freeworld", "mesa-vulkan-drivers-git"),
                               ("mesa-vulkan-drivers-git", "mesa-vulkan-drivers")):
            with self.subTest(source=source, target=target):
                # Match cfhdb's existing remove-first profile, only inside the
                # disposable RPMDB. The subsequent DNF call keeps plugins on.
                subprocess.run(["rpm", "--root", str(self.root), "-e", "--nodeps",
                                source + ".x86_64", source + ".i686"], check=True, capture_output=True)
                result = self.run_dnf("install", target + ".x86_64", target + ".i686")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_dependency_inventory_covers_version_file_and_rich_requirements(self):
        base = fixtures.configure_fixture_base(self.root, self.repos, self.job)
        self.addCleanup(base.unlock_system_repo)
        protected = installed_codec_dependencies(base)
        for name in ("ffmpeg", "codec-support", "codec-leaf", "codec-left", "codec-right", "codec-conditional"):
            self.assertIn((name, "noarch"), protected)

    def test_dnf_blocks_direct_removal_and_transitive_dependency_removal(self):
        for name in ("ffmpeg", "codec-support", "codec-leaf", "codec-left", "codec-right", "codec-conditional"):
            with self.subTest(name=name):
                result = self.run_dnf("remove", name)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("Nobara manages", result.stdout + result.stderr)
        installed = subprocess.check_output(["rpm", "--root", str(self.root), "-q", "ffmpeg", "codec-support", "codec-leaf"], text=True)
        self.assertIn("codec-leaf-1", installed)

    def test_dnf_allows_version_upgrade_and_blocks_provider_swap(self):
        result = self.run_dnf("upgrade", "ffmpeg", "--setopt=obsoletes=0")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = self.run_dnf("swap", "ffmpeg", "ffmpeg-free")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Nobara manages", result.stdout + result.stderr)

    def test_managed_replacement_is_followed_by_protection_of_new_dependencies(self):
        # The updater's tested replay also runs with --no-plugins.
        result = self.run_dnf("swap", "ffmpeg", "ffmpeg-free", plugins=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = self.run_dnf("remove", "new-codec-support")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Nobara manages", result.stdout + result.stderr)

    def test_python_libdnf_transactions_used_by_app_center_are_also_guarded(self):
        import libdnf5.base as native
        import libdnf5.exception as errors
        base = native.Base()
        config = base.get_config()
        for name, value in {"installroot": str(self.root), "config_file_path": "/dev/null",
                            "pluginpath": str(PLUGIN_DIR), "pluginconfpath": str(self.config),
                            "reposdir": [str(self.repos)], "use_host_config": True,
                            "pkg_gpgcheck": False, "logdir": str(self.job)}.items():
            getattr(config, f"get_{name}_option")().set(value)
        base.get_vars().set("releasever", "44")
        base.setup()
        base.get_repo_sack().create_repos_from_system_configuration()
        base.get_repo_sack().load_repos()
        goal = native.Goal(base)
        goal.add_remove("ffmpeg")
        transaction = goal.resolve()
        with self.assertRaisesRegex(errors.Error, "Nobara manages"):
            transaction.run()
