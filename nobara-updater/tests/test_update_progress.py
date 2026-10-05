"""Offline display lifecycle and progress; never contacts the host's splash."""
import io
from pathlib import Path
import subprocess
import sys
import types
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1] / "src"
if "nobara_updater" not in sys.modules:
    package = types.ModuleType("nobara_updater")
    package.__path__ = [str(SOURCE)]
    sys.modules["nobara_updater"] = package

from nobara_updater import update_backend as backend, update_progress as progress


class PlymouthProgressTests(unittest.TestCase):
    def setUp(self):
        self.splash = progress.OfflineProgress()
        patcher = patch.object(progress.shutil, "which", return_value="/usr/bin/plymouth")
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(progress.subprocess, "run", return_value=Mock(returncode=0))
        self.run = patcher.start()
        self.addCleanup(patcher.stop)

    def commands(self):
        return [invocation.args[0][1:] for invocation in self.run.call_args_list]

    def percentages(self):
        return [int(command[1].split("=")[1]) for command in self.commands() if command[0] == "system-update"]

    def test_display_is_inert_until_offline_start(self):
        self.splash.message("not an offline update", percent=100)
        self.splash.transaction_line("[1/1] Upgrading package | 100% | done")
        self.run.assert_not_called()

    def test_switches_out_of_boot_mode_before_showing_update_progress(self):
        self.splash.start()
        self.assertEqual(self.commands(), [["change-mode", "--updates"], ["system-update", "--progress=0"]])

    def test_release_upgrade_uses_upgrade_theme_mode(self):
        self.splash.start(release_upgrade=True)
        self.assertEqual(self.commands()[0], ["change-mode", "--system-upgrade"])

    def test_completed_rpm_items_advance_but_do_not_finish_update(self):
        self.splash.start()
        for line in ("[ 1/60] Verify package files 100% | 100% | done",
                     "[30/60] Upgrading editor | 100% | done",
                     "[60/60] Removing old library | 100% | done"):
            self.splash.transaction_line(line)
        self.assertEqual(self.percentages(), [0, 6, 45, 85])

    def test_scriptlet_and_partial_or_malformed_progress_are_not_overall_progress(self):
        self.splash.start()
        for line in ("[1/1] Building driver | 50% | remaining", "scriptlet: 100%",
                     ">>> [1/1] build | 100% | done", "[1/0] Bad | 100% | done",
                     "[61/60] Bad | 100% | done", "unexpected output format", "Complete!",
                     "[" + "9" * 5000 + "/60] Not a transaction counter | 100% | done"):
            self.splash.transaction_line(line)
        self.assertEqual(self.percentages(), [0])

    def test_duplicate_or_older_progress_does_not_regress_or_spawn_more_clients(self):
        self.splash.start()
        for completed in (1, 1, 1, 80, 60, 80):
            self.splash.transaction_line(f"\x1b[32m[{completed}/100] Installing package | 100% | done\x1b[0m")
        self.assertEqual(self.percentages(), [0, 5, 69])

    def test_new_interpreter_resumes_validation_then_finishes_explicitly(self):
        self.splash.start(percent=90)
        self.splash.message("Building boot files. Keep the computer powered on.")
        self.splash.transaction_line("[1/1] late RPM output | 100% | done")
        self.assertEqual(self.percentages(), [90])
        self.splash.message("Restarting", percent=100)
        self.assertEqual(self.percentages(), [90, 100])

    def test_stage_messages_replace_previous_message_on_themes_that_show_them(self):
        self.splash.start()
        self.splash.message("Installing packages")
        self.splash.message("Installing packages")
        self.splash.message("Checking boot files")
        self.assertEqual(self.commands()[2:], [
            ["display-message", "--text=Installing packages"],
            ["hide-message", "--text=Installing packages"],
            ["display-message", "--text=Checking boot files"]])

    def test_missing_plymouth_is_optional(self):
        with patch.object(progress.shutil, "which", return_value=None):
            self.splash.start()
        self.run.assert_not_called()

    def test_unavailable_daemon_or_timeout_does_not_fail_or_slow_each_rpm(self):
        for error in (OSError("missing binary"), subprocess.TimeoutExpired("plymouth", 2), None):
            with self.subTest(error=error):
                self.run.reset_mock()
                self.run.side_effect = error
                self.run.return_value = Mock(returncode=1)
                self.splash.start()
                self.splash.message("Still installing")
                self.splash.advance(85)
                self.assertEqual(self.run.call_count, 1)
                self.assertEqual(self.run.call_args.kwargs["timeout"], 2)

    def test_recovery_worker_can_show_failure_without_reporting_success(self):
        self.splash.recovery()
        self.splash.message("The update failed. Restarting into the previous system.")
        self.assertEqual(self.commands()[0], ["change-mode", "--reboot"])
        self.assertEqual(self.percentages(), [])
        self.assertIn("failed", self.commands()[-1][-1])

    def test_only_dnf_replay_output_is_forwarded_without_losing_failure_detection(self):
        for command in (["dnf5", "replay", "/job"], ["dracut", "--force"]):
            with self.subTest(command=command):
                output = "[1/1] Installing fixture | 100% | done\n"
                output += "Non-critical error in %post scriptlet: fixture\n"
                process = Mock(stdout=io.StringIO(output), returncode=0)
                process.wait.return_value = 0
                with patch.object(backend, "SPLASH") as splash, \
                     patch.object(backend.subprocess, "Popen", return_value=process):
                    if command[0] == "dnf5":
                        with self.assertRaisesRegex(backend.UpdateError, "scriptlet"):
                            backend.run(command)
                        self.assertEqual(splash.transaction_line.call_count, 2)
                    else:
                        backend.run(command)
                        splash.transaction_line.assert_not_called()

    def test_progress_module_is_part_of_the_frozen_engine(self):
        self.assertIn("update_progress.py", backend.ENGINE_FILES)


if __name__ == "__main__":
    unittest.main()
