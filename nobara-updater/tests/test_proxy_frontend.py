"""Proxy credentials cross pkexec via IPC without leaking between requests."""
import contextlib
import importlib.util
import io
import json
import os
import types
import unittest
from unittest.mock import Mock, patch

from test_frontend_contract import ROOT, functions_from_file
from test_update_cli import client  # Load this checkout's updater modules.
from nobara_updater import update_state as state


@unittest.skipUnless((ROOT / "appcenter/proxy.py").is_file(), "Updated App Center checkout unavailable")
class ProxyFrontendTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("appcenter_proxy_test", ROOT / "appcenter/proxy.py")
        self.proxy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.proxy)

    def helper(self, names):
        ns = functions_from_file("privileged_helper.py", names)
        ns["proxy_environment"] = self.proxy.proxy_environment
        ns["emit"] = Mock()
        return ns

    def test_validation_matches_updater_and_never_allows_other_environment(self):
        for value in ("http://user:secret@proxy:3128", "proxy:3128", "socks5h://[::1]:1080",
                      "localhost,127.0.0.1,.example.com", "*", "http://proxy:3128\nPATH=/tmp", "", [], None):
            for name in state.PROXY_VARIABLES:
                settings = {name: value, "PYTHONPATH": "/tmp", "HTTP_PROXY": "http://ignored:3128"}
                self.assertEqual(self.proxy.proxy_settings(settings), state.proxy_settings(settings))

    def test_cached_helper_sends_current_proxy_over_stdin_for_each_operation(self):
        ns = functions_from_file("dnf_backend.py", {"_run_privileged_helper_payload"}, "DnfBackend")
        ns["proxy_settings"] = self.proxy.proxy_settings
        proc = Mock(stdin=io.StringIO(), stdout=io.StringIO(
            '{"event":"result","ok":true}\n{"event":"result","ok":true}\n'))
        proc.poll.return_value = None
        backend = Mock(_helper_lock=contextlib.nullcontext(), cache_authorization=True, _helper_proc=proc)
        payload = dict(cmd="action", action="system-update", pkg_names=["editor", "--progress"])
        with patch.dict(os.environ, {"https_proxy": "http://user:secret@proxy:3128", "LD_PRELOAD": "/tmp/bad"}, clear=True):
            self.assertTrue(ns["_run_privileged_helper_payload"](backend, payload)[0])
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(ns["_run_privileged_helper_payload"](backend, payload)[0])
        requests = [json.loads(line) for line in proc.stdin.getvalue().splitlines()]
        self.assertEqual(requests[0]["proxies"], {"https_proxy": "http://user:secret@proxy:3128"})
        self.assertEqual(requests[1]["proxies"], {})
        self.assertEqual(requests[0]["pkg_names"], payload["pkg_names"])
        self.assertNotIn("proxies", payload)
        backend._start_privileged_helper.assert_not_called()

    def test_server_receives_proxy_before_metadata_and_restores_environment(self):
        ns = self.helper({"server_main", "_handle_command"})
        seen = []
        def build():
            seen.append(os.environ.get("https_proxy"))
            return (Mock(), Mock()), None
        ns["_build_backend"] = Mock(side_effect=build)
        ns["_run_transaction"] = Mock(return_value=(True, "Complete"))
        requests = [dict(cmd="action", action="install", pkg_names=["editor"], proxies=p)
                    for p in ({"https_proxy": "http://user:secret@proxy:3128"}, {})]
        def incoming():
            # The authorization handshake must succeed without any network.
            ns["_build_backend"].assert_not_called()
            ns["emit"].assert_called_once_with("ready", message="Privileged helper ready")
            yield from (json.dumps(request) for request in requests)
        ns["sys"] = types.SimpleNamespace(stdin=incoming())
        with patch.dict(os.environ, {"https_proxy": "http://manager:3128"}, clear=True):
            self.assertEqual(ns["server_main"](), 0)
            self.assertEqual(os.environ["https_proxy"], "http://manager:3128")
        self.assertEqual(seen, ["http://user:secret@proxy:3128", None])

    def test_system_update_keeps_selection_and_proxy_without_loading_helper_repositories(self):
        ns = self.helper({"server_main", "_handle_command", "_run_transaction", "_run_system_update",
                          "_system_update_args", "_nobara_update_result"})
        ns["_build_backend"] = Mock(side_effect=AssertionError("Unexpected metadata load"))
        request = dict(cmd="action", action="system-update", pkg_names=["--package=editor", "--progress"],
                       proxies={"https_proxy": "http://user:secret@proxy:3128", "PYTHONPATH": "/tmp/bad"})
        ns["sys"] = types.SimpleNamespace(stdin=[json.dumps(request)])
        proc = Mock(stdout=io.StringIO('NOBARA_UPDATE_RESULT {"status":"scheduled","message":"Ready"}\n'))
        proc.wait.return_value = 0
        with patch.dict(os.environ, {}, clear=True), patch.object(ns["subprocess"], "Popen", return_value=proc) as start:
            self.assertEqual(ns["server_main"](), 0)
            self.assertNotIn("https_proxy", os.environ)
        self.assertTrue(start.called, ns["emit"].call_args_list)
        command = start.call_args.args[0]
        self.assertIn("--package=editor", command)
        self.assertIn("--progress", command)
        self.assertNotIn("secret", " ".join(command))
        self.assertEqual(start.call_args.kwargs["env"]["https_proxy"], request["proxies"]["https_proxy"])
        self.assertNotIn("PYTHONPATH", start.call_args.kwargs["env"])

    def test_error_and_invalid_input_do_not_leak_credentials_into_next_request(self):
        ns = self.helper({"server_main"})
        seen = []
        def handle(libdnf5, base, payload):
            seen.append(os.environ.get("https_proxy"))
            if len(seen) == 1:
                raise RuntimeError("Failed transaction")
            return True
        ns["_handle_command"] = handle
        ns["sys"] = types.SimpleNamespace(stdin=[json.dumps(p) for p in (
            dict(proxies={"https_proxy": "http://user:secret@proxy:3128"}),
            dict(proxies=[]), dict(proxies={}))])
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(ns["server_main"](), 0)
            self.assertNotIn("https_proxy", os.environ)
        self.assertEqual(seen, ["http://user:secret@proxy:3128", None])
        self.assertNotIn("secret", str(ns["emit"].call_args_list))
