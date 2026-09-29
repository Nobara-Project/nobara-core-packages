"""LVM capacity/ownership checks and recovery boot state transitions."""
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
from nobara_updater import update_lvm as lvm, update_recovery as recovery
from nobara_updater.update_state import UpdateError

GIB = 1024**3
JOB = "a" * 32
ORIGIN = dict(vg_name="nobara_test", vg_uuid="group-uuid", lv_name="root", lv_uuid="root-uuid",
              lv_path="/dev/nobara_test/root", lv_size=str(24 * GIB), lv_attr="-wi-a-----",
              segtype="linear", origin="", origin_uuid="", lv_tags="", data_percent="")
RESERVE = dict(ORIGIN, lv_name="nobara_reserve", lv_uuid="reserve-uuid", lv_path="/dev/nobara_test/nobara_reserve",
               lv_size=str(lvm.snapshot_bytes(24 * GIB)), lv_attr="-ri-a-----", lv_tags=lvm.RESERVE_TAG)
SNAPSHOT = dict(RESERVE, lv_name="nobara_rollback_" + JOB, lv_uuid="snapshot-uuid", lv_attr="swi-a-s---",
               origin="root", origin_uuid="root-uuid", lv_tags=lvm.SNAPSHOT_TAG, segtype="snapshot")


class LvmRecoveryTests(unittest.TestCase):
    def probe(self, rows=None, free=0, fstype="ext4", home="/dev/nobara_test/home", fstab="UUID=abc /home ext4 defaults 0 2\n"):
        def output(args):
            if args[0] == "findmnt":
                return json.dumps(dict(filesystems=[dict(target="/home", source=home)]))
            self.assertEqual(args[:2], ["lvm", "vgs"])
            return json.dumps(dict(report=[dict(vg=[dict(vg_free=str(free), vg_uuid="group-uuid", vg_extent_size=str(4 * 1024**2))])]))
        with patch.object(lvm, "inventory", return_value=rows or [ORIGIN, RESERVE]), \
             patch.object(lvm, "output", side_effect=output), patch.object(Path, "read_text", return_value=fstab), \
             patch.object(Path, "is_file", return_value=True):
            return lvm.probe(dict(fstype=fstype, source=ORIGIN["lv_path"]))

    def test_ext4_and_xfs_reserve_the_full_origin_including_metadata(self):
        for fs in ("ext4", "xfs"):
            with self.subTest(fs=fs):
                layout = self.probe(fstype=fs)
                self.assertGreater(layout["snapshot_bytes"], 24 * GIB)
                self.assertEqual(layout["backend"], "lvm")
                self.assertEqual(layout["reclaim"], [dict(name="nobara_reserve", uuid="reserve-uuid")])

    def test_existing_owned_snapshot_supplies_the_next_reserve(self):
        self.assertEqual(self.probe(rows=[ORIGIN, SNAPSHOT])["reclaim"],
                         [dict(name=SNAPSHOT["lv_name"], uuid="snapshot-uuid")])

    def test_foreign_snapshot_and_writable_or_untagged_reserve_are_not_reclaimed(self):
        for foreign in (dict(RESERVE, lv_tags=""), dict(RESERVE, lv_attr="-wi-a-----"),
                        dict(SNAPSHOT, origin_uuid="other-root"), dict(SNAPSHOT, lv_tags=""),
                        dict(SNAPSHOT, lv_name="administrator_snapshot")):
            with self.subTest(foreign=foreign), self.assertRaises(UpdateError):
                self.probe(rows=[ORIGIN, foreign])

    def test_thin_raid_and_snapshot_roots_are_rejected(self):
        for origin in (dict(ORIGIN, segtype="thin"), dict(ORIGIN, segtype="raid1"),
                       dict(ORIGIN, lv_attr="swi-a-s---", origin="something")):
            with self.subTest(origin=origin), self.assertRaisesRegex(UpdateError, "linear"):
                self.probe(rows=[origin, RESERVE])

    def test_home_must_be_independent_and_persistent(self):
        for args in (dict(home=ORIGIN["lv_path"]), dict(fstab=""), dict(fstab="UUID=abc /home xfs noauto 0 0")):
            with self.subTest(args=args), self.assertRaisesRegex(UpdateError, "/home"):
                self.probe(**args)

    def test_free_space_must_cover_full_root_even_if_current_use_is_small(self):
        with self.assertRaisesRegex(UpdateError, "rollback needs"):
            self.probe(rows=[ORIGIN], free=12 * GIB)
        self.assertEqual(self.probe(rows=[ORIGIN], free=26 * GIB)["reclaim"], [])

    def test_unsafe_names_are_rejected(self):
        for name in ("-bad", "../root", "root;reboot", "root\n", "", "a" * 128):
            with self.subTest(name=name), self.assertRaises(UpdateError):
                lvm.name(name)


class RecoveryBootTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.env = {}
        self.commands = []
        for entry in ("normal", "nobara-recovery-" + JOB):
            p = self.root / "boot/loader/entries" / (entry + ".conf")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.touch()
        def path(value):
            return self.root / str(value).lstrip("/")
        def run(command, **kwargs):
            self.commands.append(command)
            if command[0] == "grub2-set-default":
                self.env["saved_entry"] = command[-1]
            elif command[0] == "grub2-editenv":
                if command[2] == "set":
                    for item in command[3:]:
                        key, value = item.split("=", 1)
                        self.env[key] = value
                elif command[2] == "unset":
                    for key in command[3:]:
                        self.env.pop(key, None)
            return subprocess.CompletedProcess(command, 0)
        self.addCleanup(patch.stopall)
        patch.object(recovery, "Path", side_effect=path).start()
        patch.object(recovery.subprocess, "run", side_effect=run).start()
        patch.object(recovery, "grub_environment", side_effect=lambda: dict(self.env)).start()
        patch.object(recovery.os, "sync").start()
        self.recovery = dict(created=True, entry_id="nobara-recovery-" + JOB)

    def test_fallback_is_armed_before_the_trial_and_retired_after_confirmation(self):
        recovery.trial_boot(self.recovery)
        self.assertEqual(self.env["nobara_fallback"], self.recovery["entry_id"])
        self.assertTrue((self.root / "boot/nobara-updater/recovery-armed").exists())
        # Kernel-install can change its ordinary variables without disarming
        # our fallback, including between individual RPM scriptlets.
        self.env.update(saved_entry="new-kernel", next_entry="new-kernel")
        recovery.trial_boot(self.recovery, "normal")
        self.assertEqual(self.env["nobara_trial"], "normal")
        self.assertEqual(self.env["nobara_fallback"], self.recovery["entry_id"])
        recovery.confirm_trial("normal")
        self.assertEqual(self.env["saved_entry"], "normal")
        self.assertNotIn("nobara_fallback", self.env)
        self.assertNotIn("nobara_trial", self.env)
        self.assertFalse((self.root / "boot/nobara-updater/recovery-armed").exists())

    def test_missing_trial_cannot_disarm_recovery(self):
        with self.assertRaisesRegex(UpdateError, "missing or invalid"):
            recovery.trial_boot(self.recovery, "missing")
        self.assertEqual(self.env["nobara_fallback"], self.recovery["entry_id"])
        self.assertNotIn("nobara_trial", self.env)


if __name__ == "__main__":
    unittest.main()
