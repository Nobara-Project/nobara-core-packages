"""After a confirmed kernel update, the rescue image is rebuilt from the previous kernel."""
from pathlib import Path
import subprocess
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
from nobara_updater import update_backend as backend, update_boot as boot, update_state as state

MACHINE = "2338a90ec5224809a3026e08b2887b80"
OLD, NEW = "6.18.60-203.lts.nobara.fc44.x86_64", "6.18.60-204.lts.nobara.fc44.x86_64"


def core(version, eac=False):
    name = "kernel-eac-core" if eac else "kernel-core"
    return {"name": name, "action": "Install", "nevra": f"{name}-0:{version}"}


class RescueSelectionTests(unittest.TestCase):
    def test_inbound_core_packages_name_the_new_kernels(self):
        packages = [core(NEW), core(NEW.replace(".lts.", ".lts.eac."), eac=True),
                    {"name": "kernel-modules", "action": "Install", "nevra": "kernel-modules-0:" + NEW},
                    {"name": "kernel-core", "action": "Remove", "nevra": "kernel-core-0:" + OLD}]
        self.assertEqual(boot.inbound_kernel_versions(packages), [NEW, NEW.replace(".lts.", ".lts.eac.")])

    def test_previous_kernel_is_the_newest_older_standard_kernel_of_the_family(self):
        installed = {OLD, NEW, "6.18.48-200.lts.nobara.fc44.x86_64", OLD.replace(".lts.", ".lts.eac."),
                     "7.3.4-201.nobara.fc44.x86_64"}
        self.assertEqual(boot.previous_standard_kernel([NEW], installed), OLD)

    def test_an_update_without_a_new_standard_kernel_selects_nothing(self):
        self.assertIsNone(boot.previous_standard_kernel([NEW.replace(".lts.", ".lts.eac.")], {OLD, NEW}))
        self.assertIsNone(boot.previous_standard_kernel([NEW], {NEW}))

    def test_rescue_entry_keeps_the_kernel_options(self):
        text = (f"title Nobara Linux ({OLD}) 44 (KDE Plasma Desktop Edition)\nversion {OLD}\n"
                f"linux /vmlinuz-{OLD}\ninitrd /initramfs-{OLD}.img\noptions root=UUID=x ro quiet\n"
                "grub_users $grub_users\n")
        self.assertEqual(boot.rescue_entry_text(text, OLD, MACHINE),
                         f"title Nobara Linux Rescue Kernel ({OLD})\nversion 0-rescue-{MACHINE}\n"
                         f"linux /vmlinuz-0-rescue-{MACHINE}\ninitrd /initramfs-0-rescue-{MACHINE}.img\n"
                         "options root=UUID=x ro quiet\ngrub_users $grub_users\n")


class RescueRebuildTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.boot = self.root / "boot"
        entries = self.boot / "loader/entries"
        entries.mkdir(parents=True)
        (self.root / "var/tmp").mkdir(parents=True)
        for version in (OLD, NEW):
            modules = self.root / "usr/lib/modules" / version
            modules.mkdir(parents=True)
            (modules / "vmlinuz").write_bytes(version.encode())
            (entries / f"{MACHINE}-{version}.conf").write_text(
                f"title Nobara Linux ({version}) 44\nversion {version}\nlinux /vmlinuz-{version}\n"
                f"initrd /initramfs-{version}.img\noptions root=UUID=x ro\n")
        self.entry = entries / f"{MACHINE}-0-rescue.conf"
        self.entry.write_text("title Nobara Linux Rescue Kernel (6.12.6-200.fsync.fc41.x86_64)\n")
        (self.boot / f"vmlinuz-0-rescue-{MACHINE}").write_bytes(b"old kernel")
        (self.boot / f"initramfs-0-rescue-{MACHINE}.img").write_bytes(b"old image")
        machine = self.root / "machine-id"
        machine.write_text(MACHINE + "\n")
        patch.object(boot, "MACHINE_ID", machine).start()
        patch.object(boot, "RESCUE_BUILD_DIR", self.root / "var/tmp").start()
        self.dracut = patch.object(boot.subprocess, "run", side_effect=self.build).start()
        self.addCleanup(patch.stopall)

    def build(self, command, **kwargs):
        Path(command[-1]).write_bytes(b"rescue for " + command[command.index("--kver") + 1].encode())
        return subprocess.CompletedProcess(command, 0, "", "")

    def refresh(self):
        return boot.refresh_rescue_image([NEW], boot=self.boot, root=self.root)

    def test_rescue_image_is_rebuilt_from_the_previous_kernel(self):
        self.assertEqual(self.refresh(), OLD)
        self.assertEqual((self.boot / f"vmlinuz-0-rescue-{MACHINE}").read_bytes(), OLD.encode())
        self.assertEqual((self.boot / f"initramfs-0-rescue-{MACHINE}.img").read_bytes(), b"rescue for " + OLD.encode())
        self.assertIn(f"Rescue Kernel ({OLD})", self.entry.read_text())
        self.assertIn("--no-hostonly", self.dracut.call_args[0][0])
        # Already built from that kernel: nothing more to do.
        self.assertIsNone(self.refresh())
        self.assertEqual(self.dracut.call_count, 1)

    def test_a_system_without_a_rescue_entry_is_left_without_one(self):
        self.entry.unlink()
        self.assertIsNone(self.refresh())
        self.dracut.assert_not_called()

    def test_a_failed_build_keeps_the_current_rescue_image(self):
        self.dracut.side_effect = lambda command, **kw: subprocess.CompletedProcess(command, 1, "", "no space")
        with self.assertRaises(state.UpdateError):
            self.refresh()
        self.assertEqual((self.boot / f"initramfs-0-rescue-{MACHINE}.img").read_bytes(), b"old image")
        self.assertIn("6.12.6", self.entry.read_text())

    def test_without_room_on_boot_the_current_rescue_image_is_kept(self):
        with patch.object(boot.shutil, "disk_usage", return_value=types.SimpleNamespace(free=0)):
            self.assertIsNone(self.refresh())
        self.assertEqual((self.boot / f"initramfs-0-rescue-{MACHINE}.img").read_bytes(), b"old image")

    def test_when_only_the_old_files_make_room_they_are_replaced_in_place(self):
        needed = len(b"rescue for " + OLD.encode()) + len(OLD.encode()) + 16 * 1024**2
        free = needed - len(b"old kernel") - len(b"old image")
        with patch.object(boot.shutil, "disk_usage", return_value=types.SimpleNamespace(free=free)):
            self.assertEqual(self.refresh(), OLD)
        self.assertIn(f"Rescue Kernel ({OLD})", self.entry.read_text())
        self.assertEqual((self.boot / f"initramfs-0-rescue-{MACHINE}.img").read_bytes(), b"rescue for " + OLD.encode())


class RescueRefreshStateTests(unittest.TestCase):
    def test_tried_once_per_completed_update_even_when_it_fails(self):
        with tempfile.TemporaryDirectory() as root, \
             patch.object(backend, "refresh_rescue_image", side_effect=state.UpdateError("dracut failed")) as rebuild:
            update = {"job": "a" * 32, "status": "complete", "packages": [core(NEW)]}
            backend.refresh_rescue(Path(root), update)
            backend.refresh_rescue(Path(root), update)
            self.assertEqual(rebuild.call_count, 1)
            self.assertTrue(state.read_state(Path(root))["rescue_refreshed"])

    def test_recovered_or_kernel_free_updates_leave_the_rescue_image(self):
        with tempfile.TemporaryDirectory() as root, patch.object(backend, "refresh_rescue_image") as rebuild:
            backend.refresh_rescue(Path(root), {"job": "a" * 32, "status": "recovered", "packages": [core(NEW)]})
            backend.refresh_rescue(Path(root), {"job": "b" * 32, "status": "complete", "packages": []})
            rebuild.assert_not_called()


if __name__ == "__main__":
    unittest.main()
