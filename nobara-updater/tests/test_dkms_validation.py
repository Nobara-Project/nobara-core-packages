"""Exercise boot validation without changing host modules or boot files."""
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1] / "src"
if "nobara_updater" not in sys.modules:
    package = types.ModuleType("nobara_updater")
    package.__path__ = [str(SOURCE)]
    sys.modules["nobara_updater"] = package

from nobara_updater import update_backend as backend, update_boot as boot
from nobara_updater.update_state import UpdateError

OWNED = backend.dkms_package_owned
KERNEL = "7.2.4-201.nobara.fc44.x86_64"
ARCH = "x86_64"


class ReachedInitramfs(Exception):
    """Stop after module validation, before any boot files can be written."""


class DkmsValidationTests(unittest.TestCase):
    def setUp(self):
        self.before = "new-lg4ff/0.5.0, 7.2.6-201.nobara.fc44.x86_64, x86_64: installed\n"
        self.after = f"new-lg4ff/0.5.0, {KERNEL}, {ARCH}: installed\n"
        self.status_calls = []
        self.autoinstall_error = None
        self.unowned = set()
        self.native_run = subprocess.run
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        boot_directory = Path(temporary.name)

        def subprocess_run(command, **kwargs):
            if command[:3] == ["rpm", "-q", "--qf"]:
                return Mock(stdout=KERNEL + "\n", returncode=0)
            if command[:2] == ["dkms", "status"]:
                self.status_calls.append((command, kwargs))
                return Mock(stdout=self.after if "-k" in command else self.before, returncode=0)
            if command == ["rpm", "-q", "dkms-nvidia"]:
                return Mock(returncode=1)
            raise AssertionError(command)

        def run(command):
            if command[:2] == ["dkms", "autoinstall"]:
                if self.autoinstall_error:
                    raise self.autoinstall_error
            elif command[0] == "dracut":
                raise ReachedInitramfs()
            else:
                raise AssertionError(command)

        for patcher in (
            patch.object(backend.subprocess, "run", side_effect=subprocess_run),
            patch.object(backend, "run", side_effect=run),
            patch.object(backend, "ensure_grub_kernel"),
            patch.object(backend, "dkms_package_owned", side_effect=lambda name, version: name not in self.unowned),
            patch.object(backend, "Path", side_effect=lambda value: boot_directory if value == "/boot" else Path(value)),
            patch.object(backend.shutil, "which", side_effect=lambda name: "/usr/bin/dkms" if name == "dkms" else None),
            patch.object(backend.os, "uname", return_value=Mock(machine=ARCH)),
            patch.object(Path, "is_file", return_value=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def validate(self):
        backend.validate_boot([dict(name="kernel-core", action="Install", nevra="kernel-core-fixture")])

    def validate_result(self):
        return backend.validate_boot([dict(name="kernel-core", action="Install", nevra="kernel-core-fixture")])

    def test_original_modules_annotation_passes_for_any_driver_name(self):
        for name in ("new-lg4ff", "another-driver"):
            with self.subTest(name=name):
                self.before = f"{name}/0.5.0: added\n"
                self.after = f"{name}/0.5.0, {KERNEL}, {ARCH}: installed (Original modules exist)\n"
                with self.assertRaises(ReachedInitramfs):
                    self.validate()

    def test_plain_installed_status_passes(self):
        with self.assertRaises(ReachedInitramfs):
            self.validate()

    def test_missing_or_mismatched_modules_still_stop_validation_and_name_status(self):
        for suffix in ("built", "installed (Differences between built and installed modules)",
                       "installed (Built modules are missing in the kernel modules folder)",
                       "installed (Original modules exist) (Differences between built and installed modules)",
                       "installed (unknown warning)", "installed-weak from another-kernel"):
            with self.subTest(status=suffix):
                self.after = f"new-lg4ff/0.5.0, {KERNEL}, {ARCH}: {suffix}\n"
                with self.assertRaises(UpdateError) as caught:
                    self.validate()
                self.assertIn("new-lg4ff", str(caught.exception))
                self.assertIn(KERNEL, str(caught.exception))
                self.assertIn(suffix, str(caught.exception))

    def test_other_kernel_or_architecture_does_not_satisfy_validation(self):
        for kernel, arch in (("7.2.6-201.nobara.fc44.x86_64", ARCH), (KERNEL, "aarch64")):
            with self.subTest(kernel=kernel, arch=arch):
                self.after = f"new-lg4ff/0.5.0, {kernel}, {arch}: installed\n"
                with self.assertRaises(UpdateError):
                    self.validate()

    def test_unrelated_driver_cannot_satisfy_missing_driver(self):
        self.after = f"another-driver/0.5.0, {KERNEL}, {ARCH}: installed\n"
        with self.assertRaisesRegex(UpdateError, "new-lg4ff"):
            self.validate()

    def test_missing_status_includes_explanation(self):
        self.after = ""
        with self.assertRaisesRegex(UpdateError, "No DKMS status entries"):
            self.validate()

    def test_broken_source_status_is_in_failure_report(self):
        self.after = "new-lg4ff/0.5.0: broken\n"
        with self.assertRaisesRegex(UpdateError, "new-lg4ff/0.5.0: broken"):
            self.validate()

    def test_warning_containing_a_path_is_not_a_registered_driver(self):
        self.before += "Deprecated feature: CLEAN (/var/lib/dkms/new-lg4ff/0.5.0/source/dkms.conf)\n"
        with self.assertRaises(ReachedInitramfs):
            self.validate()

    def test_failed_autoinstall_still_stops_before_accepting_installed_status(self):
        self.autoinstall_error = UpdateError("module build failed")
        with self.assertRaisesRegex(UpdateError, "module build failed"):
            self.validate()
        self.assertEqual(self.status_calls[-1][0], ["dkms", "status", "-k", KERNEL, "-a", ARCH])

    def test_hand_installed_module_that_fails_to_build_is_skipped(self):
        # As reported: xone and xpad-noone from upstream install.sh no longer
        # build for 7.2; the packaged nvidia module did.
        self.before = ("nvidia/595.104.02, 7.2.10-200.nobara.fc44.x86_64, x86_64: installed\n"
                       "xone/v0.3-58-g28df566, 7.2.10-200.nobara.fc44.x86_64, x86_64: installed\n"
                       "xpad-noone/1.0, 7.2.10-200.nobara.fc44.x86_64, x86_64: installed\n")
        self.after = f"nvidia/595.104.02, {KERNEL}, {ARCH}: installed\n"
        self.unowned = {"xone", "xpad-noone"}
        self.autoinstall_error = UpdateError("dkms failed with exit code 11")
        with self.assertLogs(backend.LOG, "WARNING") as logs, self.assertRaises(ReachedInitramfs):
            self.validate()
        text = "\n".join(logs.output)
        self.assertIn("xone v0.3-58-g28df566", text)
        self.assertIn("xpad-noone 1.0", text)

    def test_skipped_modules_are_returned(self):
        self.after = ""
        self.unowned = {"new-lg4ff"}
        self.autoinstall_error = UpdateError("dkms failed with exit code 11")
        with patch.object(backend, "validate_grub_initramfs"), patch.object(backend.os, "replace"), \
             patch.object(backend, "run", side_effect=lambda command: None if command[0] != "dkms"
                          else (_ for _ in ()).throw(self.autoinstall_error)):
            skipped = self.validate_result()
        self.assertEqual(skipped, [dict(name="new-lg4ff", version="0.5.0", kernel=KERNEL)])

    def test_packaged_module_failure_still_stops_the_update(self):
        self.before += "xone/v0.3-58-g28df566: added\n"
        self.after = ""
        self.unowned = {"xone"}
        self.autoinstall_error = UpdateError("dkms failed with exit code 11")
        with self.assertRaisesRegex(UpdateError, "new-lg4ff"):
            self.validate()

    def test_package_ownership_checks_the_dkms_source(self):
        with tempfile.TemporaryDirectory() as directory:
            tree, sources = Path(directory) / "dkms", Path(directory) / "src"
            (sources / "xone-0.5.8.3").mkdir(parents=True)
            (tree / "xone/0.5.8.3").mkdir(parents=True)
            (tree / "xone/0.5.8.3/source").symlink_to(sources / "xone-0.5.8.3")
            queried = []

            def rpm(command, **kwargs):
                queried.append(command[-1])
                return Mock(returncode=0 if command[-1].endswith("xone-0.5.8.3") else 1)
            with patch.object(backend, "DKMS_TREE", tree), patch.object(backend, "DKMS_SOURCES", sources), \
                 patch.object(backend.subprocess, "run", side_effect=rpm):
                self.assertTrue(OWNED("xone", "0.5.8.3"))
                self.assertFalse(OWNED("xpad-noone", "1.0"))
            self.assertEqual(queried, [str((sources / "xone-0.5.8.3").resolve())])

    def test_status_checks_use_stable_locale_and_target_architecture(self):
        with self.assertRaises(ReachedInitramfs):
            self.validate()
        for command, options in self.status_calls:
            self.assertEqual(options["env"]["LC_ALL"], "C.UTF-8")
        self.assertEqual(self.status_calls[-1][0], ["dkms", "status", "-k", KERNEL, "-a", ARCH])

    def test_planned_legacy_kernel_is_not_rebuilt_before_new_kernel(self):
        old = "6.12.6-200.fsync.fc41.x86_64"
        with patch.object(backend, "installed_boot_kernels", return_value={old, KERNEL}), \
             patch.object(backend.os, "uname", return_value=Mock(machine=ARCH, release=old)):
            with self.assertRaises(ReachedInitramfs):
                backend.validate_boot([dict(name="kernel-core", action="Install", nevra="kernel-core-fixture")],
                                      rebuild_all=True, preserved_kernels=[old])
        backend.run.assert_any_call(["dkms", "autoinstall", "-k", KERNEL])
        self.assertFalse(any(old in call.args[0] for call in backend.run.call_args_list))

    def test_eac_kernel_gets_boot_validation_without_dkms(self):
        eac = "7.2.4-201.eac.nobara.fc44.x86_64"
        with patch.object(backend.os, "uname", return_value=Mock(machine=ARCH, release=eac)), \
             patch.object(backend.subprocess, "run",
                          side_effect=lambda command, **kwargs: Mock(stdout=eac + "\n", returncode=0)
                          if command[:3] == ["rpm", "-q", "--qf"] else self.fail(command)):
            with self.assertRaises(ReachedInitramfs):
                backend.validate_boot([dict(name="kernel-eac-core", action="Install", nevra="kernel-eac-core-fixture")])
        self.assertFalse(any(call.args[0][0] in {"dkms", "akmods", "modinfo"} for call in backend.run.call_args_list))
        backend.ensure_grub_kernel.assert_called_with(eac, backend.run)

    def test_eac_kernel_is_skipped_when_all_kernels_are_rebuilt(self):
        eac = "7.2.4-201.eac.nobara.fc44.x86_64"
        calls = []

        def run(command):
            calls.append(command)
            if command[0] == "dracut" and KERNEL in command:
                raise ReachedInitramfs()

        # The EAC kernel sorts first: let its boot files through, stop at the normal one
        with patch.object(backend, "installed_boot_kernels", return_value={eac, KERNEL}), \
             patch.object(backend, "run", side_effect=run), \
             patch.object(backend, "validate_grub_initramfs"), \
             patch.object(backend.os, "replace"):
            with self.assertRaises(ReachedInitramfs):
                backend.validate_boot([dict(name="kernel-core", action="Install", nevra="kernel-core-fixture")],
                                      rebuild_all=True)
        self.assertIn(["dkms", "autoinstall", "-k", KERNEL], calls)
        self.assertIn(eac, next(c for c in calls if c[0] == "dracut"))
        self.assertFalse(any(eac in c for c in calls if c[0] in {"dkms", "akmods", "modinfo"}))

    def test_eac_kernel_detection(self):
        self.assertTrue(boot.eac_kernel("7.3.0-204.eac.nobara.fc44.x86_64"))
        self.assertTrue(boot.eac_kernel("6.18.55-201.lts.eac.nobara.fc44.x86_64"))
        self.assertFalse(boot.eac_kernel(KERNEL))
        self.assertFalse(boot.eac_kernel("6.18.55-201.lts.nobara.fc44.x86_64"))

    def test_preservation_cannot_bypass_new_kernel_validation(self):
        with self.assertRaisesRegex(UpdateError, "Cannot skip module and boot validation"):
            backend.validate_boot([dict(name="kernel-core", action="Install", nevra="kernel-core-fixture")],
                                  preserved_kernels=[KERNEL])
        backend.run.assert_not_called()

    def test_reboot_into_preserved_kernel_without_new_kernel_is_rejected(self):
        with patch.object(backend.os, "uname", return_value=Mock(machine=ARCH, release=KERNEL)):
            with self.assertRaisesRegex(UpdateError, "Cannot skip module and boot validation"):
                backend.validate_boot([dict(name="dkms", action="Upgrade")], preserved_kernels=[KERNEL])
        backend.run.assert_not_called()

    def test_unplanned_missing_headers_still_fail(self):
        with patch.object(Path, "is_file", return_value=False):
            with self.assertRaisesRegex(UpdateError, "Kernel development files are missing"):
                self.validate()
        backend.run.assert_not_called()

    @unittest.skipUnless(shutil.which("dkms"), "needs the native DKMS status command")
    def test_native_status_with_renamed_module_and_original_archive(self):
        # Model DKMS's files under /tmp; status only reads them. No driver is
        # registered, compiled, loaded, or installed on the host. The payloads
        # are comparison fixtures, not loadable kernel objects.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            name = "nobara-updater-test-driver"
            source = root / "src" / (name + "-1.0")
            source.mkdir(parents=True)
            (source / "dkms.conf").write_text(
                f'PACKAGE_NAME="{name}"\nPACKAGE_VERSION="1.0"\n'
                'BUILT_MODULE_NAME[0]="compiled-name"\nDEST_MODULE_NAME[0]="installed-name"\n'
                'DEST_MODULE_LOCATION[0]="/kernel/drivers/hid"\nAUTOINSTALL="yes"\n')
            tree = root / "dkms" / name
            built = tree / "1.0" / KERNEL / ARCH / "module" / "installed-name.ko"
            installed = root / "lib" / KERNEL / "kernel/drivers/hid/installed-name.ko"
            for path in (built, installed):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"DKMS status comparison fixture\n")
            (tree / "1.0/source").symlink_to(source, target_is_directory=True)
            (tree / f"kernel-{KERNEL}-{ARCH}").symlink_to(Path("1.0") / KERNEL / ARCH, target_is_directory=True)
            (tree / "original_module" / KERNEL / ARCH).mkdir(parents=True)

            def status():
                return self.native_run([
                    "dkms", "status", "--dkmstree", str(root / "dkms"),
                    "--sourcetree", str(root / "src"), "--installtree", str(root / "lib"),
                    "-k", KERNEL, "-a", ARCH],
                    capture_output=True, text=True, check=True, env=dict(os.environ, LC_ALL="C.UTF-8")).stdout

            self.before = self.after = status()
            self.assertIn(": installed (Original modules exist)", self.after)
            with self.assertRaises(ReachedInitramfs):
                self.validate()
            installed.write_bytes(b"different installed module contents\n")
            self.after = status()
            with self.assertRaisesRegex(UpdateError, "Differences between built and installed modules"):
                self.validate()
            built.unlink()
            self.after = status()
            with self.assertRaisesRegex(UpdateError, "Built modules are missing"):
                self.validate()


class BootKernelInventoryTests(unittest.TestCase):
    def test_inventory_excludes_module_only_remnants_and_devel_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "boot").mkdir()
            for kernel in ("split", "module-image", "orphan", "build-only"):
                (root / "usr/lib/modules" / kernel).mkdir(parents=True)
            (root / "boot/vmlinuz-split").touch()
            (root / "usr/lib/modules/module-image/vmlinuz").touch()
            (root / "usr/lib/modules/build-only/build").mkdir()
            self.assertEqual(boot.installed_boot_kernels(root), {"split", "module-image"})

    def test_empty_target_has_no_boot_kernels(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(boot.installed_boot_kernels(Path(directory)), set())


if __name__ == "__main__":
    unittest.main()
