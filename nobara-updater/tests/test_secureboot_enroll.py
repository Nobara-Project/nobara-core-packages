"""Secure Boot key setup for the Easy Anti-Cheat kernel, offered at login."""
from pathlib import Path
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

from nobara_updater import update_secureboot as sb

CA = sb.EAC_CA


def completed(command, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


class EnrollmentNeededTests(unittest.TestCase):
    def check(self, *, sb_state="SecureBoot enabled\n", test_key=f"{CA} is not enrolled\n",
              eac_installed=True, uefi=True, ostree=False, files=True):
        def exists(path):
            return {"/run/ostree-booted": ostree}.get(str(path), False)

        def is_dir(path):
            return uefi if str(path) == "/sys/firmware/efi" else False

        def is_file(path):
            return files if str(path) in {sb.ENROLL, CA} else False

        def run(command, **_):
            if command[:2] == ["mokutil", "--sb-state"]:
                return completed(command, 0, sb_state)
            if command[:2] == ["mokutil", "--test-key"]:
                return completed(command, 0 if "not enrolled" in test_key else 1, test_key)
            if command[:2] == ["rpm", "-q"]:
                return completed(command, 0 if eac_installed else 1)
            raise AssertionError(command)

        with patch.object(Path, "exists", exists), patch.object(Path, "is_dir", is_dir), \
                patch.object(Path, "is_file", is_file), patch.object(sb.subprocess, "run", side_effect=run):
            return sb.enrollment_needed()

    def test_needed_when_secure_boot_on_and_ca_missing(self):
        self.assertTrue(self.check())

    def test_not_needed_once_enrolled(self):
        self.assertFalse(self.check(test_key=f"{CA} is already enrolled\n"))

    def test_not_needed_while_waiting_for_mokmanager(self):
        self.assertFalse(self.check(test_key=f"{CA} is already in the enrollment request\n"))

    def test_not_needed_with_secure_boot_off(self):
        self.assertFalse(self.check(sb_state="SecureBoot disabled\n"))

    def test_not_needed_with_shim_validation_disabled(self):
        self.assertFalse(self.check(sb_state="SecureBoot enabled\nSecureBoot validation is disabled in shim\n"))

    def test_not_needed_without_the_eac_kernel(self):
        self.assertFalse(self.check(eac_installed=False))

    def test_not_needed_without_uefi_or_on_image_mode(self):
        self.assertFalse(self.check(uefi=False))
        self.assertFalse(self.check(ostree=True))

    def test_not_needed_without_nobara_eac(self):
        self.assertFalse(self.check(files=False))

    def test_unreadable_mokutil_output_does_not_prompt(self):
        # mokutil missing or failing: no "SecureBoot enabled", so no prompt
        with patch.object(Path, "is_dir", return_value=True), patch.object(Path, "exists", return_value=False), \
                patch.object(Path, "is_file", return_value=True), \
                patch.object(sb.subprocess, "run", side_effect=FileNotFoundError("mokutil")):
            self.assertFalse(sb.enrollment_needed())


class PasswordTests(unittest.TestCase):
    def test_letters_and_digits_only(self):
        for password in ("abcd", "Nobara2026", "A" * 32):
            self.assertTrue(sb.valid_password(password), password)
        for password in ("", "abc", "A" * 33, "with space", "abc!1234", "pässwort", "Ünïcode1", "tab\tabc"):
            self.assertFalse(sb.valid_password(password), password)


class EnrollTests(unittest.TestCase):
    def test_password_goes_through_stdin(self):
        with patch.object(sb.subprocess, "run", return_value=completed([], 0)) as run:
            self.assertEqual(sb.enroll("Nobara2026"), (True, ""))
        command = run.call_args.args[0]
        self.assertEqual(command, ["pkexec", sb.ENROLL, "--password-stdin"])
        self.assertNotIn("Nobara2026", " ".join(command))
        self.assertEqual(run.call_args.kwargs["input"], "Nobara2026\n")

    def test_invalid_password_is_not_sent(self):
        with patch.object(sb.subprocess, "run") as run:
            success, message = sb.enroll("abc!")
        self.assertFalse(success)
        self.assertIn("4 to 32", message)
        run.assert_not_called()

    def test_cancelled_authentication(self):
        for code in (126, 127):
            with patch.object(sb.subprocess, "run", return_value=completed([], code)):
                success, message = sb.enroll("Nobara2026")
            self.assertFalse(success)
            self.assertIn("cancelled", message)

    def test_failure_reports_the_error(self):
        with patch.object(sb.subprocess, "run",
                          return_value=completed([], 1, "", "nobara-eac-enroll: mokutil --import failed\n")):
            success, message = sb.enroll("Nobara2026")
        self.assertFalse(success)
        self.assertIn("mokutil --import failed", message)


class NotifyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = patch.dict(sb.os.environ, {"XDG_STATE_HOME": directory.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_dont_ask_again_is_remembered(self):
        with patch.object(sb.subprocess, "run", return_value=completed([], 0, "never\n")):
            self.assertFalse(sb.notify())
        self.assertTrue(sb.declined_marker().exists())
        with patch.object(sb, "enrollment_needed") as needed, patch.object(sb, "show_dialog") as dialog, \
                patch.object(sys, "argv", ["nobara-eac-enroll-notice", "--notify"]):
            sb.main()
        needed.assert_not_called()
        dialog.assert_not_called()

    def test_setup_opens_the_dialog(self):
        with patch.object(sb, "enrollment_needed", return_value=True), \
                patch.object(sb.subprocess, "run", return_value=completed([], 0, "setup\n")), \
                patch.object(sb, "show_dialog") as dialog, \
                patch.object(sys, "argv", ["nobara-eac-enroll-notice", "--notify"]):
            sb.main()
        dialog.assert_called_once()
        self.assertFalse(sb.declined_marker().exists())

    def test_dismissed_notification_asks_again_next_login(self):
        with patch.object(sb.subprocess, "run", return_value=completed([], 0, "")):
            self.assertFalse(sb.notify())
        self.assertFalse(sb.declined_marker().exists())

    def test_no_notification_daemon_shows_the_dialog(self):
        with patch.object(sb.subprocess, "run", return_value=completed([], 1)):
            self.assertTrue(sb.notify())

    def test_nothing_shown_when_not_needed(self):
        with patch.object(sb, "enrollment_needed", return_value=False), \
                patch.object(sb, "notify") as notify, patch.object(sb, "show_dialog") as dialog, \
                patch.object(sys, "argv", ["nobara-eac-enroll-notice", "--notify"]):
            sb.main()
        notify.assert_not_called()
        dialog.assert_not_called()


if __name__ == "__main__":
    unittest.main()
