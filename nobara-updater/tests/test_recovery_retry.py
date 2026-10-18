"""Retry a saved rollback without replaying RPMs or reopening a reboot loop."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from test_recovery_boot_guard import RecoveryFixture, JOB, UUID, ENTRY, SUBVOL, SOURCE
from nobara_updater import update_backend as backend, update_recovery as recovery
from nobara_updater import update_lvm as lvm, update_state as state, update_report as reports


class RetryFixture(RecoveryFixture):
    def setUp(self):
        super().setUp()
        self.state_root = self.root / "state"
        self.layout.update(backend="btrfs", fsroot="/@")
        self.failed = dict(job=JOB, started=True, recovery=self.layout, error="RPM replay interrupted",
                           recovery_blocked=True, files={"transaction.json": "checksum"})
        state.write_state(self.state_root, self.failed, "interrupted")
        (self.archive / "reboot-attempted").mkdir()
        for patcher in (patch.object(backend, "TRIGGER", self.root / "system-update"),
                        patch.object(backend, "in_installer_root", return_value=False),
                        patch.object(recovery, "validate_saved_recovery"),
                        patch.object(recovery, "ensure_recovery_bootloader"),
                        patch.object(recovery, "grub_environment", return_value={"nobara_fallback": ENTRY, "saved_entry": ENTRY}),
                        patch.object(recovery.subprocess, "run"),
                        patch.object(reports, "capture_boot_diagnostics"),
                        patch.object(backend, "publish_failure")):
            patcher.start()
            self.addCleanup(patcher.stop)


class RetryTests(RetryFixture):
    def test_retry_repairs_bls_selects_recovery_and_keeps_original_failure_and_ticket(self):
        self.overwrite_options()
        before = (self.state_root / "state.json").read_text()
        backend.retry_recovery(self.state_root)
        saved = state.read_state(self.state_root)
        self.assertEqual(self.entry.read_text(), self.saved)
        self.assertEqual(saved["status"], "interrupted")
        self.assertTrue(saved["started"])
        self.assertTrue(saved["recovery_blocked"])
        self.assertTrue(saved["recovery_retry_ready"])
        self.assertEqual(saved["error"], "RPM replay interrupted")
        self.assertEqual(saved["files"], self.failed["files"])
        self.assertTrue((self.archive / "reboot-attempted").is_dir())
        archived = self.state_root / "jobs" / JOB / "recovery-retry-state.json"
        self.assertEqual(json.loads(archived.read_text()), json.loads(before))
        calls = [call.args[0] for call in recovery.subprocess.run.call_args_list]
        self.assertTrue(any(c[0] == "lsinitrd" for c in calls))
        self.assertIn(["grub2-set-default", "--boot-directory=" + str(self.boot), ENTRY], calls)
        self.assertFalse(any(c[0] in {"dnf5", "rpm", "systemctl", "reboot"} for c in calls))
        backend.retry_recovery(self.state_root)
        self.assertEqual(json.loads(archived.read_text()), json.loads(before))
        with patch.object(backend, "select_recovery") as select:
            self.assertFalse(backend.recover(self.state_root))
            self.assertTrue(state.read_state(self.state_root)["recovery_retry_ready"])
            saved = state.read_state(self.state_root)
            state.write_state(self.state_root, saved, "interrupted", recovery_retry_boot_id="earlier-boot")
            backend.confirm_boot(self.state_root)
            self.assertFalse(backend.recover(self.state_root))
            select.assert_not_called()
        self.assertFalse(state.read_state(self.state_root)["recovery_retry_ready"])
        self.assertTrue(state.read_state(self.state_root)["started"])

    def test_rejected_snapshot_does_not_change_state_entry_or_grub(self):
        before = (self.state_root / "state.json").read_text()
        recovery.validate_saved_recovery.side_effect = state.UpdateError("Snapshot missing")
        with self.assertRaisesRegex(state.UpdateError, "Snapshot missing"):
            backend.retry_recovery(self.state_root)
        self.assertEqual((self.state_root / "state.json").read_text(), before)
        self.assertEqual(self.entry.read_text(), self.saved)
        recovery.subprocess.run.assert_not_called()
        backend.publish_failure.assert_called_once()

    def test_bad_archived_initramfs_never_selects_grub(self):
        recovery.subprocess.run.side_effect = subprocess.CalledProcessError(1, "lsinitrd")
        with self.assertRaisesRegex(state.UpdateError, "Cannot safely retry"):
            backend.retry_recovery(self.state_root)
        self.assertEqual([c.args[0][0] for c in recovery.subprocess.run.call_args_list], ["lsinitrd"])
        self.assertNotIn("recovery_retry_ready", state.read_state(self.state_root))

    def test_unverified_grub_selection_does_not_report_retry_ready(self):
        for environment in (dict(nobara_fallback=ENTRY), dict(nobara_fallback=ENTRY, saved_entry="wrong"),
                            dict(nobara_fallback=ENTRY, saved_entry=ENTRY, nobara_trial="wrong")):
            with self.subTest(environment=environment), \
                 patch.object(recovery, "grub_environment", return_value=environment), \
                 self.assertRaisesRegex(state.UpdateError, "Could not arm"):
                backend.retry_recovery(self.state_root)
            self.assertFalse(state.read_state(self.state_root).get("recovery_retry_ready"))

    def test_invalid_states_trigger_and_concurrent_operation_cannot_retry(self):
        for status, started, created in (("ready", False, True), ("complete", True, True),
                                         ("installing", True, True), ("failed", True, False)):
            with self.subTest(status=status):
                state.write_state(self.state_root, dict(self.failed, recovery=dict(self.layout, created=created)),
                                  status, started=started)
                with self.assertRaises(state.UpdateError):
                    backend.retry_recovery(self.state_root)
        state.write_state(self.state_root, self.failed, "interrupted")
        with state.update_lock(self.state_root), self.assertRaisesRegex(state.UpdateError, "Another Nobara"):
            backend.retry_recovery(self.state_root)
        backend.TRIGGER.symlink_to(self.root / "absent")
        with self.assertRaisesRegex(state.UpdateError, "trigger"):
            backend.retry_recovery(self.state_root)
        recovery.validate_saved_recovery.assert_not_called()
        recovery.subprocess.run.assert_not_called()

    def test_retry_from_installer_is_refused(self):
        with patch.object(backend, "in_installer_root", return_value=True), self.assertRaises(state.UpdateError):
            backend.retry_recovery(self.state_root)
        recovery.validate_saved_recovery.assert_not_called()

    def test_cli_requests_only_recovery_retry_and_reports_restart(self):
        spec = importlib.util.spec_from_file_location("recovery_retry_cli", SOURCE / "nobara_sync.py")
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        with patch.object(cli, "in_installer_root", return_value=False), \
             patch.object(cli, "worker_action", return_value=True) as worker, \
             patch.object(cli, "prepare_update") as prepare, \
             patch.object(cli, "read_state", return_value=dict(self.failed, recovery_retry_ready=True)), \
             patch("builtins.print"):
            self.assertTrue(cli.dispatch(cli.parse_args(["retry-recovery"])))
        worker.assert_called_once_with("retry-recovery", cli.LOG)
        prepare.assert_not_called()
        result = cli.result_message(dict(self.failed, recovery_retry_ready=True))
        self.assertTrue(result["reboot_required"])
        self.assertIn("restart normally", result["message"])


class SnapshotValidationTests(RecoveryFixture):
    def setUp(self):
        super().setUp()
        self.layout.update(backend="btrfs", fsroot="/@")
        self.top = self.root / "top"
        self.parent = self.top / ".nobara-updater" / JOB
        self.snapshot = self.parent / "root"
        (self.snapshot / "etc").mkdir(parents=True)
        state.atomic_json(self.parent / "owner.json", dict(owner="nobara-updater", job=JOB))
        state.atomic_json(self.parent / "origin.json", dict(version=1, job=JOB, uuid=UUID, fsroot="/@"))
        self.marker = self.snapshot / "etc/nobara-updater-recovery.json"
        state.atomic_json(self.marker, dict(job=JOB, original_subvolume="/@"))
        self.mount = dict(fstype="btrfs", uuid=UUID, fsroot="/@", source="/dev/mapper/luks-root")
        self.commands = []
        original_stat = Path.stat
        original_rmdir = Path.rmdir
        def separate_boot(path, *args, **kwargs):
            value = original_stat(path, *args, **kwargs)
            if path == self.boot:
                fields = list(value)
                fields[2] += 1
                return os.stat_result(fields)
            return value
        def output(command):
            self.commands.append(command)
            return json.dumps(dict(filesystems=[self.mount])) if command[0] == "findmnt" else "ro=false"
        for patcher in (patch.object(Path, "is_mount", lambda path: path == self.boot),
                        patch.object(Path, "stat", separate_boot),
                        patch.object(recovery, "output", side_effect=output),
                        patch.object(recovery.subprocess, "run"),
                        patch.object(recovery.tempfile, "mkdtemp", return_value=str(self.top)),
                        patch.object(Path, "rmdir", lambda path: None if path == self.top else original_rmdir(path))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_btrfs_snapshot_checked_read_only_and_unmounted(self):
        recovery.validate_saved_recovery(self.layout, JOB)
        calls = [call.args[0] for call in recovery.subprocess.run.call_args_list]
        self.assertEqual(calls[0], ["mount", "-t", "btrfs", "-o", "ro,subvolid=5", "UUID=" + UUID, str(self.top)])
        self.assertIn(["btrfs", "subvolume", "show", str(self.snapshot)], calls)
        self.assertEqual(calls[-1], ["umount", str(self.top)])

    def test_wrong_job_root_and_filesystem_are_rejected_before_mounting(self):
        for field, value in (("uuid", "other-uuid"), ("fsroot", "/@other"), ("fstype", "ext4")):
            with self.subTest(field=field), patch.dict(self.mount, {field: value}), self.assertRaises(state.UpdateError):
                recovery.validate_saved_recovery(self.layout, JOB)
        with self.assertRaises(state.UpdateError):
            recovery.validate_saved_recovery(self.layout, "b" * 32)
        recovery.subprocess.run.assert_not_called()

    def test_missing_foreign_and_symlinked_snapshot_markers_fail_and_unmount(self):
        for kind in ("missing", "wrong-job", "symlink"):
            with self.subTest(kind=kind):
                self.marker.unlink(missing_ok=True)
                if kind == "wrong-job":
                    state.atomic_json(self.marker, dict(job="b" * 32, original_subvolume="/@"))
                elif kind == "symlink":
                    self.marker.symlink_to(self.parent / "owner.json")
                with self.assertRaises((OSError, state.UpdateError)):
                    recovery.validate_saved_recovery(self.layout, JOB)
                self.assertEqual(recovery.subprocess.run.call_args.args[0], ["umount", str(self.top)])

    def test_regular_directory_cannot_substitute_for_snapshot(self):
        def command(args, **kwargs):
            if args[:3] == ["btrfs", "subvolume", "show"]:
                raise subprocess.CalledProcessError(1, args)
        recovery.subprocess.run.side_effect = command
        with self.assertRaises(subprocess.CalledProcessError):
            recovery.validate_saved_recovery(self.layout, JOB)
        self.assertEqual(recovery.subprocess.run.call_args.args[0], ["umount", str(self.top)])

    def test_failed_unmount_does_not_attempt_to_delete_snapshot_contents(self):
        def command(args, **kwargs):
            if args[0] == "umount":
                raise subprocess.CalledProcessError(1, args)
        recovery.subprocess.run.side_effect = command
        with patch.object(Path, "rmdir") as remove, self.assertRaises(subprocess.CalledProcessError):
            recovery.validate_saved_recovery(self.layout, JOB)
        remove.assert_not_called()
        self.assertTrue(self.marker.is_file())


class LvmRetryValidationTests(RecoveryFixture):
    def setUp(self):
        super().setUp()
        self.layout.update(backend="lvm", vg="nobara", lv="root", origin_path="/dev/nobara/root",
                           origin_uuid="origin-id", vg_uuid="vg-id", snapshot="nobara_rollback_" + JOB,
                           snapshot_uuid="snapshot-id", fstype="ext4")
        self.current = dict(source="/dev/nobara/root", fstype="ext4")
        self.origin = dict(vg_name="nobara", lv_name="root", lv_uuid="origin-id", vg_uuid="vg-id",
                           lv_path="/dev/nobara/root", segtype="linear", lv_attr="owi-a-s---", origin_uuid="", lv_tags="")
        self.snapshot = dict(self.origin, lv_name=self.layout["snapshot"], lv_uuid="snapshot-id", segtype="snapshot",
                             lv_attr="swi-a-s---", data_percent="2.5", origin_uuid="origin-id", lv_tags=lvm.SNAPSHOT_TAG)
        state.atomic_json(self.archive / "owner.json", dict(owner="nobara-updater", job=JOB, backend="lvm"))
        for patcher in (patch.object(lvm, "BOOT", self.boot),
                        patch.object(lvm, "inventory", return_value=[self.origin, self.snapshot])):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_both_lvm_filesystems_accept_intact_owned_snapshot(self):
        for fstype in ("ext4", "xfs"):
            for segment in ("linear", "snapshot"):
                with self.subTest(fstype=fstype, segment=segment), patch.dict(self.snapshot, segtype=segment):
                    lvm.validate_saved_snapshot(dict(self.layout, fstype=fstype), JOB, dict(self.current, fstype=fstype))

    def test_full_invalid_merging_foreign_and_replaced_snapshots_are_refused(self):
        for fields in (dict(data_percent="100.0"), dict(data_percent=""), dict(data_percent="NaN"),
                       dict(lv_attr="swi-I-s---"), dict(lv_attr="Swi-a-s---"), dict(lv_tags=""),
                       dict(lv_uuid="different-id"), dict(origin_uuid="foreign-id"), dict(segtype="thin")):
            with self.subTest(fields=fields), patch.dict(self.snapshot, fields), self.assertRaises(state.UpdateError):
                lvm.validate_saved_snapshot(self.layout, JOB, self.current)
        with patch.dict(self.origin, lv_attr="Owi-a-s---"), self.assertRaises(state.UpdateError):
            lvm.validate_saved_snapshot(self.layout, JOB, self.current)

    def test_linear_storage_does_not_bypass_snapshot_health_or_identity_checks(self):
        for fields in (dict(lv_attr="-wi-a-----"), dict(lv_attr="swi-a-t---"), dict(lv_attr="swi-a-s-p-"),
                       dict(lv_attr="swi-a-s--k"), dict(lv_attr="swi-X-s---"), dict(lv_attr="swi-m-s---"),
                       dict(lv_attr="swi-M-s---"), dict(lv_attr="swi-s-s---"), dict(lv_attr="swi-a"),
                       dict(data_percent="Infinity"), dict(data_percent="-1"), dict(data_percent="invalid"),
                       dict(lv_uuid="other"), dict(origin_uuid="other"), dict(lv_tags="")):
            with self.subTest(fields=fields), patch.dict(self.snapshot, dict(segtype="linear", **fields)), \
                 self.assertRaises(state.UpdateError):
                lvm.validate_saved_snapshot(self.layout, JOB, self.current)
        with patch.object(lvm, "inventory", return_value=[self.origin]), self.assertRaises(state.UpdateError):
            lvm.validate_saved_snapshot(self.layout, JOB, self.current)


if __name__ == "__main__":
    unittest.main()
