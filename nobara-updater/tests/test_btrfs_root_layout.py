"""Resumable root promotion using disposable files and simulated mount IDs."""
import json
import os
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
from nobara_updater import update_btrfs as layout
from nobara_updater.update_state import UpdateError

JOB = "a" * 32
UUID = "12345678-1234-1234-1234-123456789abc"
MACHINE = "b" * 32
SOURCE_ROOT = ".nobara-updater/" + JOB + "/root"


class RootLayoutTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="nobara-root-layout-")
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.top, self.boot = self.directory / "top", self.directory / "boot"
        self.source = self.top / SOURCE_ROOT
        self.original = self.top / "@"
        self.active = self.directory / "active"
        self.active.symlink_to(self.source, target_is_directory=True)
        for path, identifier in ((self.source, 300), (self.original, 256), (self.top / "@home", 257)):
            path.mkdir(parents=True)
            (path / "id").write_text(str(identifier))
            (path / "etc").mkdir()
            (path / "etc/machine-id").write_text(MACHINE + "\n")
        (self.source / "labwc").write_text("custom labwc installed after recovery")
        (self.original / "labwc").write_text("abandoned system")
        (self.source.parent / "owner.json").write_text(json.dumps(dict(owner="nobara-updater", job=JOB)))
        self.state_root = self.active / "var/lib/nobara-updater"
        (self.state_root / "jobs" / JOB).mkdir(parents=True)
        self.plan(JOB, "/@")
        (self.active / "etc/fstab").write_text(
            f"UUID={UUID} / btrfs relatime,compress=zstd:1,discard=async,subvol={SOURCE_ROOT} 0 0\n"
            f"UUID={UUID} /home btrfs subvol=@home,relatime,discard=async 0 0\n")
        (self.active / "etc/kernel").mkdir()
        self.options = f"root=UUID={UUID} ro rd.luks.uuid=luks-123 quiet rootflags=subvol={SOURCE_ROOT},compress=zstd:1"
        (self.active / "etc/kernel/cmdline").write_text(self.options + "\n")
        (self.active / "etc/default").mkdir()
        (self.active / "etc/default/grub").write_text(
            f'GRUB_DEFAULT=saved\nGRUB_TIMEOUT=17\nGRUB_CMDLINE_LINUX="quiet rd.luks.uuid=luks-123 rootflags=subvol={SOURCE_ROOT},compress=zstd:1"\n')
        self.entries = self.boot / "loader/entries"
        self.entries.mkdir(parents=True)
        self.normal = self.entry(MACHINE + "-kernel", self.options)
        self.recovery = self.entry("nobara-recovery-" + JOB, self.options)
        self.environment = dict(saved_entry=self.normal.stem, kernelopts=self.options)
        self.state = dict(status="complete", job=JOB, normal_boot_entry=self.normal.stem)
        self.commands = []
        native_rename, native_stat = os.rename, Path.stat

        def rename(source, target):
            self.assertTrue(all("subvolid=300" in path.read_text() for path in (self.normal, self.recovery)))
            self.assertIn("subvolid=300", (self.active / "etc/fstab").read_text())
            native_rename(source, target)
            if source == self.source:
                self.active.unlink()
                self.active.symlink_to(target, target_is_directory=True)

        def stat(path, *args, **kwargs):
            value = native_stat(path, *args, **kwargs)
            return types.SimpleNamespace(st_dev=value.st_dev + 1) if path == self.boot else value

        self.rename = rename
        for patcher in (patch.object(layout, "ROOT", self.active), patch.object(layout, "BOOT", self.boot),
                        patch.object(layout, "TOP", self.top), patch.object(layout, "output", side_effect=self.output),
                        patch.object(layout, "subvolume_id", side_effect=lambda path: int((path / "id").read_text())),
                        patch.object(layout.os, "rename", side_effect=rename), patch.object(layout.os, "sync"),
                        patch.object(Path, "is_mount", lambda path: path == self.boot),
                        patch.object(Path, "stat", stat)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def plan(self, job, original, uuid=UUID):
        parent = self.state_root / "jobs" / job
        parent.mkdir(parents=True, exist_ok=True)
        (parent / "plan.json").write_text(json.dumps(dict(recovery=dict(fsroot=original, uuid=uuid))))

    def entry(self, name, options):
        for image in ("vmlinuz", "initramfs"):
            (self.boot / image).write_text(image)
        path = self.entries / (name + ".conf")
        path.write_text(f"title Nobara\nversion 7.2\nlinux /vmlinuz\ninitrd /initramfs\noptions {options}\n")
        return path

    def output(self, command):
        self.commands.append(command)
        if command[0] == "findmnt":
            current = self.active.resolve().relative_to(self.top)
            return json.dumps(dict(filesystems=[dict(fstype="btrfs", uuid=UUID, fsroot="/" + str(current))]))
        if command[0] == "grub2-editenv":
            if command[-1] == "list":
                return "\n".join(k + "=" + v for k, v in self.environment.items())
            if command[-2] == "set":
                key, value = command[-1].split("=", 1)
                self.environment[key] = value
                return ""
        if command[0] in {"mount", "umount"}:
            return ""
        raise AssertionError(command)

    def restore(self):
        return layout.restore_root_layout(self.state_root, self.state)

    def assert_restored(self):
        self.assertEqual(self.active.resolve(), self.original)
        self.assertEqual((self.original / "id").read_text(), "300")
        self.assertEqual((self.active / "labwc").read_text(), "custom labwc installed after recovery")
        displaced = self.source.parent / "displaced"
        self.assertEqual((displaced / "labwc").read_text(), "abandoned system")
        self.assertFalse((self.state_root / layout.JOURNAL).exists())
        self.assertFalse(self.source.exists())
        for path in (self.active / "etc/fstab", self.active / "etc/kernel/cmdline", self.normal,
                     self.active / "etc/default/grub"):
            text = path.read_text()
            self.assertIn("subvol=@", text)
            self.assertNotIn(SOURCE_ROOT, text)
            self.assertNotIn("subvolid=300", text)
        self.assertIn("rd.luks.uuid=luks-123", self.normal.read_text())
        self.assertIn("discard=async", (self.active / "etc/fstab").read_text())
        self.assertIn("relatime", (self.active / "etc/fstab").read_text())
        self.assertEqual(self.environment["saved_entry"], self.normal.stem)

    def test_restores_same_live_subvolume_and_keeps_recovery_edits_without_timeshift(self):
        result = self.restore()
        self.assertEqual(result["target"], "@")
        self.assertEqual(result["root_id"], 300)
        self.assert_restored()
        self.assertIsNone(self.restore())

    def tuned_entry(self, params="", initrd=""):
        self.normal.write_text("title Nobara\nversion 7.2\nlinux /vmlinuz\n"
                               "initrd /initramfs $tuned_initrd\noptions $kernelopts $tuned_params\n")
        cmdline = self.active / "etc/kernel/cmdline"
        cmdline.write_text(self.options + " $tuned_params\n")
        config = self.boot / "grub2/grub.cfg"
        config.parent.mkdir(exist_ok=True)
        config.write_text('### BEGIN /etc/grub.d/00_tuned ###\n'
                          f'set tuned_params="{params}"\nset tuned_initrd="{initrd}"\n'
                          '### END /etc/grub.d/00_tuned ###\n')

    def assert_tuned_restored(self):
        self.assert_restored()
        for path in (self.normal, self.active / "etc/kernel/cmdline"):
            self.assertIn(" $tuned_params\n", path.read_text())
            self.assertNotIn("'$tuned_params'", path.read_text())
            self.assertNotIn("isolcpus=", path.read_text())
        self.assertIn("initrd /initramfs $tuned_initrd\n", self.normal.read_text())

    def test_empty_tuned_variables_allow_restoring_the_root_name(self):
        self.tuned_entry()
        self.restore()
        self.assert_tuned_restored()

    def test_tuned_overlay_uses_grub_config_precedence_and_options_stay_dynamic(self):
        self.tuned_entry("isolcpus=1", "/tuned.img")
        self.environment.update(tuned_params="isolcpus=2", tuned_initrd="/stale.img")
        (self.boot / "tuned.img").write_bytes(b"overlay")
        self.restore()
        self.assert_tuned_restored()

    def test_tuned_root_repair_can_resume_after_interruption(self):
        self.tuned_entry("isolcpus=1")
        with patch.object(layout.os, "rename", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                self.restore()
        self.assertIn("subvolid=300 $tuned_params", self.normal.read_text())
        self.restore()
        self.assert_tuned_restored()

    def test_missing_or_external_tuned_overlay_stops_before_any_root_changes(self):
        outside = self.directory / "outside.img"
        outside.write_bytes(b"overlay")
        (self.boot / "external.img").symlink_to(outside)
        for initrd in ("/missing.img", "/external.img", "/../outside.img", "$unknown"):
            with self.subTest(initrd=initrd):
                self.tuned_entry(initrd=initrd)
                with self.assertRaises(UpdateError):
                    self.restore()
                self.assertTrue(self.source.exists())
                self.assertFalse((self.state_root / layout.JOURNAL).exists())

    def test_unknown_boot_variable_still_stops_before_any_root_changes(self):
        self.normal.write_text(self.normal.read_text().replace(self.options, self.options + " $unknown"))
        with self.assertRaisesRegex(UpdateError, "Unsupported variable"):
            self.restore()
        self.assertTrue(self.source.exists())
        self.assertFalse((self.state_root / layout.JOURNAL).exists())

    def test_uses_recorded_non_timeshift_root_name(self):
        original = self.top / "root"
        self.original.replace(original)
        self.plan(JOB, "/root")
        self.assertEqual(self.restore()["target"], "root")
        self.assertEqual(self.active.resolve(), original)
        self.assertIn("subvol=root", (self.active / "etc/fstab").read_text())

    def test_follows_multiple_recoveries_back_to_original_root(self):
        older = "c" * 32
        self.plan(JOB, "/.nobara-updater/" + older + "/root")
        self.plan(older, "/@")
        self.restore()
        self.assert_restored()

    def test_recorded_timeshift_snapshot_is_never_replaced_by_the_live_root(self):
        relative = "timeshift-btrfs/snapshots/2026-10-04_04-00-00/@"
        snapshot = self.top / relative
        snapshot.parent.mkdir(parents=True)
        self.original.replace(snapshot)
        before = {path: path.read_text() for path in (
            self.normal, self.recovery, self.active / "etc/fstab", self.active / "etc/kernel/cmdline")}
        for chained in (False, True):
            with self.subTest(chained=chained):
                older = "c" * 32
                self.plan(JOB, "/.nobara-updater/" + older + "/root" if chained else "/" + relative)
                if chained:
                    self.plan(older, "/" + relative)
                with self.assertRaisesRegex(UpdateError, "inside Timeshift snapshot storage"):
                    self.restore()
                self.assertEqual(self.active.resolve(), self.source)
                self.assertEqual((snapshot / "id").read_text(), "256")
                self.assertEqual((snapshot / "labwc").read_text(), "abandoned system")
                self.assertEqual({path: path.read_text() for path in before}, before)
                self.assertFalse((self.source.parent / "displaced").exists())
                self.assertFalse((self.state_root / layout.JOURNAL).exists())

    def test_root_path_can_be_recovered_from_saved_marker_when_old_plan_is_missing(self):
        (self.state_root / "jobs" / JOB / "plan.json").unlink()
        self.state["recovery_boot"] = dict(job=JOB, original_subvolume="/@")
        self.restore()
        self.assert_restored()

    def test_missing_or_cyclic_or_foreign_ancestry_does_not_guess_root_path(self):
        for original, uuid in (("/../@", UUID), ("//etc", UUID), ("/" + SOURCE_ROOT, UUID), ("/@", "other")):
            with self.subTest(original=original, uuid=uuid):
                self.plan(JOB, original, uuid)
                with self.assertRaises(UpdateError):
                    self.restore()
                self.assertTrue(self.source.exists())
                self.assertFalse((self.state_root / layout.JOURNAL).exists())
        (self.state_root / "jobs" / JOB / "plan.json").unlink()
        with self.assertRaisesRegex(UpdateError, "not recorded"):
            self.restore()

    def test_foreign_boot_root_and_top_level_home_are_preserved(self):
        other = self.entry("other-os", f"root=UUID={UUID} rootflags=subvol=@")
        foreign = self.entry("other-device", "root=UUID=other rootflags=subvol=@")
        (self.entries / "memtest.conf").write_text("title Memory test\nefi /memtest.efi\n")
        self.restore()
        self.assertIn("subvolid=256", other.read_text())
        self.assertIn("rootflags=subvol=@", foreign.read_text())
        self.assertIn("/home btrfs subvol=@home", (self.active / "etc/fstab").read_text())

    def test_nested_shared_mount_is_pinned_by_id_before_its_parent_moves(self):
        nested = self.original / "home"
        nested.mkdir()
        (nested / "id").write_text("258")
        fstab = self.active / "etc/fstab"
        fstab.write_text(fstab.read_text().replace("subvol=@home", "subvol=@/home"))
        self.restore()
        self.assertIn("subvolid=258", fstab.read_text())
        self.assertEqual((self.source.parent / "displaced/home/id").read_text(), "258")

    def test_unmounted_fstab_reference_to_original_root_prevents_its_retirement(self):
        fstab = self.active / "etc/fstab"
        fstab.write_text(fstab.read_text() + f"UUID={UUID} /mnt/old-root btrfs noauto,subvol=@ 0 0\n")
        result = self.restore()
        self.assertIn(256, result["retained_ids"])
        self.assertIn("noauto,subvolid=256", fstab.read_text())

    def test_existing_id_reference_to_unmounted_original_root_is_retained(self):
        fstab = self.active / "etc/fstab"
        fstab.write_text(fstab.read_text() + f"UUID={UUID} /mnt/old-root btrfs noauto,subvolid=256 0 0\n")
        self.assertIn(256, self.restore()["retained_ids"])

    def test_ambiguous_boot_device_for_moved_root_stops_before_changes(self):
        self.entry("other-os", "root=LABEL=Nobara rootflags=subvol=@")
        with self.assertRaisesRegex(UpdateError, "ambiguous root device"):
            self.restore()
        self.assertFalse((self.state_root / layout.JOURNAL).exists())
        self.assertTrue(self.source.exists())

    def test_never_moves_original_path_reused_by_another_installation(self):
        (self.original / "etc/machine-id").write_text("d" * 32)
        with self.assertRaisesRegex(UpdateError, "different installation"):
            self.restore()
        self.assertFalse((self.state_root / layout.JOURNAL).exists())

    def test_armed_update_or_scheduled_trigger_prevents_any_move(self):
        for key in ("nobara_fallback", "nobara_trial", "next_entry"):
            with self.subTest(key=key):
                self.environment[key] = "pending"
                with self.assertRaises(UpdateError):
                    self.restore()
                del self.environment[key]
        (self.active / "system-update").symlink_to("/missing-job")
        with self.assertRaisesRegex(UpdateError, "scheduled"):
            self.restore()
        self.assertFalse((self.state_root / layout.JOURNAL).exists())
        self.assertTrue(self.source.exists())

    def test_restarts_after_displaced_rename_without_reversing_completed_move(self):
        def interrupted(source, target):
            self.rename(source, target)
            raise OSError("simulated power loss")
        with patch.object(layout.os, "rename", side_effect=interrupted):
            with self.assertRaisesRegex(OSError, "simulated power loss"):
                self.restore()
        self.assertIn("subvolid=300", self.normal.read_text())
        self.restore()
        self.assert_restored()

    def test_restarts_after_active_root_rename(self):
        def interrupted(source, target):
            self.rename(source, target)
            if source == self.source:
                raise OSError("power loss after active rename")
        with patch.object(layout.os, "rename", side_effect=interrupted):
            with self.assertRaises(OSError):
                self.restore()
        self.restore()
        self.assert_restored()

    def test_partial_reference_writes_are_resumed_before_moving_anything(self):
        write = layout.write_boot_file
        count = 0
        def interrupted(path, **kwargs):
            nonlocal count
            write(path, **kwargs)
            count += 1
            if count == 2:
                raise OSError("power loss writing boot references")
        with patch.object(layout, "write_boot_file", side_effect=interrupted):
            with self.assertRaises(OSError):
                self.restore()
        self.assertTrue(self.source.exists())
        self.assertEqual((self.original / "id").read_text(), "256")
        self.restore()
        self.assert_restored()

    def test_configuration_changes_after_interruption_are_not_overwritten(self):
        with patch.object(layout.os, "rename", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                self.restore()
        self.normal.write_text(self.normal.read_text() + "# administrator edit\n")
        with self.assertRaisesRegex(UpdateError, "Configuration changed"):
            self.restore()
        self.assertTrue(self.source.exists())
        self.assertIn("# administrator edit", self.normal.read_text())

    def test_non_btrfs_systems_do_not_enter_root_layout_maintenance(self):
        for fstype in ("ext4", "xfs"):
            with self.subTest(fstype=fstype), patch.object(layout, "output", return_value=json.dumps(
                    dict(filesystems=[dict(fstype=fstype, uuid=UUID, fsroot="/")]))) as command:
                self.assertIsNone(self.restore())
                command.assert_called_once()

    def test_missing_ownership_does_not_create_a_pending_operation(self):
        (self.source.parent / "owner.json").write_text("{}")
        with self.assertRaisesRegex(UpdateError, "ownership"):
            self.restore()
        self.assertFalse((self.state_root / layout.JOURNAL).exists())

    def test_partial_final_references_are_resumed_after_active_root_moved(self):
        native_write = layout.write_boot_file
        def interrupted(path, **kwargs):
            native_write(path, **kwargs)
            if not self.source.exists() and path == self.active / "etc/fstab":
                raise OSError("power loss writing final references")
        with patch.object(layout, "write_boot_file", side_effect=interrupted):
            with self.assertRaises(OSError):
                self.restore()
        self.assertIn("subvolid=300", self.normal.read_text())
        self.assertIn("subvol=@", (self.active / "etc/fstab").read_text())
        self.restore()
        self.assert_restored()


class RootLayoutWorkflowTests(unittest.TestCase):
    def setUp(self):
        from nobara_updater import update_backend as backend
        from nobara_updater import update_state as state
        self.backend, self.state = backend, state
        temp = tempfile.TemporaryDirectory(prefix="nobara-layout-workflow-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.saved = dict(status="complete", job=JOB, normal_boot_entry="normal", recovery_cleanup_complete=True)
        state.write_state(self.root, self.saved, "complete")
        for name in ("prune_recovery", "prune_payloads", "resolve_notice", "cleanup_kernel_retention"):
            patcher = patch.object(backend, name)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_newly_restored_legacy_root_gets_cleanup_even_if_old_cleanup_completed(self):
        result = dict(uuid=UUID, target="@", root_id=300)
        with patch.object(layout, "restore_root_layout", return_value=result):
            self.backend.cleanup_confirmed_update(self.root, self.saved)
        self.backend.prune_recovery.assert_called_once()
        saved = self.state.read_state(self.root)
        self.assertEqual(saved["btrfs_root_layout"], result)
        self.assertEqual(saved["status"], "complete")
        self.assertTrue(saved["recovery_cleanup_complete"])

    def test_layout_failure_leaves_confirmed_boot_valid_and_defers_cleanup(self):
        with patch.object(layout, "restore_root_layout", side_effect=UpdateError("configuration changed")), \
             self.assertLogs(self.backend.LOG, level="ERROR"):
            self.backend.cleanup_confirmed_update(self.root, self.saved)
        self.backend.prune_recovery.assert_not_called()
        self.backend.prune_payloads.assert_not_called()
        self.assertEqual(self.state.read_state(self.root)["status"], "complete")

    def test_root_layout_module_is_included_in_frozen_worker(self):
        self.assertIn("update_btrfs.py", self.backend.ENGINE_FILES)


if __name__ == "__main__":
    unittest.main()
