from __future__ import annotations

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

from nobara_updater import update_backend as backend, update_state as state
from nobara_updater.update_migrations import RSYSLOG_MARKER, keep_rsyslog, plan_migrations, rsyslog_removed


def package(name, arch="x86_64"):
    return dict(name=name, arch=arch)


class PlanTests(unittest.TestCase):
    def test_rsyslog_and_its_modules_are_removed_once(self):
        result = plan_migrations([package("rsyslog"), package("rsyslog-gnutls"), package("rsync")])
        self.assertEqual({"rsyslog", "rsyslog-gnutls"}, {n for n in result.remove if n.startswith("rsyslog")})
        self.assertNotIn("rsync", result.remove)
        self.assertIn("rsyslog-logs", result.hooks)

    def test_rsyslog_reinstalled_after_the_removal_is_kept(self):
        result = plan_migrations([package("rsyslog")], rsyslog_removed=True)
        self.assertNotIn("rsyslog", result.remove)
        self.assertNotIn("rsyslog-logs", result.hooks)

    def test_without_rsyslog_nothing_is_planned(self):
        result = plan_migrations([package("rsyslog-doc", "noarch")])
        self.assertFalse({n for n in result.remove if n.startswith("rsyslog")})
        self.assertNotIn("rsyslog-logs", result.hooks)

    def test_rsyslog_required_by_another_package_is_kept(self):
        result = plan_migrations([package("rsyslog"), package("rsyslog-gnutls"), package("sddm")])
        keep_rsyslog(result, {"logwatch"})
        self.assertFalse({n for n in result.remove if n.startswith("rsyslog")})
        self.assertNotIn("rsyslog-logs", result.hooks)
        self.assertIn("sddm", result.remove)  # other migrations are unaffected

    def test_marker_is_read_below_the_install_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertFalse(rsyslog_removed(root))
            marker = root / RSYSLOG_MARKER.lstrip("/")
            marker.parent.mkdir(parents=True)
            marker.write_text("{}")
            self.assertTrue(rsyslog_removed(root))


class LogCleanupTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.logs = Path(directory.name) / "log"
        self.logs.mkdir()
        self.state = Path(directory.name) / "rsyslog"
        self.state.mkdir()
        (self.state / "imjournal.state").write_text("cursor")
        self.removed = ["messages", "messages-20261108", "secure-20261101", "cron.1.gz", "maillog", "spooler-20261018.xz"]
        self.kept = ["boot.log", "messages.bak", "dnf5.log", "secure-old", "Xorg.0.log"]
        for name in self.removed + self.kept:
            (self.logs / name).write_text("x" * 10)
        (self.logs / "journal").mkdir()
        self.outside = Path(directory.name) / "precious"
        self.outside.write_text("keep")
        (self.logs / "cron").symlink_to(self.outside)

    def run_cleanup(self, installed):
        with patch.object(backend.subprocess, "run", return_value=subprocess.CompletedProcess([], 0 if installed else 1)):
            backend.remove_rsyslog_logs(self.logs, self.state)

    def test_only_rsyslog_logs_are_removed(self):
        self.run_cleanup(installed=False)
        self.assertEqual(sorted(p.name for p in self.logs.iterdir()), sorted(self.kept + ["journal", "cron"]))
        self.assertTrue((self.logs / "cron").is_symlink())
        self.assertEqual(self.outside.read_text(), "keep")
        self.assertFalse(self.state.exists())

    def test_logs_stay_while_rsyslog_is_installed(self):
        self.run_cleanup(installed=True)
        self.assertTrue(all((self.logs / name).exists() for name in self.removed))
        self.assertTrue(self.state.exists())

    def test_hook_is_known(self):
        with patch.object(backend, "remove_rsyslog_logs") as cleanup:
            backend.apply_hooks(["rsyslog-logs"])
        cleanup.assert_called_once_with()


class FinalizeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        job = self.root / "jobs" / ("a" * 32)
        job.mkdir(parents=True)
        (job / "transaction.json").write_text("{}")
        ready = dict(job="a" * 32, installer=True, started=True, fingerprint="fixture", target_release="44",
                     files={"transaction.json": state.file_digest(job / "transaction.json")},
                     migrations={"hooks": ["rsyslog-logs"]}, packages=[])
        state.write_state(self.root, ready, "validating")
        self.addCleanup(patch.stopall)
        patch.object(backend, "in_installer_root", return_value=True).start()
        patch.object(backend, "rpm_fingerprint", return_value="fixture").start()
        patch.object(backend, "apply_hooks", return_value={}).start()
        patch.object(backend, "os_release", return_value="44").start()
        self.marker = self.root / Path(RSYSLOG_MARKER).name

    def test_marker_is_written_after_a_successful_update(self):
        with patch.object(backend, "validate_boot"):
            backend.finalize(self.root, installer=True)
        self.assertTrue(self.marker.is_file())

    def test_failed_update_leaves_no_marker(self):
        with patch.object(backend, "validate_boot", side_effect=state.UpdateError("dracut failed")):
            with self.assertRaisesRegex(state.UpdateError, "dracut failed"):
                backend.finalize(self.root, installer=True)
        self.assertFalse(self.marker.exists())


if __name__ == "__main__":
    unittest.main()
