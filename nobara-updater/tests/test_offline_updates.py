from __future__ import annotations

import json
import io
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

SOURCE = Path(__file__).resolve().parents[1] / "src"
if "nobara_updater" not in sys.modules:
    package = types.ModuleType("nobara_updater")
    package.__path__ = [str(SOURCE)]
    sys.modules["nobara_updater"] = package

from nobara_updater import update_backend as backend
from nobara_updater.update_migrations import add_codec_migration, plan_migrations
from nobara_updater.update_plan import validate_removals
from nobara_updater.update_recovery import replace_subvolume
from nobara_updater.update_state import (UpdateError, atomic_json, file_digest, inventory_digest,
                                         os_release, read_state, update_lock, verify_files, write_state)


def package(name, arch="x86_64", **fields):
    return dict(name=name, arch=arch, **fields)


class MigrationTests(unittest.TestCase):
    def test_rpmfusion_repo_packages_do_not_trigger_fixups(self):
        installed = [package(f"rpmfusion-{family}-release{suffix}", "noarch")
                     for family in ("free", "nonfree") for suffix in ("", "-tainted", "-rawhide")]
        self.assertEqual(plan_migrations(installed).as_dict(), plan_migrations([]).as_dict())

    def test_login_replacement_is_one_plan_with_deferred_config(self):
        result = plan_migrations([package("sddm"), package("sddm-wayland-plasma"), package("kde-settings-sddm")])
        self.assertTrue({"sddm", "sddm-wayland-plasma", "kde-settings-sddm"} <= result.remove)
        self.assertIn("plasma-login-manager", result.install)
        self.assertIn("plasma-login", result.hooks)

    def test_login_migration_cleans_leftover_settings_after_plasma_login_install(self):
        result = plan_migrations([package("plasma-login-manager"), package("kde-settings-sddm")])
        self.assertIn("kde-settings-sddm", result.remove)
        self.assertNotIn("plasma-login", result.hooks)

    def test_sddm_settings_alone_do_not_trigger_login_migration(self):
        result = plan_migrations([package("kde-settings-sddm")])
        self.assertNotIn("kde-settings-sddm", result.remove)
        self.assertNotIn("plasma-login-manager", result.install)

    def test_nvidia_same_name_packages_are_not_removed(self):
        result = plan_migrations([package("akmod-nvidia"), package("nvidia-driver", epoch="4")], nvidia_closed=True)
        self.assertIn("akmod-nvidia", result.remove)
        self.assertNotIn("nvidia-driver", result.remove)
        self.assertIn("dkms-nvidia", result.install)
        self.assertIn("nvidia-closed", result.hooks)

    def test_codec_transition_preserves_architecture_and_has_no_erase_reinstall_overlap(self):
        installed = [package("mesa-libgallium"), package("mesa-vulkan-drivers-git"), package("ffmpeg-libs", "i686")]
        plan = plan_migrations(installed)
        add_codec_migration(plan, installed)
        self.assertIn("mesa-libgallium.x86_64", plan.remove)
        self.assertNotIn("mesa-libgallium.i686", plan.remove)
        self.assertIn("mesa-vulkan-drivers-git-freeworld.x86_64", plan.install)
        self.assertIn("libavcodec-free.i686", plan.install)
        self.assertFalse(plan.remove & plan.install)

    def test_essential_or_unplanned_removal_blocks_preparation(self):
        with self.assertRaisesRegex(UpdateError, "essential"):
            validate_removals([dict(name="systemd", arch="x86_64", action="Remove")], {"systemd"})
        with self.assertRaisesRegex(UpdateError, "also remove"):
            validate_removals([dict(name="steam", arch="x86_64", action="Remove")], {"sddm"})
        validate_removals([dict(name="sddm", arch="x86_64", action="Remove")], {"sddm"})
        with self.assertRaisesRegex(UpdateError, "essential"):
            validate_removals([dict(name="systemd", arch="x86_64", action="Replaced")], set())


class StateTests(unittest.TestCase):
    def test_prepare_leaves_dependency_repair_and_validation_to_planner(self):
        with patch.object(backend, "run") as run:
            backend.check_installed_system(preparing=True)
        run.assert_called_once_with(["rpm", "--verifydb"])

    def test_retry_still_requires_installed_system_to_be_healthy(self):
        with patch.object(backend, "run") as run:
            backend.check_installed_system()
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            ["rpm", "--verifydb"], ["dnf5", "--disable-repo=*", "check", "--dependencies", "--duplicates"]])

    def test_scheduling_checks_service_load_and_target_membership(self):
        for load, wants, valid in (("loaded", "nobara-updater-offline.service other.service", True),
                                   ("masked", "nobara-updater-offline.service", False),
                                   ("loaded", "other.service", False)):
            with self.subTest(load=load, wants=wants), patch.object(backend.subprocess, "run",
                    side_effect=[Mock(stdout=load), Mock(stdout=wants)]):
                if valid:
                    backend.check_offline_service()
                else:
                    with self.assertRaisesRegex(UpdateError, "offline update service"):
                        backend.check_offline_service()

    def test_installed_release_is_parsed_as_a_value(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "os-release"
            path.write_text('NAME=Nobara\nVERSION_ID="44"\n')
            self.assertEqual(os_release(path), "44")
            path.write_text("NAME=Nobara\n")
            with self.assertRaises(UpdateError):
                os_release(path)

    def test_state_is_durable_and_private_and_lock_is_exclusive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = write_state(root, {}, "ready", job="a" * 32)
            self.assertEqual(read_state(root), state)
            self.assertEqual((root / "state.json").stat().st_mode & 0o777, 0o600)
            with update_lock(root):
                with self.assertRaises(UpdateError):
                    with update_lock(root):
                        pass

    def test_inventory_fingerprint_ignores_order_but_not_version_changes(self):
        self.assertEqual(inventory_digest("a\nb\n"), inventory_digest("b\na\n"))
        self.assertNotEqual(inventory_digest("a-1\n"), inventory_digest("a-2\n"))

    def test_integrity_rejects_tampering_and_path_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "transaction.json"
            path.write_text("{}")
            files = {"transaction.json": file_digest(path)}
            verify_files(root, files)
            path.write_text("changed")
            with self.assertRaises(UpdateError):
                verify_files(root, files)
            with self.assertRaises(UpdateError):
                verify_files(root, {"transaction.json": file_digest(path), "../escape": "anything"})

    def test_recovery_options_replace_subvol_and_subvolid_without_losing_encryption(self):
        options = replace_subvolume("root=UUID=abc ro rootflags=subvol=root,subvolid=256,compress=zstd rd.luks.uuid=xyz", ".nobara-updater/a/root")
        self.assertNotIn("subvolid=", options)
        self.assertNotIn("subvol=root", options)
        self.assertIn("compress=zstd", options)
        self.assertIn("rd.luks.uuid=xyz", options)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.trigger = self.root / "system-update"
        self.job = self.root / "jobs" / ("a" * 32)
        self.job.mkdir(parents=True)
        (self.job / "transaction.json").write_text("{}")
        self.state = dict(job="a" * 32, fingerprint="original", started=False,
                          files={"transaction.json": file_digest(self.job / "transaction.json")},
                          recovery={"available": False, "reason": "test"})
        write_state(self.root, self.state, "ready")
        self.addCleanup(patch.stopall)
        patch.object(backend, "TRIGGER", self.trigger).start()
        patch.object(backend, "rpm_fingerprint", return_value="original").start()
        patch.object(backend, "announce").start()
        patch.object(backend, "check_offline_service").start()
        patch.object(backend, "arm_report").start()
        patch.object(backend, "publish_failure").start()
        patch.object(backend, "check_installed_system").start()

    def test_repaired_failed_installation_can_prepare_fresh_without_replaying_old_job(self):
        write_state(self.root, self.state, "interrupted", started=True, error="old failure")
        with patch.object(backend, "replay") as replay:
            backend.retry_failed(self.root)
            replay.assert_not_called()
        archived = json.loads((self.job / "failed-state.json").read_text())
        self.assertEqual(archived["error"], "old failure")
        self.assertTrue(archived["started"])
        self.assertFalse(read_state(self.root)["started"])
        with patch("nobara_updater.update_plan.prepare_transaction", return_value={"empty": True}):
            backend.prepare(self.root)
        self.assertNotEqual(read_state(self.root)["job"], archived["job"])

    def test_retry_keeps_failure_if_repairs_are_incomplete(self):
        write_state(self.root, self.state, "failed", started=True)
        before = read_state(self.root)
        with patch.object(backend, "check_installed_system", side_effect=UpdateError("broken dependencies")):
            with self.assertRaisesRegex(UpdateError, "broken dependencies"):
                backend.retry_failed(self.root)
        self.assertEqual(read_state(self.root), before)

    def test_retry_refuses_active_update_snapshot_recovery_and_boot_trigger(self):
        for status, recovery, trigger in (("installing", False, False), ("failed", True, False), ("failed", False, True)):
            with self.subTest(status=status, recovery=recovery, trigger=trigger):
                write_state(self.root, self.state, status, started=True, recovery={"created": recovery})
                if trigger:
                    self.trigger.symlink_to(self.root)
                with self.assertRaises(UpdateError):
                    backend.retry_failed(self.root)
                self.assertTrue(read_state(self.root)["started"])

    def test_corrupt_rpm_database_stops_before_transaction_preparation(self):
        write_state(self.root, self.state, "idle")
        with patch.object(backend, "check_installed_system", side_effect=UpdateError("RPM database is corrupt")), \
             patch("nobara_updater.update_plan.prepare_transaction") as prepare:
            with self.assertRaisesRegex(UpdateError, "RPM database"):
                backend.prepare(self.root)
            prepare.assert_not_called()
        self.assertFalse(read_state(self.root)["started"])

    def test_lost_trigger_is_recreated_only_after_validation(self):
        write_state(self.root, self.state, "scheduled")
        self.assertFalse(self.trigger.exists())
        backend.refresh_pending(self.root)
        self.assertEqual(read_state(self.root)["status"], "ready")
        backend.schedule(self.root)
        self.assertEqual(self.trigger.resolve(), self.root)
        self.assertEqual(read_state(self.root)["status"], "scheduled")

    def test_changed_pending_plan_is_invalidated_and_its_trigger_removed(self):
        self.trigger.symlink_to(self.root)
        write_state(self.root, self.state, "scheduled")
        with patch.object(backend, "rpm_fingerprint", return_value="changed"):
            backend.refresh_pending(self.root)
        self.assertEqual(read_state(self.root)["status"], "failed")
        self.assertFalse(self.trigger.is_symlink())

    def test_invalidating_stale_plan_does_not_remove_foreign_trigger(self):
        foreign = self.root / "foreign"
        self.trigger.symlink_to(foreign)
        write_state(self.root, self.state, "scheduled")
        with patch.object(backend, "rpm_fingerprint", return_value="changed"):
            backend.refresh_pending(self.root)
        self.assertEqual(self.trigger.readlink(), foreign)

    def test_corrupt_scheduled_payload_cannot_leave_an_armed_trigger(self):
        self.trigger.symlink_to(self.root)
        write_state(self.root, self.state, "scheduled")
        (self.job / "transaction.json").write_text("tampered")
        with self.assertRaisesRegex(UpdateError, "missing or changed"):
            backend.schedule(self.root)
        self.assertFalse(self.trigger.is_symlink())
        self.assertEqual(read_state(self.root)["status"], "failed")

    def test_refresh_never_invalidates_or_retries_an_installation_that_started(self):
        write_state(self.root, self.state, "installing", started=True)
        with patch.object(backend, "rpm_fingerprint") as fingerprint:
            backend.refresh_pending(self.root)
        fingerprint.assert_not_called()
        self.assertEqual(read_state(self.root)["status"], "installing")

    def test_normal_boot_detects_a_missed_scheduled_update(self):
        write_state(self.root, self.state, "scheduled")
        backend.confirm_boot(self.root)
        self.assertEqual(read_state(self.root)["status"], "ready")

    def test_readonly_status_does_not_claim_missing_trigger_is_scheduled(self):
        from nobara_updater import update_state
        write_state(self.root, self.state, "scheduled")
        with patch.object(update_state, "TRIGGER", self.trigger):
            reported = update_state.read_status(self.root)
        self.assertEqual(reported["status"], "ready")
        self.assertIn("trigger", update_state.status_message(reported))
        self.assertEqual(read_state(self.root)["status"], "scheduled")

    def test_changed_rpmdb_never_schedules_installation(self):
        with patch.object(backend, "rpm_fingerprint", return_value="changed"):
            with self.assertRaises(UpdateError):
                backend.schedule(self.root)
        self.assertFalse(self.trigger.is_symlink())
        self.assertEqual(read_state(self.root)["status"], "failed")

    def test_expected_preparation_error_is_recorded_without_retry_or_traceback(self):
        write_state(self.root, self.state, "idle")
        with patch("nobara_updater.update_plan.prepare_transaction", side_effect=UpdateError("Package conflict")) as prepare, \
             self.assertLogs(backend.LOG, level="ERROR") as logs:
            with self.assertRaisesRegex(UpdateError, "Package conflict"):
                backend.prepare(self.root)
        prepare.assert_called_once()
        self.assertIn("Package conflict", "\n".join(logs.output))
        self.assertNotIn("Traceback", "\n".join(logs.output))
        state = read_state(self.root)
        self.assertEqual(state["status"], "failed")
        self.assertFalse(state["started"])

    def test_does_not_replace_another_updaters_trigger(self):
        other = self.root / "other"
        other.mkdir()
        self.trigger.symlink_to(other)
        with self.assertRaises(UpdateError):
            backend.schedule(self.root)
        self.assertEqual(self.trigger.resolve(), other)

    def test_native_preflight_failure_never_marks_update_ready_or_schedules_boot(self):
        write_state(self.root, self.state, "idle")
        with patch("nobara_updater.update_plan.prepare_transaction", return_value={"empty": False}), \
             patch.object(backend, "replay", side_effect=UpdateError("local replay failed")) as replay:
            with self.assertRaisesRegex(UpdateError, "local replay failed"):
                backend.prepare(self.root)
        self.assertTrue(replay.call_args.kwargs["test"])
        self.assertEqual(read_state(self.root)["status"], "failed")
        self.assertFalse(read_state(self.root)["started"])
        self.assertFalse(self.trigger.is_symlink())

    def test_schedule_and_cancel_do_not_install_packages(self):
        with patch.object(backend, "run") as run:
            backend.schedule(self.root)
            self.assertEqual(read_state(self.root)["status"], "scheduled")
            backend.cancel(self.root)
            self.assertFalse(self.trigger.is_symlink())
            run.assert_not_called()

    def test_tampered_payload_cannot_start_installation(self):
        self.trigger.symlink_to(self.root)
        (self.job / "transaction.json").write_text("tampered")
        with patch.object(backend, "run") as run, patch.object(backend, "create_recovery") as snapshot:
            with self.assertRaises(UpdateError):
                backend.execute(self.root)
            run.assert_not_called()
            snapshot.assert_not_called()

    def test_install_failure_is_recorded_before_replay_and_is_not_retried(self):
        self.trigger.symlink_to(self.root)
        with patch.object(backend, "run", side_effect=UpdateError("RPM failure")) as run, \
             patch.object(backend, "create_recovery", return_value={"created": True}), \
             patch.object(backend, "trial_boot"), patch.object(Path, "touch"):
            with self.assertRaisesRegex(UpdateError, "RPM failure"):
                backend.execute(self.root)
        state = read_state(self.root)
        self.assertEqual(state["status"], "installing")
        self.assertTrue(state["started"])
        self.assertEqual(run.call_count, 1)
        self.assertIn("--setopt=reposdir=", run.call_args.args[0])
        with self.assertRaises(UpdateError):
            backend.cancel(self.root)

    def test_interrupted_install_is_not_replayed(self):
        write_state(self.root, self.state, "installing", started=True)
        self.trigger.symlink_to(self.root)
        with patch.object(backend, "run") as run:
            with self.assertRaises(UpdateError):
                backend.execute(self.root)
            run.assert_not_called()

    def test_bootable_interrupted_system_without_snapshot_reports_failure_without_replaying(self):
        write_state(self.root, self.state, "installing", started=True)
        with patch.object(backend, "run") as run, patch.object(backend, "publish_failure") as report:
            backend.confirm_boot(self.root)
        saved = read_state(self.root)
        self.assertEqual(saved["status"], "interrupted")
        self.assertTrue(saved["started"])
        report.assert_called_once()
        run.assert_not_called()

    def test_interrupted_system_with_snapshot_still_triggers_rollback(self):
        write_state(self.root, self.state, "installing", started=True, recovery={"created": True})
        with self.assertRaisesRegex(UpdateError, "did not finish"):
            backend.confirm_boot(self.root)

    def test_recovery_requires_actual_saved_system_not_dnf_history(self):
        write_state(self.root, self.state, "failed", started=True)
        with patch.object(backend, "select_recovery") as select:
            with self.assertRaises(UpdateError):
                backend.recover(self.root)
            select.assert_not_called()
        self.assertEqual(read_state(self.root)["status"], "interrupted")

    def test_zero_exit_does_not_hide_noncritical_rpm_scriptlet_failure(self):
        process = Mock(stdout=io.StringIO("Non-critical error in %post scriptlet: fixture-1-1.x86_64\n"), returncode=0)
        process.wait.return_value = 0
        with patch.object(backend.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(UpdateError, "RPM reported installation errors"):
                backend.run(["dnf5", "replay", str(self.job)])

    def test_nonzero_exit_keeps_scriptlet_failure_before_unrelated_trailing_output(self):
        output = "[RPM] %posttrans(fixture-driver-1-1.x86_64) scriptlet failed, exit status 10\n"
        output += "Finished another package scriptlet\n" * 10
        output += "Transaction failed: Rpm transaction failed.\n"
        process = Mock(stdout=io.StringIO(output), returncode=1)
        process.wait.return_value = 1
        with patch.object(backend.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(UpdateError, "fixture-driver-1-1.x86_64"):
                backend.run(["dnf5", "replay", str(self.job)])

    def test_module_failure_stops_before_building_initramfs(self):
        packages = [dict(name="kernel-core", action="Install", nevra="kernel-core-1-1.x86_64")]
        result = Mock(stdout="1-1.x86_64\n", returncode=0)
        with patch.object(backend.subprocess, "run", return_value=result), \
             patch.object(backend.shutil, "which", side_effect=lambda name: "/usr/bin/akmods" if name == "akmods" else None), \
             patch.object(backend, "run", side_effect=UpdateError("module build failed")) as run:
            with self.assertRaisesRegex(UpdateError, "module build failed"):
                backend.validate_boot(packages)
        run.assert_called_once_with(["akmods", "--force", "--kernels", "1-1.x86_64"])


if __name__ == "__main__":
    unittest.main()
