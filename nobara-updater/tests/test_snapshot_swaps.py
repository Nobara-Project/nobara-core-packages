"""Btrfs swap preflight uses read-only fixtures; never activates real swap."""
import json
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
from nobara_updater import update_recovery as recovery
from nobara_updater.update_state import UpdateError

UUID = "11111111-2222-3333-4444-555555555555"
LAYOUT = dict(available=True, uuid=UUID, fsroot="/@")
HEADER = "Filename\tType\tSize\tUsed\tPriority\n"


class SnapshotSwapTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.swaps = self.root / "proc-swaps"
        self.swaps.write_text(HEADER)
        self.fstab = self.root / "fstab"
        self.fstab.write_text(f"UUID={UUID} / btrfs subvol=@ 0 0\n")
        self.mounts = {}
        self.ids = {"/": "256"}
        for patcher in (patch.object(recovery, "ACTIVE_SWAPS", self.swaps),
                        patch.object(recovery, "FSTAB", self.fstab)):
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(recovery, "output", side_effect=self.output)
        self.command = patcher.start()
        self.addCleanup(patcher.stop)

    def output(self, command):
        if command[:3] == ["btrfs", "inspect-internal", "rootid"]:
            return self.ids[command[3]]
        self.assertEqual(command[:3], ["findmnt", "--json", "--target"])
        return json.dumps({"filesystems": [self.mounts[command[3]]]})

    def add_swap(self, name="swapfile", *, active=True, configured=True,
                 root_id="256", uuid=UUID, fstype="btrfs", options="defaults"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        value = str(path)
        encoded = value.replace("\\", "\\134").replace(" ", "\\040").replace("\t", "\\011")
        if active:
            with self.swaps.open("a") as stream:
                stream.write(f"{encoded} file 1048576 0 -2\n")
        if configured:
            with self.fstab.open("a") as stream:
                stream.write(f"{encoded} none swap {options} 0 0\n")
        self.mounts[value] = dict(fstype=fstype, uuid=uuid)
        self.ids[value] = root_id
        return path

    def test_active_root_swap_is_named_once_with_relocation_guidance(self):
        path = self.add_swap()
        before = (self.swaps.read_bytes(), self.fstab.read_bytes(), path.read_bytes())
        with self.assertRaises(UpdateError) as raised:
            recovery.check_snapshot_swapfiles(LAYOUT)
        message = str(raised.exception)
        self.assertEqual(message.count(repr(str(path))), 1)
        for expected in ("active", "@swap", "/etc/fstab", "RAM", "hibernation", "RECOVERY.md"):
            self.assertIn(expected, message)
        self.assertEqual(before, (self.swaps.read_bytes(), self.fstab.read_bytes(), path.read_bytes()))

    def test_inactive_configured_root_swap_is_blocked_including_noauto(self):
        self.add_swap("automatic", active=False)
        self.add_swap("manual", active=False, options="noauto")
        with self.assertRaises(UpdateError) as raised:
            recovery.check_snapshot_swapfiles(LAYOUT)
        message = str(raised.exception)
        self.assertIn("automatic", message)
        self.assertIn("manual", message)
        self.assertIn("configured in /etc/fstab", message)

    def test_active_file_without_fstab_entry_is_blocked(self):
        self.add_swap(configured=False)
        with self.assertRaisesRegex(UpdateError, "root subvolume"):
            recovery.check_snapshot_swapfiles(LAYOUT)

    def test_separate_subvolumes_and_other_filesystems_do_not_block_root(self):
        self.add_swap("swap/swapfile", root_id="258")
        self.add_swap("nested/swapfile", root_id="260")
        self.add_swap("other-disk/swapfile", uuid="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        self.add_swap("ext4/swapfile", fstype="ext4")
        recovery.check_snapshot_swapfiles(LAYOUT)

    def test_directory_named_swap_is_not_a_separate_subvolume(self):
        path = self.add_swap("swap/swapfile")
        with self.assertRaisesRegex(UpdateError, "root subvolume") as raised:
            recovery.check_snapshot_swapfiles(LAYOUT)
        self.assertIn(str(path), str(raised.exception))

    def test_alias_of_root_file_is_detected_by_identity_not_mount_path(self):
        path = self.add_swap("bind-alias/swapfile")
        self.mounts[str(path)]["target"] = "/mnt/another-name"
        with self.assertRaisesRegex(UpdateError, "root subvolume"):
            recovery.check_snapshot_swapfiles(LAYOUT)

    def test_configured_symlink_to_root_swapfile_is_not_ignored(self):
        path = self.add_swap(configured=False, active=False)
        alias = self.root / "swap-link"
        alias.symlink_to(path)
        self.fstab.write_text(f"{alias} none swap defaults 0 0\n")
        self.mounts[str(alias)] = self.mounts[str(path)]
        self.ids[str(alias)] = "256"
        with self.assertRaises(UpdateError) as raised:
            recovery.check_snapshot_swapfiles(LAYOUT)
        self.assertIn(str(alias), str(raised.exception))

    def test_escaped_spaces_tabs_and_literal_backslashes_are_decoded_once(self):
        path = self.add_swap("space tab\tbackslash\\040swap")
        with self.assertRaises(UpdateError) as raised:
            recovery.check_snapshot_swapfiles(LAYOUT)
        self.assertIn(repr(str(path)), str(raised.exception))

    def test_swap_partitions_zram_and_missing_inactive_files_are_ignored(self):
        self.swaps.write_text(HEADER + "/dev/mapper/swap partition 1024 0 -2\n/dev/zram0 partition 1024 0 100\n")
        self.fstab.write_text("UUID=swap-uuid none swap defaults 0 0\n/dev/does-not-exist none swap defaults 0 0\n"
                              + str(self.root / "missing") + " none swap defaults 0 0\n")
        recovery.check_snapshot_swapfiles(LAYOUT)
        self.command.assert_not_called()

    def test_lvm_and_unsupported_layouts_do_not_inspect_btrfs_swap(self):
        self.add_swap()
        self.swaps.unlink()
        for layout in (dict(LAYOUT, backend="lvm"), dict(available=False), {}):
            recovery.check_snapshot_swapfiles(layout)
        self.command.assert_not_called()

    def test_unreadable_active_file_or_inventory_does_not_silently_disable_recovery(self):
        path = self.add_swap()
        path.unlink()
        with self.assertRaisesRegex(UpdateError, "Cannot verify Btrfs snapshot safety"):
            recovery.check_snapshot_swapfiles(LAYOUT)
        self.swaps.write_text("bad inventory")
        with self.assertRaisesRegex(UpdateError, "Unrecognized /proc/swaps"):
            recovery.check_snapshot_swapfiles(LAYOUT)

    def test_failed_or_ambiguous_identity_check_stops_snapshotting(self):
        self.add_swap()
        self.command.side_effect = subprocess.CalledProcessError(1, ["btrfs"])
        with self.assertRaisesRegex(UpdateError, "Cannot verify Btrfs snapshot safety"):
            recovery.check_snapshot_swapfiles(LAYOUT)
        self.command.side_effect = self.output
        self.ids["/"] = "not an id"
        with self.assertRaises(UpdateError):
            recovery.check_snapshot_swapfiles(LAYOUT)

    def test_offline_recheck_fails_before_archives_or_boot_configuration_changes(self):
        self.add_swap()
        with patch.object(recovery, "probe_recovery", return_value=LAYOUT), \
             patch.object(recovery, "ensure_recovery_bootloader") as bootloader, \
             patch.object(recovery.subprocess, "run") as mutate:
            with self.assertRaisesRegex(UpdateError, "swapfile"):
                recovery.create_recovery(self.root / ("a" * 32), LAYOUT)
        bootloader.assert_not_called()
        mutate.assert_not_called()

    def test_swap_activated_during_archiving_still_stops_before_first_snapshot(self):
        top = self.root / "top"
        native_path = Path
        def path(value):
            return top if value == "/run/nobara-updater-btrfs" else native_path(value)
        with patch.object(recovery, "Path", side_effect=path), \
             patch.object(Path, "is_mount", return_value=False), \
             patch.object(recovery, "probe_recovery", return_value=LAYOUT), \
             patch.object(recovery, "check_snapshot_swapfiles", side_effect=[None, UpdateError("New active swapfile")]) as check, \
             patch.object(recovery, "ensure_recovery_bootloader"), \
             patch.object(recovery, "check_recovery_space"), \
             patch.object(recovery.shutil, "disk_usage", return_value=types.SimpleNamespace(free=2**40)), \
             patch.object(recovery.subprocess, "run") as command:
            with self.assertRaisesRegex(UpdateError, "New active swapfile"):
                recovery.create_recovery(self.root / ("a" * 32), LAYOUT)
        self.assertEqual(check.call_count, 2)
        self.assertEqual([call.args[0][0] for call in command.call_args_list], ["mount", "umount"])


if __name__ == "__main__":
    unittest.main()
