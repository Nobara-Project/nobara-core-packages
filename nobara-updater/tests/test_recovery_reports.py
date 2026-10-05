"""Rollback diagnostics, offline sharing, and recovery/reboot ordering."""
import configparser
import contextlib
import importlib.util
import io
import json
import logging
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1] / "src"
if "nobara_updater" not in sys.modules:
    package = types.ModuleType("nobara_updater")
    package.__path__ = [str(SOURCE)]
    sys.modules["nobara_updater"] = package
from nobara_updater import update_report as reports
from nobara_updater import update_notice as notice
from nobara_updater.update_state import UpdateError

JOB = "a" * 32


class ReportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.boot = self.root / "boot"
        self.public = self.root / "public"
        self.old_handlers = list(logging.getLogger().handlers)
        self.addCleanup(self.close_handlers)
        for name, value in (("BOOT_REPORTS", self.boot), ("REPORTS", self.public),
                            ("STATE_DIR", self.root / "state"), ("DKMS_LOG_ROOT", self.root / "dkms")):
            patcher = patch.object(reports, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(reports.os, "sync")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state = dict(job=JOB, started=True, recovery={"created": True}, source_release="43", target_release="44")

    def close_handlers(self):
        for handler in list(logging.getLogger().handlers):
            if handler not in self.old_handlers:
                logging.getLogger().removeHandler(handler)
                handler.close()

    def publish(self):
        reports.arm_report(self.state)
        logging.getLogger().warning("Cannot download fixture; cacheonly option is activated.")
        with patch.object(reports.subprocess, "run"):
            reports.record_failure(self.state, "execute", UpdateError("offline RPM failure"))
        # Publishing runs in a restored root with none of the failed root's
        # update state/logs, using the separately mounted /boot diagnostics.
        reports.publish_recovery(JOB)
        return reports.latest_report()

    def test_error_and_bundle_survive_loss_of_failed_root_without_uploading(self):
        report = self.publish()
        self.assertTrue(report["active"])
        self.assertEqual(report["error"], "offline RPM failure")
        text = Path(report["log"]).read_text()
        self.assertIn("cacheonly option is activated", text)
        self.assertIn("sudo nobara-sync cli", text)
        with tarfile.open(report["bundle"]) as archive:
            self.assertEqual(set(archive.getnames()), {"README.txt", "update.log"})
            self.assertEqual(archive.extractfile("update.log").read().decode(), text)
        self.assertEqual(Path(report["bundle"]).stat().st_mode & 0o777, 0o644)
        self.assertEqual((self.boot / JOB / "failure.json").stat().st_mode & 0o777, 0o600)

    def test_catastrophic_failure_still_leaves_report_without_exception_handler(self):
        reports.arm_report(self.state)
        reports.publish_recovery(JOB)
        self.assertIn("interrupted", reports.latest_report()["error"])

    def test_secondary_recovery_failure_does_not_replace_original_error(self):
        self.publish()
        with patch.object(reports.subprocess, "run"):
            reports.record_failure(self.state, "recover", RuntimeError("boot unavailable"))
        reports.publish_recovery(JOB)
        report = reports.latest_report()
        self.assertEqual(report["error"], "offline RPM failure")
        self.assertEqual(report["recovery_error"], "boot unavailable")

    def test_confirmation_failure_preserves_installation_error_and_its_journal(self):
        self.publish()
        journal = self.boot / JOB / "failure-journal.log"
        journal.write_text("original installation journal\n")
        with patch.object(reports.subprocess, "run"):
            reports.record_failure(self.state, "confirm", UpdateError("The previous update did not finish."))
        reports.publish_recovery(JOB)
        report = reports.latest_report()
        self.assertEqual(report["phase"], "execute")
        self.assertEqual(report["error"], "offline RPM failure")
        self.assertEqual(report["confirmation_error"], "The previous update did not finish.")
        self.assertEqual(journal.read_text(), "original installation journal\n")
        self.assertIn("original installation journal", Path(report["log"]).read_text())

    def test_journal_timeout_keeps_partial_output_and_publishes_without_snapshot(self):
        self.state["recovery"] = {}
        reports.arm_report(self.state)

        def timeout(command, **options):
            options["stdout"].write(b"partial journal evidence\n")
            raise subprocess.TimeoutExpired(command, options["timeout"])

        with patch.object(reports.subprocess, "run", side_effect=timeout):
            reports.record_failure(self.state, "execute", UpdateError("driver compilation failed"))
        report = reports.latest_report()
        self.assertEqual(report["error"], "driver compilation failed")
        self.assertFalse(report["recovered"])
        text = Path(report["log"]).read_text()
        self.assertIn("partial journal evidence", text)
        self.assertIn("Journal collection timed out", text)
        self.assertTrue(Path(report["bundle"]).is_file())

    def test_systemd_timeout_survives_rollback_even_without_python_exception(self):
        reports.arm_report(self.state)
        self.state["status"] = "awaiting-boot"
        def journal(command, **options):
            options["stdout"].write(b"nobara-updater-confirm.service: start operation timed out. Terminating.\n")
        with patch.object(reports.subprocess, "run", side_effect=journal):
            reports.record_service_failure(self.state, "confirm", "timeout", "killed", "TERM")
        reports.publish_recovery(JOB)
        report = reports.latest_report()
        self.assertEqual(report["phase"], "confirm")
        self.assertIn("result=timeout", report["error"])
        self.assertIn("exit status=TERM", report["error"])
        self.assertIn("start operation timed out", Path(report["log"]).read_text())
        self.assertEqual(self.state["status"], "awaiting-boot")

    def test_service_failure_keeps_existing_error_and_its_journal(self):
        self.publish()
        journal = self.boot / JOB / "failure-journal.log"
        journal.write_text("original failure evidence\n")
        def capture(path):
            path.write_text("later systemd exit details\n")
        with patch.object(reports, "capture_journal", side_effect=capture):
            reports.record_service_failure(self.state, "execute", "exit-code", "exited", "1")
        reports.publish_recovery(JOB)
        report = reports.latest_report()
        self.assertEqual(report["error"], "offline RPM failure")
        self.assertEqual(report["service_failure"]["result"], "exit-code")
        self.assertEqual(journal.read_text(), "original failure evidence\n")
        self.assertIn("later systemd exit details", Path(report["log"]).read_text())

    def test_service_kill_without_snapshot_publishes_desktop_report(self):
        self.state["recovery"] = {}
        with patch.object(reports.subprocess, "run"):
            reports.record_service_failure(self.state, "execute", "oom-kill", "killed", "KILL")
        report = reports.latest_report()
        self.assertFalse(report["recovered"])
        self.assertIn("result=oom-kill", report["error"])

    def test_clean_or_confirmed_service_stop_does_not_create_failure(self):
        with patch.object(reports, "record_failure") as record, patch.object(reports, "capture_journal") as journal:
            for result in ("", "success", "exec-condition"):
                reports.record_service_failure(self.state, "confirm", result, "exited", "0")
            for status in ("complete", "recovered"):
                reports.record_service_failure(dict(self.state, status=status), "confirm", "timeout", "killed", "TERM")
            reports.record_service_failure(dict(self.state, started=False), "execute", "exec-condition", "exited", "1")
        record.assert_not_called()
        journal.assert_not_called()
        self.assertFalse(self.boot.exists())

    def test_failure_report_io_error_never_prevents_recovery(self):
        with patch.object(reports, "report_directory", side_effect=OSError("boot filesystem read-only")), \
             self.assertLogs(reports.LOG, level="ERROR"):
            reports.record_service_failure(self.state, "confirm", "timeout", "killed", "TERM")

    def test_unavailable_journal_does_not_prevent_failure_publication(self):
        self.state["recovery"] = {}
        reports.arm_report(self.state)
        with patch.object(reports.subprocess, "run", side_effect=OSError("journal unavailable")):
            reports.record_failure(self.state, "execute", UpdateError("driver compilation failed"))
        report = reports.latest_report()
        self.assertEqual(report["error"], "driver compilation failed")
        self.assertIn("journal unavailable", Path(report["log"]).read_text())

    def test_referenced_dkms_build_log_survives_rollback_and_journal_timeout(self):
        path = reports.DKMS_LOG_ROOT / "fixture-driver/1/build/make.log"
        path.parent.mkdir(parents=True)
        path.write_text("fixture.c:42: error: incompatible kernel API\n")
        reports.arm_report(self.state)
        logging.getLogger().warning("Consult %s for more information.", path)
        with patch.object(reports.subprocess, "run", side_effect=subprocess.TimeoutExpired("journalctl", 15)):
            reports.record_failure(self.state, "execute", UpdateError("fixture driver failed"))
        path.unlink()  # Rollback restored the root, losing the failed build.
        with patch.object(reports.subprocess, "run"):
            reports.record_failure(self.state, "confirm", UpdateError("installation incomplete"))
        reports.publish_recovery(JOB)
        report = reports.latest_report()
        with tarfile.open(report["bundle"]) as archive:
            text = archive.extractfile("update.log").read().decode()
        self.assertIn("fixture.c:42: error: incompatible kernel API", text)
        self.assertEqual(report["error"], "fixture driver failed")
        self.assertEqual((self.boot / JOB / "failure-build.log").stat().st_mode & 0o777, 0o600)

    def test_build_log_collection_is_bounded_and_cannot_follow_symlinks_outside_dkms(self):
        reports.arm_report(self.state)
        outside = self.root / "private.txt"
        outside.write_text("private credentials\n")
        link = reports.DKMS_LOG_ROOT / "outside/1/build/make.log"
        link.parent.mkdir(parents=True)
        link.symlink_to(outside)
        valid = reports.DKMS_LOG_ROOT / "fixture/1/build/make.log"
        valid.parent.mkdir(parents=True)
        valid.write_text("x" * 2000 + "\nFinal compiler error\n")
        logging.getLogger().warning("Consult %s or %s for more information.", link, valid)
        with patch.object(reports, "LIMIT", 1024):
            reports.capture_build_logs(self.boot / JOB)
        saved = self.boot / JOB / "failure-build.log"
        self.assertLessEqual(saved.stat().st_size, 1024)
        self.assertNotIn("private credentials", saved.read_text())
        self.assertIn("Final compiler error", saved.read_text())

    def test_build_log_collection_error_does_not_block_report(self):
        self.state["recovery"] = {}
        with patch.object(reports, "capture_build_logs", side_effect=PermissionError("unreadable")), \
             patch.object(reports.subprocess, "run"), self.assertLogs(reports.LOG, level="WARNING"):
            reports.record_failure(self.state, "execute", UpdateError("driver compilation failed"))
        self.assertEqual(reports.latest_report()["error"], "driver compilation failed")

    def test_build_log_collection_ignores_previous_jobs_when_current_log_exists(self):
        old = reports.DKMS_LOG_ROOT / "old-driver/1/build/make.log"
        old.parent.mkdir(parents=True)
        old.write_text("unrelated compiler failure\n")
        reports.STATE_DIR.mkdir()
        (reports.STATE_DIR / "update.log").write_text(f"Old job: consult {old}\n")
        reports.arm_report(self.state)
        logging.getLogger().warning("Current job failed for a different reason")
        reports.capture_build_logs(self.boot / JOB)
        self.assertFalse((self.boot / JOB / "failure-build.log").exists())

    def origin(self):
        return dict(name="labwc", arch="x86_64", nevra="labwc-0:5-1.x86_64",
                    aliases=["labwc-5-1.x86_64", "labwc-0:5-1.x86_64"],
                    repo="copr:test:labwc", kind="third-party", nobara_repos=["nobara"])

    def test_origin_and_repo_details_survive_rollback_in_offline_report(self):
        self.state["package_origins"] = [self.origin()]
        reports.arm_report(self.state)
        with patch.object(reports.subprocess, "run"):
            reports.record_failure(self.state, "execute", UpdateError("labwc-5-1.x86_64 conflicts with a system package"))
        reports.publish_recovery(JOB)
        report = reports.latest_report()
        self.assertEqual(report["package_conflicts"][0]["repo"], "copr:test:labwc")
        self.assertIn("not a Nobara-provided repository", report["error"])
        with tarfile.open(report["bundle"]) as archive:
            log = archive.extractfile("update.log").read().decode()
        self.assertIn("copr:test:labwc", log)
        self.assertIn("Nobara provides this package in: nobara", log)

    def test_crash_log_origin_attribution_ignores_unrelated_context(self):
        self.state["package_origins"] = [self.origin()]
        reports.arm_report(self.state)
        logging.getLogger().warning("Keeping installed labwc-5-1.x86_64")
        reports.publish_recovery(JOB)
        self.assertEqual(reports.latest_report()["package_conflicts"], [])
        logging.getLogger().error("Error: labwc-5-1.x86_64 conflicts with another package")
        reports.publish_recovery(JOB)
        self.assertEqual(reports.latest_report()["package_conflicts"][0]["name"], "labwc")

    def test_preparation_conflict_produces_report_without_claiming_rollback(self):
        self.state.update(started=False, recovery={}, package_origins=[self.origin()])
        with patch.object(reports.subprocess, "run"):
            reports.record_failure(self.state, "prepare", UpdateError("labwc-5-1.x86_64 requires missing-abi"))
        report = reports.latest_report()
        self.assertFalse(report["installation_started"])
        self.assertFalse(report["recovered"])
        self.assertEqual(report["package_conflicts"][0]["name"], "labwc")
        self.assertTrue(Path(report["bundle"]).is_file())

    def test_success_retires_notification_but_keeps_report_for_support(self):
        report = self.publish()
        reports.resolve_notice()
        self.assertFalse(reports.latest_report()["active"])
        self.assertTrue(Path(report["bundle"]).is_file())

    def test_credentials_are_redacted_and_tail_is_bounded(self):
        path = self.root / "large.log"
        path.write_bytes(b"x" * (reports.LIMIT + 500))
        self.assertEqual(len(reports.tail(path)), reports.LIMIT)
        text = reports.redact("https://user:password@repo.example/p?token=secret&name=fixture")
        self.assertNotIn("password", text)
        self.assertNotIn("secret", text)
        self.assertIn("name=fixture", text)

    def test_explicit_upload_returns_link_and_offline_failure_keeps_bundle(self):
        report = self.publish()
        url = "https://pb.nobaraproject.org/00p6f"
        with patch.object(reports.subprocess, "run", return_value=Mock(returncode=0, stdout=url + "\n")) as run:
            self.assertEqual(reports.upload_report(report), url)
        self.assertEqual(run.call_args.args[0][0], "pbcli")
        self.assertIn("cacheonly", run.call_args.kwargs["input"])
        with patch.object(reports.subprocess, "run", return_value=Mock(returncode=1, stderr="offline")):
            with self.assertRaisesRegex(UpdateError, "Save the report"):
                reports.upload_report(report)
        self.assertTrue(Path(report["bundle"]).exists())

    def test_login_notification_is_once_per_user_and_does_not_upload(self):
        report = self.publish()
        with patch.dict(notice.os.environ, {"XDG_STATE_HOME": str(self.root / "user")}), \
             patch.object(notice.subprocess, "run", return_value=Mock(returncode=0, stdout="details\n")) as run, \
             patch.object(notice, "upload_report") as upload:
            self.assertTrue(notice.notify(report))
            self.assertFalse(notice.notify(report))
        self.assertEqual(run.call_count, 1)
        upload.assert_not_called()

    def test_invalid_job_cannot_escape_report_directory(self):
        with self.assertRaises(UpdateError):
            reports.publish_recovery("../../etc")

    def test_no_snapshot_keeps_logs_on_root_and_never_claims_restoration(self):
        self.state["recovery"] = {"available": False}
        reports.arm_report(self.state)
        logging.getLogger().warning("Fixture RPM failed without a snapshot")
        with patch.object(reports.subprocess, "run"):
            reports.record_failure(self.state, "execute", UpdateError("fixture installation failure"))
        report = reports.latest_report()
        self.assertFalse(report["recovered"])
        self.assertTrue(report["installation_started"])
        self.assertFalse(self.boot.exists())
        log = Path(report["log"]).read_text()
        self.assertIn("Fixture RPM failed without a snapshot", log)
        self.assertIn("sudo dnf5 check", log)
        self.assertNotIn("previous system was restored", log)


class ServiceOrderingTests(unittest.TestCase):
    def unit(self, name):
        config = configparser.ConfigParser(interpolation=None, strict=False)
        config.read(SOURCE.parent / "data" / ("nobara-updater-" + name + ".service"))
        return config

    def test_offline_failure_cannot_start_recovery_and_reboot_concurrently(self):
        self.assertEqual(self.unit("offline")["Unit"]["OnFailure"], "nobara-updater-recovery.service")
        recovery = self.unit("recovery")["Unit"]
        self.assertIn("shutdown.target", recovery["Before"])
        self.assertEqual(recovery["RequiresMountsFor"], "/boot")
        self.assertIn("nobara-updater-fallback-reboot.service", recovery["OnFailure"])
        self.assertEqual(recovery["SuccessAction"], "reboot")

    def test_confirmation_failure_without_backup_keeps_failed_state_but_allows_desktop(self):
        spec = importlib.util.spec_from_file_location("report_worker_test", SOURCE / "update_worker.py")
        worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(worker)
        for snapshot in (False, True):
            with self.subTest(snapshot=snapshot), tempfile.TemporaryDirectory() as directory:
                state = dict(job=JOB, status="awaiting-boot", started=True, recovery={"created": snapshot})
                with patch.object(worker, "STATE_DIR", Path(directory)), \
                     patch.object(worker.sys, "argv", ["worker", "confirm"]), \
                     patch.object(worker.os, "geteuid", return_value=0), \
                     patch.object(worker.os, "umask"), \
                     patch.object(worker.logging, "basicConfig"), \
                     patch.object(worker, "RotatingFileHandler"), \
                     patch.object(worker, "read_state", return_value=state), \
                     patch.object(worker, "write_state") as write, \
                     patch.object(worker, "attach_log"), patch.object(worker, "record_failure") as report, \
                     patch.object(worker.backend, "confirm_boot", side_effect=UpdateError("boot checks failed")), \
                     contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(worker.main(), 1 if snapshot else 0)
                    self.assertEqual(write.call_args.args[2], "failed")
                    report.assert_called_once()

    def test_services_capture_systemd_failures_before_recovery(self):
        for unit, action in (("confirm", "confirm-stopped"), ("offline", "offline-stopped")):
            self.assertEqual(self.unit(unit)["Service"]["ExecStopPost"],
                             "-/usr/libexec/nobara-update-worker " + action)
        self.assertEqual(self.unit("confirm")["Service"]["TimeoutStartSec"], "15min")

    def test_stop_hook_reports_systemd_result_without_changing_state_or_rerunning_update(self):
        spec = importlib.util.spec_from_file_location("stopped_worker_test", SOURCE / "update_worker.py")
        worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(worker)
        for action, phase in (("confirm-stopped", "confirm"), ("offline-stopped", "execute")):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as directory:
                state = dict(job=JOB, status="awaiting-boot", started=True)
                with patch.object(worker, "STATE_DIR", Path(directory)), \
                     patch.object(worker.sys, "argv", ["worker", action]), \
                     patch.object(worker.os, "geteuid", return_value=0), patch.object(worker.os, "umask"), \
                     patch.object(worker.logging, "basicConfig"), patch.object(worker, "RotatingFileHandler"), \
                     patch.object(worker, "read_state", return_value=state), patch.object(worker, "write_state") as write, \
                     patch.object(worker, "record_service_failure") as report, \
                     patch.object(worker.backend, "confirm_boot") as confirm, patch.object(worker.backend, "execute") as execute, \
                     patch.dict(worker.os.environ, SERVICE_RESULT="timeout", EXIT_CODE="killed", EXIT_STATUS="TERM"):
                    self.assertEqual(worker.main(), 0)
                    report.assert_called_once_with(state, phase, "timeout", "killed", "TERM")
                    write.assert_not_called()
                    confirm.assert_not_called()
                    execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
