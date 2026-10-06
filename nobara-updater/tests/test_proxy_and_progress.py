"""Proxy settings reach the downloads, and downloads report their progress.

The unit tests use fakes only. The two integration tests prepare a real
libdnf5 transaction in a disposable root, and the proxy test reaches its
repository only through a local fake proxy (no network, no host services).
"""
import contextlib
import http.server
import importlib.util
import io
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import types
import unittest
import urllib.parse
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1] / "src"
if "nobara_updater" not in sys.modules:
    package = types.ModuleType("nobara_updater")
    package.__path__ = [str(SOURCE)]
    sys.modules["nobara_updater"] = package

from nobara_updater import update_backend as backend, update_client as client, update_plan as planner, update_state as state

spec = importlib.util.spec_from_file_location("nobara_cli_proxy_test", SOURCE / "nobara_sync.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)

PROXY_NAMES = ("http_proxy", "https_proxy", "HTTPS_PROXY", "HTTP_PROXY", "all_proxy", "ALL_PROXY", "no_proxy", "NO_PROXY")


def without_proxies(**values):
    """An environment patch that starts from no proxy settings at all."""
    patcher = patch.dict(os.environ, values)
    patcher.start()
    for name in PROXY_NAMES:
        if name not in values:
            os.environ.pop(name, None)
    return patcher


def load_worker():
    spec = importlib.util.spec_from_file_location("proxy_worker_test", SOURCE / "update_worker.py")
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    return worker


class ProxySettingsTests(unittest.TestCase):
    def test_usual_proxy_values_are_accepted(self):
        for name, value in (("https_proxy", "http://proxy.example:3128"),
                            ("HTTPS_PROXY", "http://user:p%40ss@10.0.0.1:8080/"),
                            ("all_proxy", "socks5h://[fd00::1]:1080"),
                            ("http_proxy", "proxy.example:3128"),
                            ("no_proxy", "localhost,127.0.0.1,.example.com, 10.0.0.0/8,[::1]"),
                            ("NO_PROXY", "*")):
            with self.subTest(name=name, value=value):
                self.assertEqual(state.proxy_settings({name: value}), ({name: value}, []))

    def test_values_that_are_not_a_proxy_are_refused(self):
        for name, value in (("https_proxy", "http://proxy:3128\nLD_PRELOAD=/tmp/x.so"),
                            ("https_proxy", "http://proxy:3128 --upload-file /etc/shadow"),
                            ("https_proxy", "ftp://proxy:21"),
                            ("https_proxy", "http://:3128"),
                            ("https_proxy", "http://proxy:99999"),
                            ("https_proxy", "http://[fd00::1:3128"),
                            ("https_proxy", "http://proxy:3128/path?query"),
                            ("https_proxy", "http://$(reboot):3128"),
                            ("https_proxy", "http://`reboot`:3128"),
                            ("https_proxy", "http://" + "a" * 2048 + ":3128"),
                            ("no_proxy", "localhost;reboot"),
                            ("no_proxy", "localhost\nLD_PRELOAD=/tmp/x.so"),
                            ("no_proxy", "'localhost'")):
            with self.subTest(name=name, value=value):
                self.assertEqual(state.proxy_settings({name: value}), ({}, [name]))
        self.assertEqual(state.proxy_settings({"https_proxy": 3128}), ({}, ["https_proxy"]))

    def test_only_variables_curl_reads_for_proxies_are_considered(self):
        environ = {"LD_PRELOAD": "/tmp/x.so", "PYTHONPATH": "/tmp", "HTTP_PROXY": "http://proxy:3128",
                   "https_proxy": "http://proxy:3128", "no_proxy": ""}
        self.assertEqual(state.proxy_settings(environ), ({"https_proxy": "http://proxy:3128"}, []))


class SudoTests(unittest.TestCase):
    def elevate(self, **environment):
        patcher = without_proxies(**environment)
        self.addCleanup(patcher.stop)
        with patch.object(cli.os, "geteuid", return_value=1000), patch.object(cli.os, "execvp") as execvp, \
             patch.object(cli.sys, "argv", ["nobara-sync", "cli", "--all"]), \
             contextlib.redirect_stderr(io.StringIO()) as errors:
            cli.elevate()
        command = execvp.call_args.args[1]
        self.assertEqual(execvp.call_args.args[0], "sudo")
        self.assertEqual(command[-3:], [str((SOURCE / "nobara_sync.py").resolve()), "cli", "--all"])
        return command, errors.getvalue()

    def test_proxy_settings_survive_sudo_env_reset(self):
        command, _ = self.elevate(https_proxy="http://proxy.example:3128", no_proxy="localhost",
                                  HTTP_PROXY="http://ignored:3128", LD_PRELOAD="/tmp/x.so")
        self.assertEqual(command[:3], ["sudo", "--preserve-env=https_proxy,no_proxy", "--"])

    def test_sudo_command_is_unchanged_without_proxy_settings(self):
        command, errors = self.elevate()
        self.assertEqual(command[:2], ["sudo", "--"])
        self.assertEqual(errors, "")

    def test_invalid_proxy_setting_is_reported_and_not_preserved(self):
        command, errors = self.elevate(https_proxy="http://proxy:3128\nLD_PRELOAD=/tmp/x.so",
                                       http_proxy="http://proxy.example:3128")
        self.assertEqual(command[:3], ["sudo", "--preserve-env=http_proxy", "--"])
        self.assertIn("Ignoring https_proxy", errors)
        self.assertNotIn("LD_PRELOAD", errors)


class ServiceHandoverTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.handover = self.directory / "nobara-updater-proxy.json"
        for patcher in (patch.object(client, "PROXY_FILE", self.handover, create=True),
                        patch.object(client, "STATE_DIR", self.directory),
                        patch.object(client, "read_state", side_effect=[{"status": "idle"}, {"status": "ready"}])):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.seen = []

        def start(command):
            # What the service would find when systemd starts it.
            if self.handover.exists():
                self.seen.append((json.loads(self.handover.read_text()), self.handover.stat().st_mode & 0o777))
            else:
                self.seen.append(None)
            process = Mock(returncode=0)
            process.poll.return_value = 0
            return process
        patcher = patch.object(client.subprocess, "Popen", side_effect=start)
        self.popen = patcher.start()
        self.addCleanup(patcher.stop)

    def test_preparation_service_receives_proxy_settings_and_they_are_removed(self):
        patcher = without_proxies(https_proxy="http://user:secret@proxy.example:3128",
                                  NO_PROXY=".example.com", LD_PRELOAD="/tmp/x.so")
        self.addCleanup(patcher.stop)
        self.assertTrue(client.prepare_update(logging.getLogger()))
        self.popen.assert_called_once_with(["systemctl", "start", "nobara-updater-prepare.service"])
        self.assertEqual(self.seen, [({"https_proxy": "http://user:secret@proxy.example:3128",
                                       "NO_PROXY": ".example.com"}, 0o600)],
                         "nobara-sync did not hand its proxy settings to nobara-updater-prepare.service")
        self.assertFalse(self.handover.exists())

    def test_settings_from_an_interrupted_run_never_reach_a_later_one(self):
        self.handover.write_text(json.dumps({"https_proxy": "http://old-proxy.example:3128"}))
        patcher = without_proxies()
        self.addCleanup(patcher.stop)
        self.assertTrue(client.prepare_update(logging.getLogger()))
        self.assertEqual(self.seen, [None])
        self.assertFalse(self.handover.exists())

    def test_handover_is_removed_when_the_client_is_interrupted(self):
        patcher = without_proxies(https_proxy="http://proxy.example:3128")
        self.addCleanup(patcher.stop)
        with patch.object(client, "run_service", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            client.prepare_update(logging.getLogger())
        self.assertFalse(self.handover.exists())

    def test_invalid_setting_is_reported_and_not_handed_over(self):
        patcher = without_proxies(https_proxy="http://proxy:3128\nLD_PRELOAD=/tmp/x.so",
                                  http_proxy="http://proxy.example:3128")
        self.addCleanup(patcher.stop)
        with self.assertLogs(level="WARNING") as logs:
            self.assertTrue(client.prepare_update(logging.getLogger()))
        self.assertEqual(self.seen, [({"http_proxy": "http://proxy.example:3128"}, 0o600)])
        self.assertIn("Ignoring https_proxy", "\n".join(logs.output))


class WorkerProxyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.handover = Path(temp.name) / "nobara-updater-proxy.json"
        patcher = without_proxies()
        self.addCleanup(patcher.stop)

    def load(self):
        return state.load_proxy_settings(self.handover)

    def test_service_applies_only_valid_proxy_settings(self):
        state.atomic_json(self.handover, {"https_proxy": "http://proxy.example:3128", "no_proxy": "localhost",
                                          "http_proxy": "http://proxy:3128\nX=1", "LD_PRELOAD": "/tmp/x.so",
                                          "PATH": "/tmp", "all_proxy": ["socks5://proxy:1080"]})
        self.assertEqual(self.load(), ["https_proxy", "no_proxy"])
        self.assertEqual(os.environ["https_proxy"], "http://proxy.example:3128")
        self.assertEqual(os.environ["no_proxy"], "localhost")
        for name in ("http_proxy", "LD_PRELOAD", "all_proxy"):
            self.assertNotIn(name, os.environ)
        self.assertNotEqual(os.environ.get("PATH"), "/tmp")

    def test_no_handover_changes_nothing(self):
        before = dict(os.environ)
        self.assertEqual(self.load(), [])
        self.assertEqual(dict(os.environ), before)

    def test_unsafe_or_unreadable_handover_is_refused(self):
        target = self.handover.with_name("target.json")
        state.atomic_json(target, {"https_proxy": "http://proxy.example:3128"})
        cases = {
            "symlink": lambda: self.handover.symlink_to(target),
            "writable by others": lambda: (shutil.copy(target, self.handover), self.handover.chmod(0o666)),
            "directory": lambda: self.handover.mkdir(),
            "malformed": lambda: self.handover.write_text("{"),
            "not an object": lambda: self.handover.write_text('["https_proxy"]'),
        }
        for case, create in cases.items():
            with self.subTest(case=case):
                create()
                with self.assertRaisesRegex(state.UpdateError, "Refusing proxy settings"):
                    self.load()
                self.assertNotIn("https_proxy", os.environ)
            if self.handover.is_dir() and not self.handover.is_symlink():
                self.handover.rmdir()
            else:
                self.handover.unlink()

    def test_only_the_first_preparation_interpreter_reads_the_handover(self):
        worker = load_worker()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        for patcher in (patch.object(worker, "STATE_DIR", Path(temp.name)),
                        patch.object(worker.os, "geteuid", return_value=0),
                        patch.object(worker.os, "umask"),
                        patch.object(worker.logging, "basicConfig"),
                        patch.object(worker, "RotatingFileHandler"),
                        patch.object(backend, "in_installer_root", return_value=True),
                        patch.object(worker, "read_state", return_value={"status": "idle"}),
                        patch.object(worker, "write_state"),
                        patch.object(worker, "attach_log"),
                        patch.object(worker, "record_failure")):
            patcher.start()
            self.addCleanup(patcher.stop)
        calls = []
        for arguments, expected in ((["prepare"], ["proxies", "upgrade_early"]),
                                    (["prepare-codecs"], ["proxies", "upgrade_early"]),
                                    (["prepare", "--early-upgrade-complete"], ["prepare"]),
                                    (["installer-update"], ["upgrade_early"]),
                                    (["execute"], ["execute"]),
                                    (["refresh-pending"], ["refresh_pending"])):
            calls.clear()
            with self.subTest(arguments=arguments), patch.object(worker.sys, "argv", ["worker", *arguments]), \
                 patch.object(worker, "load_proxy_settings", side_effect=lambda: calls.append("proxies") or [], create=True), \
                 patch.object(worker.os, "execv"), \
                 patch.object(backend, "upgrade_early", side_effect=lambda **_: calls.append("upgrade_early")), \
                 patch.object(backend, "prepare", side_effect=lambda **_: calls.append("prepare")), \
                 patch.object(backend, "execute", side_effect=lambda: calls.append("execute")), \
                 patch.object(backend, "refresh_pending", side_effect=lambda: calls.append("refresh_pending")):
                self.assertEqual(worker.main(), 0)
                self.assertEqual(calls, expected)


class DownloadProgressTests(unittest.TestCase):
    MIB = 1024**2

    def setUp(self):
        self.now = 1000.0
        clock = types.SimpleNamespace(monotonic=lambda: self.now)
        patcher = patch.object(planner, "time", clock, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.callbacks = planner.DownloadProgress()
        # libdnf5 announces every package first, all with the same user_data.
        self.ids = [self.callbacks.add_new_download(0, name, size * self.MIB)
                    for name, size in (("kernel-core", 60), ("mesa", 30), ("firefox", 10))]

    def progress(self, index, downloaded, seconds):
        self.now += seconds
        with self.assertLogs(planner.LOG, level="INFO") as logs:
            planner.LOG.info("marker")
            self.assertEqual(self.callbacks.progress(self.ids[index], 0, downloaded * self.MIB), self.callbacks.OK)
        return logs.output[1:]

    def end(self, index, status=None):
        status = self.callbacks.TransferStatus_SUCCESSFUL if status is None else status
        with self.assertLogs(planner.LOG, level="INFO") as logs:
            planner.LOG.info("marker")
            self.assertEqual(self.callbacks.end(self.ids[index], status, None), self.callbacks.OK)
        return logs.output[1:]

    def test_running_total_is_logged_during_the_download(self):
        self.assertEqual(len(set(self.ids)), 3, "libdnf5 hands back add_new_download's value; it must tell packages apart")
        self.assertEqual(self.progress(0, 10, 1), [])
        self.assertEqual(self.progress(0, 30, 3), [])
        self.assertEqual(self.progress(1, 15, 1.5),
                         ["INFO:nobara_updater.update_plan:Downloaded 45.0 of 100.0 MiB (45%), 0 of 3 packages"])
        self.assertEqual(self.progress(1, 20, 1), [])
        self.assertEqual(self.end(0), [])
        self.assertEqual(self.progress(1, 30, 5),
                         ["INFO:nobara_updater.update_plan:Downloaded 90.0 of 100.0 MiB (90%), 1 of 3 packages"])

    def test_completion_is_logged_once_every_package_is_present(self):
        self.progress(0, 60, 1)
        self.assertEqual(self.end(0), [])
        # Already in the cache: librepo reports no progress, only the end.
        self.assertEqual(self.end(2, self.callbacks.TransferStatus_ALREADYEXISTS), [])
        self.progress(1, 12, 1)
        self.assertEqual(self.end(1), ["INFO:nobara_updater.update_plan:Downloaded 100.0 of 100.0 MiB (100%), 3 of 3 packages"])

    def test_mirror_retry_restarts_the_count_and_never_exceeds_the_size(self):
        self.progress(1, 20, 1)
        # A different mirror starts this package again from zero.
        self.progress(1, 5, 1)
        self.assertEqual(self.progress(0, 600, 5),
                         ["INFO:nobara_updater.update_plan:Downloaded 65.0 of 100.0 MiB (65%), 0 of 3 packages"])

    def test_failed_download_is_reported_and_never_shown_as_complete(self):
        self.end(0)
        self.end(1)
        with self.assertLogs(planner.LOG, level="INFO") as logs:
            self.callbacks.end(self.ids[2], self.callbacks.TransferStatus_ERROR, "Curl error (56): Failure when receiving data")
        self.assertEqual(logs.output, ["ERROR:nobara_updater.update_plan:Download failed: Curl error (56): Failure when receiving data"])

    def test_unknown_callback_data_is_ignored(self):
        for value in (None, 0, 4, "kernel-core"):
            with self.subTest(value=value):
                self.assertEqual(self.callbacks.progress(value, 0, self.MIB), self.callbacks.OK)
                self.assertEqual(self.callbacks.end(value, self.callbacks.TransferStatus_SUCCESSFUL, None), self.callbacks.OK)
        self.assertEqual(self.end(0), [])


import test_offline_integration as fixtures


class FakeProxy(http.server.BaseHTTPRequestHandler):
    """A forward proxy stand-in: answers any absolute URL from one directory."""
    root = None
    requests = []

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        FakeProxy.requests.append(self.path)
        path = (self.root / url.path.lstrip("/")).resolve()
        if not url.netloc or not path.is_relative_to(self.root) or not path.is_file():
            self.send_error(404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *arguments):
        pass


@unittest.skipUnless(all(shutil.which(tool) for tool in ("rpmbuild", "createrepo_c", "rpm", "dnf5")), "RPM integration tools unavailable")
@unittest.skipUnless(os.geteuid() == 0, "Run these tests inside unshare --user --map-root-user for RPM's test chroot")
class DownloadIntegrationTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.OfflineIntegrationTests.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.OfflineIntegrationTests.tearDownClass.__func__)
    build_rpm = classmethod(fixtures.OfflineIntegrationTests.build_rpm.__func__)
    set_origin = fixtures.OfflineIntegrationTests.set_origin
    setUp = fixtures.OfflineIntegrationTests.setUp
    prepare = fixtures.OfflineIntegrationTests.prepare
    base_url = None

    def base(self, job, release=None, **kwargs):
        repos = self.root / "fixture-repos"
        repos.mkdir(exist_ok=True)
        (repos / "fixture.repo").write_text(f"[fixture]\nname=Fixture\nbaseurl={self.base_url or self.repo.as_uri()}\nenabled=1\n")
        base = fixtures.configure_fixture_base(self.root, repos, job, release or "44", **kwargs)
        self.addCleanup(base.unlock_system_repo)
        # Only the unsigned test fixtures bypass signatures, as in test_offline_integration.
        base.get_config().get_pkg_gpgcheck_option().set(False)
        base.get_config().get_localpkg_gpgcheck_option().set(False)
        for repo in fixtures.r.RepoQuery(base):
            repo.get_config().get_pkg_gpgcheck_option().set(False)
        return base

    def test_real_libdnf5_download_reports_completion(self):
        with patch.object(planner.DownloadProgress, "INTERVAL", 0, create=True), \
             self.assertLogs(planner.LOG, level="INFO") as logs:
            self.assertFalse(self.prepare()["empty"])
        downloaded = [line for line in logs.output if ":Downloaded " in line]
        self.assertTrue(downloaded, "No download progress was logged:\n" + "\n".join(logs.output))
        self.assertRegex(downloaded[-1], r"Downloaded \d+\.\d of \d+\.\d MiB \(100%\), 1 of 1 packages$")

    def test_preparation_service_downloads_through_the_handed_over_proxy(self):
        FakeProxy.root, FakeProxy.requests = self.repo.resolve(), []
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeProxy)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        # Without the proxy this name cannot resolve (RFC 2606).
        self.base_url = "http://nobara-fixture.invalid/"
        handover = Path(self.case.name) / "nobara-updater-proxy.json"
        state.atomic_json(handover, {"http_proxy": f"http://127.0.0.1:{server.server_port}", "no_proxy": "localhost"})
        environment = without_proxies()
        self.addCleanup(environment.stop)
        worker = load_worker()
        results = []
        for patcher in (patch.object(worker, "STATE_DIR", Path(self.case.name)),
                        patch.object(worker.os, "umask"),
                        patch.object(worker.logging, "basicConfig"),
                        patch.object(worker, "RotatingFileHandler"),
                        patch.object(worker, "read_state", return_value={"status": "idle"}),
                        patch.object(worker, "write_state"),
                        patch.object(worker, "record_failure"),
                        patch.object(worker, "load_proxy_settings", create=True,
                                     side_effect=lambda: state.load_proxy_settings(handover)),
                        patch.object(worker.os, "execv"),
                        patch.object(backend, "upgrade_early"),
                        patch.object(backend, "prepare", side_effect=lambda **_: results.append(self.prepare()))):
            patcher.start()
            self.addCleanup(patcher.stop)
        # The service's first interpreter, then the fresh one it execs.
        for arguments in (["prepare"], ["prepare", "--early-upgrade-complete"]):
            with patch.object(worker.sys, "argv", ["worker", *arguments]), \
                 contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(worker.main(), 0, errors.getvalue())
        self.assertFalse(results[0]["empty"])
        self.assertIn("http://nobara-fixture.invalid/repodata/repomd.xml", FakeProxy.requests)
        self.assertTrue(any(path.endswith("/nobara-offline-fixture-2-1.noarch.rpm") for path in FakeProxy.requests),
                        FakeProxy.requests)


if __name__ == "__main__":
    unittest.main()
