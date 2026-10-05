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


class LvmTunedEntryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="nobara-lvm-tuned-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.boot = self.root / "boot"
        self.entries = self.boot / "loader/entries"
        self.entries.mkdir(parents=True)
        for image in ("vmlinuz", "initramfs", "tuned.img"):
            (self.boot / image).write_text(image)
        self.entry = self.entries / "normal.conf"
        self.entry.write_text("title Nobara\nlinux /vmlinuz\ninitrd /initramfs $tuned_initrd\n"
                              "options $kernelopts $tuned_params\n")
        self.env = dict(kernelopts="root=UUID=original ro rd.luks.uuid=luks-123 resume=UUID=swap rd.lvm.lv=old/root",
                        tuned_params="isolcpus=1", tuned_initrd="/tuned.img")
        self.job = self.root / "jobs" / JOB
        self.job.mkdir(parents=True)
        self.layout = dict(vg=ORIGIN["vg_name"], lv="root", origin_uuid=ORIGIN["lv_uuid"],
                           vg_uuid=ORIGIN["vg_uuid"], origin_path=ORIGIN["lv_path"], reclaim=[],
                           snapshot_bytes=int(RESERVE["lv_size"]), entry=str(self.entry))

        def command(args, **kwargs):
            if args[0] == "dracut":
                Path(args[-1]).write_text("recovery initrd")
            elif args[0] != "lsinitrd":
                raise AssertionError(args)
            return subprocess.CompletedProcess(args, 0)

        for patcher in (patch.object(lvm, "BOOT", self.boot), patch.object(recovery, "BOOT", self.boot),
                        patch.object(recovery, "grub_environment", side_effect=lambda: dict(self.env)),
                        patch.object(lvm, "inventory", return_value=[ORIGIN, SNAPSHOT]),
                        patch.object(lvm, "output"), patch.object(lvm.os, "sync"),
                        patch.object(lvm.subprocess, "run", side_effect=command)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.restored = self.entries / ("nobara-restored-" + JOB + ".conf")
        self.recovery_entry = self.entries / ("nobara-recovery-" + JOB + ".conf")
        self.archive = self.boot / "nobara-updater" / JOB

    def create(self):
        return lvm.create(self.job, self.layout)

    def options(self, entry):
        return next(line[8:] for line in entry.read_text().splitlines() if line.startswith("options "))

    def finish(self):
        with patch.object(lvm, "inventory", return_value=[ORIGIN, RESERVE]):
            lvm.finish(self.layout, JOB, state_root=self.root)

    def test_confirmed_restored_entry_follows_new_profiles_but_keeps_archived_images(self):
        saved = self.create()
        images = [line for line in self.restored.read_text().splitlines() if line.startswith(("linux ", "initrd "))]
        self.assertIn("/nobara-updater/" + JOB + "/tuned.img", images[-1])
        for entry in (self.restored, self.recovery_entry):
            self.assertIn("isolcpus=1", self.options(entry))
            self.assertNotIn("$", entry.read_text())
            self.assertIn("rd.luks.uuid=luks-123", self.options(entry))
        self.assertIn("nobara.rollback=" + JOB, self.options(self.recovery_entry))
        self.env.update(saved_entry=saved["restored_entry"], tuned_params="isolcpus=2", tuned_initrd="/missing-live.img")
        self.finish()
        options = self.options(self.restored)
        self.assertIn(" $tuned_params", options)
        self.assertNotIn("'$tuned_params'", options)
        self.assertNotIn("isolcpus=1", options)
        self.assertNotIn("$kernelopts", options)
        self.assertIn("isolcpus=2", recovery.expand_bls("options", options, self.env))
        self.assertIn("root=" + ORIGIN["lv_path"], options)
        self.assertIn("rd.lvm.lv=nobara_test/root", options)
        self.assertNotIn("resume=", options)
        self.assertEqual(images, [line for line in self.restored.read_text().splitlines() if line.startswith(("linux ", "initrd "))])
        self.assertEqual((self.archive / "tuned.img").read_text(), "tuned.img")
        self.assertFalse(self.recovery_entry.exists())
        self.finish()
        self.assertEqual(options, self.options(self.restored))

    def test_armed_recovery_does_not_restore_dynamic_arguments_or_remove_entries(self):
        self.create()
        before = self.restored.read_text()
        self.env.update(saved_entry=self.restored.stem, nobara_fallback=self.recovery_entry.stem)
        with self.assertRaisesRegex(UpdateError, "before boot confirmation"):
            self.finish()
        self.assertEqual(self.restored.read_text(), before)
        self.assertTrue(self.recovery_entry.exists())
        lvm.output.assert_called_once()  # Only snapshot creation ran.

    def test_successful_updated_boot_does_not_promote_unused_restored_entry(self):
        self.create()
        self.env["saved_entry"] = "normal"
        with patch.object(lvm, "restore_tuned_options") as restore:
            self.finish()
        restore.assert_not_called()
        self.assertFalse(self.restored.exists())
        self.assertFalse(self.archive.exists())

    def test_empty_tuned_variables_can_follow_a_profile_enabled_after_recovery(self):
        self.env.pop("tuned_params")
        self.env.pop("tuned_initrd")
        self.create()
        self.env.update(saved_entry=self.restored.stem, tuned_params="isolcpus=2")
        self.finish()
        self.assertIn("isolcpus=2", recovery.expand_bls("options", self.options(self.restored), self.env))

    def test_entries_without_tuned_and_legacy_archives_remain_unchanged(self):
        self.entry.write_text(self.entry.read_text().replace(" $tuned_params", ""))
        self.create()
        self.assertFalse((self.archive / "bls-source.json").exists())
        before = self.restored.read_text()
        self.env["saved_entry"] = self.restored.stem
        self.finish()
        self.assertEqual(before, self.restored.read_text())

    def test_manual_boot_option_changes_are_preserved(self):
        self.create()
        self.restored.write_text(self.restored.read_text().replace("isolcpus=1", "isolcpus=3"))
        before = self.restored.read_text()
        self.env["saved_entry"] = self.restored.stem
        with self.assertLogs(level="WARNING"):
            self.finish()
        self.assertEqual(before, self.restored.read_text())

    def test_wrong_root_in_saved_template_cannot_replace_the_confirmed_entry(self):
        self.create()
        source = self.archive / "bls-source.json"
        record = json.loads(source.read_text())
        record["options"] = record["options"].replace(ORIGIN["lv_path"], "/dev/other/root")
        source.write_text(json.dumps(record))
        before = self.restored.read_text()
        self.env["saved_entry"] = self.restored.stem
        with self.assertRaisesRegex(UpdateError, "confirmed root"):
            self.finish()
        self.assertEqual(before, self.restored.read_text())
        self.assertTrue(self.recovery_entry.exists())


if __name__ == "__main__":
    unittest.main()
