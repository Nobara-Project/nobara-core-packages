"""The desktop notice for hand-installed DKMS modules an update skipped."""
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1] / "src"
if "nobara_updater" not in sys.modules:
    package = types.ModuleType("nobara_updater")
    package.__path__ = [str(SOURCE)]
    sys.modules["nobara_updater"] = package
from nobara_updater import update_backend as backend
from nobara_updater import update_notice as notice
from nobara_updater import update_report as reports

JOB = "b" * 32
KERNEL = "7.2.11-200.nobara.fc44.x86_64"
MODULES = [dict(name="xone", version="v0.3-58-g28df566", kernel=KERNEL),
           dict(name="xpad-noone", version="1.0", kernel=KERNEL)]


class DkmsNoticeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        patcher = patch.object(reports, "REPORTS", self.root / "public")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_notice_is_public_and_read_back(self):
        reports.publish_dkms_notice(JOB, MODULES)
        path = self.root / "public" / reports.DKMS_NOTICE
        self.assertEqual(path.stat().st_mode & 0o777, 0o644)
        self.assertEqual(reports.dkms_notice(), dict(job=JOB, modules=MODULES))

    def test_missing_or_invalid_notice_is_ignored(self):
        self.assertIsNone(reports.dkms_notice())
        (self.root / "public").mkdir()
        (self.root / "public" / reports.DKMS_NOTICE).write_text('{"job": "../x", "modules": [1]}')
        self.assertIsNone(reports.dkms_notice())

    def test_desktop_notification_names_modules_and_removal_once_per_update(self):
        with patch.dict(notice.os.environ, {"XDG_STATE_HOME": str(self.root / "user")}), \
             patch.object(notice.subprocess, "run", return_value=Mock(returncode=0, stdout="")) as run:
            notice.notify_dkms(dict(job=JOB, modules=MODULES))
            notice.notify_dkms(dict(job=JOB, modules=MODULES))
        self.assertEqual(run.call_count, 1)
        body = run.call_args.args[0][-1]
        self.assertIn("xone v0.3-58-g28df566 (kernel " + KERNEL + ")", body)
        self.assertIn("sudo dkms remove xone/v0.3-58-g28df566 --all", body)
        self.assertIn("sudo dkms remove xpad-noone/1.0 --all", body)

    def test_failed_notification_is_retried_at_next_login(self):
        with patch.dict(notice.os.environ, {"XDG_STATE_HOME": str(self.root / "user")}), \
             patch.object(notice.subprocess, "run", return_value=Mock(returncode=1, stdout="")) as run:
            notice.notify_dkms(dict(job=JOB, modules=MODULES))
            notice.notify_dkms(dict(job=JOB, modules=MODULES))
        self.assertEqual(run.call_count, 2)

    def test_login_shows_the_dkms_notice_when_no_recovery_is_active(self):
        with patch.object(notice, "latest_report", return_value=dict(job=JOB, active=False)), \
             patch.object(notice, "dkms_notice", return_value=dict(job=JOB, modules=MODULES)), \
             patch.object(notice, "notify_dkms") as notify, patch.object(notice, "show_report") as show, \
             patch.object(notice.subprocess, "run", return_value=Mock(stdout="inactive\n")), \
             patch.object(sys, "argv", ["nobara-update-notice", "--notify"]):
            notice.main()
        notify.assert_called_once()
        show.assert_not_called()


class ConfirmPublishesNoticeTests(unittest.TestCase):
    def test_confirmed_update_publishes_skipped_modules(self):
        state = dict(job=JOB, status="awaiting-boot", started=True, dkms_skipped=MODULES, target_release="44")
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(backend, "read_state", return_value=state), \
             patch.object(backend, "write_state", side_effect=lambda root, state, status, **fields: state.update(fields, status=status)), \
             patch.object(backend, "validate_running_update"), patch.object(backend, "cleanup_confirmed_update"), \
             patch.object(backend, "stop_recovery_loop", return_value=False), \
             patch.object(backend, "publish_dkms_notice") as publish, \
             patch.object(backend, "status_message", return_value="done"):
            backend.confirm_boot(Path(directory))
        publish.assert_called_once_with(JOB, MODULES)
        self.assertEqual(state["status"], "complete")


if __name__ == "__main__":
    unittest.main()
