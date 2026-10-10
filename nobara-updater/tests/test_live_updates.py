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

    def test_python_modules_and_package_name_lookalikes_are_not_core(self):
        for name, files in (
            ("python3-requests", ["/usr/lib/python3.14/site-packages/requests/__init__.py"]),
            ("python3-numpy", ["/usr/lib64/python3.14/site-packages/numpy/_core/_multiarray_umath.so"]),
            ("python3-pip", ["/usr/bin/pip3", "/usr/lib/python3.14/site-packages/pip/__init__.py"]),
            ("python3-streamlink", ["/usr/bin/streamlink"]),
            ("bash-completion", ["/etc/profile.d/bash_completion.sh", "/usr/share/bash-completion/bash_completion"]),
            ("bash-color-prompt", ["/etc/profile.d/bash-color-prompt.sh"]),
            ("rpmlint", ["/usr/bin/rpmlint"]), ("rpmdevtools", ["/usr/bin/rpmdev-newspec"]),
            ("dnf-app-center", ["/usr/bin/dnf-app-center"]), ("kernelshark", ["/usr/bin/kernelshark"]),
        ):
            with self.subTest(name=name):
                self.assertEqual(self.classify([package(name=name, files=files)]), dict(mode="live", reasons=[]))

    def test_python_runtimes_and_package_manager_bindings_remain_core(self):
        for name in ("python", "python2", "python3", "python3-libs", "python3.14", "python3.14-libs",
                     "python3-freethreading", "python-unversioned-command", "python3-rpm", "python3-dnf",
                     "python3-dnf-plugins-core", "python3-libdnf", "python3-libdnf5", "python3-hawkey",
                     "python3-librepo", "python3-libcomps", "rpm", "rpm-libs", "rpm-plugin-selinux",
                     "dnf", "dnf5", "dnf5-plugins", "bash", "kernel", "kernel-core", "kernel-devel"):
            with self.subTest(name=name):
                self.assertIn("Core component: " + name, self.classify([package(name=name)])["reasons"])

    def test_auxiliary_packages_need_a_complete_non_runtime_payload(self):
        for name, files in (
            ("plasma-desktop-doc", ["/usr/share/doc/HTML/index.docbook"]),
            ("polkit-docs", ["/usr/share/gtk-doc/html/polkit-1/index.html"]),
            ("kernel-headers", ["/usr/include/linux/version.h"]),
            ("glibc-static", ["/usr/lib64/libc.a", "/usr/lib64/libm.a"]),
            ("systemd-devel", ["/usr/include/systemd/sd-bus.h", "/usr/lib64/pkgconfig/libsystemd.pc"]),
            ("kernel-debug-debuginfo", ["/usr/lib/debug/lib/modules/kernel/vmlinux"]),
            ("plasma-workspace-wallpapers", ["/usr/share/wallpapers/example.png", "/usr/share/licenses/wallpaper/COPYING"]),
            ("linux-firmware-whence", ["/usr/share/doc/linux-firmware/WHENCE"]),
        ):
            with self.subTest(name=name):
                self.assertEqual(self.classify([package(name=name, files=files)]), dict(mode="live", reasons=[]))
                for extra in ("/boot/new-image", "/usr/lib64/libexample.so.1", "/usr/bin/runtime-helper",
                              "/usr/lib/systemd/system/example.service", "/usr/share/doc/../../bin/runtime-helper"):
                    with self.subTest(extra=extra):
                        self.assertEqual(self.classify([package(name=name, files=[*files, extra])])["mode"], "offline")
                self.assertEqual(self.classify([package(name=name, files=[])])["mode"], "offline")
        # The core runtime itself does not qualify for an auxiliary-name exception.
        self.assertEqual(self.classify([package(name="glibc", files=["/usr/share/doc/glibc/README"])])["mode"], "offline")

    def test_non_core_names_still_check_system_paths_and_shared_libraries(self):
        for name in ("python3-requests", "bash-completion", "rpmdevtools", "dnf-app-center"):
            for path in ("/usr/lib/systemd/system/example.service", "/boot/new-image", "/usr/lib64/libexample.so.1"):
                with self.subTest(name=name, path=path):
                    self.assertEqual(self.classify([package(name=name, files=[path])])["mode"], "offline")

    def test_application_removals_and_downgrades_use_contents_not_action_alone(self):
        for action in ("Remove", "Downgrade"):
            with self.subTest(action=action):
                self.assertEqual(self.classify([package(action=action)]), dict(mode="live", reasons=[]))
                self.assertEqual(self.classify([package(name="glibc", action=action)])["mode"], "offline")
                self.assertEqual(self.classify([package(action=action, files=["/usr/lib64/libexample.so.1"])])["mode"], "offline")
                self.assertEqual(self.classify([package(action=action, files=[])])["mode"], "offline")
        # A downgrade must still inspect the outgoing version, including files
        # that disappear entirely from the incoming RPM.
        self.assertEqual(self.classify([package(action="Downgrade"),
                         package(action="Replaced", files=["/usr/lib/systemd/system/old.service"])])["mode"], "offline")

    def test_labwc_and_session_upgrade_can_install_now(self):
        packages = [package(name=name, action=action, files=files)
                    for action in ("Upgrade", "Replaced")
                    for name, files in (
                        ("labwc", ["/usr/bin/labwc", "/usr/share/xdg-desktop-portal/labwc-portals.conf"]),
                        ("labwc-session", ["/usr/lib/systemd/user/labwc-session.target",
                                           "/usr/share/wayland-sessions/labwc.desktop"]))]
        self.assertEqual(self.classify(packages), dict(mode="live", reasons=[]))

    def test_user_unit_exception_is_generic_and_includes_drop_ins(self):
        for directory in ("/usr/lib/systemd/user", "/etc/systemd/user"):
            for action in ("Install", "Upgrade", "Replaced"):
                with self.subTest(directory=directory, action=action):
                    files = [directory, directory + "/editor.service", directory + "/editor.socket",
                             directory + "/editor.service.d/settings.conf"]
                    self.assertEqual(self.classify([package(action=action, files=files)]),
                                     dict(mode="live", reasons=[]))

    def test_user_units_do_not_hide_other_critical_files_or_outgoing_files(self):
        unit = "/usr/lib/systemd/user/editor.service"
        for critical in ("/usr/lib/systemd/system/editor.service", "/etc/systemd/system/editor.service",
                         "/usr/lib/systemd/system-generators/editor", "/usr/lib/systemd/user-generators/editor",
                         "/usr/lib/systemd/user-environment-generators/editor", "/etc/systemd/user.conf",
                         "/etc/systemd/user.conf.d/settings.conf", "/usr/lib/systemd/user-preset/90-editor.preset",
                         "/usr/lib64/libeditor.so.1", "/boot/editor-image",
                         "/usr/lib/systemd/user/../system/editor.service", "relative/systemd/user/editor.service"):
            for action in ("Upgrade", "Replaced"):
                with self.subTest(critical=critical, action=action):
                    self.assertEqual(self.classify([package(action=action, files=[unit, critical])])["mode"], "offline")

    def test_user_units_do_not_override_other_offline_reasons(self):
        unit = "/usr/lib/systemd/user/example.service"
        for name in ("pipewire", "systemd", "plasma-workspace", "nobara-updater"):
            with self.subTest(name=name):
                self.assertIn("Core component: " + name,
                              self.classify([package(name=name, files=[unit])])["reasons"])
        for action in ("Remove", "Downgrade"):
            with self.subTest(action=action):
                self.assertEqual(self.classify([package(action=action, files=[unit])]), dict(mode="live", reasons=[]))
        application = package(files=[unit])
        self.assertEqual(self.classify([application], source="43")["mode"], "offline")
        self.assertEqual(self.classify([application], hooks=["enable-codecs"])["mode"], "offline")
        self.assertEqual(self.classify([application, package(name="kernel-core")])["mode"], "offline")

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

    def test_release_upgrade_hooks_and_unknown_inventory_are_offline(self):
        cases = [([package()], "43", "44", []), ([package()], "44", "44", ["enable-codecs"]),
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
                        patch.object(backend, "maintain_btrfs_root", return_value=True),
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
        with patch.object(backend, "run") as run, patch.object(backend, "validate_boot") as boot, \
             patch.object(backend, "SPLASH") as splash:
            backend.finalize(self.root, live=True)
        self.assertEqual([call.args[0] for call in run.call_args_list],
                         [["rpm", "-q", "editor-2-1.x86_64"], ["dnf5", "--disable-repo=*", "check", "--dependencies", "--duplicates"]])
        boot.assert_not_called()
        splash.start.assert_not_called()
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
        with patch.object(backend, "check_installed_system"), \
             patch("nobara_updater.update_plan.prepare_transaction", return_value={"empty": True}) as prepare:
            backend.prepare(self.root)
        prepare.assert_called_once()
        self.assertEqual(state.read_state(self.root)["status"], "unchanged")

    def test_administrator_can_require_offline_installation_or_recovery(self):
        for live_updates, require_recovery in ((False, False), (True, True)):
            state.write_state(self.root, dict(self.ready), "idle")
            settings = dict(prepare_attempts=1, live_updates=live_updates, require_recovery=require_recovery)
            with self.subTest(settings=settings), patch.object(backend, "policy", return_value=settings), \
                 patch.object(backend, "check_installed_system"), \
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
