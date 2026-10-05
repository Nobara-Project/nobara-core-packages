"""Kernel selection uses temporary boot trees; never changes host boot state."""
import sys
import tempfile
import types
import unittest
import shutil
from pathlib import Path
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1] / "src"
if "nobara_updater" not in sys.modules:
    package = types.ModuleType("nobara_updater")
    package.__path__ = [str(SOURCE)]
    sys.modules["nobara_updater"] = package
from nobara_updater import update_boot as boot, update_backend as backend, update_state as state


KERNEL = "6.20.1-200.nobara.x86_64"
PACKAGES = [dict(name="kernel-core", action="Install", nevra="kernel-core-0:" + KERNEL)]
SAVED_CONFIG = 'if [ "${next_entry}" ]; then\n  set default="${next_entry}"\nelse\n  set default="${saved_entry}"\nfi\nblscfg\n'


class VersionTests(unittest.TestCase):
    def test_rpm_version_and_release_order_instead_of_lexical_or_menu_order(self):
        data = "0|6.9.1|999.nobara|x86_64\n0|6.20.1|99.nobara|x86_64\n0|6.20.1|200.nobara|x86_64"
        with patch.object(boot, "output", return_value=data):
            self.assertEqual(boot.newest_updated_kernel(PACKAGES), KERNEL)

    def test_epoch_and_prerelease_order_are_respected(self):
        for data, expected in (("0|6.20.1~rc1|200|x86_64\n0|6.20.1|200|x86_64", "6.20.1-200.x86_64"),
                               ("0|7.0.0|200|x86_64\n1|6.20.1|200|x86_64", "6.20.1-200.x86_64")):
            with self.subTest(data=data), patch.object(boot, "output", return_value=data):
                self.assertEqual(boot.newest_updated_kernel(PACKAGES), expected)

    def test_only_updated_kernel_specs_are_queried_preserving_kernel_stream(self):
        packages = PACKAGES + [dict(name="kernel-devel", action="Install", nevra="development"),
                               dict(name="kernel-core", action="Replaced", nevra="old-mainline")]
        with patch.object(boot, "output", return_value="0|6.18.5|100.lts.nobara|x86_64") as output:
            self.assertEqual(boot.newest_updated_kernel(packages), "6.18.5-100.lts.nobara.x86_64")
        self.assertEqual(output.call_args.args[0][4:], [PACKAGES[0]["nevra"]])

    def test_non_kernel_transactions_do_not_query_or_change_kernel(self):
        packages = [dict(name="kernel-devel", action="Upgrade"), dict(name="editor", action="Upgrade")]
        self.assertFalse(boot.changes_kernel(packages))
        with patch.object(boot, "output") as output:
            self.assertIsNone(boot.newest_updated_kernel(packages))
        output.assert_not_called()

    def test_unusable_rpm_output_is_not_guessed(self):
        for data in ("", "invalid", "0|../../bad|1|x86_64", "0|6.20|1|x86_64\n0|6.20|1|aarch64"):
            with self.subTest(data=data), patch.object(boot, "output", return_value=data):
                with self.assertRaises(state.UpdateError):
                    boot.newest_updated_kernel(PACKAGES)


class BootTreeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.boot = self.root / "boot"
        self.entries = self.boot / "loader/entries"
        self.entries.mkdir(parents=True)
        (self.boot / "grub2").mkdir()
        self.config = self.boot / "grub2/grub.cfg"
        self.config.write_text(SAVED_CONFIG)
        self.defaults = self.root / "grub-defaults"
        self.defaults.write_text("GRUB_DEFAULT='saved'\nGRUB_TIMEOUT=5\n")
        self.machine = self.root / "machine-id"
        self.machine.write_text("a" * 32 + "\n")
        self.entry_id = "a" * 32 + "-" + KERNEL
        self.entry(self.entry_id)
        (self.boot / ("vmlinuz-" + KERNEL)).write_text("kernel")
        for patcher in (patch.object(boot, "BOOT", self.boot), patch.object(boot, "GRUB_DEFAULTS", self.defaults),
                        patch.object(boot, "MACHINE_ID", self.machine), patch.object(boot.os, "sync")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def entry(self, identifier, kernel=KERNEL, image=None):
        (self.entries / (identifier + ".conf")).write_text(
            f"title Nobara\nversion {kernel}\nlinux {image or '/vmlinuz-' + kernel}\ninitrd /initramfs-{kernel}.img\n")

    def test_normal_machine_entry_wins_and_recovery_and_rescue_are_never_selected(self):
        self.entry("nobara-recovery-" + "b" * 32)
        self.entry("a" * 32 + "-0-rescue")
        self.entry("another-machine-" + KERNEL)
        self.assertEqual(boot.kernel_entry(KERNEL), self.entry_id)
        (self.entries / (self.entry_id + ".conf")).unlink()
        (self.entries / ("another-machine-" + KERNEL + ".conf")).unlink()
        with self.assertRaisesRegex(state.UpdateError, "one normal boot entry"):
            boot.kernel_entry(KERNEL)

    def test_duplicate_unknown_entries_and_wrong_image_are_refused(self):
        (self.entries / (self.entry_id + ".conf")).unlink()
        self.entry("unknown-1")
        self.entry("unknown-2")
        with self.assertRaises(state.UpdateError):
            boot.kernel_entry(KERNEL)
        for entry in self.entries.glob("*.conf"):
            entry.unlink()
        self.entry(self.entry_id, image="/vmlinuz-old")
        with self.assertRaises(state.UpdateError):
            boot.kernel_entry(KERNEL)

    def test_btrfs_subvolume_kernel_path_matches_grubs_own_path_resolution(self):
        image = "/root/boot/vmlinuz-" + KERNEL
        self.entry(self.entry_id, image=image)
        with patch.object(boot, "output", side_effect=[image, "/boot/vmlinuz-" + KERNEL]):
            self.assertEqual(boot.kernel_entry(KERNEL), self.entry_id)
        with patch.object(boot, "output", return_value="/different-subvolume/vmlinuz-" + KERNEL):
            with self.assertRaises(state.UpdateError):
                boot.kernel_entry(KERNEL)

    def test_static_pin_is_converted_without_losing_other_settings(self):
        self.defaults.write_text('GRUB_DEFAULT="older-kernel"\nGRUB_TIMEOUT=17\nGRUB_CMDLINE_LINUX="rd.luks.uuid=test"\n')
        self.config.write_text('set default="older-kernel"\nblscfg\n')

        def output(command):
            if command[0] == "grub2-mkconfig":
                Path(command[-1]).write_text(SAVED_CONFIG)
            return ""

        with patch.object(boot, "output", side_effect=output) as run:
            boot.ensure_saved_default()
        self.assertEqual(self.config.read_text(), SAVED_CONFIG)
        self.assertIn("GRUB_DEFAULT=saved", self.defaults.read_text())
        self.assertIn("GRUB_TIMEOUT=17", self.defaults.read_text())
        self.assertIn('GRUB_CMDLINE_LINUX="rd.luks.uuid=test"', self.defaults.read_text())
        self.assertEqual([call.args[0][0] for call in run.call_args_list], ["grub2-mkconfig", "grub2-script-check"])

    def test_regeneration_failure_preserves_original_boot_config_and_defaults(self):
        self.defaults.write_text("GRUB_DEFAULT=2\nGRUB_TIMEOUT=5\n")
        original = self.defaults.read_text()
        with patch.object(boot, "output", side_effect=state.UpdateError("invalid generated config")):
            with self.assertRaises(state.UpdateError):
                boot.ensure_saved_default()
        self.assertEqual(self.defaults.read_text(), original)
        self.assertEqual(self.config.read_text(), SAVED_CONFIG)
        self.assertFalse(list(self.config.parent.glob(".nobara-*")))

    def test_command_success_does_not_hide_ignored_default_pin(self):
        with patch.object(boot, "check_selection_support"), patch.object(boot, "output", return_value="saved_entry=old"):
            with self.assertRaisesRegex(state.UpdateError, "did not retain"):
                boot.pin_kernel(KERNEL)

    def test_unsupported_layout_is_rejected_before_installation(self):
        self.config.write_text('set default="some-custom-loader"\n')
        with self.assertRaisesRegex(state.UpdateError, "GRUB/BLS"):
            boot.check_selection_support()

    def test_uki_only_layout_is_rejected_before_installation(self):
        for entry in self.entries.glob("*.conf"):
            entry.unlink()
        with self.assertRaisesRegex(state.UpdateError, "split kernel/initramfs"):
            boot.check_selection_support()

    @unittest.skipUnless(all(shutil.which(command) for command in ("grub2-set-default", "grub2-editenv", "grub2-mkconfig", "grub2-script-check", "grub2-mkrelpath")), "GRUB tools unavailable")
    def test_real_grub_tools_pin_default_and_clear_overrides_in_temporary_boot_tree(self):
        env = str(self.boot / "grub2/grubenv")
        boot.output(["grub2-editenv", env, "create"])
        boot.output(["grub2-editenv", env, "set", "saved_entry=old", "next_entry=old", "prev_saved_entry=old", "tmp_saved_entry=old"])
        selection = boot.pin_kernel(KERNEL)
        self.assertEqual(selection, dict(kernel=KERNEL, entry=self.entry_id, loader="grub"))
        self.assertEqual(boot.output(["grub2-editenv", env, "list"]), "saved_entry=" + self.entry_id)


class KernelWorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.job = self.root / "jobs" / ("a" * 32)
        self.job.mkdir(parents=True)
        (self.job / "transaction.json").write_text("{}")
        self.saved = dict(job="a" * 32, source_release="44", target_release="44", started=True,
                          files={"transaction.json": state.file_digest(self.job / "transaction.json")},
                          migrations={"hooks": []}, packages=PACKAGES)
        state.write_state(self.root, dict(self.saved), "validating")
        for patcher in (patch.object(backend, "run"), patch.object(backend, "announce"),
                        patch.object(backend, "SPLASH"),
                        patch.object(backend, "maintain_btrfs_root", return_value=True),
                        patch.object(backend, "synchronize_boot_root", return_value=None),
                        patch.object(backend, "prune_recovery"), patch.object(backend, "prune_payloads"),
                        patch.object(backend, "resolve_notice"),
                        patch.object(backend, "prune_kernel_packages"),
                        patch.object(backend, "grub_environment", return_value={}),
                        patch.object(backend, "os_release", return_value="44"),
                        patch.object(backend, "rpm_fingerprint", return_value="fixture")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_successful_validation_pins_and_records_exact_kernel_before_reboot(self):
        events = []
        selection = dict(kernel=KERNEL, entry="normal", loader="grub")
        with patch.object(backend, "validate_boot", side_effect=lambda *a, **kw: events.append("validate")), \
             patch.object(backend, "newest_updated_kernel", return_value=KERNEL), \
             patch.object(backend, "pin_kernel", side_effect=lambda *a: events.append("pin") or selection):
            backend.finalize(self.root)
        self.assertEqual(events, ["validate", "pin"])
        self.assertEqual(state.read_state(self.root)["boot_selection"], selection)
        self.assertEqual(state.read_state(self.root)["status"], "awaiting-boot")
        backend.SPLASH.start.assert_called_once_with(release_upgrade=False, percent=90)
        self.assertEqual(backend.announce.call_args.kwargs, {"percent": 100})

    def test_boot_validation_failure_never_pins_kernel(self):
        with patch.object(backend, "validate_boot", side_effect=state.UpdateError("module build failed")), \
             patch.object(backend, "pin_kernel") as pin:
            with self.assertRaises(state.UpdateError):
                backend.finalize(self.root)
        pin.assert_not_called()
        self.assertEqual(state.read_state(self.root)["status"], "validating")
        self.assertFalse(any(invocation.kwargs.get("percent") == 100 for invocation in backend.announce.call_args_list))

    def test_finalization_passes_preserved_kernel_plan_to_validation(self):
        state.write_state(self.root, dict(self.saved), "validating", preserved_kernels=["old-kernel"])
        with patch.object(backend, "validate_boot") as validate, \
             patch.object(backend, "newest_updated_kernel", return_value=KERNEL), \
             patch.object(backend, "pin_kernel", return_value=dict(kernel=KERNEL, entry="normal", loader="grub")):
            backend.finalize(self.root)
        validate.assert_called_once_with(PACKAGES, rebuild_all=False, preserved_kernels=["old-kernel"])

    def test_driver_only_update_pins_validated_kernel_when_legacy_kernel_was_preserved(self):
        packages = [dict(name="dkms", action="Upgrade", nevra="dkms-1-1.noarch")]
        state.write_state(self.root, dict(self.saved), "validating", packages=packages, preserved_kernels=["old-kernel"])
        selection = dict(kernel=KERNEL, entry="normal", loader="grub")
        with patch.object(backend, "validate_boot"), \
             patch.object(backend, "newest_updated_kernel", return_value=None), \
             patch.object(backend.os, "uname", return_value=Mock(release=KERNEL)), \
             patch.object(backend, "pin_kernel", return_value=selection) as pin:
            backend.finalize(self.root)
        pin.assert_called_once_with(KERNEL)
        self.assertEqual(state.read_state(self.root)["boot_selection"], selection)

    def test_update_from_recovered_root_trials_normal_entry_even_without_kernel_update(self):
        recovery = dict(created=True, entry="/boot/loader/entries/nobara-recovery-old.conf")
        state.write_state(self.root, dict(self.saved), "validating", packages=[], recovery=recovery)
        with patch.object(backend, "synchronize_boot_root", return_value="normal") as sync, \
             patch.object(backend, "validate_boot"), patch.object(backend, "newest_updated_kernel", return_value=None), \
             patch.object(backend.os, "uname", return_value=Mock(release=KERNEL)), \
             patch.object(backend, "kernel_entry", return_value="normal"), \
             patch.object(backend, "trial_boot") as trial:
            backend.finalize(self.root)
        trial.assert_called_once_with(recovery, "normal")
        self.assertEqual(state.read_state(self.root)["normal_boot_entry"], "normal")
        self.assertEqual(state.read_state(self.root)["boot_selection"]["kernel"], KERNEL)
        sync.assert_called_once()

    def test_legacy_completed_root_can_promote_normal_entry_without_installing_again(self):
        state.write_state(self.root, dict(self.saved), "complete")
        with patch.object(backend, "grub_environment", return_value={}), \
             patch.object(backend, "synchronize_boot_root", return_value="normal"), \
             patch.object(backend, "confirm_trial") as confirm:
            backend.confirm_boot(self.root)
        confirm.assert_called_once_with("normal")
        self.assertEqual(state.read_state(self.root)["status"], "complete")
        self.assertEqual(state.read_state(self.root)["trial_entry"], "normal")

    def test_legacy_completed_root_does_not_override_another_armed_update(self):
        state.write_state(self.root, dict(self.saved), "complete")
        with patch.object(backend, "grub_environment", return_value={"nobara_fallback": "different-job"}), \
             patch.object(backend, "synchronize_boot_root") as sync:
            backend.confirm_boot(self.root)
        sync.assert_not_called()
        backend.prune_recovery.assert_not_called()

    def test_cleanup_retries_after_failure_without_undoing_confirmed_boot(self):
        state.write_state(self.root, dict(self.saved), "complete", normal_boot_entry="normal", recovery={"created": True})
        with patch.object(backend, "grub_environment", return_value={}), \
             patch.object(backend, "prune_recovery", side_effect=[OSError("busy snapshot"), None]) as prune:
            with self.assertLogs(backend.LOG, level="ERROR"):
                backend.confirm_boot(self.root)
            failed = state.read_state(self.root)
            self.assertEqual(failed["status"], "complete")
            self.assertFalse(failed.get("recovery_cleanup_complete"))
            backend.confirm_boot(self.root)
            self.assertTrue(state.read_state(self.root)["recovery_cleanup_complete"])
            backend.confirm_boot(self.root)
            self.assertEqual(prune.call_count, 2)

    def test_kernel_retention_runs_after_recovery_cleanup_and_retries_nonfatally(self):
        events = []
        state.write_state(self.root, dict(self.saved), "complete", normal_boot_entry="normal",
                          recovery={"created": True})
        with patch.object(backend, "prune_recovery", side_effect=lambda *a, **kw: events.append("recovery")), \
             patch.object(backend, "prune_kernel_packages", side_effect=[state.UpdateError("DNF busy"), None]) as prune, \
             patch.object(Path, "exists", return_value=False), patch.object(Path, "is_symlink", return_value=False):
            with self.assertLogs(backend.LOG, level="WARNING"):
                backend.confirm_boot(self.root)
            self.assertEqual(events, ["recovery"])
            self.assertEqual(state.read_state(self.root)["status"], "complete")
            self.assertTrue(state.read_state(self.root)["recovery_cleanup_complete"])
            backend.confirm_boot(self.root)
            self.assertEqual(prune.call_count, 2)

    def test_kernel_retention_does_not_run_before_recovery_cleanup_succeeds(self):
        saved = dict(self.saved, status="complete", recovery={"created": True})
        backend.cleanup_kernel_retention(saved)
        backend.prune_kernel_packages.assert_not_called()

    def test_kernel_retention_receives_obsolete_versions_only_after_confirmation(self):
        saved = dict(self.saved, preserved_kernels=["6.12.6-200.fsync.fc41.x86_64"],
                     status="complete", recovery_cleanup_complete=True)
        with patch.object(Path, "exists", return_value=False), patch.object(Path, "is_symlink", return_value=False):
            backend.cleanup_kernel_retention(saved)
        backend.prune_kernel_packages.assert_called_once_with(obsolete_kernels=saved["preserved_kernels"])

    def test_kernel_retention_skips_active_failed_recovered_and_installer_states(self):
        for status in ("ready", "scheduled", "installing", "validating", "awaiting-boot", "failed",
                       "interrupted", "recovering", "recovered", "installer-complete"):
            backend.cleanup_kernel_retention(dict(self.saved, status=status))
        backend.prune_kernel_packages.assert_not_called()

    def test_kernel_retention_skips_armed_boot_entries_and_recovery_markers(self):
        saved = dict(self.saved, status="complete", recovery_cleanup_complete=True)
        with patch.object(Path, "exists", return_value=False), patch.object(Path, "is_symlink", return_value=False):
            for key in ("nobara_fallback", "nobara_trial", "next_entry"):
                with patch.object(backend, "grub_environment", return_value={key: "entry"}):
                    backend.cleanup_kernel_retention(saved)
            with patch.object(backend, "grub_environment", return_value={"saved_entry": "nobara-recovery-old"}):
                backend.cleanup_kernel_retention(saved)
        with patch.object(Path, "exists", return_value=True):
            backend.cleanup_kernel_retention(saved)
        backend.prune_kernel_packages.assert_not_called()

    def test_recovery_diagnostics_must_be_imported_before_snapshot_cleanup(self):
        state.write_state(self.root, dict(self.saved), "recovered", normal_boot_entry="normal", recovery={"created": True})
        with patch.object(backend, "grub_environment", return_value={}), \
             patch.object(backend, "publish_recovery", side_effect=[OSError("report storage full"), {"error": "failed RPM"}]):
            with self.assertLogs(backend.LOG, level="ERROR"):
                backend.confirm_boot(self.root)
            backend.prune_recovery.assert_not_called()
            backend.confirm_boot(self.root)
        backend.prune_recovery.assert_called_once()
        backend.resolve_notice.assert_not_called()
        self.assertTrue(state.read_state(self.root)["recovery_cleanup_complete"])

    def test_failed_pin_never_reports_ready_to_boot(self):
        with patch.object(backend, "validate_boot"), patch.object(backend, "newest_updated_kernel", return_value=KERNEL), \
             patch.object(backend, "pin_kernel", side_effect=state.UpdateError("GRUB failed")):
            with self.assertRaisesRegex(state.UpdateError, "GRUB failed"):
                backend.finalize(self.root)
        self.assertEqual(state.read_state(self.root)["status"], "validating")

    def test_booting_old_kernel_cannot_confirm_even_with_existing_modules(self):
        state.write_state(self.root, dict(self.saved), "awaiting-boot", boot_selection=dict(kernel=KERNEL))
        with patch.object(backend.os, "uname", return_value=Mock(release="old-kernel")), \
             patch.object(Path, "is_dir", return_value=True), patch.object(backend, "prune_payloads") as prune:
            with self.assertRaisesRegex(state.UpdateError, "booted kernel old-kernel"):
                backend.confirm_boot(self.root)
        self.assertEqual(state.read_state(self.root)["status"], "awaiting-boot")
        prune.assert_not_called()

    def test_recovery_boot_is_recognized_before_new_kernel_expectation(self):
        state.write_state(self.root, dict(self.saved), "awaiting-boot", boot_selection=dict(kernel=KERNEL))
        real_read = Path.read_text
        def read(path, *args, **kwargs):
            if str(path) == "/etc/nobara-updater-recovery.json":
                return '{"job": "' + "a" * 32 + '"}'
            return real_read(path, *args, **kwargs)
        with patch.object(Path, "exists", return_value=True), patch.object(Path, "read_text", read), \
             patch.object(backend, "publish_recovery", return_value={"error": "saved failure"}) as report, \
             patch.object(Path, "unlink") as unlink, patch.object(backend, "confirm_trial") as confirmed, \
             patch.object(backend.os, "uname", return_value=Mock(release="old-kernel")):
            backend.confirm_boot(self.root)
        confirmed.assert_called_once_with("nobara-recovery-" + "a" * 32)
        self.assertEqual(state.read_state(self.root)["status"], "recovered")
        self.assertEqual(state.read_state(self.root)["failure_detail"], "saved failure")
        self.assertTrue(state.read_state(self.root)["recovery"]["created"])
        self.assertTrue(backend.prune_recovery.call_args.args[0]["created"])
        report.assert_called_once_with("a" * 32)
        unlink.assert_called_once()

    def test_correct_kernel_passes_confirmation(self):
        state.write_state(self.root, dict(self.saved), "awaiting-boot", boot_selection=dict(kernel=KERNEL))
        with patch.object(backend.os, "uname", return_value=Mock(release=KERNEL)), \
             patch.object(Path, "is_dir", return_value=True), patch.object(Path, "unlink"), \
             patch.object(backend, "prune_recovery"), patch.object(backend, "prune_payloads"):
            backend.confirm_boot(self.root)
        self.assertEqual(state.read_state(self.root)["status"], "complete")

    def test_installer_pins_in_target_and_records_expectation_for_first_boot(self):
        state.write_state(self.root, dict(self.saved), "validating", installer=True)
        with patch.object(backend, "in_installer_root", return_value=True), patch.object(backend, "validate_boot"), \
             patch.object(backend, "newest_updated_kernel", return_value=KERNEL), \
             patch.object(backend, "pin_kernel", return_value=dict(kernel=KERNEL)) as pin:
            backend.finalize(self.root, installer=True)
        pin.assert_called_once_with(KERNEL)
        self.assertEqual(state.read_state(self.root)["status"], "installer-complete")
        self.assertEqual(state.read_state(self.root)["boot_selection"]["kernel"], KERNEL)

    def test_unchanged_validated_inventory_avoids_second_full_dependency_scan(self):
        state.write_state(self.root, dict(self.saved), "awaiting-boot", installed_fingerprint="fixture",
                          boot_selection=dict(kernel=KERNEL), trial_entry="normal")
        with patch.object(backend.os, "uname", return_value=Mock(release=KERNEL)), \
             patch.object(Path, "is_dir", return_value=True), patch.object(Path, "unlink"), \
             patch.object(Path, "exists", lambda p: str(p) == "/etc/systemd/system/display-manager.service"), \
             patch.object(backend, "check_installed_packages") as check, \
             patch.object(backend, "confirm_trial") as confirmed:
            backend.confirm_boot(self.root)
        check.assert_not_called()
        backend.run.assert_called_once_with(["systemctl", "is-active", "--quiet", "display-manager.service"])
        confirmed.assert_called_once_with("normal")
        self.assertEqual(state.read_state(self.root)["status"], "complete")

    def test_changed_and_legacy_inventories_still_require_dependency_check(self):
        for fingerprint in ("different-packages", None):
            with self.subTest(fingerprint=fingerprint):
                state.write_state(self.root, dict(self.saved), "awaiting-boot", installed_fingerprint=fingerprint)
                with patch.object(Path, "exists", return_value=False), \
                     patch.object(backend, "check_installed_packages", side_effect=state.UpdateError("broken dependency")) as check, \
                     patch.object(backend, "confirm_trial") as confirmed:
                    with self.assertRaisesRegex(state.UpdateError, "broken dependency"):
                        backend.confirm_boot(self.root)
                check.assert_called_once()
                confirmed.assert_not_called()
                self.assertEqual(state.read_state(self.root)["status"], "awaiting-boot")

    def test_unreadable_inventory_never_reuses_saved_validation(self):
        state.write_state(self.root, dict(self.saved), "awaiting-boot", installed_fingerprint="fixture")
        with patch.object(Path, "exists", return_value=False), \
             patch.object(backend, "rpm_fingerprint", side_effect=state.UpdateError("RPM database unreadable")), \
             patch.object(backend, "confirm_trial") as confirmed:
            with self.assertRaisesRegex(state.UpdateError, "RPM database unreadable"):
                backend.confirm_boot(self.root)
        confirmed.assert_not_called()
        backend.prune_recovery.assert_not_called()

    def test_matching_inventory_does_not_hide_missing_running_kernel_modules(self):
        state.write_state(self.root, dict(self.saved), "awaiting-boot", installed_fingerprint="fixture")
        is_dir = Path.is_dir
        with patch.object(Path, "exists", return_value=False), \
             patch.object(Path, "is_dir", lambda p: False if p.parent == Path("/usr/lib/modules") else is_dir(p)), \
             patch.object(backend, "confirm_trial") as confirmed:
            with self.assertRaisesRegex(state.UpdateError, "Modules for the running kernel are missing"):
                backend.confirm_boot(self.root)
        confirmed.assert_not_called()
        backend.prune_recovery.assert_not_called()

    def test_failed_installation_check_never_saves_validated_inventory(self):
        with patch.object(backend, "validate_boot"), \
             patch.object(backend, "check_installed_packages", side_effect=state.UpdateError("broken dependency")):
            with self.assertRaisesRegex(state.UpdateError, "broken dependency"):
                backend.finalize(self.root)
        self.assertNotIn("installed_fingerprint", state.read_state(self.root))
        backend.rpm_fingerprint.assert_not_called()

    def test_following_installer_codec_transaction_retains_kernel_expectation(self):
        from nobara_updater import update_plan as planner
        selection = dict(kernel=KERNEL, entry="normal", loader="grub")
        state.write_state(self.root, dict(self.saved), "installer-complete", installer=True, boot_selection=selection)
        real_copy = shutil.copy2

        def copy(source, target):
            if str(source) == "/usr/libexec/nobara-update-worker":
                source = SOURCE / "update_worker.py"
            return real_copy(source, target)

        def prepare(job, **kwargs):
            (job / "transaction.json").write_text("{}")
            return dict(empty=False, fingerprint="fixture", source_release="44", target_release="44", packages=[], migrations={"hooks": []},
                        files={"transaction.json": state.file_digest(job / "transaction.json")})

        with patch.object(backend, "in_installer_root", return_value=True), \
             patch.object(backend, "rpm_fingerprint", return_value="fixture"), \
             patch.object(backend, "TRIGGER", self.root / "system-update"), patch.object(backend.os, "sync"), \
             patch.object(backend.shutil, "copy2", side_effect=copy), \
             patch.object(planner, "prepare_transaction", side_effect=prepare):
            backend.prepare(self.root, installer=True, codecs=True)
        prepared = state.read_state(self.root)
        self.assertEqual(prepared["boot_selection"], selection)
        self.assertEqual(prepared["status"], "ready")
        state.write_state(self.root, prepared, "validating", started=True)
        with patch.object(backend, "in_installer_root", return_value=True), patch.object(backend, "validate_boot"), \
             patch.object(backend, "pin_kernel") as pin:
            backend.finalize(self.root, installer=True)
        pin.assert_not_called()
        self.assertEqual(state.read_state(self.root)["boot_selection"], selection)


if __name__ == "__main__":
    unittest.main()
