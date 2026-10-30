"""Boot context survives rollback without guessing which visible boot failed."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import types
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "src"
if "nobara_updater" not in sys.modules:
    package = types.ModuleType("nobara_updater")
    package.__path__ = [str(SOURCE)]
    sys.modules["nobara_updater"] = package
from nobara_updater import update_report as reports

JOB = "a" * 32
CURRENT = "1" * 32
PREVIOUS = "2" * 32
OLDER = "3" * 32


class SystemJournalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.archive = self.root / "boot" / JOB
        self.state = dict(job=JOB, started=True, status="awaiting-boot", recovery={"created": True})
        self.bootid = self.root / "boot-id"
        self.bootid.write_text(CURRENT + "\n")
        self.commands = []
        for patcher in (patch.object(reports, "BOOT_REPORTS", self.archive.parent),
                        patch.object(reports, "STATE_DIR", self.root / "state"),
                        patch.object(reports, "REPORTS", self.root / "public"),
                        patch.object(reports, "BOOT_ID", self.bootid),
                        patch.object(reports, "capture_boot_diagnostics"),
                        patch.object(reports.os, "sync"),
                        patch.object(reports.subprocess, "run", side_effect=self.journal)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def journal(self, args, **kwargs):
        self.commands.append(args)
        self.assertEqual(args[0], "journalctl")
        self.assertIn("--system", args)
        self.assertLessEqual(kwargs["timeout"], 2)
        if "--list-boots" in args:
            text = f"-2 {OLDER} first timestamp\n-1 {PREVIOUS} second timestamp\n0 {CURRENT} current timestamp\n"
        else:
            self.assertIn("_TRANSPORT=kernel", args)
            self.assertIn("_PID=1", args)
            self.assertIn("_SYSTEMD_UNIT=plasmalogin.service", args)
            self.assertIn("+", args)
            boot = next(a.split("=", 1)[1] for a in args if a.startswith("--boot="))
            text = (f"2026-10-20T15:00:00+0000 systemd[1]: Boot fixture {boot}\n"
                    "2026-10-20T15:00:01+0000 systemd[1]: Dependency failed for /boot/efi.\n"
                    "2026-10-20T15:00:02+0000 kernel: Command line: root=UUID=fixture rootflags=subvol=@ arbitrary.secret=BOOT_SECRET\n"
                    "2026-10-20T15:00:03+0000 dracut-cmdline[12]: Using kernel command line parameters: root=UUID=fixture password=INITRD_SECRET\n"
                    "2026-10-20T15:00:04+0000 example: https://user:URL_SECRET@example.test/?token=QUERY_SECRET\n")
        kwargs["stdout"].write(text.encode())
        return subprocess.CompletedProcess(args, 0)

    def test_rollback_report_contains_saved_installation_and_available_boot_ids(self):
        reports.capture_system_journal(self.state, "installation")
        offline = self.archive / "installation-system-journal.log"
        original = offline.read_bytes()
        # No exception handler or surviving failed-root journal is required.
        self.bootid.write_text("4" * 32)
        reports.publish_recovery(JOB)
        report = reports.latest_report()
        text = Path(report["log"]).read_text()
        for value in ("Dependency failed for /boot/efi", "installation-system-journal.log", CURRENT,
                      PREVIOUS, OLDER, "not proof of which boot failed", "rollback", "subvol=@"):
            self.assertIn(value, text.lower() if value == "rollback" else text)
        for secret in ("BOOT_SECRET", "INITRD_SECRET", "URL_SECRET", "QUERY_SECRET"):
            self.assertNotIn(secret, text)
        self.assertEqual(offline.read_bytes(), original)
        self.assertEqual(offline.stat().st_mode & 0o777, 0o600)
        with tarfile.open(report["bundle"]) as archive:
            self.assertEqual(archive.extractfile("update.log").read().decode(), text)
        queried = [a for cmd in self.commands for a in cmd if a.startswith("--boot=")]
        self.assertIn("--boot=" + PREVIOUS, queried)
        self.assertIn("--boot=" + OLDER, queried)
        self.assertNotIn("--boot=-1", queried)

    def test_repeated_publication_preserves_original_boot_context(self):
        reports.publish_recovery(JOB)
        path = self.archive / "report-system-journal.log"
        original = path.read_bytes()
        self.commands.clear()
        self.bootid.write_text("4" * 32)
        reports.publish_recovery(JOB)
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(self.commands)

    def test_timeout_keeps_partial_data_and_never_claims_a_missing_boot_succeeded(self):
        def timeout(args, **kwargs):
            kwargs["stdout"].write(b"partial system journal\n")
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])
        with patch.object(reports.subprocess, "run", side_effect=timeout):
            reports.publish_recovery(JOB)
        text = Path(reports.latest_report()["log"]).read_text()
        self.assertIn("partial system journal", text)
        self.assertIn("timed out", text)
        self.assertIn("not proof of which boot failed", text)
        self.assertNotIn("startup checks passed", text)
        self.assertTrue(Path(reports.latest_report()["bundle"]).is_file())

    def test_boot_list_is_validated_and_missing_history_does_not_select_other_boots(self):
        def invalid(args, **kwargs):
            if "--list-boots" in args:
                kwargs["stdout"].write(b"-1 --all injected arguments\n0 " + CURRENT.encode() + b" current\n")
                return subprocess.CompletedProcess(args, 0)
            return self.journal(args, **kwargs)
        with patch.object(reports.subprocess, "run", side_effect=invalid):
            reports.capture_system_journal(self.state, "report", previous=True)
        self.assertEqual(len(self.commands), 1)
        self.assertIn("--boot=" + CURRENT, self.commands[0])

    def test_no_recovery_uses_job_diagnostics_and_collects_no_history_for_prepare_failure(self):
        state = dict(self.state, started=False, recovery={})
        reports.publish_failure(state)
        path = reports.STATE_DIR / "jobs" / JOB / "diagnostics/report-system-journal.log"
        self.assertTrue(path.is_file())
        self.assertFalse(self.archive.exists())
        self.assertFalse(any("--list-boots" in cmd for cmd in self.commands))

    def test_size_limit_preserves_header_and_reports_truncation(self):
        def oversized(args, **kwargs):
            kwargs["stdout"].write(b"noise" * 50000 + b"\nlast boot error\n")
            return subprocess.CompletedProcess(args, 0)
        with patch.object(reports.subprocess, "run", side_effect=oversized), patch.object(reports, "LIMIT", 32768):
            reports.capture_system_journal(self.state, "confirmation")
        path = self.archive / "confirmation-system-journal.log"
        self.assertLessEqual(path.stat().st_size, 32768)
        self.assertIn("current boot ID: " + CURRENT, path.read_text())
        self.assertIn("omitted", path.read_text())
        self.assertIn("last boot error", path.read_text())

    def test_unavailable_journal_and_storage_do_not_fail_the_operation(self):
        with patch.object(reports.subprocess, "run", side_effect=OSError("journal unavailable")):
            reports.capture_system_journal(self.state, "failure")
        self.assertIn("journal unavailable", (self.archive / "failure-system-journal.log").read_text())
        with patch.object(reports, "report_directory", side_effect=OSError("read-only boot filesystem")), \
                self.assertLogs(reports.LOG, level="WARNING"):
            reports.capture_system_journal(self.state, "installation")

    def test_worker_checkpoints_bracket_startup_validation_and_follow_installation(self):
        spec = importlib.util.spec_from_file_location("journal_worker_test", SOURCE / "update_worker.py")
        worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(worker)
        for action, expected in (("confirm", ["journal-confirmation", "validate"]),
                                 ("finalize", ["validate", "journal-installation"])):
            events = []
            with self.subTest(action=action), patch.object(worker, "STATE_DIR", self.root / "worker"), \
                    patch.object(worker.sys, "argv", ["worker", action]), \
                    patch.object(worker.os, "geteuid", return_value=0), patch.object(worker.os, "umask"), \
                    patch.object(worker.logging, "basicConfig"), patch.object(worker, "RotatingFileHandler"), \
                    patch.object(worker, "read_state", return_value=self.state), patch.object(worker, "attach_log"), \
                    patch.object(worker, "capture_system_journal", side_effect=lambda s, p: events.append("journal-" + p)), \
                    patch.object(worker.backend, "confirm_boot", side_effect=lambda: events.append("validate")), \
                    patch.object(worker.backend, "finalize", side_effect=lambda: events.append("validate")):
                self.assertEqual(worker.main(), 0)
            self.assertEqual(events, expected)


if __name__ == "__main__":
    unittest.main()
