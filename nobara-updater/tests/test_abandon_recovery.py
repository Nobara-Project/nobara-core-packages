"""Abandoning a recovery that cannot boot keeps the running, checked system."""
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from test_recovery_retry import RetryFixture, JOB, SOURCE
import test_kernel_selection as kernel_tests
from nobara_updater import update_backend as backend, update_boot as boot
from nobara_updater import update_recovery as recovery, update_state as state


KERNEL = kernel_tests.KERNEL


class AbandonRecoveryTests(RetryFixture):
    def setUp(self):
        super().setUp()
        self.failed.update(packages=[], target_release="44", preserved_kernels=["legacy"])
        state.write_state(self.state_root, self.failed, "interrupted")
        self.original = (self.state_root / "state.json").read_text()
        self.cmdline = self.root / "cmdline"
        self.cmdline.write_text("BOOT_IMAGE=/vmlinuz root=UUID=fixture ro\n")
        redirected = {"/etc/nobara/newinstall": self.root / "newinstall", "/proc/cmdline": self.cmdline,
                      "/etc/nobara-updater-recovery.json": self.root / "recovery-marker.json",
                      "/etc/system-update": self.root / "etc-system-update",
                      "/run/nobara-updater-offline": self.root / "run-offline"}
        for patcher in (patch.object(backend, "verify_files"), patch.object(backend, "check_installed_system"),
                        patch.object(backend, "synchronize_boot_root", return_value=None),
                        patch.object(backend, "validate_boot"),
                        patch.object(backend, "kernel_entry", return_value="normal"),
                        patch.object(backend.os, "uname", return_value=type("U", (), {"release": KERNEL})()),
                        patch.object(boot, "validate_running_entry"), patch.object(backend, "confirm_trial"),
                        patch.object(backend, "rpm_fingerprint", return_value="b" * 64),
                        patch.object(backend, "Path", side_effect=lambda value: redirected.get(str(value), Path(value))),
                        patch.object(backend, "cleanup_confirmed_update")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_success_rebuilds_boot_files_selects_running_kernel_then_cleans_up(self):
        def cleanup(root, saved):
            self.assertEqual(state.read_state(root)["status"], "complete")
            backend.confirm_trial.assert_called_once_with("normal")
            backend.publish_failure.assert_called_once()
        backend.cleanup_confirmed_update.side_effect = cleanup
        backend.abandon_recovery(self.state_root)
        saved = state.read_state(self.state_root)
        self.assertEqual(saved["status"], "complete")
        self.assertTrue(saved["abandoned_recovery"])
        self.assertFalse(saved["recovery_blocked"])
        self.assertEqual(saved["boot_selection"], {"kernel": KERNEL, "entry": "normal", "loader": "grub"})
        self.assertEqual(saved["error"], self.failed["error"])
        self.assertIn("abandoned", state.status_message(saved))
        self.assertEqual(json.loads((self.state_root / "jobs" / JOB / "abandon-recovery-state.json").read_text()),
                         json.loads(self.original))
        backend.validate_boot.assert_called_once_with([], rebuild_all=True, preserved_kernels=["legacy"])
        boot.validate_running_entry.assert_called_once_with(KERNEL, "normal")
        backend.cleanup_confirmed_update.assert_called_once()

    def test_each_failed_check_keeps_block_and_recovery_armed(self):
        checks = (backend.verify_files, recovery.validate_saved_recovery, backend.check_installed_system,
                  backend.validate_boot, boot.validate_running_entry, backend.confirm_trial)
        for check in checks:
            with self.subTest(check=check):
                check.side_effect = state.UpdateError("validation failed")
                with self.assertRaisesRegex(state.UpdateError, "validation failed"):
                    backend.abandon_recovery(self.state_root)
                check.side_effect = None
                self.assertEqual((self.state_root / "state.json").read_text(), self.original)
                self.assertTrue(self.entry.is_file())
                backend.cleanup_confirmed_update.assert_not_called()
        backend.confirm_trial.assert_called_once()  # only by its own failing subtest

    def test_unrelated_states_and_the_recovery_system_are_refused(self):
        changes = [dict(status="installing"), dict(status="awaiting-boot"), dict(status="recovering"),
                   dict(status="recovered"), dict(status="complete"), dict(started=False),
                   dict(installer=True), dict(execution={"mode": "live"}), dict(recovery={"created": False})]
        for fields in changes:
            with self.subTest(fields=fields):
                saved = dict(self.failed, **{k: v for k, v in fields.items() if k != "status"})
                state.write_state(self.state_root, saved, fields.get("status", "interrupted"))
                with self.assertRaises(state.UpdateError):
                    backend.abandon_recovery(self.state_root)
        state.write_state(self.state_root, self.failed, "interrupted")
        self.cmdline.write_text("root=/dev/vg/root nobara.rollback=" + JOB + "\n")
        with self.assertRaisesRegex(state.UpdateError, "recovery system"):
            backend.abandon_recovery(self.state_root)
        self.cmdline.write_text("root=UUID=fixture ro\n")
        (self.root / "recovery-marker.json").write_text("{}")
        with self.assertRaisesRegex(state.UpdateError, "recovery system"):
            backend.abandon_recovery(self.state_root)
        backend.validate_boot.assert_not_called()
        backend.confirm_trial.assert_not_called()

    def test_pending_trigger_foreign_fallback_and_changing_inventory_are_refused(self):
        backend.TRIGGER.symlink_to(self.root / "missing")
        with self.assertRaisesRegex(state.UpdateError, "scheduled or running"):
            backend.abandon_recovery(self.state_root)
        backend.TRIGGER.unlink()
        with patch.object(backend, "grub_environment", return_value={"nobara_fallback": "another-job"}), \
             self.assertRaisesRegex(state.UpdateError, "Another update"):
            backend.abandon_recovery(self.state_root)
        backend.rpm_fingerprint.side_effect = ["b" * 64, "c" * 64]
        with self.assertRaisesRegex(state.UpdateError, "changed while"):
            backend.abandon_recovery(self.state_root)
        backend.confirm_trial.assert_not_called()
        self.assertEqual((self.state_root / "state.json").read_text(), self.original)

    def test_cli_runs_the_installed_worker_without_preparing(self):
        spec = importlib.util.spec_from_file_location("abandon_cli", SOURCE / "nobara_sync.py")
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        with patch.object(cli, "in_installer_root", return_value=False), \
             patch.object(cli, "worker_action", return_value=True) as worker, \
             patch.object(cli, "prepare_update") as prepare, \
             patch.object(cli, "read_state", return_value={"status": "complete"}), patch("builtins.print"):
            self.assertTrue(cli.dispatch(cli.parse_args(["abandon-recovery"])))
        worker.assert_called_once_with("abandon-recovery", cli.LOG)
        prepare.assert_not_called()


if __name__ == "__main__":
    unittest.main()
