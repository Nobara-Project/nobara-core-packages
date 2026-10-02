"""Keep boot-space checks tied to the selected boot images and transaction."""
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
from nobara_updater.update_boot import boot_space_requirements
from nobara_updater import update_recovery as recovery
from nobara_updater.update_state import UpdateError

MIB = 1024**2


class BootSpaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.boot = Path(self.temp.name)
        self.kernel = self.file("vmlinuz-7.2.6", 16)
        self.initramfs = self.file("initramfs-7.2.6.img", 64)
        self.file("initramfs-0-rescue-machine.img", 300)

    def file(self, name, size):
        path = self.boot / name
        with path.open("wb") as stream:
            stream.truncate(size * MIB)
        return path

    def test_non_kernel_update_does_not_budget_rescue_or_rebuild_images(self):
        self.assertEqual(boot_space_requirements([dict(name="systemd", action="Upgrade")], [], boot=self.boot),
                         {str(self.boot): 32 * MIB})

    def test_kernel_update_budgets_normal_images_plus_one_temporary_initramfs(self):
        result = boot_space_requirements([dict(name="kernel-core", action="Install")], [], boot=self.boot)
        self.assertEqual(result[str(self.boot)], (32 + 16 + 64 + 64) * MIB)

    def test_rebuilding_multiple_existing_kernels_needs_one_temporary_image(self):
        self.file("initramfs-7.2.0.img", 60)
        result = boot_space_requirements([dict(name="dkms-nvidia", action="Upgrade")], [], boot=self.boot)
        self.assertEqual(result[str(self.boot)], (32 + 64) * MIB)

    def test_recovery_adds_actual_payload_and_checks_combined_peak_space(self):
        layout = dict(available=True, entry="test.conf")
        with patch.object(recovery, "boot_payloads", return_value=[self.kernel, self.initramfs]), \
             patch.object(recovery.shutil, "disk_usage", return_value=types.SimpleNamespace(free=200 * MIB)):
            recovery.check_recovery_space(layout, {"/boot": 96 * MIB})
            with self.assertRaisesRegex(UpdateError, "recovery and this update"):
                recovery.check_recovery_space(layout, {"/boot": 176 * MIB})
            self.assertGreater(recovery.recovery_boot_space(dict(layout, backend="lvm")),
                               recovery.recovery_boot_space(layout))

    def test_no_recovery_does_not_charge_for_snapshot_boot_images(self):
        with patch.object(recovery, "boot_payloads") as payloads:
            recovery.check_recovery_space({"available": False}, {})
        payloads.assert_not_called()


if __name__ == "__main__":
    unittest.main()
