"""Compatibility tests against dnf-app-center's CLI result consumers."""
import ast
import importlib.util
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
    output_module = ROOT / "appcenter/update_output.py"
    if output_module.is_file():
        spec = importlib.util.spec_from_file_location("appcenter_update_output_test", output_module)
        output_api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(output_api)
        namespace["update_output"] = output_api
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

    def test_log_event_never_shows_raw_status_metadata(self):
        ns = functions_from_file("ui.py", {"_handle_queue_event"}, "MainWindow")
        window = Mock()
        item = types.SimpleNamespace(action="system-update", display_name="System Update")
        message = 'NOBARA_UPDATE_RESULT {"status":"scheduled","message":"Restart to install.","reboot_required":true}'
        ns["_handle_queue_event"](window, item, dict(event="log", message=message))
        self.assertEqual(item.message, "Restart to install.")
        window._append_queue_log.assert_called_once_with("System Update: Restart to install.")

    def test_display_filters_metadata_in_prefixed_multiline_logs_without_losing_errors(self):
        ns = functions_from_file("ui.py", {"_append_queue_log"}, "MainWindow")
        window = types.SimpleNamespace(queue_log_full=[], queue_logs=[])
        message = ('System Update: Preparation succeeded\n'
                   '2026-10-02 INFO \x1b[32mNOBARA_UPDATE_RESULT {"message":"Restart to install."}\x1b[0m\n'
                   'Actual error: network unavailable')
        ns["_append_queue_log"](window, message)
        text = "\n".join(window.queue_log_full)
        self.assertNotIn("NOBARA_UPDATE_RESULT", text)
        self.assertNotIn('"message"', text)
        self.assertIn("Restart to install.", text)
        self.assertIn("Actual error: network unavailable", text)

    def test_prefixed_results_keep_restart_event_and_stay_out_of_helper_logs(self):
        result = dict(status="scheduled", message="Restart to install.", reboot_required=True, automatic_recovery=True)
        for prefix in ("  ", "2026-10-02 INFO ", "System Update: \x1b[32m"):
            for helper in (True, False):
                with self.subTest(prefix=prefix, helper=helper):
                    process = Mock(stdout=io.StringIO(prefix + "NOBARA_UPDATE_RESULT " + json.dumps(result) + "\x1b[0m\n"))
                    process.wait.return_value = 0
                    events = []
                    if helper:
                        ns = functions_from_file("privileged_helper.py", {"_nobara_update_result", "_run_system_update"})
                        ns["emit"] = lambda event, **fields: events.append(dict(event=event, **fields))
                        run = lambda: ns["_run_system_update"]()
                    else:
                        ns = functions_from_file("dnf_backend.py", {"_run_nobara_sync_cli"}, "DnfBackend")
                        run = lambda: ns["_run_nobara_sync_cli"](Mock(), event_cb=events.append)
                    with patch.object(subprocess, "Popen", return_value=process):
                        self.assertEqual(run(), (True, "Restart to install."))
                    self.assertIn(dict(event="update-status", **result), events)
                    self.assertFalse(any(e["event"] == "log" and "NOBARA_UPDATE_RESULT" in e["message"] for e in events))

    def test_failed_helper_hides_prefixed_metadata_without_claiming_success(self):
        ns = functions_from_file("privileged_helper.py", {"_nobara_update_result", "_run_system_update"})
        process = Mock(stdout=io.StringIO('  NOBARA_UPDATE_RESULT {"message":"Restart to install."}\nReal failure\n'))
        process.wait.return_value = 1
        events = []
        ns["emit"] = lambda event, **fields: events.append(dict(event=event, **fields))
        with patch.object(subprocess, "Popen", return_value=process):
            self.assertEqual(ns["_run_system_update"](), (False, "Real failure"))
        self.assertFalse(any(e["event"] == "update-status" for e in events))

    def test_malformed_metadata_is_not_displayed_and_does_not_crash(self):
        ns = functions_from_file("ui.py", {"_append_queue_log"}, "MainWindow")
        window = types.SimpleNamespace(queue_log_full=[], queue_logs=[])
        for payload in ("[]", "bad", '{"message":23}'):
            ns["_append_queue_log"](window, "System Update: NOBARA_UPDATE_RESULT " + payload)
        self.assertEqual(window.queue_log_full, [])

    def test_system_update_completion_refreshes_the_separate_tray_checker(self):
        ns = functions_from_file("ui.py", {"_queue_item_finished"}, "MainWindow")
        ns.update(Gio=Mock(), GLib=Mock())
        window = Mock()
        item = types.SimpleNamespace(action="system-update", display_name="System Update")
        ns["_queue_item_finished"](window, item, True, "Applications updated.")
        call = ns["Gio"].bus_get_sync.return_value.call
        call.assert_called_once()
        self.assertEqual(call.call_args.args[3], "RefreshUpdates")

    def test_completion_summary_and_toast_use_only_the_human_status_message(self):
        ns = functions_from_file("ui.py", {"_queue_item_finished"}, "MainWindow")
        ns.update(Gio=Mock(), GLib=Mock())
        window = Mock()
        item = types.SimpleNamespace(action="system-update", display_name="System Update")
        message = 'NOBARA_UPDATE_RESULT {"message":"Restart to install.","reboot_required":true}'
        ns["_queue_item_finished"](window, item, True, message)
        self.assertEqual(item.message, "Restart to install.")
        window._append_queue_log.assert_called_once_with("System Update: Restart to install.")
        window._show_toast.assert_called_once_with("Restart to install.")

    def test_external_package_change_refreshes_tray_before_next_scheduled_check(self):
        ns = functions_from_file("updater_service.py", {"schedule"}, "Updater")
        ns.update(load_updater_settings=lambda: {"enabled": True},
                  updater_interval_seconds=lambda settings: 21600,
                  time=types.SimpleNamespace(monotonic=lambda: 101),
                  _rpmdb_signature=lambda: ("new-rpmdb",))
        updater = Mock(_last_check_monotonic=100, _last_rpmdb_signature=("old-rpmdb",))
        self.assertTrue(ns["schedule"](updater))
        updater.refresh_updates.assert_called_once_with(False)

    def test_disabling_checker_while_check_runs_cannot_restore_tray_icon(self):
        ns = functions_from_file("updater_service.py", {"_apply_update_count"}, "Updater")
        ns.update(load_updater_settings=lambda: {"enabled": False},
                  _notifications_allowed=lambda settings: True)
        updater = Mock()
        ns["_apply_update_count"](updater, 10)
        updater.indicator.set_updates.assert_called_once_with(0)
        updater.notification.send.assert_called_once_with(0)


if __name__ == "__main__":
    unittest.main()
