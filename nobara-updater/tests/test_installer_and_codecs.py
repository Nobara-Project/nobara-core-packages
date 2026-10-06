"""Installer-target and standalone codec-wizard regression coverage."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, Mock, patch

SOURCE = Path(__file__).resolve().parents[1] / "src"
if "nobara_updater" not in sys.modules:
    package = types.ModuleType("nobara_updater")
    package.__path__ = [str(SOURCE)]
    sys.modules["nobara_updater"] = package
from nobara_updater import update_backend as backend, update_state as state, update_plan as planner

spec = importlib.util.spec_from_file_location("codec_progress_test", SOURCE.parent / "data/nobara-codec-wizard/process.py")
wizard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wizard)


class CodecWizardTests(unittest.TestCase):
    def test_staging_requires_restart_and_never_claims_installed(self):
        success, heading, message = wizard.completion_result(0, {"status": "scheduled"})
        self.assertTrue(success)
        self.assertEqual(heading, "Restart required")
        self.assertIn("restart", message)
        self.assertNotIn("are installed", message)

    def test_failed_command_cannot_be_overridden_by_old_success_state(self):
        self.assertFalse(wizard.completion_result(1 << 8, {"status": "complete"})[0])
        self.assertFalse(wizard.completion_result(15, {"status": "complete"})[0])
        self.assertFalse(wizard.completion_result(0, {})[0])

    def test_up_to_date_codecs_need_no_restart(self):
        success, heading, message = wizard.completion_result(0, {"status": "unchanged"})
        self.assertTrue(success)
        self.assertEqual(heading, "Complete")
        self.assertIn("installed", message)


class CodecFixupTests(unittest.TestCase):
    def test_existing_opt_in_and_explicit_request_enable_repairs_without_opt_in_for_fresh_system(self):
        for enabled, names, requested, wanted, enable in [
            (False, [], False, False, False),
            (False, ["mesa-libgallium-freeworld"], False, True, True),
            (True, ["libavcodec-free"], False, True, False),
            (False, [], True, True, True),
        ]:
            with self.subTest(enabled=enabled, names=names, requested=requested):
                base = Mock()
                repo = Mock()
                repo.get_id.return_value = "nobara-pikaos-additional"
                repo.get_config.return_value.get_baseurl_option.return_value.get_value.return_value = []
                option = repo.get_config.return_value.get_enabled_option.return_value
                option.get_value.return_value = enabled
                with patch.object(planner.base_api, "Base", return_value=base), \
                     patch.object(planner, "collect_package_origins", return_value=[]), \
                     patch.object(planner.rpm_api, "PackageQuery", return_value=MagicMock()), \
                     patch.object(planner.repo_api, "RepoQuery", return_value=[repo]), \
                     patch.object(planner.subprocess, "run", return_value=Mock(stdout="\n".join(names))):
                    result = planner.configure_base(Path("/unused-test-job"), "44", codecs=requested)
                self.assertEqual(result.nobara_codecs, wanted)
                self.assertEqual(result.nobara_enable_codecs, enable)
                if enable:
                    option.set.assert_called_once_with(True)
                else:
                    option.set.assert_not_called()


class InstallerTargetTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.job = self.root / "jobs" / ("a" * 32)
        self.job.mkdir(parents=True)
        (self.job / "transaction.json").write_text("{}")
        self.ready = dict(job="a" * 32, installer=True, started=False, fingerprint="fixture", target_release="44",
                          files={"transaction.json": state.file_digest(self.job / "transaction.json")},
                          migrations={"hooks": []}, packages=[])
        state.write_state(self.root, dict(self.ready), "ready")
        self.addCleanup(patch.stopall)
        patch.object(backend, "TRIGGER", self.root / "system-update").start()
        patch.object(backend, "in_installer_root", return_value=True).start()
        patch.object(backend, "rpm_fingerprint", return_value="fixture").start()
        patch.object(backend, "check_installed_system").start()

    def test_target_install_replays_once_then_reexecs_validation_without_services_or_snapshot(self):
        with patch.object(backend, "prepare") as prepare, patch.object(backend, "run") as run, \
             patch.object(backend, "create_recovery") as snapshot, patch.object(backend, "announce") as announce, \
             patch.object(backend.os, "execv") as execute:
            backend.install_in_target(self.root, codecs=True)
        prepare.assert_called_once_with(self.root, codecs=True, installer=True)
        self.assertEqual(run.call_count, 1)
        self.assertIn("replay", run.call_args.args[0])
        snapshot.assert_not_called()
        announce.assert_not_called()
        self.assertFalse((self.root / "system-update").exists())
        self.assertEqual(execute.call_args.args[1][-1], "installer-finalize")
        self.assertEqual(state.read_state(self.root)["status"], "validating")

    def test_target_install_failure_is_not_retried_and_keeps_started_marker(self):
        with patch.object(backend, "prepare"), \
             patch.object(backend, "run", side_effect=state.UpdateError("RPM failed")) as run, \
             patch.object(backend.os, "execv") as execute:
            with self.assertRaisesRegex(state.UpdateError, "RPM failed"):
                backend.install_in_target(self.root)
        self.assertEqual(run.call_count, 1)
        execute.assert_not_called()
        self.assertTrue(state.read_state(self.root)["started"])

    def test_desktop_root_cannot_use_installer_execution(self):
        with patch.object(backend, "in_installer_root", return_value=False), \
             patch.object(backend, "prepare") as prepare, patch.object(backend, "run") as run:
            with self.assertRaisesRegex(state.UpdateError, "actual chroot"):
                backend.install_in_target(self.root)
        prepare.assert_not_called()
        run.assert_not_called()

    def test_installer_jobs_cannot_be_scheduled_for_desktop_replay(self):
        with self.assertRaisesRegex(state.UpdateError, "installer transaction"):
            backend.schedule(self.root)
        self.assertFalse((self.root / "system-update").exists())

    def test_target_validation_returns_to_installer_and_allows_second_codec_command(self):
        state.write_state(self.root, dict(self.ready), "validating", started=True)
        with patch.object(backend, "run") as run, patch.object(backend, "apply_hooks"), \
             patch.object(backend, "validate_boot"), patch.object(backend, "os_release", return_value="44"), \
             patch.object(backend, "announce") as announce, patch.object(backend, "SPLASH") as splash:
            backend.finalize(self.root, installer=True)
        self.assertEqual(state.read_state(self.root)["status"], "installer-complete")
        announce.assert_not_called()
        splash.start.assert_not_called()
        self.assertEqual([call.args[0] for call in run.call_args_list], [["dnf5", "--disable-repo=*", "check", "--dependencies", "--duplicates"]])
        # Calamares next invokes install-codecs. It must not be blocked by a
        # pending desktop reboot or the first installer's started marker.
        with patch.object(planner, "prepare_transaction", return_value={"empty": True, "codecs": True}), \
             patch.object(backend, "probe_recovery") as probe:
            with patch.object(backend, "replay") as replay:
                backend.install_in_target(self.root, codecs=True)
        replay.assert_not_called()
        self.assertEqual(state.read_state(self.root)["status"], "installer-complete")
        self.assertEqual(state.read_state(self.root)["target_release"], "44")
        self.assertTrue(state.read_state(self.root)["codecs"])
        probe.assert_not_called()

    def test_service_enable_hooks_write_target_files_without_contacting_pid_one(self):
        with patch.object(backend, "run") as run:
            backend.apply_hooks(["enable-falcond"])
        run.assert_called_once_with(["systemctl", "--root=/", "enable", "falcond.service"])

    def test_plymouth_payload_repair_rebuilds_boot_images_during_finalization(self):
        ready = dict(self.ready, migrations={"hooks": ["plymouth-rebuild"]})
        state.write_state(self.root, ready, "validating", started=True)
        with patch.object(backend, "run"), patch.object(backend, "os_release", return_value="44"), \
             patch.object(backend, "validate_boot") as validate:
            backend.finalize(self.root, installer=True)
        validate.assert_called_once_with([], rebuild_all=True, preserved_kernels=[])

    def test_chroot_detection_uses_explicit_probe_and_rejects_probe_errors(self):
        with patch.object(state.subprocess, "run", return_value=Mock(returncode=0, stderr="")) as run:
            self.assertTrue(state.in_installer_root())
            run.assert_called_once_with(["systemd-detect-virt", "--chroot"], capture_output=True, text=True)
        with patch.object(state.subprocess, "run", return_value=Mock(returncode=1, stderr="")):
            self.assertFalse(state.in_installer_root())
        with patch.object(state.subprocess, "run", return_value=Mock(returncode=2, stderr="probe failed")):
            with self.assertRaises(state.UpdateError):
                state.in_installer_root()


if __name__ == "__main__":
    unittest.main()
