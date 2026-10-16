"""Package hooks must not redirect recovery, or cause endless restart loops."""
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
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
from nobara_updater import update_backend as backend, update_recovery as recovery, update_state as state

JOB = "a" * 32
UUID = "12345678-1234-1234-1234-123456789abc"
SUBVOL = ".nobara-updater/" + JOB + "/root"
ENTRY = "nobara-recovery-" + JOB


class RecoveryFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.boot = self.root / "boot"
        self.entries = self.boot / "loader/entries"
        self.entries.mkdir(parents=True)
        (self.boot / "kernel").write_text("known good kernel")
        (self.boot / "initrd").write_text("known good initrd")
        self.normal = self.entries / "normal.conf"
        self.normal.write_text(f"title Nobara\nversion old-kernel\nlinux /kernel\ninitrd /initrd\n"
                               f"options root=UUID={UUID} rootflags=subvol=@,compress=zstd ro rd.luks.uuid=encrypted\n")
        self.layout = dict(created=True, uuid=UUID, entry_id=ENTRY, subvolume=SUBVOL)
        self.archive = self.boot / "nobara-updater" / JOB
        self.entry = self.entries / (ENTRY + ".conf")
        for patcher in (patch.object(recovery, "BOOT", self.boot),
                        patch.object(recovery, "grub_variables", return_value={}), patch.object(recovery.os, "sync")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.saved = recovery.recovery_entry(self.normal, JOB, SUBVOL)
        recovery.save_recovery_entry(self.layout, self.saved)

    def overwrite_options(self):
        # grub2-mkconfig's update_bls_cmdline writes normal-root options into
        # every BLS entry, including the fallback created before the RPM replay.
        for entry in self.entries.glob("*.conf"):
            entry.write_text(re.sub(r"(?m)^options .+$", f"options root=UUID={UUID} ro rootflags=subvol=@",
                                    entry.read_text()))


class RecoveryEntryTests(RecoveryFixture):
    def test_global_bls_rewrite_is_repaired_before_fallback_is_armed(self):
        self.overwrite_options()
        calls = []
        def command(args, **kwargs):
            self.assertEqual(self.entry.read_text(), self.saved)
            calls.append(args)
        with patch.object(recovery, "ensure_recovery_bootloader", side_effect=self.overwrite_options) as ensure, \
             patch.object(recovery.subprocess, "run", side_effect=command), \
             patch.object(recovery, "grub_environment", return_value={"nobara_fallback": ENTRY, "saved_entry": ENTRY}):
            recovery.select_recovery(self.layout)
        ensure.assert_called_once()
        self.assertEqual((self.archive / "recovery-entry.conf").read_text(), self.saved)
        self.assertIn("rd.luks.uuid=encrypted", self.entry.read_text())
        self.assertTrue(calls)
        self.assertTrue((self.boot / "nobara-updater/recovery-armed").exists())

    def test_missing_active_entry_is_restored_without_copying_updated_boot_images(self):
        self.entry.unlink()
        (self.boot / "kernel").write_text("updated kernel")
        recovery.restore_recovery_entry(self.layout)
        self.assertEqual(self.entry.read_text(), self.saved)
        self.assertEqual((self.archive / "linux-0-kernel").read_text(), "known good kernel")

    def test_legacy_entry_with_normal_root_is_repaired_using_recorded_snapshot(self):
        (self.archive / "recovery-entry.conf").unlink()
        self.overwrite_options()
        recovery.restore_recovery_entry(self.layout)
        options = self.entry.read_text().split("options ")[1]
        self.assertIn("rootflags=subvol=" + SUBVOL, options)
        self.assertNotIn("subvol=@", options)
        self.assertEqual((self.archive / "recovery-entry.conf").read_text(), self.entry.read_text())

    def test_invalid_private_copy_is_never_silently_replaced_from_modified_entry(self):
        saved = self.archive / "recovery-entry.conf"
        for text in (self.saved.replace(SUBVOL, "@"), self.saved.replace(UUID, "other"),
                     self.saved.replace("/nobara-updater/" + JOB + "/linux-0-kernel", "/kernel"),
                     self.saved + "options root=UUID=other\n"):
            with self.subTest(text=text):
                saved.write_text(text)
                with self.assertRaises(state.UpdateError):
                    recovery.restore_recovery_entry(self.layout)
                self.assertEqual(self.entry.read_text(), self.saved)

    def test_missing_archived_payload_is_not_replaced_with_installed_kernel(self):
        (self.archive / "linux-0-kernel").unlink()
        with self.assertRaisesRegex(state.UpdateError, "unavailable"):
            recovery.restore_recovery_entry(self.layout)

    def test_rejected_private_copy_cannot_be_booted_by_native_failure_handler(self):
        armed = self.boot / "nobara-updater/recovery-armed"
        state.atomic_json(armed, dict(entry=ENTRY))
        (self.archive / "recovery-entry.conf").write_text(self.saved.replace(SUBVOL, "@"))
        with patch.object(recovery, "ensure_recovery_bootloader"), \
             patch.object(recovery.subprocess, "run") as command, \
             self.assertRaises(state.UpdateError):
            recovery.select_recovery(self.layout)
        self.assertFalse(armed.exists())
        command.assert_not_called()

    def test_symlinked_copy_and_foreign_job_are_refused(self):
        saved = self.archive / "recovery-entry.conf"
        saved.unlink()
        saved.symlink_to(self.entry)
        with self.assertRaisesRegex(state.UpdateError, "symlink"):
            recovery.restore_recovery_entry(self.layout)
        with self.assertRaises(state.UpdateError):
            recovery.recovery_archive(dict(entry_id="../../etc/passwd"))

    def test_tabs_in_source_entry_do_not_leave_normal_root_in_fallback(self):
        self.normal.write_text(self.normal.read_text().replace("options ", "options\t"))
        text = recovery.recovery_entry(self.normal, "b" * 32, ".nobara-updater/" + "b" * 32 + "/root")
        self.assertNotIn("subvol=@", text)


class RestartGuardTests(RecoveryFixture):
    """Run the actual shell helper against temporary files and stub boot tools."""
    def setUp(self):
        super().setUp()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.calls = self.root / "calls"
        self.envfile = self.root / "grubenv.json"
        self.envfile.write_text(json.dumps(dict(saved_entry="normal", next_entry="normal", nobara_trial="normal")))
        stub = f"#!{sys.executable}\n" + """
import json, os, sys
from pathlib import Path
command = Path(sys.argv[0]).name
path = Path(os.environ['NB_TEST_ENV'])
environment = json.loads(path.read_text())
with open(os.environ['NB_TEST_CALLS'], 'a') as log:
    log.write(json.dumps([command, *sys.argv[1:]]) + '\\n')
if command == 'grub2-set-default':
    environment['saved_entry'] = sys.argv[-1]
elif command == 'grub2-editenv':
    action = sys.argv[2]
    if action == 'set':
        environment.update(item.split('=', 1) for item in sys.argv[3:])
    elif action == 'unset':
        for item in sys.argv[3:]: environment.pop(item, None)
    elif action == 'list':
        print('\\n'.join(k + '=' + v for k, v in environment.items()))
elif command == 'systemctl':
    assert sys.argv[1:] == ['--no-block', 'reboot']
    assert (Path(os.environ['NB_TEST_BOOT']) / 'nobara-updater' / os.environ['NB_TEST_JOB'] / 'reboot-attempted').is_dir()
else:
    raise AssertionError(command)
path.write_text(json.dumps(environment))
"""
        for command in ("grub2-set-default", "grub2-editenv", "systemctl"):
            tool = self.bin / command
            tool.write_text(stub)
            tool.chmod(0o755)
        self.environment = dict(os.environ, PATH=str(self.bin) + ":" + os.environ["PATH"],
                                NB_TEST_ENV=str(self.envfile), NB_TEST_CALLS=str(self.calls),
                                NB_TEST_BOOT=str(self.boot), NB_TEST_JOB=JOB)
        state.atomic_json(self.boot / "nobara-updater/recovery-armed", dict(entry=ENTRY))

    def guard(self):
        return subprocess.run(["bash", str(SOURCE.parent / "data/nobara-update-reboot"), str(self.boot)],
                              text=True, capture_output=True, env=self.environment)

    def reboot_calls(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines() if json.loads(line)[0] == "systemctl"] if self.calls.exists() else []

    def test_corrupted_fallback_is_restored_and_restarts_only_once_across_invocations(self):
        self.overwrite_options()
        first = self.guard()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(self.entry.read_text(), self.saved)
        self.assertEqual(json.loads(self.envfile.read_text()), dict(saved_entry=ENTRY, nobara_fallback=ENTRY))
        second = self.guard()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("Stopping repeated restarts", second.stderr)
        self.assertEqual(len(self.reboot_calls()), 1)

    def test_unarmed_or_missing_saved_entry_cannot_restart(self):
        (self.boot / "nobara-updater/recovery-armed").unlink()
        self.assertEqual(self.guard().returncode, 0)
        state.atomic_json(self.boot / "nobara-updater/recovery-armed", dict(entry=ENTRY))
        (self.archive / "recovery-entry.conf").unlink()
        self.assertNotEqual(self.guard().returncode, 0)
        self.assertEqual(self.reboot_calls(), [])
        self.assertFalse((self.archive / "reboot-attempted").exists())

    def test_symlinked_ticket_cannot_bypass_restart_limit(self):
        (self.archive / "reboot-attempted").symlink_to(self.root / "absent")
        self.assertEqual(self.guard().returncode, 0)
        self.assertEqual(self.reboot_calls(), [])

    def test_new_job_gets_its_own_restart_budget(self):
        self.assertEqual(self.guard().returncode, 0)
        job = "b" * 32
        text = recovery.recovery_entry(self.normal, job, ".nobara-updater/" + job + "/root")
        recovery.save_recovery_entry(dict(created=True, uuid=UUID, entry_id="nobara-recovery-" + job), text)
        state.atomic_json(self.boot / "nobara-updater/recovery-armed", dict(entry="nobara-recovery-" + job))
        self.environment["NB_TEST_JOB"] = job
        second = self.guard()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(len(self.reboot_calls()), 2)


class RecoveryLoopTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "state"
        self.boot = Path(temporary.name) / "boot"
        self.layout = dict(created=True, uuid=UUID, entry_id=ENTRY)
        self.saved = dict(job=JOB, started=True, recovery=self.layout)
        self.attempt = self.boot / "nobara-updater" / JOB / "reboot-attempted"
        for patcher in (patch.object(recovery, "BOOT", self.boot), patch.object(backend, "publish_failure"),
                        patch.object(backend, "select_recovery"), patch.object(backend, "announce"),
                        patch.object(backend, "SPLASH")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_initial_failure_still_arms_recovery(self):
        state.write_state(self.root, self.saved, "failed", error="wrong kernel")
        self.assertTrue(backend.recover(self.root))
        backend.select_recovery.assert_called_once_with(self.layout)
        self.assertEqual(state.read_state(self.root)["status"], "recovering")

    def test_second_failed_boot_stops_and_reports_without_calling_it_successful(self):
        state.write_state(self.root, self.saved, "recovering", error="wrong kernel")
        self.attempt.mkdir(parents=True)
        with patch.object(backend, "confirm_trial") as confirm, \
             patch.object(backend, "prune_recovery") as prune, \
             patch.object(backend, "run") as command:
            backend.confirm_boot(self.root)
            self.assertFalse(backend.recover(self.root))
        saved = state.read_state(self.root)
        self.assertEqual(saved["status"], "interrupted")
        self.assertTrue(saved["started"])
        self.assertTrue(saved["recovery_blocked"])
        backend.publish_failure.assert_called()
        backend.select_recovery.assert_not_called()
        confirm.assert_not_called()
        prune.assert_not_called()
        command.assert_not_called()

    def test_report_failure_cannot_restart_the_loop(self):
        state.write_state(self.root, self.saved, "recovering")
        self.attempt.mkdir(parents=True)
        backend.publish_failure.side_effect = OSError("report storage full")
        with self.assertLogs(backend.LOG, level="ERROR"):
            backend.confirm_boot(self.root)
        self.assertTrue(state.read_state(self.root)["recovery_blocked"])

    def test_actual_recovered_snapshot_can_confirm_after_restart_budget_was_used(self):
        marker = self.root / "marker.json"
        state.write_state(self.root, dict(job=JOB, started=False, recovery={},
                                         recovery_blocked=True, recovery_retry_ready=True), "scheduled")
        marker.write_text(json.dumps(dict(job=JOB, original_subvolume="/@")))
        self.attempt.mkdir(parents=True)
        native_path = Path
        def path(value):
            return marker if value == "/etc/nobara-updater-recovery.json" else native_path(value)
        with patch.object(backend, "Path", side_effect=path), \
             patch.object(backend, "synchronize_boot_root", return_value="normal"), \
             patch.object(backend, "confirm_trial") as confirm, \
             patch.object(backend, "publish_recovery", return_value={"error": "original failure"}), \
             patch.object(backend, "cleanup_confirmed_update"):
            backend.confirm_boot(self.root)
        saved = state.read_state(self.root)
        self.assertEqual(saved["status"], "recovered")
        self.assertFalse(saved["started"])
        self.assertFalse(saved["recovery_blocked"])
        self.assertFalse(saved["recovery_retry_ready"])
        confirm.assert_called_once_with("normal")
        self.assertFalse(marker.exists())

    def test_worker_queues_guard_only_when_recovery_requests_a_restart(self):
        spec = importlib.util.spec_from_file_location("guard_worker_test", SOURCE / "update_worker.py")
        worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(worker)
        for requested in (False, True):
            with patch.object(worker, "STATE_DIR", self.root), \
                 patch.object(worker.sys, "argv", ["worker", "recover"]), \
                 patch.object(worker.os, "geteuid", return_value=0), patch.object(worker.os, "umask"), \
                 patch.object(worker.logging, "basicConfig"), patch.object(worker, "RotatingFileHandler"), \
                 patch.object(worker, "read_state", return_value=self.saved), patch.object(worker, "attach_log"), \
                 patch.object(backend, "recover", return_value=requested), patch.object(backend, "run") as command:
                self.assertEqual(worker.main(), 0)
                if requested:
                    command.assert_called_once_with(["systemctl", "--no-block", "start", "nobara-updater-fallback-reboot.service"])
                else:
                    command.assert_not_called()
