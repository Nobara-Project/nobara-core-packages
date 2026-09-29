"""Compatibility tests against dnf-app-center's CLI result consumers."""
import ast
import io
import json
import os
from pathlib import Path
import subprocess
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(os.environ.get("NOBARA_APP_CENTER_SOURCE", str(Path(__file__).resolve().parents[2] / "dnf-app-center")))


def functions_from_file(filename, names, class_name=None):
    tree = ast.parse((ROOT / "appcenter" / filename).read_text())
    nodes = tree.body
    if class_name:
        nodes = next(n for n in nodes if isinstance(n, ast.ClassDef) and n.name == class_name).body
    selected = [n for n in nodes if isinstance(n, ast.FunctionDef) and n.name in names]
    future = ast.parse("from __future__ import annotations").body
    module = ast.Module(body=future + selected, type_ignores=[])
    namespace = dict(json=json, os=os, subprocess=subprocess, emit=lambda *a, **k: None,
                     _looks_like_dependency_conflict=lambda lines: False)
    exec(compile(module, filename, "exec"), namespace)
    return namespace


@unittest.skipUnless((ROOT / "appcenter/privileged_helper.py").is_file(), "dnf-app-center checkout unavailable")
class FrontendContractTests(unittest.TestCase):
    def test_privileged_helper_reports_staging_instead_of_completed_installation(self):
        ns = functions_from_file("privileged_helper.py", {"_nobara_update_result", "_run_system_update"})
        self.assertIn("_nobara_update_result", ns, "Apply the dnf-app-center integration patch")
        result = dict(status="scheduled", message="Restart to install.", reboot_required=True)
        process = Mock(stdout=io.StringIO("NOBARA_UPDATE_RESULT " + json.dumps(result) + "\n"))
        process.wait.return_value = 0
        events = []
        ns["emit"] = lambda event, **fields: events.append(dict(event=event, **fields))
        with patch.object(subprocess, "Popen", return_value=process):
            self.assertEqual(ns["_run_system_update"](), (True, "Restart to install."))
        self.assertIn(dict(event="update-status", **result), events)
        self.assertFalse(any(e["event"] == "log" and "NOBARA_UPDATE_RESULT" in e["message"] for e in events))

    def test_direct_root_backend_uses_same_staging_result(self):
        ns = functions_from_file("dnf_backend.py", {"_run_nobara_sync_cli"}, "DnfBackend")
        result = dict(status="scheduled", message="Restart to install.", reboot_required=True)
        process = Mock(stdout=io.StringIO("NOBARA_UPDATE_RESULT " + json.dumps(result) + "\n"))
        process.wait.return_value = 0
        events = []
        with patch.object(subprocess, "Popen", return_value=process):
            self.assertEqual(ns["_run_nobara_sync_cli"](Mock(), event_cb=events.append), (True, "Restart to install."))
        self.assertTrue(any(event["event"] == "update-status" for event in events))
        self.assertFalse(any(e["event"] == "log" and "NOBARA_UPDATE_RESULT" in e["message"] for e in events))

    def test_nonzero_exit_remains_failure_even_with_a_success_marker(self):
        ns = functions_from_file("privileged_helper.py", {"_nobara_update_result", "_run_system_update"})
        process = Mock(stdout=io.StringIO('NOBARA_UPDATE_RESULT {"message":"Ready","status":"scheduled"}\n'))
        process.wait.return_value = 1
        with patch.object(subprocess, "Popen", return_value=process):
            ok, message = ns["_run_system_update"]()
            self.assertFalse(ok)
            self.assertNotIn("NOBARA_UPDATE_RESULT", message)

    def test_malformed_marker_does_not_crash_older_cli_compatibility(self):
        ns = functions_from_file("privileged_helper.py", {"_nobara_update_result"})
        self.assertEqual(ns["_nobara_update_result"](["NOBARA_UPDATE_RESULT []", "NOBARA_UPDATE_RESULT bad"]), {})

    def test_helper_reports_live_completion_without_reboot(self):
        ns = functions_from_file("privileged_helper.py", {"_nobara_update_result", "_run_system_update"})
        result = dict(status="live-complete", message="Applications updated.", reboot_required=False)
        process = Mock(stdout=io.StringIO("NOBARA_UPDATE_RESULT " + json.dumps(result) + "\n"))
        process.wait.return_value = 0
        events = []
        ns["emit"] = lambda event, **fields: events.append(dict(event=event, **fields))
        with patch.object(subprocess, "Popen", return_value=process):
            self.assertEqual(ns["_run_system_update"](), (True, result["message"]))
        self.assertIn(dict(event="update-status", **result), events)

    def test_result_event_shows_restart_banner_only_when_required_without_relogging(self):
        ns = functions_from_file("ui.py", {"_handle_queue_event"}, "MainWindow")
        window = Mock()
        item = types.SimpleNamespace(action="system-update", display_name="System Update")
        for required in (True, False):
            window.reset_mock()
            payload = dict(event="update-status", reboot_required=required, message="Update result")
            ns["_handle_queue_event"](window, item, payload)
            window.update_banner.set_revealed.assert_called_once_with(required)
            window.update_banner.set_title.assert_called_once_with("Update result")
            window._append_queue_log.assert_not_called()
            self.assertEqual(item.update_result, payload)

    def test_identical_consecutive_completion_messages_are_not_repeated(self):
        ns = functions_from_file("ui.py", {"_append_queue_log"}, "MainWindow")
        window = types.SimpleNamespace(queue_log_full=[], queue_logs=[])
        for _ in range(2):
            ns["_append_queue_log"](window, "System Update: Restart to install.")
        self.assertEqual(window.queue_log_full, ["System Update: Restart to install."])
        self.assertEqual(window.queue_logs, window.queue_log_full)


if __name__ == "__main__":
    unittest.main()
