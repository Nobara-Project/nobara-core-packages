"""CLI and service-client contract tests, without authentication or services."""
import contextlib
import importlib.util
import io
import json
import logging
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch, Mock

SOURCE = Path(__file__).resolve().parents[1] / "src"
if "nobara_updater" not in sys.modules:
    package = types.ModuleType("nobara_updater")
    package.__path__ = [str(SOURCE)]
    sys.modules["nobara_updater"] = package
spec = importlib.util.spec_from_file_location("nobara_cli_test", SOURCE / "nobara_sync.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)
from nobara_updater import update_client as client


class CliTests(unittest.TestCase):
    def setUp(self):
        detector = patch.object(cli, "in_installer_root", return_value=False)
        detector.start()
        self.addCleanup(detector.stop)
        # Never write the real proxy handover if the test runner uses a proxy.
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        handover = patch.object(client, "PROXY_FILE", Path(temp.name) / "proxy.json", create=True)
        handover.start()
        self.addCleanup(handover.stop)

    def test_app_center_invocations_and_legacy_username_are_supported(self):
        self.assertEqual(cli.parse_args([]).command, "cli")
        self.assertFalse(cli.parse_args(["cli"]).all)
        self.assertTrue(cli.parse_args(["cli", "--all"]).all)
        args = cli.parse_args(["alice", "--all"])
        self.assertEqual(args.username, "alice")
        self.assertEqual(args.command, "cli")

    def test_offline_report_export_needs_no_root_or_network(self):
        from nobara_updater import update_report
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "original.tar.gz"
            source.write_bytes(b"report fixture")
            target = Path(directory) / "saved.tar.gz"
            with patch.object(update_report, "latest_report", return_value={"bundle": str(source)}), \
                 patch.object(update_report, "upload_report") as upload, patch.object(cli, "elevate") as elevate, \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(["recovery-report", "--save", str(target)]), 0)
                self.assertEqual(cli.main(["recovery-report", "--save", str(target)]), 1)
            self.assertEqual(target.read_bytes(), b"report fixture")
            upload.assert_not_called()
            elevate.assert_not_called()

    def test_default_cli_prepares_then_schedules_without_reboot_or_flatpak(self):
        states = [dict(status="ready"), dict(status="scheduled")]
        with patch.object(cli, "prepare_update", return_value=True), \
             patch.object(cli, "read_state", side_effect=states), \
             patch.object(cli, "worker_action", return_value=True) as worker, \
             patch.object(cli, "update_flatpaks") as flatpak, \
             patch.object(cli, "original_user", return_value=None), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertTrue(cli.dispatch(cli.parse_args(["cli"])))
        worker.assert_called_once_with("schedule", cli.LOG)
        flatpak.assert_not_called()
        result = json.loads(output.getvalue().removeprefix(cli.RESULT_PREFIX))
        self.assertEqual(result["status"], "scheduled")
        self.assertTrue(result["reboot_required"])

    def test_retry_validates_repair_before_preparing_and_scheduling(self):
        with patch.object(cli, "worker_action", return_value=True) as worker, \
             patch.object(cli, "prepare_update", return_value=True) as prepare, \
             patch.object(cli, "read_state", side_effect=[dict(status="ready"), dict(status="scheduled")]), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(cli.dispatch(cli.parse_args(["retry-update"])))
        self.assertEqual([call.args[0] for call in worker.call_args_list], ["retry", "schedule"])
        prepare.assert_called_once()

    def test_failed_repair_validation_does_not_start_a_new_preparation(self):
        with patch.object(cli, "worker_action", return_value=False) as worker, \
             patch.object(cli, "prepare_update") as prepare:
            self.assertFalse(cli.dispatch(cli.parse_args(["retry-update"])))
        worker.assert_called_once_with("retry", cli.LOG)
        prepare.assert_not_called()

    def test_preparation_failure_is_nonzero_and_does_not_schedule(self):
        with patch.object(cli, "elevate"), patch.object(cli, "initialize_logging"), \
             patch.object(cli, "prepare_update", return_value=False), \
             patch.object(cli, "worker_action") as worker:
            self.assertEqual(cli.main(["cli"]), 1)
        worker.assert_not_called()

    def test_status_json_is_not_contaminated_with_log_or_network_output(self):
        with patch.object(cli, "elevate"), patch.object(cli, "read_status", return_value={"status": "scheduled"}), \
             patch.object(cli, "initialize_logging") as logging_setup, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(["update-status", "--json"]), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "scheduled")
        logging_setup.assert_not_called()

    def test_prepare_only_does_not_schedule(self):
        with patch.object(cli, "prepare_update", return_value=True), \
             patch.object(cli, "read_state", return_value={"status": "ready"}), \
             patch.object(cli, "worker_action") as worker, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(cli.dispatch(cli.parse_args(["prepare-update"])))
        worker.assert_not_called()

    def test_application_only_cli_installs_without_scheduling_or_requesting_reboot(self):
        states = [dict(status="ready", execution={"mode": "live"}), dict(status="live-complete")]
        with patch.object(cli, "prepare_update", return_value=True), \
             patch.object(cli, "read_state", side_effect=states), \
             patch.object(cli, "install_live_update", return_value=True) as install, \
             patch.object(cli, "worker_action") as worker, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertTrue(cli.dispatch(cli.parse_args(["cli"])))
        install.assert_called_once_with(cli.LOG)
        worker.assert_not_called()
        result = json.loads(output.getvalue().removeprefix(cli.RESULT_PREFIX))
        self.assertEqual(result["status"], "live-complete")
        self.assertFalse(result["reboot_required"])

    def test_prepare_only_never_installs_live_either(self):
        with patch.object(cli, "prepare_update", return_value=True), \
             patch.object(cli, "read_state", return_value=dict(status="ready", execution={"mode": "live"})), \
             patch.object(cli, "install_live_update") as install, patch.object(cli, "worker_action") as worker, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertTrue(cli.dispatch(cli.parse_args(["prepare-update"])))
        install.assert_not_called()
        worker.assert_not_called()
        self.assertFalse(json.loads(output.getvalue().removeprefix(cli.RESULT_PREFIX))["reboot_required"])

    def test_live_failure_is_nonzero_and_does_not_emit_success_or_schedule(self):
        with patch.object(cli, "prepare_update", return_value=True), \
             patch.object(cli, "read_state", return_value=dict(status="ready", execution={"mode": "live"})), \
             patch.object(cli, "install_live_update", return_value=False), patch.object(cli, "worker_action") as worker, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertFalse(cli.dispatch(cli.parse_args(["cli"])))
        worker.assert_not_called()
        self.assertNotIn(cli.RESULT_PREFIX, output.getvalue())

    def test_live_service_failure_cannot_reuse_old_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            for rc, status, expected in ((1, "live-complete", False), (0, "validating-live", False), (0, "live-complete", True)):
                process = Mock(returncode=rc)
                process.poll.return_value = rc
                with self.subTest(rc=rc, status=status), patch.object(client, "STATE_DIR", Path(directory)), \
                     patch.object(client, "read_state", return_value=dict(status=status)), \
                     patch.object(client.subprocess, "Popen", return_value=process) as start:
                    self.assertEqual(client.install_live_update(logging.getLogger()), expected)
                    start.assert_called_once_with(["systemctl", "start", "nobara-updater-live.service"])

    def test_all_updates_flatpaks_only_after_preparation_succeeds(self):
        with patch.object(cli, "prepare_update", return_value=True), \
             patch.object(cli, "read_state", return_value={"status": "scheduled"}), \
             patch.object(cli, "worker_action", return_value=True), \
             patch.object(cli, "update_flatpaks") as flatpak, \
             patch.object(cli, "original_user", return_value="user"), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(cli.dispatch(cli.parse_args(["cli", "--all"])))
        flatpak.assert_called_once_with("user")

    def test_codec_request_does_not_silently_reuse_unrelated_job(self):
        with patch.object(client, "read_state", return_value={"status": "ready", "migrations": {"hooks": []}}), \
             patch.object(client, "worker_action", return_value=True), \
             patch.object(client.subprocess, "Popen") as start:
            self.assertFalse(client.prepare_update(logging.getLogger(), codecs=True))
        start.assert_not_called()

    def test_systemctl_failure_cannot_reuse_an_old_success_state(self):
        with tempfile.TemporaryDirectory() as directory:
            process = Mock(returncode=1)
            process.poll.return_value = 1
            with patch.object(client, "STATE_DIR", Path(directory)), \
                 patch.object(client, "read_state", side_effect=[{"status": "idle"}, {"status": "ready"}]), \
                 patch.object(client.subprocess, "Popen", return_value=process):
                self.assertFalse(client.prepare_update(logging.getLogger()))

    def test_calamares_existing_commands_run_synchronously_without_systemd_client(self):
        for command, action in (("cli", "installer-update"), ("install-codecs", "installer-codecs")):
            with self.subTest(command=command), patch.object(cli, "in_installer_root", return_value=True), \
                 patch.object(cli, "run") as run, patch.object(cli, "prepare_update") as prepare, \
                 patch.object(cli, "worker_action") as action_client, \
                 patch.object(cli, "read_state", return_value={"status": "installer-complete"}), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertTrue(cli.dispatch(cli.parse_args([command])))
                run.assert_called_once_with(["/usr/libexec/nobara-update-worker", action])
                prepare.assert_not_called()
                action_client.assert_not_called()
                self.assertEqual(json.loads(output.getvalue().removeprefix(cli.RESULT_PREFIX))["status"], "installer-complete")

    def test_installer_cannot_request_a_reboot_or_user_flatpaks(self):
        with patch.object(cli, "in_installer_root", return_value=True), \
             patch.object(cli, "run") as run, patch.object(cli, "worker_action") as worker:
            for args in (["reboot"], ["schedule-update"], ["cli", "--all"]):
                with self.assertRaises(cli.UpdateError):
                    cli.dispatch(cli.parse_args(args))
            run.assert_not_called()
            worker.assert_not_called()

    def test_codec_request_recognizes_already_enabled_codec_plan_without_enable_hook(self):
        with patch.object(client, "read_state", return_value={"status": "ready", "codecs": True, "migrations": {"hooks": []}}), \
             patch.object(client, "worker_action", return_value=True), \
             patch.object(client.subprocess, "Popen") as start:
            self.assertTrue(client.prepare_update(logging.getLogger(), codecs=True))
        start.assert_not_called()

    def test_scheduled_cli_revalidates_and_rearms_before_reporting_restart(self):
        with patch.object(cli, "prepare_update", return_value=True), \
             patch.object(cli, "read_state", return_value={"status": "scheduled"}), \
             patch.object(cli, "worker_action", return_value=True) as worker, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(cli.dispatch(cli.parse_args(["cli"])))
        worker.assert_called_once_with("schedule", cli.LOG)

    def test_failed_rearming_does_not_emit_success_or_run_flatpaks(self):
        with patch.object(cli, "prepare_update", return_value=True), \
             patch.object(cli, "read_state", return_value={"status": "scheduled"}), \
             patch.object(cli, "worker_action", return_value=False), \
             patch.object(cli, "update_flatpaks") as flatpaks, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertFalse(cli.dispatch(cli.parse_args(["cli", "--all"])))
        self.assertNotIn(cli.RESULT_PREFIX, output.getvalue())
        flatpaks.assert_not_called()

    def test_stale_pending_job_is_prepared_again_without_reusing_old_success(self):
        with patch.object(client, "read_state", side_effect=[{"status": "scheduled"}, {"status": "failed", "started": False}]), \
             patch.object(client, "worker_action", return_value=True) as refresh, \
             patch.object(client, "run_service", return_value=True) as prepare:
            self.assertTrue(client.prepare_update(cli.LOG))
        refresh.assert_called_once_with("refresh-pending", cli.LOG)
        prepare.assert_called_once_with("nobara-updater-prepare.service", cli.LOG, {"ready", "scheduled", "unchanged"})


if __name__ == "__main__":
    unittest.main()
