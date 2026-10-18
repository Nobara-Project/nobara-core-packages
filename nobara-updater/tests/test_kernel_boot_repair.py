"""Missing-image repair must produce a usable GRUB entry, not demand a UKI."""
import json
from pathlib import Path
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
from nobara_updater import update_backend as backend, update_boot as boot, update_recovery as recovery
from nobara_updater.update_state import UpdateError

KERNEL = "7.2.11-200.nobara.fc44.x86_64"
OLD = "7.2.9-200.nobara.fc44.x86_64"
MACHINE = "a" * 32


class BootRepairTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.boot = self.root / "boot"
        self.modules = self.root / "modules"
        self.entries = self.boot / "loader/entries"
        self.entries.mkdir(parents=True)
        self.image = self.boot / ("vmlinuz-" + KERNEL)
        self.initrd = self.boot / ("initramfs-" + KERNEL + ".img")
        self.entry = self.entries / (MACHINE + "-" + KERNEL + ".conf")
        self.source = self.modules / KERNEL / "vmlinuz"
        self.source.parent.mkdir(parents=True)
        self.source.write_text("packaged kernel")
        self.machine = self.root / "machine-id"
        self.machine.write_text(MACHINE)
        self.defaults = self.root / "grub-defaults"
        self.defaults.write_text("GRUB_ENABLE_BLSCFG=true\n")
        (self.boot / "grub2").mkdir()
        self.config = self.boot / "grub2/grub.cfg"
        self.config.write_text("blscfg\n")
        self.old = self.entries / (MACHINE + "-" + OLD + ".conf")
        self.old.write_text(self.bls(OLD))
        self.hook = self.root / "20-grub.install"
        self.hook.write_text("#!/bin/sh\n")
        self.hook.chmod(0o755)
        self.override = self.root / "override"
        self.layout = "other"
        self.image_type = "pe"
        self.commands = []
        self.installer_repairs = True
        self.hook_repairs = True
        self.fail = None

        def command(args, **kwargs):
            if args[:3] == ["rpm", "-q", "--qf"]:
                return Mock(stdout=KERNEL + "\n", returncode=0)
            if args == ["rpm", "-q", "dkms-nvidia"]:
                return Mock(returncode=1)
            if args[:3] == ["kernel-install", "--json=short", "inspect"]:
                return Mock(stdout=json.dumps(dict(Layout=self.layout, BootRoot="/boot/efi", KernelImageType=self.image_type,
                                                   Plugins=[str(self.hook)], PluginEnvironment=["SECRET=do-not-log"])))
            self.fail_command(args)

        for patcher in (
            patch.object(boot, "BOOT", self.boot), patch.object(boot, "MODULES", self.modules),
            patch.object(boot, "MACHINE_ID", self.machine), patch.object(boot, "GRUB_DEFAULTS", self.defaults),
            patch.object(boot, "GRUB_INSTALL", self.hook), patch.object(boot, "GRUB_INSTALL_OVERRIDE", self.override),
            patch.object(boot, "output", side_effect=lambda args: "/" + Path(args[-1]).name),
            patch.object(boot.shutil, "which", side_effect=lambda name: None if name in {"dkms", "akmods"} else "/usr/bin/" + name),
            patch.object(boot.os, "uname", return_value=Mock(release=OLD, machine="x86_64")),
            patch.object(boot.os, "sync"), patch.object(boot.subprocess, "run", side_effect=command),
            patch.object(recovery, "grub_variables", return_value={}),
            patch.object(backend, "run", side_effect=self.run_command),
            patch.object(backend, "Path", side_effect=lambda value: self.boot if value == "/boot" else
                         self.modules if value == "/usr/lib/modules" else Path(value)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def fail_command(self, args):
        raise AssertionError("Unexpected command: " + repr(args))

    def bls(self, kernel=KERNEL):
        return (f"title Nobara\nversion {kernel}\nlinux /vmlinuz-{kernel}\n"
                f"initrd /initramfs-{kernel}.img\noptions root=UUID=root rootflags=subvol=@\n")

    def installed(self):
        self.image.write_text("packaged kernel")
        self.entry.write_text(self.bls())

    def run_command(self, command):
        self.commands.append(command)
        if command[0] == self.fail:
            if command[0] == "dracut":
                Path(command[-1]).write_text("partial initramfs")
            raise UpdateError("simulated " + self.fail + " failure")
        if command[0] == "kernel-install":
            self.assertEqual(command[1], "--boot-path=" + str(self.boot))
            if self.installer_repairs:
                self.installed()
        elif command[0] == "env":
            self.assertIn("KERNEL_INSTALL_LAYOUT=other", command)
            self.assertIn(str(self.hook), command)
            self.assertFalse(Path(command[-2]).exists())
            if self.hook_repairs:
                self.installed()
        elif command[0] == "dracut":
            self.assertIn("--no-uefi", command)
            Path(command[-1]).write_text("new initramfs")
        elif command[0] == "lsinitrd":
            self.assertTrue(Path(command[-1]).is_file())
        else:
            self.fail_command(command)

    def validate(self):
        backend.validate_boot([dict(name="kernel-core", action="Install", nevra="fixture")])

    def test_missing_flat_image_rechecks_normal_install_result_without_requiring_uki(self):
        self.validate()
        self.assertEqual(self.initrd.read_text(), "new initramfs")
        self.assertEqual(boot.kernel_entry(KERNEL), self.entry.stem)
        self.assertEqual([c[0] for c in self.commands], ["kernel-install", "dracut", "lsinitrd"])

    def test_existing_image_with_missing_or_wrong_entry_is_repaired(self):
        self.image.write_text("kernel")
        for text in ("", self.bls(OLD), self.bls().replace(f"initrd /initramfs-{KERNEL}.img\n", "")):
            with self.subTest(text=text):
                self.entry.write_text(text)
                self.commands.clear()
                self.validate()
                self.assertEqual(self.commands[0][0], "kernel-install")

    def test_existing_usable_boot_files_only_rebuild_initramfs(self):
        self.installed()
        self.validate()
        self.assertEqual([c[0] for c in self.commands], ["dracut", "lsinitrd"])

    def test_generic_layout_repairs_grub_without_another_unused_boot_tree(self):
        for layout in ("bls", "uki"):
            with self.subTest(layout=layout):
                self.image.unlink(missing_ok=True)
                self.entry.unlink(missing_ok=True)
                self.commands.clear()
                self.layout = layout
                unused = self.boot / MACHINE / KERNEL / "linux"
                unused.parent.mkdir(parents=True, exist_ok=True)
                unused.write_text("existing generic kernel")
                self.validate()
                self.assertEqual(self.commands[0][0], "env")
                self.assertEqual(unused.read_text(), "existing generic kernel")
                self.assertEqual(self.initrd.read_text(), "new initramfs")

    def test_successful_noop_installer_still_needs_a_real_grub_repair(self):
        self.installer_repairs = False
        self.validate()
        self.assertEqual([c[0] for c in self.commands[:2]], ["kernel-install", "env"])

    def test_tuned_variables_survive_recreating_generic_entry(self):
        self.layout = "bls"
        self.entry.write_text(self.bls().replace(f"/vmlinuz-{KERNEL}", f"/{MACHINE}/{KERNEL}/linux")
                              .replace(".img\n", ".img $tuned_initrd\n").replace("subvol=@", "subvol=@ $tuned_params"))
        self.validate()
        self.assertIn("$tuned_initrd", self.entry.read_text())
        self.assertIn("$tuned_params", self.entry.read_text())

    def test_tuned_variables_use_running_entry_when_new_entry_went_to_esp(self):
        self.layout = "bls"
        self.old.write_text(self.old.read_text().replace(".img\n", ".img $tuned_initrd\n"))
        self.validate()
        self.assertIn("$tuned_initrd", self.entry.read_text())

    def test_masked_overridden_or_missing_distribution_hook_is_not_bypassed(self):
        self.layout = "bls"
        for mode in ("masked", "custom", "missing"):
            with self.subTest(mode=mode):
                self.override.unlink(missing_ok=True)
                self.commands.clear()
                if mode == "masked":
                    self.override.symlink_to("/dev/null")
                elif mode == "custom":
                    self.override.write_text("custom hook")
                else:
                    self.hook.unlink()
                with self.assertRaisesRegex(UpdateError, "missing, disabled, or overridden"):
                    self.validate()
                self.assertEqual(self.commands, [])

    def test_missing_or_empty_packaged_image_fails_before_installation(self):
        for missing in (False, True):
            if missing:
                self.source.unlink()
            else:
                self.source.write_text("")
            with self.assertRaisesRegex(UpdateError, "packaged kernel image"):
                self.validate()
        self.assertEqual(self.commands, [])

    def test_unsupported_bootloader_is_not_converted_to_grub(self):
        self.config.write_text("custom bootloader")
        with self.assertRaisesRegex(UpdateError, "GRUB/BLS"):
            self.validate()
        self.assertEqual(self.commands, [])

    def test_actual_uki_payload_is_not_copied_as_split_kernel(self):
        self.image_type = "uki"
        with self.assertRaisesRegex(UpdateError, "packaged image.*is a UKI"):
            self.validate()
        self.assertEqual(self.commands, [])

    def test_symlinked_entry_is_not_overwritten_during_repair(self):
        other = self.root / "unrelated.conf"
        other.write_text("unrelated boot settings")
        self.entry.symlink_to(other)
        with self.assertRaisesRegex(UpdateError, "symlinked kernel boot files"):
            self.validate()
        self.assertEqual(other.read_text(), "unrelated boot settings")
        self.assertEqual(self.commands, [])

    def test_plain_entry_does_not_require_grub_environment_to_validate_initrd(self):
        self.installed()
        self.initrd.write_text("rebuilt initramfs")
        with patch.object(recovery, "grub_variables", side_effect=UpdateError("No grubenv yet")) as variables:
            boot.validate_grub_initramfs(KERNEL, self.run_command)
        variables.assert_not_called()

    def test_installer_and_grub_hook_errors_are_not_ignored(self):
        for command, layout in (("kernel-install", "other"), ("env", "bls")):
            with self.subTest(command=command):
                self.layout = layout
                self.fail = command
                with self.assertRaisesRegex(UpdateError, "simulated"):
                    self.validate()
                self.assertFalse(self.initrd.exists())

    def test_successful_noop_grub_hook_still_fails_validation(self):
        self.layout = "bls"
        self.hook_repairs = False
        with self.assertRaisesRegex(UpdateError, "Could not restore GRUB boot files"):
            self.validate()
        self.assertFalse(self.initrd.exists())

    def test_failed_dracut_or_initrd_validation_preserves_previous_image(self):
        for command in ("dracut", "lsinitrd"):
            with self.subTest(command=command):
                self.installed()
                self.initrd.write_text("old usable initramfs")
                self.fail = command
                with self.assertRaisesRegex(UpdateError, "simulated"):
                    self.validate()
                self.assertEqual(self.initrd.read_text(), "old usable initramfs")
                self.assertFalse((self.boot / (".nobara-initramfs-" + KERNEL + ".img")).exists())

    def test_wrong_or_missing_initramfs_reference_is_not_accepted(self):
        self.installed()
        self.initrd.write_text("rebuilt initramfs")
        for replacement in ("initrd /wrong.img", "# no initrd"):
            self.entry.write_text(self.bls().replace("initrd /initramfs-" + KERNEL + ".img", replacement))
            with self.assertRaisesRegex(UpdateError, "does not reference its rebuilt initramfs"):
                boot.validate_grub_initramfs(KERNEL, self.run_command)

    def test_missing_auxiliary_initrd_is_not_accepted(self):
        self.installed()
        self.initrd.write_text("rebuilt initramfs")
        self.entry.write_text(self.bls().replace(".img\n", ".img /missing-microcode.img\n"))
        with self.assertRaisesRegex(UpdateError, "missing, empty, or outside"):
            boot.validate_grub_initramfs(KERNEL, self.run_command)

    def test_btrfs_boot_paths_and_optional_tuned_initrd_are_checked(self):
        self.installed()
        self.initrd.write_text("rebuilt initramfs")
        self.entry.write_text(self.bls().replace("linux /", "linux /@/boot/").replace("initrd /", "initrd /@/boot/")
                              .replace(".img\n", ".img $tuned_initrd\n"))
        (self.boot / "tuned.img").write_text("tuned initrd")
        with patch.object(boot, "output", side_effect=lambda args: "/@/boot" if args[-1] == str(self.boot) else
                          "/@/boot/" + Path(args[-1]).name), \
             patch.object(recovery, "grub_variables", return_value={"tuned_initrd": "/@/boot/tuned.img"}):
            boot.validate_grub_initramfs(KERNEL, self.run_command)
        self.assertEqual(self.commands, [["lsinitrd", str(self.boot / "tuned.img")]])

    def test_diagnostics_do_not_publish_plugin_environment(self):
        with self.assertLogs(boot.LOG, level="INFO") as log:
            boot.kernel_install_diagnostics(KERNEL, self.source)
        self.assertIn("BootRoot", "\n".join(log.output))
        self.assertIn("20-grub.install", "\n".join(log.output))
        self.assertNotIn("SECRET", "\n".join(log.output))

    def test_inspection_failure_does_not_block_normal_repair(self):
        with patch.object(boot.subprocess, "run", side_effect=subprocess.TimeoutExpired("inspect", 15)):
            self.assertEqual(boot.ensure_grub_kernel(KERNEL, self.run_command), self.entry.stem)


if __name__ == "__main__":
    unittest.main()
