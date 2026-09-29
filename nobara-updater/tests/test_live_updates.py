"""Live classification and service execution use disposable state only."""
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "src"
if "nobara_updater" not in sys.modules:
    package = types.ModuleType("nobara_updater")
    package.__path__ = [str(SOURCE)]
    sys.modules["nobara_updater"] = package
from nobara_updater import update_backend as backend, update_state as state
from nobara_updater.update_policy import execution_policy, format_restart_reasons


def package(name="editor", action="Upgrade", files=None):
    return dict(name=name, action=action, files=files if files is not None else ["/usr/bin/editor", "/usr/share/editor/data"])


class ClassificationTests(unittest.TestCase):
    def classify(self, packages, source="44", target="44", hooks=None):
        return execution_policy(packages, source, target, hooks or [])

    def test_app_with_private_libraries_and_data_can_install_now(self):
        result = self.classify([package(files=["/usr/bin/editor", "/usr/lib64/editor/plugin.so", "/usr/share/editor/data"])])
        self.assertEqual(result, dict(mode="live", reasons=[]))

    def test_reason_changes_do_not_make_an_application_update_require_restart(self):
        reasons_only = [package(name="mesa-libgallium-freeworld", action="Reason Change", files=["/usr/lib64/libgallium.so"]),
                        package(name="nobara-release-kde", action="Reason Change", files=[])]
        self.assertEqual(self.classify([package(), *reasons_only]), dict(mode="live", reasons=[]))
        reasons_only[0]["action"] = "Upgrade"
        self.assertEqual(self.classify([package(), *reasons_only])["mode"], "offline")

    def test_restart_reasons_are_grouped_wrapped_and_deduplicated_without_losing_policy_data(self):
        reasons = ["Core component: nobara-updater", "System files: nobara-updater", "Package removals or downgrades"]
        reasons += [f"Shared system library: example-library-{index:02}" for index in range(16)]
        original = list(reasons)
        message = format_restart_reasons(reasons)
        self.assertEqual(reasons, original)
        self.assertEqual(message.count("nobara-updater"), 1)
        self.assertIn("- Core components (1):\n", message)
        self.assertIn("- Shared system libraries (16):\n", message)
        self.assertIn("- Package removals or downgrades\n", message)
        self.assertTrue(all(len(line) <= 88 for line in message.splitlines()))
        for index in range(16):
            self.assertIn(f"example-library-{index:02}", message)

    def test_mixed_transaction_stays_together_when_dependency_is_critical(self):
        for name in ("kernel-core", "glibc", "systemd", "dnf5", "libdnf5", "python3", "nobara-updater",
                     "nobara-release-common", "mesa-libgallium", "dkms-nvidia", "plasma-workspace", "kwin", "pipewire"):
            with self.subTest(name=name):
                result = self.classify([package(), package(name=name)])
                self.assertEqual(result["mode"], "offline")
                self.assertTrue(result["reasons"])

    def test_system_paths_and_outgoing_files_detect_unrecognized_package_names(self):
        for filename in ("/usr/lib64/libexample.so.2", "/lib/libexample.so", "/boot/new-image",
                         "/usr/lib/systemd/system/example.service", "/etc/pam.d/login",
                         "/usr/lib64/security/pam_example.so", "/usr/sbin/example"):
            with self.subTest(filename=filename):
                old = package(name="unexpected-name", action="Replaced", files=[filename])
                self.assertEqual(self.classify([package(), old])["mode"], "offline")

    def test_release_upgrade_removal_downgrade_hooks_and_unknown_inventory_are_offline(self):
        cases = [([package()], "43", "44", []), ([package()], "44", "44", ["enable-codecs"]),
                 ([package(action="Remove")], "44", "44", []),
                 ([package(action="Downgrade")], "44", "44", []),
                 ([package(files=[])], "44", "44", []),
                 ([package(files=["relative/path"])], "44", "44", [])]
        for args in cases:
            with self.subTest(args=args):
                self.assertEqual(self.classify(*args)["mode"], "offline")


class LiveWorkflowTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.job = self.root / "jobs" / ("a" * 32)
        self.job.mkdir(parents=True)
        (self.job / "transaction.json").write_text("{}")
        self.ready = dict(job="a" * 32, started=False, fingerprint="fixture", source_release="44", target_release="44",
                          execution={"mode": "live", "reasons": []},
                          files={"transaction.json": state.file_digest(self.job / "transaction.json")},
                          migrations={"hooks": []}, packages=[dict(name="editor", action="Upgrade", nevra="editor-2-1.x86_64")])
        state.write_state(self.root, dict(self.ready), "ready")
        for patcher in (patch.object(backend, "TRIGGER", self.root / "system-update"),
                        patch.object(backend, "in_installer_root", return_value=False),
                        patch.object(backend, "rpm_fingerprint", return_value="fixture"),
                        patch.object(backend, "os_release", return_value="44")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_live_install_replays_local_payload_once_and_reexecs_validation(self):
        with patch.object(backend, "run") as run, patch.object(backend, "create_recovery") as snapshot, \
             patch.object(backend.os, "execv") as execute:
            backend.execute_live(self.root)
        self.assertEqual(run.call_count, 1)
        command = run.call_args.args[0]
        self.assertIn("replay", command)
        self.assertIn("--setopt=reposdir=", command)
        self.assertIn("--setopt=localpkg_gpgcheck=1", command)
        self.assertEqual(execute.call_args.args[1][-1], "live-finalize")
        snapshot.assert_not_called()
        self.assertFalse(backend.TRIGGER.exists())
        self.assertEqual(state.read_state(self.root)["status"], "validating-live")
        self.assertTrue(state.read_state(self.root)["started"])

    def test_critical_old_scheduled_or_other_release_job_cannot_install_live(self):
        changes = [{"execution": {"mode": "offline"}}, {"execution": {}}, {"target_release": "45"},
                   {"installer": True}, {"status": "scheduled"}, {"started": True}]
        for change in changes:
            with self.subTest(change=change), patch.object(backend, "replay") as replay:
                saved = dict(self.ready, status="ready", **{k: v for k, v in change.items() if k != "status"})
                saved["status"] = change.get("status", "ready")
                state.atomic_json(self.root / "state.json", saved)
                with self.assertRaises(state.UpdateError):
                    backend.execute_live(self.root)
                replay.assert_not_called()
                self.assertEqual(state.read_state(self.root), saved)

    def test_live_install_refuses_tampering_or_changed_installed_packages(self):
        for tampered in (False, True):
            with self.subTest(tampered=tampered), patch.object(backend, "replay") as replay, \
                 patch.object(backend, "rpm_fingerprint", return_value="changed" if not tampered else "fixture"):
                state.write_state(self.root, dict(self.ready), "ready")
                if tampered:
                    (self.job / "transaction.json").write_text("tampered")
                with self.assertRaises(state.UpdateError):
                    backend.execute_live(self.root)
                replay.assert_not_called()
                self.assertFalse(state.read_state(self.root)["started"])

    def test_failure_is_never_replayed_and_does_not_reboot(self):
        with patch.object(backend, "run", side_effect=state.UpdateError("scriptlet failed")) as run, \
             patch.object(backend.os, "execv") as execute:
            with self.assertRaisesRegex(state.UpdateError, "scriptlet failed"):
                backend.execute_live(self.root)
            with self.assertRaises(state.UpdateError):
                backend.execute_live(self.root)
            run.assert_called_once()
            execute.assert_not_called()
        self.assertTrue(state.read_state(self.root)["started"])

    def test_validation_checks_packages_and_dependencies_without_boot_work(self):
        state.write_state(self.root, dict(self.ready), "validating-live", started=True)
        with patch.object(backend, "run") as run, patch.object(backend, "validate_boot") as boot:
            backend.finalize(self.root, live=True)
        self.assertEqual([call.args[0] for call in run.call_args_list],
                         [["rpm", "-q", "editor-2-1.x86_64"], ["dnf5", "--disable-repo=*", "check"]])
        boot.assert_not_called()
        self.assertEqual(state.read_state(self.root)["status"], "live-complete")

    def test_validation_failure_cannot_claim_completion(self):
        state.write_state(self.root, dict(self.ready), "validating-live", started=True)
        with patch.object(backend, "run", side_effect=state.UpdateError("package missing")):
            with self.assertRaisesRegex(state.UpdateError, "package missing"):
                backend.finalize(self.root, live=True)
        self.assertNotEqual(state.read_state(self.root)["status"], "live-complete")

    def test_next_boot_records_live_interruption_without_triggering_boot_recovery(self):
        state.write_state(self.root, dict(self.ready), "installing-live", started=True)
        with patch.object(backend, "run") as run, patch.object(backend, "publish_failure") as report, self.assertLogs(backend.LOG, level="ERROR"):
            backend.confirm_boot(self.root)
        self.assertEqual(state.read_state(self.root)["status"], "interrupted")
        report.assert_called_once()
        run.assert_not_called()

    def test_completed_live_job_allows_a_new_preparation(self):
        state.write_state(self.root, dict(self.ready), "live-complete", started=True)
        with patch("nobara_updater.update_plan.prepare_transaction", return_value={"empty": True}) as prepare:
            backend.prepare(self.root)
        prepare.assert_called_once()
        self.assertEqual(state.read_state(self.root)["status"], "unchanged")

    def test_administrator_can_require_offline_installation_or_recovery(self):
        for live_updates, require_recovery in ((False, False), (True, True)):
            state.write_state(self.root, dict(self.ready), "idle")
            settings = dict(prepare_attempts=1, live_updates=live_updates, require_recovery=require_recovery)
            with self.subTest(settings=settings), patch.object(backend, "policy", return_value=settings), \
                 patch("nobara_updater.update_plan.prepare_transaction", return_value={"empty": True}) as prepare:
                backend.prepare(self.root)
            self.assertIs(prepare.call_args.kwargs["allow_live"], False)

    def test_lock_contention_does_not_overwrite_running_live_job(self):
        state.write_state(self.root, dict(self.ready), "installing-live", started=True)
        before = state.read_state(self.root)
        with state.update_lock(self.root), patch.object(backend, "replay") as replay:
            with self.assertRaisesRegex(state.UpdateError, "Another Nobara update operation"):
                backend.execute_live(self.root)
        replay.assert_not_called()
        self.assertEqual(state.read_state(self.root), before)


if __name__ == "__main__":
    unittest.main()
