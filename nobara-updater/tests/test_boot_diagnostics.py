"""Boot selection evidence is useful, bounded, redacted, and survives rollback."""
import json
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


class BootDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.boot = self.root / "boot"
        self.archive = self.boot / "nobara-updater" / JOB
        self.archive.mkdir(parents=True)
        self.entries = self.boot / "loader/entries"
        self.entries.mkdir(parents=True)
        self.state = dict(job=JOB, status="interrupted", started=True, recovery=dict(created=True, entry_id="nobara-recovery-" + JOB),
                          files={"PRIVATE-TRANSACTION-FIELD": "do-not-publish"}, recovery_blocked=True)
        self.cmdline = self.root / "cmdline"
        self.cmdline.write_text("root=UUID=fixture rootflags=subvol=@ ro rd.luks.uuid=luks-id arbitrary.secret=COMMANDLINE_SECRET\n")
        self.bootid = self.root / "boot-id"
        self.bootid.write_text("test-boot-id\n")
        entry = "title Nobara\nversion old\nlinux /saved-kernel\ninitrd /saved-initrd\noptions root=UUID=fixture rootflags=subvol=@ password=BLS_SECRET\n"
        (self.entries / ("nobara-recovery-" + JOB + ".conf")).write_text(entry)
        (self.archive / "recovery-entry.conf").write_text(entry.replace("subvol=@", "subvol=.nobara-updater/" + JOB + "/root"))
        stub = self.boot / "efi/EFI/fedora/grub.cfg"
        stub.parent.mkdir(parents=True)
        stub.write_text("search --no-floppy --fs-uuid --set=dev boot-uuid\nset prefix=($dev)/grub2\nconfigfile $prefix/grub.cfg\nset secret=STUB_SECRET\n")
        nobara_stub = self.boot / "efi/EFI/nobara/grub.cfg"
        nobara_stub.parent.mkdir(parents=True)
        nobara_stub.write_text(stub.read_text())
        self.boot_contents = {path: path.read_bytes() for path in self.boot.rglob("*") if path.is_file()}
        for patcher in (patch.object(reports, "BOOT_REPORTS", self.archive.parent),
                        patch.object(reports, "REPORTS", self.root / "public"),
                        patch.object(reports, "STATE_DIR", self.root / "state"),
                        patch.object(reports, "CMDLINE", self.cmdline),
                        patch.object(reports, "BOOT_ID", self.bootid), patch.object(reports.os, "sync"),
                        patch.object(reports.subprocess, "run", side_effect=self.command)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def command(self, args, **kwargs):
        if args[0] == "grub2-editenv":
            output = "saved_entry=normal\nnobara_fallback=nobara-recovery-" + JOB + "\nkernelopts=root=UUID=fixture password=ENV_SECRET\nprivate=OTHER_SECRET\n"
        elif args[0] == "findmnt":
            self.assertIn(args[-1], {"/", "/boot", "/boot/efi"})
            output = json.dumps(dict(filesystems=[dict(target=args[-1], uuid="fixture", fsroot="/@")]))
        elif args[0] == "efibootmgr":
            output = "BootCurrent: 0001\nBootOrder: 0001,0000\nBoot0001* Nobara\n"
        else:
            self.fail("Unexpected diagnostic command: " + str(args))
        kwargs["stdout"].write(output.encode())
        return subprocess.CompletedProcess(args, 0)

    def test_boot_evidence_is_in_text_and_offline_bundle_without_private_settings(self):
        reports.capture_boot_diagnostics(self.state, "failure")
        reports.capture_boot_diagnostics(self.state, "recovery-retry-before")
        reports.capture_boot_diagnostics(self.state, "recovery-retry-after",
                                         note="Retry refused: https://user:pass@example.test/?token=QUERY_SECRET")
        original = (self.archive / "failure-boot.log").read_text()
        self.cmdline.write_text("root=UUID=fixture rootflags=subvol=recovered\n")
        reports.publish_recovery(JOB)
        result = reports.latest_report()
        text = Path(result["log"]).read_text()
        for expected in ("subvol=@", "subvol=recovered", ".nobara-updater/" + JOB + "/root", "nobara_fallback=",
                         "BootCurrent: 0001", "boot-uuid", "test-boot-id", "Retry refused", "recovery-retry-after-boot.log"):
            self.assertIn(expected, text)
        for secret in ("COMMANDLINE_SECRET", "BLS_SECRET", "ENV_SECRET", "OTHER_SECRET", "STUB_SECRET", "QUERY_SECRET", "user:pass", "PRIVATE-TRANSACTION-FIELD"):
            self.assertNotIn(secret, text)
        self.assertEqual((self.archive / "failure-boot.log").read_text(), original)
        for path, contents in self.boot_contents.items():
            self.assertEqual(path.read_bytes(), contents)
        reports.capture_boot_diagnostics(self.state, "recovery-retry-before")
        self.assertNotIn("subvol=recovered", (self.archive / "recovery-retry-before-boot.log").read_text())
        self.assertEqual((self.archive / "failure-boot.log").stat().st_mode & 0o777, 0o600)
        with tarfile.open(result["bundle"]) as archive:
            self.assertEqual(archive.extractfile("update.log").read().decode(), text)

    def test_timed_out_tools_and_unreadable_boot_files_do_not_prevent_failure_report(self):
        def timeout(args, **kwargs):
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])
        with patch.object(reports.subprocess, "run", side_effect=timeout):
            reports.publish_failure(self.state)
        report = reports.latest_report()
        self.assertTrue(Path(report["bundle"]).is_file())
        self.assertIn("timed out", Path(report["log"]).read_text())

    def test_oversized_and_symlinked_entries_do_not_publish_unrelated_files(self):
        secret = self.root / "secret"
        secret.write_text("title SYMLINK_SECRET\n")
        (self.entries / "symlink.conf").symlink_to(secret)
        (self.entries / "huge.conf").write_text("title " + "x" * 100000)
        reports.capture_boot_diagnostics(self.state, "failure")
        text = (self.archive / "failure-boot.log").read_text()
        self.assertLessEqual(len(text), 65537)
        self.assertNotIn("SYMLINK_SECRET", text)


if __name__ == "__main__":
    unittest.main()
