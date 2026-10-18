"""Explicit confirmation must not turn an interrupted replay into success."""
import importlib.util
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import Mock, patch

from test_recovery_retry import RetryFixture, JOB, SOURCE
import test_kernel_selection as kernel_tests
from nobara_updater import update_backend as backend, update_boot as boot
from nobara_updater import update_recovery as recovery, update_report as reports, update_state as state


KERNEL = kernel_tests.KERNEL


class ConfirmationTests(RetryFixture):
    def setUp(self):
        super().setUp()
        self.failed.update(installed_fingerprint="b" * 64, packages=[], target_release="44",
                           boot_selection=dict(kernel=KERNEL, entry="normal"))
        state.write_state(self.state_root, self.failed, "interrupted")
        self.original = (self.state_root / "state.json").read_text()
        def path(value):
            return self.root / "newinstall" if str(value) == "/etc/nobara/newinstall" else Path(value)
        for patcher in (patch.object(backend, "verify_files"), patch.object(backend, "check_installed_system"),
                        patch.object(backend, "validate_completed_packages"), patch.object(backend, "validate_running_update"),
                        patch.object(boot, "validate_running_entry"), patch.object(backend, "confirm_trial"),
                        patch.object(backend, "rpm_fingerprint", return_value="b" * 64),
                        patch.object(backend, "Path", side_effect=path),
                        patch.object(backend, "cleanup_confirmed_update")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_success_confirms_expected_entry_before_cleanup_preserving_original_failure(self):
        def cleanup(root, saved):
            self.assertEqual(state.read_state(root)["status"], "complete")
            backend.confirm_trial.assert_called_once_with("normal")
            backend.publish_failure.assert_called_once()
        backend.cleanup_confirmed_update.side_effect = cleanup
        backend.confirm_update(self.state_root)
        saved = state.read_state(self.state_root)
        self.assertEqual(saved["status"], "complete")
        self.assertTrue(saved["manually_confirmed"])
        self.assertFalse(saved["recovery_blocked"])
        self.assertFalse(saved["recovery_retry_ready"])
        self.assertEqual(saved["error"], self.failed["error"])
        self.assertEqual(json.loads((self.state_root / "jobs" / JOB / "confirmation-retry-state.json").read_text()),
                         json.loads(self.original))
        self.assertTrue((self.archive / "reboot-attempted").is_dir())
        backend.check_installed_system.assert_called_once()
        backend.validate_completed_packages.assert_called_once_with([])
        boot.validate_running_entry.assert_called_once_with(KERNEL, "normal")

    def test_each_failed_validation_keeps_block_and_snapshot_and_does_not_disarm(self):
        checks = (backend.verify_files, recovery.validate_saved_recovery, backend.check_installed_system,
                  backend.validate_completed_packages, backend.validate_running_update, boot.validate_running_entry,
                  backend.publish_failure)
        for check in checks:
            with self.subTest(check=check):
                check.side_effect = state.UpdateError("validation failed")
                with self.assertRaisesRegex(state.UpdateError, "validation failed"):
                    backend.confirm_update(self.state_root)
                check.side_effect = None
                self.assertEqual((self.state_root / "state.json").read_text(), self.original)
                self.assertTrue((self.archive / "reboot-attempted").is_dir())
                self.assertTrue(self.entry.is_file())
                backend.confirm_trial.assert_not_called()
                backend.cleanup_confirmed_update.assert_not_called()

    def test_partial_installation_and_unrelated_states_are_not_confirmed(self):
        changes = [dict(installed_fingerprint=""), dict(installed_fingerprint="invalid"), dict(boot_selection={}),
                   dict(status="installing"), dict(status="validating"), dict(status="recovering"),
                   dict(status="recovered"), dict(status="complete"), dict(started=False),
                   dict(installer=True), dict(execution={"mode": "live"}), dict(recovery={"created": False})]
        for fields in changes:
            with self.subTest(fields=fields):
                saved = dict(self.failed, status="interrupted", **{k:v for k,v in fields.items() if k != "status"})
                state.write_state(self.state_root, saved, fields.get("status", "interrupted"))
                with self.assertRaises(state.UpdateError):
                    backend.confirm_update(self.state_root)
        backend.confirm_trial.assert_not_called()
        backend.cleanup_confirmed_update.assert_not_called()

    def test_pending_trigger_installer_and_concurrent_operation_are_refused(self):
        with state.update_lock(self.state_root), self.assertRaisesRegex(state.UpdateError, "Another Nobara"):
            backend.confirm_update(self.state_root)
        with patch.object(backend, "in_installer_root", return_value=True), self.assertRaises(state.UpdateError):
            backend.confirm_update(self.state_root)
        backend.TRIGGER.symlink_to(self.root / "missing")
        with self.assertRaisesRegex(state.UpdateError, "scheduled or running"):
            backend.confirm_update(self.state_root)
        backend.confirm_trial.assert_not_called()

    def test_foreign_fallback_and_changing_inventory_cannot_confirm(self):
        with patch.object(backend, "grub_environment", return_value={"nobara_fallback": "another-job"}), \
             self.assertRaisesRegex(state.UpdateError, "Another update"):
            backend.confirm_update(self.state_root)
        backend.rpm_fingerprint.side_effect = ["b" * 64, "c" * 64]
        with self.assertRaisesRegex(state.UpdateError, "changed during"):
            backend.confirm_update(self.state_root)
        backend.confirm_trial.assert_not_called()
        backend.cleanup_confirmed_update.assert_not_called()

    def test_failed_grub_confirmation_does_not_mark_complete_or_remove_recovery(self):
        backend.confirm_trial.side_effect = state.UpdateError("Could not persist entry")
        with self.assertRaisesRegex(state.UpdateError, "Could not persist"):
            backend.confirm_update(self.state_root)
        self.assertEqual((self.state_root / "state.json").read_text(), self.original)
        backend.cleanup_confirmed_update.assert_not_called()

    def test_cli_uses_installed_worker_and_never_prepares_or_schedules(self):
        spec = importlib.util.spec_from_file_location("confirmation_cli", SOURCE / "nobara_sync.py")
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        with patch.object(cli, "in_installer_root", return_value=False), \
             patch.object(cli, "worker_action", return_value=True) as worker, \
             patch.object(cli, "prepare_update") as prepare, patch.object(cli, "read_state", return_value={"status": "complete"}), \
             patch("builtins.print"):
            self.assertTrue(cli.dispatch(cli.parse_args(["confirm-update"])))
        worker.assert_called_once_with("confirm-update", cli.LOG)
        prepare.assert_not_called()


class PackageResultTests(unittest.TestCase):
    def test_exact_or_later_same_architecture_is_accepted(self):
        for installed in ("2.0.1|78", "2.0.1|79", "2.1|1"):
            with self.subTest(installed=installed), patch.object(backend.subprocess, "run", return_value=Mock(
                    stdout=f"nobara-updater|0|{installed}|noarch\n")):
                backend.validate_completed_packages([dict(name="nobara-updater", action="Upgrade",
                                                         nevra="nobara-updater-0:2.0.1-78.noarch")])

    def test_missing_arch_changed_older_or_removed_packages_are_rejected(self):
        planned = [dict(name="application", action="Reinstall", nevra="application-0:1-1.noarch"),
                   dict(name="application", action="Replaced", nevra="application-0:1-1.x86_64")]
        for installed in ("", "application|0|1|1|x86_64\n", "application|0|0.9|1|noarch\n",
                          "application|0|1|1|noarch\napplication|0|1|1|x86_64\n"):
            with self.subTest(installed=installed), patch.object(backend.subprocess, "run", return_value=Mock(stdout=installed)), \
                 self.assertRaises(state.UpdateError):
                backend.validate_completed_packages(planned)

    def test_rpm_epoch_order_and_replaced_exact_reinstall(self):
        packages = [dict(name="application", action=action, nevra="application-1:1-2.noarch")
                    for action in ("Reinstall", "Replaced")]
        with patch.object(backend.subprocess, "run", return_value=Mock(stdout="application|1|1|2|noarch\n")):
            backend.validate_completed_packages(packages)
        with patch.object(backend.subprocess, "run", return_value=Mock(stdout="application|0|100|2|noarch\n")), \
             self.assertRaises(state.UpdateError):
            backend.validate_completed_packages(packages)


class RunningEntryTests(unittest.TestCase):
    def setUp(self):
        kernel_tests.BootTreeTests.setUp(self)
        self.modules = self.root / "modules"
        (self.modules / KERNEL / "kernel").mkdir(parents=True)
        (self.modules / KERNEL / "modules.dep").write_text("module dependencies")
        (self.boot / ("initramfs-" + KERNEL + ".img")).write_text("image")
        self.path = self.entries / (self.entry_id + ".conf")
        self.path.write_text(self.path.read_text() + "options root=UUID=original rootflags=subvol=@ ro\n")
        for patcher in (patch.object(boot, "MODULES", self.modules), patch.object(boot.os, "uname", return_value=Mock(release=KERNEL)),
                        patch.object(boot, "output", return_value=json.dumps(dict(filesystems=[dict(
                            fstype="btrfs", uuid="original", fsroot="/@", source="/dev/root")]))),
                        patch.object(recovery, "BOOT", self.boot), patch.object(recovery, "grub_variables", return_value={}),
                        patch.object(boot.subprocess, "run")):
            patcher.start()
            self.addCleanup(patcher.stop)

    entry = kernel_tests.BootTreeTests.entry

    def test_valid_boot_checks_initramfs_without_writing_boot_files(self):
        before = self.path.read_text()
        boot.validate_running_entry(KERNEL, self.entry_id)
        boot.subprocess.run.assert_called_once_with(["lsinitrd", str(self.boot / ("initramfs-" + KERNEL + ".img"))],
                                                    check=True, stdout=subprocess.DEVNULL)
        self.assertEqual(self.path.read_text(), before)

    def test_lvm_filesystems_accept_the_same_device_or_uuid_root(self):
        original = self.path.read_text().split("options ")[0]
        for filesystem in ("ext4", "xfs"):
            boot.output.return_value = json.dumps(dict(filesystems=[dict(
                fstype=filesystem, uuid="original", fsroot="/", source="/dev/nobara/root")]))
            for root in ("UUID=original", "/dev/nobara/root"):
                with self.subTest(filesystem=filesystem, root=root):
                    self.path.write_text(original + "options root=" + root + " ro\n")
                    boot.validate_running_entry(KERNEL, self.entry_id)

    def test_wrong_kernel_root_subvolume_and_rollback_options_are_refused(self):
        before = self.path.read_text()
        for options in ("root=UUID=other rootflags=subvol=@", "root=UUID=original rootflags=subvol=backup",
                        "root=UUID=original rootflags=subvol=@,subvolid=5", "root=UUID=original rootflags=subvol=@ nobara.rollback=job"):
            self.path.write_text(before.split("options ")[0] + "options " + options + "\n")
            with self.subTest(options=options), self.assertRaises(state.UpdateError):
                boot.validate_running_entry(KERNEL, self.entry_id)
        self.path.write_text(before)
        with patch.object(boot.os, "uname", return_value=Mock(release="old-kernel")), self.assertRaises(state.UpdateError):
            boot.validate_running_entry(KERNEL, self.entry_id)

    def test_incomplete_modules_or_invalid_initramfs_are_refused(self):
        boot.subprocess.run.side_effect = subprocess.CalledProcessError(1, "lsinitrd")
        with self.assertRaises(subprocess.CalledProcessError):
            boot.validate_running_entry(KERNEL, self.entry_id)
        (self.modules / KERNEL / "modules.dep").unlink()
        with self.assertRaisesRegex(state.UpdateError, "module tree"):
            boot.validate_running_entry(KERNEL, self.entry_id)


if __name__ == "__main__":
    unittest.main()
