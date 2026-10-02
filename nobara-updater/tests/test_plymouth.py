"""Plymouth repairs are planned before theme selection and boot-image rebuilds."""
from pathlib import Path
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

from nobara_updater import update_backend as backend
from nobara_updater.update_migrations import BGRT_FILES, MigrationPlan, add_plymouth_migration


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

    def test_missing_packages_are_installed_before_selecting_bgrt(self):
        repairs = self.plan_for("plymouth", "gamescope-htpc-common", theme="text")
        self.assertEqual(self.plan.install, set(BGRT_FILES))
        self.assertEqual(self.plan.hooks, ["plymouth-bgrt"])
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


if __name__ == "__main__":
    unittest.main()
