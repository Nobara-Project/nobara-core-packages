"""Plymouth repairs are planned before theme selection and boot-image rebuilds."""
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1] / "src"
if "nobara_updater" not in sys.modules:
    package = types.ModuleType("nobara_updater")
    package.__path__ = [str(SOURCE)]
    sys.modules["nobara_updater"] = package

from nobara_updater import update_backend as backend, update_state as state
from nobara_updater.update_migrations import (BGRT_FILES, NOBARA_THEME_FILES, NOBARA_THEME_MARKER, PLYMOUTH_CONFIG,
                                              PLASMA_LOGIN_MARKER, PLYMOUTH_DEFAULTS, PLYMOUTH_THEMES, MigrationPlan, add_plymouth_migration,
                                              configured_plymouth_theme, settle_nobara_theme)


class PlymouthMigrationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.plan = MigrationPlan()

    def healthy_files(self):
        for filename in BGRT_FILES.values():
            path = self.root / filename.lstrip("/")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("present")

    def plan_for(self, *names, theme="bgrt"):
        return add_plymouth_migration(self.plan, [dict(name=name) for name in names],
                                     current_theme=theme, root=self.root)

    def test_missing_packages_are_installed_and_the_chosen_theme_stays(self):
        repairs = self.plan_for("plymouth", "gamescope-session-common", theme="text")
        self.assertEqual(self.plan.install, set(BGRT_FILES))
        self.assertEqual(self.plan.hooks, ["plymouth-rebuild"])
        self.assertEqual(repairs, set())

    def test_missing_payload_of_installed_package_requires_repair(self):
        self.healthy_files()
        (self.root / BGRT_FILES["plymouth-theme-spinner"].lstrip("/")).unlink()
        repairs = self.plan_for("plymouth", *BGRT_FILES)
        self.assertEqual(repairs, {"plymouth-theme-spinner"})
        self.assertEqual(self.plan.hooks, ["plymouth-rebuild"])

    def test_missing_plugin_is_repaired_even_if_theme_descriptor_exists(self):
        self.healthy_files()
        (self.root / BGRT_FILES["plymouth-plugin-two-step"].lstrip("/")).unlink()
        repairs = self.plan_for("plymouth", *BGRT_FILES)
        self.assertEqual(repairs, {"plymouth-plugin-two-step"})

    def test_missing_files_repair_does_not_switch_a_custom_theme(self):
        self.plan_for("plymouth", theme="my-custom-theme")
        self.assertEqual(self.plan.hooks, ["plymouth-rebuild"])
        with patch.object(backend, "run") as run:
            backend.apply_hooks(self.plan.hooks)
        run.assert_not_called()

    def test_steamos_selection_is_preserved(self):
        self.plan_for("plymouth", "gamescope-htpc-common", "gamescope-session-common", theme="text")
        self.assertIn("plymouth-plugin-script", self.plan.install)
        self.assertEqual(self.plan.hooks, ["plymouth-steamos"])

    def test_healthy_system_does_not_reinstall_or_rebuild(self):
        self.healthy_files()
        self.assertEqual(self.plan_for("plymouth", *BGRT_FILES), set())
        self.assertEqual(self.plan.install, set())
        self.assertEqual(self.plan.hooks, [])

    def test_system_without_plymouth_is_not_modified(self):
        self.assertEqual(self.plan_for("editor"), set())
        self.assertEqual(self.plan.install, set())
        self.assertEqual(self.plan.hooks, [])



class Fixture(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.plan = MigrationPlan()

    def write(self, filename, text="present"):
        path = self.root / filename.lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def healthy_files(self, *, nobara=True):
        for filename in (*BGRT_FILES.values(), *(NOBARA_THEME_FILES if nobara else ())):
            self.write(filename)

    def install_theme(self, name):
        return self.write(f"{PLYMOUTH_THEMES}/{name}/{name}.plymouth")

    def plan_for(self, *names, theme=None):
        return add_plymouth_migration(self.plan, [dict(name=name) for name in names],
                                     current_theme=theme, root=self.root)


class HtpcPlymouthPolicyTests(Fixture):
    """HTPC and handheld installs keep exactly the policy they had before."""
    HTPC = ("gamescope-htpc-common", "gamescope-session-common")
    # current theme -> (installs, hooks), as before the Nobara theme.
    EXPECTED = {
        "steamos": (set(), []),
        "bgrt": ({"plymouth-plugin-script"}, ["plymouth-steamos"]),
        "spinner": ({"plymouth-plugin-script"}, ["plymouth-steamos"]),
        "nobara": ({"plymouth-plugin-script"}, ["plymouth-steamos"]),
    }

    def test_htpc_installs_never_get_the_nobara_theme(self):
        for theme, (install, hooks) in self.EXPECTED.items():
            # Everything a standard install would migrate with is present.
            for nobara_on_disk in (True, False):
                with self.subTest(theme=theme, nobara_on_disk=nobara_on_disk):
                    self.setUp()
                    self.healthy_files(nobara=nobara_on_disk)
                    repairs = self.plan_for("plymouth", "nobara-logos", *BGRT_FILES, *self.HTPC, theme=theme)
                    self.assertEqual(repairs, set())
                    self.assertEqual(self.plan.install, install)
                    self.assertEqual(self.plan.hooks, hooks)
                    settle_nobara_theme(self.plan, [dict(name="nobara-logos", epoch="0", version="44.0.0",
                                                         release="2.fc44")], root=self.root)
                    self.assertEqual(self.plan.hooks, hooks)

    def test_htpc_policy_with_missing_bgrt_files_is_unchanged(self):
        for theme, install, hooks in [
            ("steamos", set(BGRT_FILES), ["plymouth-rebuild"]),
            ("bgrt", set(BGRT_FILES) | {"plymouth-plugin-script"}, ["plymouth-steamos"]),
        ]:
            with self.subTest(theme=theme):
                self.setUp()
                for filename in NOBARA_THEME_FILES:
                    self.write(filename)
                self.plan_for("plymouth", "nobara-logos", *self.HTPC, theme=theme)
                self.assertEqual(self.plan.install, install)
                self.assertEqual(self.plan.hooks, hooks)

    def test_htpc_installs_ignore_the_migration_marker(self):
        self.healthy_files()
        self.write(NOBARA_THEME_MARKER, "{}")
        self.plan_for("plymouth", *BGRT_FILES, *self.HTPC, theme="bgrt")
        self.assertEqual(self.plan.hooks, ["plymouth-steamos"])

    def test_gaming_hooks_still_select_their_theme(self):
        for hook, theme in (("plymouth-steamos", "steamos"), ("plymouth-bgrt", "bgrt")):
            with self.subTest(hook=hook), patch.object(backend, "run") as run, \
                 patch.object(backend, "select_nobara_theme") as select:
                self.assertEqual(backend.apply_hooks([hook]), {})
                run.assert_called_once_with(["plymouth-set-default-theme", theme])
                select.assert_not_called()


class GamescopeSessionIsStandardTests(Fixture):
    """Every standard install has gamescope-session-common for the Steam
    session. One gamescope package alone does not make an HTPC."""
    SINGLE = ("gamescope-session-common", "gamescope-htpc-common")

    def test_old_default_moves_to_the_nobara_theme(self):
        for package in self.SINGLE:
            for theme in ("bgrt", "", None):
                with self.subTest(package=package, theme=theme):
                    self.setUp()
                    self.healthy_files()
                    self.write(PLYMOUTH_DEFAULTS, "[Daemon]\nTheme=bgrt\n")
                    self.assertEqual(self.plan_for("plymouth", "nobara-logos", *BGRT_FILES, package, theme=theme), set())
                    self.assertEqual(self.plan.install, set())
                    self.assertEqual(self.plan.hooks, ["plymouth-nobara"])

    def test_the_nobara_theme_is_never_reverted(self):
        # A new installation arrives with it selected by the installer.
        for package in self.SINGLE:
            for marker in (False, True):
                with self.subTest(package=package, marker=marker):
                    self.setUp()
                    self.healthy_files()
                    if marker:
                        self.write(NOBARA_THEME_MARKER, "{}")
                    self.plan_for("plymouth", "nobara-logos", *BGRT_FILES, package, theme="nobara")
                    self.assertEqual(self.plan.install, set())
                    self.assertEqual(self.plan.hooks, [])

    def test_a_chosen_theme_is_left_alone(self):
        for theme in ("spinner", "details", "my-custom-theme", "steamos"):
            with self.subTest(theme=theme):
                self.setUp()
                self.healthy_files()
                self.plan_for("plymouth", "nobara-logos", *BGRT_FILES, "gamescope-session-common", theme=theme)
                self.assertEqual(self.plan.hooks, [])

    def test_a_return_to_bgrt_is_kept(self):
        self.healthy_files()
        self.write(NOBARA_THEME_MARKER, "{}")
        self.plan_for("plymouth", "nobara-logos", *BGRT_FILES, "gamescope-session-common", theme="bgrt")
        self.assertEqual(self.plan.hooks, [])

    def test_missing_bgrt_files_are_repaired_without_selecting_bgrt(self):
        for theme, hooks in (("bgrt", ["plymouth-nobara"]), ("spinner", ["plymouth-rebuild"]),
                             ("nobara", ["plymouth-rebuild"])):
            with self.subTest(theme=theme):
                self.setUp()
                for filename in NOBARA_THEME_FILES:
                    self.write(filename)
                self.plan_for("plymouth", "nobara-logos", "gamescope-session-common", theme=theme)
                self.assertEqual(self.plan.install, set(BGRT_FILES))
                self.assertEqual(self.plan.hooks, hooks)

    def test_theme_line_left_by_a_removed_htpc_package_shows_the_default(self):
        # A former HTPC: Theme=steamos stays in plymouthd.conf, the theme is
        # gone with gamescope-htpc-common, and Plymouth shows BGRT.
        self.healthy_files()
        self.write(PLYMOUTH_DEFAULTS, "[Daemon]\nTheme=bgrt\nShowDelay=0\n")
        self.write(PLYMOUTH_CONFIG, "[Daemon]\nTheme=steamos\n")
        self.assertEqual(configured_plymouth_theme(self.root), "bgrt")
        self.plan_for("plymouth", "nobara-logos", *BGRT_FILES, "gamescope-session-common")
        self.assertEqual(self.plan.hooks, ["plymouth-nobara"])

        def run(command):
            self.write(PLYMOUTH_CONFIG, "[Daemon]\nTheme=nobara\n")
        with patch.object(backend, "run", side_effect=run):
            switch = backend.select_nobara_theme(self.root)
        self.assertEqual(switch["theme"], "bgrt")
        self.assertEqual(switch["previous"], b"[Daemon]\nTheme=steamos\n")
        self.assertEqual(configured_plymouth_theme(self.root), "nobara")


class StandardNobaraThemePlanningTests(Fixture):
    def test_old_default_moves_to_the_nobara_theme(self):
        self.healthy_files()
        self.assertEqual(self.plan_for("plymouth", *BGRT_FILES, theme="bgrt"), set())
        self.assertEqual(self.plan.install, set())
        self.assertEqual(self.plan.hooks, ["plymouth-nobara"])

    def test_unset_theme_uses_the_distribution_default(self):
        self.healthy_files()
        self.write(PLYMOUTH_DEFAULTS, "[Daemon]\nTheme=bgrt\nShowDelay=0\n")
        for config in (None, "", "[Daemon]\n#Theme=spinner\nShowDelay=5\n"):
            with self.subTest(config=config):
                self.plan = MigrationPlan()
                path = self.root / PLYMOUTH_CONFIG.lstrip("/")
                path.unlink(missing_ok=True)
                if config is not None:
                    self.write(PLYMOUTH_CONFIG, config)
                self.assertEqual(configured_plymouth_theme(self.root), "bgrt")
                self.plan_for("plymouth", *BGRT_FILES)
                self.assertEqual(self.plan.hooks, ["plymouth-nobara"])

    def test_no_theme_anywhere_counts_as_the_default(self):
        self.healthy_files()
        self.assertEqual(configured_plymouth_theme(self.root), "")
        self.plan_for("plymouth", *BGRT_FILES)
        self.assertEqual(self.plan.hooks, ["plymouth-nobara"])
        self.plan = MigrationPlan()
        self.plan_for("plymouth", *BGRT_FILES, theme="")
        self.assertEqual(self.plan.hooks, ["plymouth-nobara"])

    def test_a_chosen_theme_is_left_alone(self):
        self.healthy_files()
        self.write(PLYMOUTH_DEFAULTS, "[Daemon]\nTheme=bgrt\n")
        for theme in ("spinner", "details", "text", "my-custom-theme", "nobara", "steamos"):
            with self.subTest(theme=theme):
                self.plan = MigrationPlan()
                self.write(PLYMOUTH_CONFIG, f"[Daemon]\nTheme={theme}\n")
                self.install_theme(theme)
                self.assertEqual(configured_plymouth_theme(self.root), theme)
                self.plan_for("plymouth", "nobara-logos", *BGRT_FILES)
                self.assertEqual(self.plan.hooks, [])
                self.plan = MigrationPlan()
                self.plan_for("plymouth", "nobara-logos", *BGRT_FILES, theme=theme)
                self.assertEqual(self.plan.hooks, [])

    def test_migrated_system_is_not_switched_again(self):
        self.healthy_files()
        self.write(NOBARA_THEME_MARKER, '{"theme": "nobara"}')
        self.plan_for("plymouth", "nobara-logos", *BGRT_FILES, theme="bgrt")
        self.assertEqual(self.plan.hooks, [])

    def test_migrated_system_still_repairs_bgrt(self):
        self.write(NOBARA_THEME_MARKER, "{}")
        for filename in NOBARA_THEME_FILES:
            self.write(filename)
        self.plan_for("plymouth", theme="bgrt")
        self.assertEqual(self.plan.install, set(BGRT_FILES))
        self.assertEqual(self.plan.hooks, ["plymouth-rebuild"])

    def test_without_plymouth_nothing_is_planned(self):
        self.healthy_files()
        self.plan_for("nobara-logos", theme="bgrt")
        self.assertEqual(self.plan.hooks, [])

    def test_without_the_theme_or_its_package_nothing_is_planned(self):
        self.healthy_files(nobara=False)
        self.plan_for("plymouth", *BGRT_FILES, theme="bgrt")
        self.assertEqual(self.plan.hooks, [])

    def test_partial_theme_payload_is_not_installed(self):
        self.healthy_files(nobara=False)
        self.write(NOBARA_THEME_FILES[0])
        self.plan_for("plymouth", *BGRT_FILES, theme="bgrt")
        self.assertEqual(self.plan.hooks, [])

    def test_bgrt_repair_and_theme_switch_share_one_rebuild(self):
        for filename in NOBARA_THEME_FILES:
            self.write(filename)
        self.write(BGRT_FILES["plymouth-plugin-two-step"])
        repairs = self.plan_for("plymouth", *BGRT_FILES, theme="bgrt")
        self.assertEqual(repairs, {"plymouth-theme-spinner"})
        self.assertEqual(self.plan.install, set())
        self.assertEqual(self.plan.hooks, ["plymouth-nobara"])
        self.assertEqual(sum(hook.startswith("plymouth-") for hook in self.plan.hooks), 1)


class SettleNobaraThemeTests(Fixture):
    def logos(self, version="44.0.0", release="2.fc44", epoch="0"):
        return [dict(name="nobara-logos", epoch=epoch, version=version, release=release)]

    def test_theme_arriving_with_nobara_logos_keeps_the_hook(self):
        self.healthy_files(nobara=False)
        self.plan_for("plymouth", "nobara-logos", *BGRT_FILES, theme="bgrt")
        self.assertEqual(self.plan.hooks, ["plymouth-nobara"])
        for version, release in (("44.0.0", "2.fc44"), ("44.0.0", "3.fc44"), ("44.1.0", "1.fc44"), ("45", "1")):
            with self.subTest(version=version, release=release):
                settle_nobara_theme(self.plan, self.logos(version, release), root=self.root)
                self.assertEqual(self.plan.hooks, ["plymouth-nobara"])

    def test_old_or_absent_nobara_logos_drops_the_hook(self):
        for inbound in ([], self.logos(release="1.fc44"), self.logos("43.0.0", "9"),
                        [dict(name="plymouth", epoch="0", version="44.0.0", release="2")]):
            with self.subTest(inbound=inbound):
                self.setUp()
                self.healthy_files(nobara=False)
                self.plan.hooks.append("enable-falcond")
                self.plan_for("plymouth", "nobara-logos", *BGRT_FILES, theme="bgrt")
                settle_nobara_theme(self.plan, inbound, root=self.root)
                self.assertEqual(self.plan.hooks, ["enable-falcond"])

    def test_dropped_hook_keeps_the_bgrt_repair_rebuild(self):
        self.plan_for("plymouth", "nobara-logos", theme="bgrt")
        self.assertEqual(self.plan.hooks, ["plymouth-nobara"])
        settle_nobara_theme(self.plan, [], root=self.root)
        self.assertEqual(self.plan.hooks, ["plymouth-rebuild"])
        self.assertEqual(self.plan.install, set(BGRT_FILES))

    def test_installed_theme_keeps_the_hook_without_an_update(self):
        self.healthy_files()
        self.plan_for("plymouth", *BGRT_FILES, theme="bgrt")
        settle_nobara_theme(self.plan, [], root=self.root)
        self.assertEqual(self.plan.hooks, ["plymouth-nobara"])


class InboundPackagesTests(unittest.TestCase):
    def test_only_arriving_packages_count(self):
        from nobara_updater import update_plan as planner

        def item(name, action, version="44.0.0", release="2.fc44"):
            package = Mock()
            package.get_name.return_value = name
            package.get_epoch.return_value = "0"
            package.get_version.return_value = version
            package.get_release.return_value = release
            return Mock(get_package=Mock(return_value=package), get_action=Mock(return_value=action))
        transaction = Mock()
        transaction.get_transaction_packages.return_value = [
            item("nobara-logos", planner.trans.TransactionItemAction_UPGRADE),
            item("old-logos", planner.trans.TransactionItemAction_REMOVE),
            item("nobara-logos", planner.trans.TransactionItemAction_REPLACED, "43.0.0", "1")]
        self.assertEqual(planner.inbound_packages(transaction),
                         [dict(name="nobara-logos", epoch="0", version="44.0.0", release="2.fc44")])


class SelectNobaraThemeTests(Fixture):
    def selecting(self, theme="nobara"):
        def run(command):
            self.assertEqual(command, ["plymouth-set-default-theme", "nobara"])
            self.write(PLYMOUTH_CONFIG, f"[Daemon]\nTheme={theme}\n")
        return patch.object(backend, "run", side_effect=run)

    def test_selects_the_theme_and_remembers_the_previous_one(self):
        self.healthy_files()
        self.write(PLYMOUTH_CONFIG, "[Daemon]\nTheme=bgrt\n")
        with self.selecting() as run:
            switch = backend.select_nobara_theme(self.root)
        run.assert_called_once()
        self.assertEqual(switch["theme"], "bgrt")
        self.assertEqual(switch["previous"], b"[Daemon]\nTheme=bgrt\n")
        self.assertEqual(configured_plymouth_theme(self.root), "nobara")

    def test_missing_theme_or_plugin_changes_nothing(self):
        for missing in NOBARA_THEME_FILES:
            with self.subTest(missing=missing):
                self.setUp()
                self.healthy_files()
                (self.root / missing.lstrip("/")).unlink()
                with patch.object(backend, "run") as run, self.assertLogs(backend.LOG, "WARNING") as logs:
                    self.assertIsNone(backend.select_nobara_theme(self.root))
                run.assert_not_called()
                self.assertIn(missing, "\n".join(logs.output))
                self.assertFalse((self.root / PLYMOUTH_CONFIG.lstrip("/")).exists())

    def test_theme_chosen_after_preparation_is_kept(self):
        self.healthy_files()
        self.write(PLYMOUTH_CONFIG, "[Daemon]\nTheme=spinner\n")
        self.install_theme("spinner")
        with patch.object(backend, "run") as run:
            self.assertIsNone(backend.select_nobara_theme(self.root))
        run.assert_not_called()

    def test_failed_selection_restores_the_previous_configuration(self):
        for previous in (None, "[Daemon]\nTheme=bgrt\nShowDelay=5\n"):
            with self.subTest(previous=previous):
                self.setUp()
                self.healthy_files()
                config = self.root / PLYMOUTH_CONFIG.lstrip("/")
                if previous is not None:
                    self.write(PLYMOUTH_CONFIG, previous)

                def broken(command):
                    self.write(PLYMOUTH_CONFIG, "[Daemon]\nTheme=nob")
                    raise state.UpdateError("Command failed")
                with patch.object(backend, "run", side_effect=broken), self.assertLogs(backend.LOG, "ERROR"):
                    self.assertIsNone(backend.select_nobara_theme(self.root))
                if previous is None:
                    self.assertFalse(config.exists())
                else:
                    self.assertEqual(config.read_text(), previous)

    def test_selection_that_did_not_take_effect_is_undone(self):
        self.healthy_files()
        self.write(PLYMOUTH_CONFIG, "[Daemon]\nTheme=bgrt\n")
        with self.selecting("text"), self.assertLogs(backend.LOG, "ERROR"):
            self.assertIsNone(backend.select_nobara_theme(self.root))
        self.assertEqual(configured_plymouth_theme(self.root), "bgrt")

    def test_hook_never_fails_the_update(self):
        with patch.object(backend, "select_nobara_theme", return_value=None) as select, \
             patch.object(backend, "run") as run:
            self.assertEqual(backend.apply_hooks(["plymouth-nobara"]), {})
        select.assert_called_once_with()
        run.assert_not_called()


class NobaraThemeFinalizeTests(Fixture):
    def setUp(self):
        super().setUp()
        self.job = self.root / "jobs" / ("a" * 32)
        self.job.mkdir(parents=True)
        (self.job / "transaction.json").write_text("{}")
        ready = dict(job="a" * 32, installer=True, started=True, fingerprint="fixture", target_release="44",
                     files={"transaction.json": state.file_digest(self.job / "transaction.json")},
                     migrations={"hooks": ["plymouth-nobara"]}, packages=[])
        state.write_state(self.root, ready, "validating")
        self.addCleanup(patch.stopall)
        patch.object(backend, "in_installer_root", return_value=True).start()
        patch.object(backend, "rpm_fingerprint", return_value="fixture").start()
        patch.object(backend, "run").start()
        patch.object(backend, "os_release", return_value="44").start()
        self.config = self.write("etc-plymouthd.conf", "[Daemon]\nTheme=nobara\n")
        self.marker = self.root / Path(NOBARA_THEME_MARKER).name

    def switch(self):
        return dict(config=self.config, previous=b"[Daemon]\nTheme=bgrt\n", theme="bgrt")

    def test_marker_is_written_after_one_rebuild(self):
        with patch.object(backend, "select_nobara_theme", return_value=self.switch()), \
             patch.object(backend, "validate_boot") as validate:
            backend.finalize(self.root, installer=True)
        validate.assert_called_once_with([], rebuild_all=True, preserved_kernels=[])
        self.assertTrue(self.marker.is_file())
        self.assertEqual(self.config.read_text(), "[Daemon]\nTheme=nobara\n")
        self.assertEqual(state.read_state(self.root)["status"], "installer-complete")

    def test_skipped_switch_rebuilds_but_leaves_no_marker(self):
        with patch.object(backend, "select_nobara_theme", return_value=None), \
             patch.object(backend, "validate_boot") as validate:
            backend.finalize(self.root, installer=True)
        validate.assert_called_once_with([], rebuild_all=True, preserved_kernels=[])
        self.assertFalse(self.marker.exists())

    def test_failed_rebuild_restores_the_previous_theme_without_marker(self):
        with patch.object(backend, "select_nobara_theme", return_value=self.switch()), \
             patch.object(backend, "validate_boot", side_effect=state.UpdateError("dracut failed")):
            with self.assertRaisesRegex(state.UpdateError, "dracut failed"):
                backend.finalize(self.root, installer=True)
        self.assertEqual(self.config.read_text(), "[Daemon]\nTheme=bgrt\n")
        self.assertFalse(self.marker.exists())

    def test_marker_blocks_the_next_plan(self):
        with patch.object(backend, "select_nobara_theme", return_value=self.switch()), \
             patch.object(backend, "validate_boot"):
            backend.finalize(self.root, installer=True)
        # The planner looks for the marker below the installation root.
        system = self.root / "system"
        target = system / NOBARA_THEME_MARKER.lstrip("/")
        target.parent.mkdir(parents=True)
        target.write_bytes(self.marker.read_bytes())
        self.root = system
        self.healthy_files()
        self.plan_for("plymouth", *BGRT_FILES, theme="bgrt")
        self.assertEqual(self.plan.hooks, [])


class PlasmaLoginFinalizeTests(Fixture):
    def setUp(self):
        super().setUp()
        job = self.root / "jobs" / ("a" * 32)
        job.mkdir(parents=True)
        (job / "transaction.json").write_text("{}")
        ready = dict(job="a" * 32, installer=True, started=True, fingerprint="fixture", target_release="44",
                     files={"transaction.json": state.file_digest(job / "transaction.json")},
                     migrations={"hooks": ["plasma-login"]}, packages=[])
        state.write_state(self.root, ready, "validating")
        self.addCleanup(patch.stopall)
        patch.object(backend, "in_installer_root", return_value=True).start()
        patch.object(backend, "rpm_fingerprint", return_value="fixture").start()
        patch.object(backend, "apply_hooks", return_value={}).start()
        patch.object(backend, "os_release", return_value="44").start()
        self.marker = self.root / Path(PLASMA_LOGIN_MARKER).name

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
