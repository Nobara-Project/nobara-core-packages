"""Early helper upgrades must precede planning, without bypassing job safety."""
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "src"
if "nobara_updater" not in sys.modules:
    package = types.ModuleType("nobara_updater")
    package.__path__ = [str(SOURCE)]
    sys.modules["nobara_updater"] = package
from nobara_updater import update_backend as backend, update_plan as planner
from nobara_updater.update_state import UpdateError, read_state, write_state, update_lock


class EarlyUpgradeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.trigger = self.root / "system-update"
        for patcher in (
            patch.object(backend, "TRIGGER", self.trigger),
            patch.object(backend, "maintain_btrfs_root", return_value=True),
            patch.object(backend, "check_installed_system"),
            patch.object(backend, "rpm_fingerprint", return_value="before"),
            patch.object(planner, "prepare_early_transaction", return_value={"empty": False, "package_origins": []}),
            patch.object(backend, "replay"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_helpers_upgrade_before_main_planning_and_test_before_install(self):
        calls = []
        def replay(job, *, test=False):
            calls.append(test)
            self.assertEqual(read_state(self.root)["started"], not test)
        with patch.object(backend, "prune_kernel_packages") as prune:
            backend.replay.side_effect = replay
            backend.upgrade_early(self.root)
        self.assertEqual(calls, [True, False])
        self.assertEqual(planner.prepare_early_transaction.call_args.args[1],
                         ("nobara-updater", "drm-awaiter"))
        prune.assert_not_called()
        self.assertEqual(read_state(self.root)["status"], "idle")

    def test_no_updates_never_replays_or_starts_installation(self):
        planner.prepare_early_transaction.return_value["empty"] = True
        backend.upgrade_early(self.root)
        backend.replay.assert_not_called()
        self.assertEqual(read_state(self.root)["status"], "idle")

    def test_repeated_preflight_failures_discard_old_payloads_but_keep_diagnostics(self):
        job = self.root / "jobs" / ("a" * 32)
        (job / "cache").mkdir(parents=True)
        (job / "diagnostics").mkdir()
        (job / "diagnostics/failure.log").write_text("original failure")
        planner.prepare_early_transaction.side_effect = UpdateError("repository unavailable")
        with self.assertRaises(UpdateError):
            backend.upgrade_early(self.root)
        self.assertFalse((job / "cache").exists())
        self.assertEqual((job / "diagnostics/failure.log").read_text(), "original failure")

    def test_no_package_changes_while_another_job_or_recovery_is_pending(self):
        for status, started in (("ready", False), ("scheduled", False), ("awaiting-boot", True),
                                ("installing-live", True), ("failed", True)):
            with self.subTest(status=status):
                write_state(self.root, dict(started=started), status)
                with self.assertRaises(UpdateError):
                    backend.upgrade_early(self.root)
        backend.replay.assert_not_called()

    def test_trigger_and_update_lock_block_early_changes(self):
        self.trigger.symlink_to(self.root / "missing-foreign-job")
        with self.assertRaisesRegex(UpdateError, "scheduled"):
            backend.upgrade_early(self.root)
        self.trigger.unlink()
        with update_lock(self.root), self.assertRaisesRegex(UpdateError, "operation is running"):
            backend.upgrade_early(self.root)
        backend.replay.assert_not_called()

    def test_unfinished_root_layout_repair_blocks_packages(self):
        backend.maintain_btrfs_root.return_value = False
        (self.root / "btrfs-root-layout.json").touch()
        with self.assertRaisesRegex(UpdateError, "root-layout repair"):
            backend.upgrade_early(self.root)
        backend.replay.assert_not_called()

    def test_installer_preserves_previous_kernel_confirmation(self):
        previous = dict(job="a" * 32, started=True, installer=True,
                        boot_selection={"kernel": "new-kernel"})
        write_state(self.root, previous, "installer-complete")
        with patch.object(backend, "in_installer_root", return_value=True):
            backend.upgrade_early(self.root, installer=True)
        self.assertEqual(read_state(self.root)["boot_selection"], previous["boot_selection"])
        self.assertEqual(read_state(self.root)["status"], "installer-complete")
        backend.maintain_btrfs_root.assert_not_called()

    def test_solver_failure_without_changes_can_be_retried(self):
        planner.prepare_early_transaction.side_effect = UpdateError("unsatisfied dependency")
        with self.assertRaisesRegex(UpdateError, "Early updater/helper upgrade failed"):
            backend.upgrade_early(self.root)
        self.assertFalse(read_state(self.root)["started"])
        self.assertEqual(read_state(self.root)["status"], "failed")
        planner.prepare_early_transaction.side_effect = None
        backend.upgrade_early(self.root)

    def test_partial_install_failure_requires_explicit_repair(self):
        backend.replay.side_effect = [None, UpdateError("RPM write failed")]
        backend.rpm_fingerprint.side_effect = ["before", "before", "after"]
        with self.assertRaises(UpdateError):
            backend.upgrade_early(self.root)
        self.assertTrue(read_state(self.root)["started"])
        with self.assertRaisesRegex(UpdateError, "retry-update"):
            backend.upgrade_early(self.root)
        self.assertEqual(backend.replay.call_count, 2)

    def test_abrupt_interruption_keeps_installation_marker(self):
        backend.replay.side_effect = [None, KeyboardInterrupt()]
        with self.assertRaises(KeyboardInterrupt):
            backend.upgrade_early(self.root)
        state = read_state(self.root)
        self.assertEqual(state["status"], "installing-live")
        self.assertTrue(state["started"])
        self.assertTrue(state["early_upgrade"])


class WorkerOrderingTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("early_worker_test", SOURCE / "update_worker.py")
        self.worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.worker)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        for patcher in (patch.object(self.worker, "STATE_DIR", Path(temp.name)),
                        patch.object(self.worker.os, "geteuid", return_value=0),
                        patch.object(self.worker.os, "umask"),
                        patch.object(self.worker.logging, "basicConfig"),
                        patch.object(self.worker, "RotatingFileHandler"),
                        patch.object(backend, "in_installer_root", return_value=True),
                        patch.dict(self.worker.os.environ),
                        patch.object(self.worker, "read_state", return_value={"status": "idle"}),
                        patch.object(self.worker, "write_state"),
                        patch.object(self.worker, "record_failure")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_every_prepare_entrypoint_restarts_worker_before_planning(self):
        for action in ("prepare", "prepare-codecs", "installer-update", "installer-codecs"):
            with self.subTest(action=action), patch.object(self.worker.sys, "argv", ["worker", action]), \
                 patch.object(backend, "upgrade_early") as upgrade, patch.object(self.worker.os, "execv") as restart, \
                 patch.object(backend, "prepare") as prepare, patch.object(backend, "install_in_target") as install:
                self.assertEqual(self.worker.main(), 0)
                upgrade.assert_called_once_with(installer=action.startswith("installer-"))
                self.assertEqual(restart.call_args.args[1], ["/usr/bin/python3", "-I",
                    "/usr/libexec/nobara-update-worker", action, "--early-upgrade-complete"])
                prepare.assert_not_called()
                install.assert_not_called()

    def test_selection_survives_early_upgrade_reexec_and_reaches_preparation(self):
        for early_done in (False, True):
            arguments = ["worker", "prepare", "--request", "a" * 32]
            if early_done:
                arguments.append("--early-upgrade-complete")
            with patch.object(self.worker.sys, "argv", arguments), \
                 patch.object(self.worker, "read_request", return_value=["editor"]) as request, \
                 patch.object(backend, "upgrade_early") as upgrade, patch.object(backend, "prepare") as prepare, \
                 patch.object(self.worker.os, "execv") as restart:
                self.assertEqual(self.worker.main(), 0)
                request.assert_called_once_with("a" * 32)
                if early_done:
                    prepare.assert_called_once_with(packages=["editor"])
                    upgrade.assert_not_called()
                else:
                    self.assertEqual(restart.call_args.args[1][-2:], ["--request", "a" * 32])
                    prepare.assert_not_called()

    def test_worker_cleans_selection_after_frontend_disconnect(self):
        request = self.worker.STATE_DIR / "requests" / ("b" * 32 + ".json")
        request.parent.mkdir()
        request.write_text('{"packages":["editor"]}')
        with patch.object(self.worker.sys, "argv", ["worker", "prepare", "--request", "b" * 32, "--early-upgrade-complete"]), \
             patch.object(self.worker, "read_request", return_value=["editor"]), \
             patch.object(backend, "prepare"):
            self.assertEqual(self.worker.main(), 0)
        self.assertFalse(request.exists())

    def test_fresh_interpreter_proceeds_without_upgrade_loop(self):
        with patch.object(self.worker.sys, "argv", ["worker", "prepare", "--early-upgrade-complete"]), \
             patch.object(backend, "upgrade_early") as upgrade, patch.object(backend, "prepare") as prepare:
            self.assertEqual(self.worker.main(), 0)
        upgrade.assert_not_called()
        prepare.assert_called_once_with()

    def test_early_failure_never_starts_main_preparation(self):
        with patch.object(self.worker.sys, "argv", ["worker", "prepare"]), \
             patch.object(backend, "upgrade_early", side_effect=UpdateError("fixture failure")), \
             patch.object(backend, "prepare") as prepare, patch.object(self.worker.os, "execv") as restart, \
             patch.object(self.worker.sys, "stderr", io.StringIO()):
            self.assertEqual(self.worker.main(), 1)
        prepare.assert_not_called()
        restart.assert_not_called()

    def test_offline_execution_and_confirmation_never_run_early_upgrades(self):
        for action, method in (("execute", "execute"), ("confirm", "confirm_boot"), ("cancel", "cancel")):
            with self.subTest(action=action), patch.object(self.worker.sys, "argv", ["worker", action]), \
                 patch.object(backend, "upgrade_early") as upgrade, patch.object(backend, method) as operation, \
                 patch.object(self.worker, "attach_log"):
                self.assertEqual(self.worker.main(), 0)
                upgrade.assert_not_called()
                operation.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
