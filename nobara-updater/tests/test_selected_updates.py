"""Selected-update input, service lifetime and frontend progress contracts."""
import contextlib
import importlib.util
import io
import json
import logging
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_update_cli import cli, client
from test_frontend_contract import ROOT, functions_from_file
from nobara_updater import update_state as state, update_progress as progress


def load_module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "appcenter" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SelectedClientTests(unittest.TestCase):
    def test_cli_forwards_selection_and_progress_but_flatpaks_remain_separate(self):
        args = cli.parse_args(["cli", "--package=editor", "--package=library.i686", "--all", "--progress"])
        with patch.object(cli, "in_installer_root", return_value=False), patch.object(cli, "original_user", return_value=None), \
             patch.object(cli, "prepare_update", return_value=True) as prepare, \
             patch.object(cli, "read_state", return_value={"status": "unchanged"}), \
             patch.object(cli, "update_flatpaks") as flatpaks, contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(cli.dispatch(args))
        prepare.assert_called_once_with(cli.LOG, codecs=False, packages=["editor", "library.i686"], progress=True)
        flatpaks.assert_called_once_with(None)

    def test_no_empty_glob_path_or_option_can_be_used_as_a_selection(self):
        for value in ([], ["*"], ["--all"], ["/tmp/package.rpm"], ["pkg\nother"], "editor", [None]):
            with self.subTest(value=value), self.assertRaises(state.UpdateError):
                state.package_selection(value)
        self.assertEqual(state.package_selection(["pkg.i686", "editor", "editor"]), ["editor", "pkg.i686"])
        self.assertIsNone(state.package_selection(None))

    def test_service_instance_reads_durable_selection_and_client_cleans_it_after_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def run(service, logger, success, **kwargs):
                request = service.split("@", 1)[1].removesuffix(".service")
                self.assertEqual(state.read_request(request, root), ["editor"])
                self.assertEqual((root / "requests" / (request + ".json")).stat().st_mode & 0o777, 0o600)
                return True
            with patch.object(client, "STATE_DIR", root), patch.object(client, "read_state", return_value={"status": "idle"}), \
                 patch.object(client, "run_service", side_effect=run):
                self.assertTrue(client.prepare_update(logging.getLogger(), packages=["editor"], progress=True))
            self.assertFalse(list((root / "requests").iterdir()))

    def test_client_disconnect_preserves_worker_input(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(client, "STATE_DIR", Path(directory)), \
             patch.object(client, "read_state", return_value={"status": "idle"}), \
             patch.object(client, "run_service", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                client.prepare_update(logging.getLogger(), packages=["editor"])
            self.assertEqual(len(list((Path(directory) / "requests").glob("*.json"))), 1)

    def test_failed_systemctl_wait_does_not_delete_a_running_workers_request(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(client, "STATE_DIR", Path(directory)), \
             patch.object(client, "read_state", return_value={"status": "idle"}), \
             patch.object(client, "run_service", return_value=False):
            self.assertFalse(client.prepare_update(logging.getLogger(), packages=["editor"]))
            self.assertEqual(len(list((Path(directory) / "requests").glob("*.json"))), 1)

    def test_different_pending_selection_cannot_silently_install_an_old_plan(self):
        for packages, saved in ((["editor"], None), (None, ["editor"]), (["editor"], ["browser"])):
            with patch.object(client, "read_state", return_value={"status": "ready", "selection": saved}), \
                 patch.object(client, "worker_action", return_value=True), patch.object(client, "run_service") as run:
                with self.assertRaisesRegex(state.UpdateError, "different package selection"):
                    client.prepare_update(logging.getLogger(), packages=packages)
                run.assert_not_called()

    def test_reattached_client_receives_the_resolved_plan_and_completed_downloads(self):
        saved = dict(status="ready", job="a" * 32, packages=[dict(name="dependency", arch="noarch", nevra="dependency-0:2-1.noarch", action="Upgrade")])
        with patch.object(client, "read_state", side_effect=[{"status": "preparing"}, saved]), \
             patch.object(client, "run_service", return_value=True), patch.object(progress, "emit_progress") as emit:
            self.assertTrue(client.prepare_update(logging.getLogger(), progress=True))
        self.assertEqual(emit.call_args_list[0].args, ("plan",))
        self.assertEqual(emit.call_args_list[0].kwargs["packages"][0]["name"], "dependency")
        self.assertEqual(emit.call_args_list[-1].kwargs["phase"], "downloaded")

    def test_progress_is_opt_in_and_final_service_log_is_drained(self):
        for enabled in (False, True):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                proc = Mock(returncode=0)
                def poll():
                    (root / "update.log").write_text('NOBARA_UPDATE_PROGRESS {"event":"plan"}\nlast human message\n')
                    return 0
                proc.poll.side_effect = poll
                logger = Mock()
                with patch.object(client, "STATE_DIR", root), patch.object(client.subprocess, "Popen", return_value=proc), \
                     patch.object(client, "read_state", return_value={"status": "ready"}):
                    self.assertTrue(client.run_service("fixture.service", logger, {"ready"}, progress=enabled))
                output = "\n".join(call.args[1] for call in logger.info.call_args_list)
                self.assertIn("last human message", output)
                self.assertEqual(progress.EVENT_PREFIX in output, enabled)

    def test_log_rotation_and_partial_lines_do_not_drop_progress_events(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            logfile = root / "update.log"
            logfile.write_text("old unrelated log\n")
            message = 'NOBARA_UPDATE_PROGRESS {"version":1,"event":"package"}\n'
            polls = 0
            def poll():
                nonlocal polls
                polls += 1
                if polls == 1:
                    with logfile.open("a") as stream:
                        stream.write(message[:20])
                    return None
                if polls == 2:
                    with logfile.open("a") as stream:
                        stream.write(message[20:])
                    logfile.rename(root / "update.log.1")
                    logfile.write_text("new logfile\n")
                    return 0
            proc = Mock(returncode=0)
            proc.poll.side_effect = poll
            logger = Mock()
            with patch.object(client, "STATE_DIR", root), patch.object(client.subprocess, "Popen", return_value=proc), \
                 patch.object(client.time, "sleep"), patch.object(client, "read_state", return_value={"status": "ready"}):
                self.assertTrue(client.run_service("fixture.service", logger, {"ready"}, progress=True))
            self.assertEqual([call.args[1] for call in logger.info.call_args_list], [message.rstrip(), "new logfile"])


@unittest.skipUnless((ROOT / "appcenter/update_queue.py").exists(), "Updated App Center unavailable")
class SelectedFrontendTests(unittest.TestCase):
    def setUp(self):
        self.api = load_module("update_queue")
        self.output = load_module("update_output")

    def plan(self, model, job="job", stage="main", names=("editor", "dependency")):
        records = [dict(id="Upgrade:" + name, name=name, action="Upgrade", nevra=name + "-2.x86_64") for name in names]
        model.handle(dict(event="plan", transaction=job, stage=stage, packages=records))

    def test_selected_items_expand_to_actual_plan_and_offline_never_claims_installed(self):
        model = self.api.UpdateProgress(["editor"])
        self.plan(model)
        model.handle(dict(event="package", transaction="job", id="Upgrade:editor", phase="downloaded", fraction=1))
        self.assertEqual(model.summary(), (0.5, "Downloading: 1/2 processed items"))
        model.finish(True, {"status": "scheduled"})
        self.assertEqual(model.summary(), (1, "Prepared for restart: 2/2 processed items"))
        self.assertTrue(all("restart" in model.label(row) for row in model.rows.values()))
        self.assertFalse(any("Installed" in model.label(row) for row in model.rows.values()))

    def test_live_install_has_distinct_progress_and_requires_successful_finalization(self):
        model = self.api.UpdateProgress(["editor"])
        self.plan(model)
        for name in ("editor", "dependency"):
            model.handle(dict(event="package", transaction="job", id="Upgrade:" + name, phase="downloaded", fraction=1))
        model.handle(dict(event="package", transaction="job", id="Upgrade:editor", phase="waiting-install", fraction=0))
        self.assertEqual(model.summary()[0], 0)
        model.handle(dict(event="package", transaction="job", id="Upgrade:editor", phase="applying", fraction=1))
        self.assertEqual(model.summary()[0], 0.5)
        model.finish(False, {})
        self.assertTrue(all(row["phase"] == "failed" for row in model.rows.values()))
        model.finish(True, {"status": "live-complete"})
        self.assertTrue(all(row["phase"] == "complete" for row in model.rows.values()))

    def test_retry_discards_stale_main_rows_and_retains_early_upgrade(self):
        model = self.api.UpdateProgress(["editor"])
        self.plan(model, job="early", stage="early", names=("nobara-updater",))
        self.plan(model, job="attempt1", names=("old-dependency",))
        self.plan(model, job="attempt2")
        self.assertEqual({row["name"] for row in model.rows.values()}, {"nobara-updater", "editor", "dependency"})
        before = dict(model.rows)
        self.plan(model, job="attempt2")
        self.assertEqual(before, model.rows)

    def test_select_all_does_not_enqueue_or_start_updates(self):
        ns = functions_from_file("ui.py", {"_select_all_updates"}, "MainWindow")
        window = Mock(current_items=[Mock(primary_pkg="editor"), Mock(primary_pkg="browser")], update_selection=set())
        ns["_select_all_updates"](window)
        self.assertEqual(window.update_selection, {"editor", "browser"})
        window._queue_system_update.assert_not_called()
        window._start_queue_worker.assert_not_called()

    def test_selected_button_passes_only_selected_packages_and_single_update_uses_sync(self):
        ns = functions_from_file("ui.py", {"_queue_selected_updates", "_enqueue_update_batch"}, "MainWindow")
        apps = [Mock(primary_pkg="editor"), Mock(primary_pkg="browser")]
        window = Mock(current_items=apps, update_selection={"editor"})
        ns["_queue_selected_updates"](window)
        window._enqueue_update_batch.assert_called_once_with(apps[:1])
        window._queued_state_label.return_value = None
        window._should_use_nobara_sync.return_value = True
        ns["_enqueue_update_batch"](window, apps[:1])
        window._queue_system_update.assert_called_once_with(apps[:1])

    def test_frontend_arguments_preserve_selection_and_reject_other_options(self):
        values = ["--progress", "--package=editor", "--package=lib.i686", "--all"]
        for filename, name in (("privileged_helper.py", None), ("dnf_backend.py", "DnfBackend")):
            ns = functions_from_file(filename, {"_system_update_args"}, name)
            fn = ns["_system_update_args"]
            self.assertEqual(fn(Mock(), values) if name else fn(values), values)
        with self.assertRaises(ValueError):
            self.output.sync_arguments(["--installroot=/"])

    def test_both_helpers_stream_structured_progress_without_json_in_logs(self):
        event = dict(version=1, event="plan", transaction="job", stage="main", packages=[])
        line = "2026-10-05 INFO " + progress.EVENT_PREFIX + json.dumps(event)
        for helper in (True, False):
            process = Mock(stdout=io.StringIO(line + '\nActual error\n'))
            process.wait.return_value = 1
            events = []
            if helper:
                ns = functions_from_file("privileged_helper.py", {"_run_system_update", "_nobara_update_result"})
                ns["emit"] = lambda event, **fields: events.append(dict(event=event, **fields))
                run = ns["_run_system_update"]
            else:
                ns = functions_from_file("dnf_backend.py", {"_run_nobara_sync_cli"}, "DnfBackend")
                run = lambda: ns["_run_nobara_sync_cli"](Mock(_looks_like_dependency_conflict=lambda _: False), event_cb=events.append)
            with patch("subprocess.Popen", return_value=process):
                self.assertEqual(run(), (False, "Actual error"))
            self.assertIn(dict(event="update-progress", progress=event), events)
        self.assertEqual(self.output.visible_text(line + '\nActual error'), "Actual error")


class NativeProgressParsingTests(unittest.TestCase):
    def test_multilib_and_truncated_identity_is_never_guessed(self):
        records = [dict(name="editor", arch=arch, nevra="editor-0:2-1." + arch, action="Upgrade") for arch in ("x86_64", "i686")]
        reporter = progress.PackageProgress("job", records)
        self.assertIsNone(reporter.match("editor-0:2-1..."))
        with patch.object(progress, "emit_progress") as emit:
            reporter.transaction_line("[1/2] Upgrading editor-0:2-1.i686 | 100% | done")
            emit.assert_called_once_with("package", transaction="job", id="Upgrade:editor-0:2-1.i686", phase="applying", fraction=1)

    def test_scriptlet_output_and_verification_lines_do_not_mark_rpms_installed(self):
        reporter = progress.PackageProgress("job", [dict(name="editor", arch="noarch", nevra="editor-0:2-1.noarch", action="Upgrade")])
        with patch.object(progress, "emit_progress") as emit:
            for line in (">>> [1/1] Upgrading editor-0:2-1.noarch | 100% | done", "[1/1] Verify package files | 100% | done", "[2/1] Upgrading editor-0:2-1.noarch | 100% | done"):
                reporter.transaction_line(line)
            emit.assert_not_called()
