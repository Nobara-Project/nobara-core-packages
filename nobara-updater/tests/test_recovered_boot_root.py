"""Boot entries after Btrfs recovery, using only disposable files."""
import json
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
from nobara_updater import update_boot as boot
from nobara_updater.update_state import UpdateError

MACHINE = "1" * 32
JOB = "a" * 32
UUID = "12345678-1234-1234-1234-123456789abc"
KERNEL = "7.2.6-201.nobara.fc44.x86_64"
SUBVOL = ".nobara-updater/" + JOB + "/root"


class RecoveredBootRootTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.boot = self.root / "boot"
        self.entries = self.boot / "loader/entries"
        self.entries.mkdir(parents=True)
        self.defaults = self.root / "grub-defaults"
        self.defaults.write_text('GRUB_DEFAULT=saved\nGRUB_TIMEOUT=17\nGRUB_CMDLINE_LINUX="quiet rootflags=subvol=@,subvolid=256,compress=zstd"\n')
        self.machine = self.root / "machine-id"
        self.machine.write_text(MACHINE)
        self.cmdline = self.root / "kernel/cmdline"
        self.cmdline.parent.mkdir()
        self.options = "root=UUID=" + UUID + " ro rootflags=subvol=@,subvolid=256,compress=zstd quiet rd.luks.uuid=encrypted"
        self.cmdline.write_text(self.options + "\n")
        self.canonical = self.entry(MACHINE + "-" + KERNEL)
        self.archive = self.boot / "nobara-updater" / JOB
        self.archive.mkdir(parents=True)
        self.recovery = self.entry("nobara-recovery-" + JOB, recovery=True)
        for name, contents in (("linux-0-vmlinuz-" + KERNEL, b"known-good kernel"),
                               ("initrd-0-initramfs-" + KERNEL + ".img", b"known-good initramfs")):
            (self.archive / name).write_bytes(contents)
        (self.boot / ("vmlinuz-" + KERNEL)).write_bytes(b"possibly failed replacement kernel")
        (self.boot / ("initramfs-" + KERNEL + ".img")).write_bytes(b"possibly failed replacement initramfs")
        self.mount = dict(fstype="btrfs", fsroot="/" + SUBVOL, uuid=UUID)
        self.environment = dict(saved_entry="nobara-recovery-" + JOB, nobara_fallback="nobara-recovery-" + JOB)
        self.commands = []
        for name, value in (("BOOT", self.boot), ("GRUB_DEFAULTS", self.defaults), ("MACHINE_ID", self.machine), ("KERNEL_CMDLINE", self.cmdline)):
            patcher = patch.object(boot, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for patcher in (patch.object(boot, "output", side_effect=self.output), patch.object(boot.os, "sync"),
                        patch.object(boot.os, "uname", return_value=Mock(release=KERNEL))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def output(self, command):
        self.commands.append(command)
        if command[0] == "findmnt":
            return json.dumps({"filesystems": [self.mount]})
        if command[0] == "grub2-editenv" and command[-1] == "list":
            return "\n".join(k + "=" + v for k, v in self.environment.items())
        if command[0] == "grub2-editenv" and command[-2] == "set":
            key, value = command[-1].split("=", 1)
            self.environment[key] = value
            return ""
        raise AssertionError(command)

    def entry(self, identifier, *, recovery=False, options=None):
        path = self.entries / (identifier + ".conf")
        prefix = "/nobara-updater/" + JOB + "/" if recovery else "/"
        linux = prefix + ("linux-0-" if recovery else "") + "vmlinuz-" + KERNEL
        initrd = prefix + ("initrd-0-" if recovery else "") + "initramfs-" + KERNEL + ".img"
        path.write_text(f"title Nobara {'previous system' if recovery else 'Linux'}\nversion {KERNEL}\nlinux {linux}\ninitrd {initrd}\noptions {options or self.options}\n")
        return path

    def test_recovery_becomes_normal_boot_using_saved_images_and_preserves_fallback(self):
        original = self.recovery.read_bytes()
        selected = boot.synchronize_boot_root(recovery_entry=self.recovery.stem)
        self.assertEqual(selected, self.canonical.stem)
        self.assertEqual(boot.kernel_entry(KERNEL), selected)
        self.assertEqual((self.boot / ("vmlinuz-" + KERNEL)).read_bytes(), b"known-good kernel")
        self.assertEqual((self.boot / ("initramfs-" + KERNEL + ".img")).read_bytes(), b"known-good initramfs")
        self.assertEqual(self.recovery.read_bytes(), original)
        for path in (self.canonical, self.cmdline, self.defaults):
            text = path.read_text()
            self.assertIn("subvol=" + SUBVOL, text)
            self.assertNotIn("subvolid=", text)
            self.assertNotIn("subvol=@", text)
            self.assertIn("compress=zstd", text)
        self.assertIn("rd.luks.uuid=encrypted", self.cmdline.read_text())
        self.assertEqual(self.environment["nobara_fallback"], self.recovery.stem)
        self.assertEqual(self.environment["saved_entry"], self.recovery.stem)

    def test_subsequent_update_rewrites_new_entries_but_keeps_new_boot_payloads(self):
        newer = self.entry(MACHINE + "-new-kernel")
        image = (self.boot / ("vmlinuz-" + KERNEL)).read_bytes()
        selected = boot.synchronize_boot_root()
        self.assertEqual(selected, self.canonical.stem)
        self.assertIn("subvol=" + SUBVOL, newer.read_text())
        self.assertEqual((self.boot / ("vmlinuz-" + KERNEL)).read_bytes(), image)
        before = self.canonical.stat().st_mtime_ns
        boot.synchronize_boot_root()
        self.assertEqual(self.canonical.stat().st_mtime_ns, before)

    def test_only_current_installation_entries_are_retargeted(self):
        other_machine = self.entry("2" * 32 + "-" + KERNEL)
        foreign_root = self.entry(MACHINE + "-foreign", options="root=UUID=other ro rootflags=subvol=@")
        originals = {path: path.read_bytes() for path in (other_machine, foreign_root, self.recovery)}
        boot.synchronize_boot_root()
        for path, contents in originals.items():
            self.assertEqual(path.read_bytes(), contents)

    def test_kernelopts_expansion_updates_persistent_options_without_disarming_fallback(self):
        self.environment["kernelopts"] = self.options
        self.canonical.write_text(self.canonical.read_text().replace(self.options, "$kernelopts"))
        boot.synchronize_boot_root()
        self.assertIn("subvol=" + SUBVOL, self.environment["kernelopts"])
        self.assertNotIn("$kernelopts", self.canonical.read_text())
        self.assertIn("nobara_fallback", self.environment)

    def test_tuned_params_are_kept_for_grub_to_expand(self):
        # TuneD's kernel-install hook appends $tuned_params to normal entries.
        self.canonical.write_text(self.canonical.read_text().replace(self.options, self.options + " $tuned_params"))
        self.cmdline.write_text(self.options + " $tuned_params\n")
        boot.synchronize_boot_root()
        for options in (self.canonical.read_text().split("\noptions ")[1].split("\n")[0], self.cmdline.read_text().strip()):
            self.assertTrue(options.endswith(" $tuned_params"), options)
            self.assertEqual(options.count("$tuned_params"), 1)
            self.assertIn("subvol=" + SUBVOL, options)

    def test_recovered_normal_entry_gets_tuned_params_back_instead_of_snapshot_values(self):
        # The recovery entry holds TuneD's values resolved at snapshot time and a
        # copied TuneD initrd; the archive records the original options line.
        overlay = "/nobara-updater/" + JOB + "/initrd-1-tuned-initrd.img"
        (self.archive / "initrd-1-tuned-initrd.img").write_bytes(b"tuned overlay")
        text = self.recovery.read_text()
        text = text.replace(".img\n", ".img " + overlay + "\n").replace(self.options, self.options + " nobara_e2e=1")
        self.recovery.write_text(text)
        (self.archive / "bls-source.json").write_text(json.dumps({"options": self.options + " $tuned_params"}))
        boot.synchronize_boot_root(recovery_entry="nobara-recovery-" + JOB)
        canonical = self.canonical.read_text()
        options = canonical.split("\noptions ")[1].split("\n")[0]
        self.assertTrue(options.endswith(" $tuned_params"), options)
        self.assertNotIn("nobara_e2e=1", options)
        self.assertIn("subvol=" + SUBVOL, options)
        # The copied TuneD initrd keeps booting even if TuneD's own variable is stale.
        self.assertIn("initrd /initramfs-" + KERNEL + ".img " + overlay + "\n", canonical)

    def test_invalid_archived_options_record_is_refused(self):
        (self.archive / "bls-source.json").write_text(json.dumps({"options": ["not", "text"]}))
        before = self.canonical.read_text()
        with self.assertRaises(UpdateError):
            boot.synchronize_boot_root(recovery_entry="nobara-recovery-" + JOB)
        self.assertEqual(self.canonical.read_text(), before)

    def test_missing_recovery_payload_stops_before_changing_any_boot_files(self):
        (self.archive / ("initrd-0-initramfs-" + KERNEL + ".img")).unlink()
        original = self.canonical.read_bytes(), self.cmdline.read_bytes()
        with self.assertRaisesRegex(UpdateError, "image is missing"):
            boot.synchronize_boot_root(recovery_entry=self.recovery.stem)
        self.assertEqual((self.canonical.read_bytes(), self.cmdline.read_bytes()), original)

    def test_other_filesystems_and_unmanaged_subvolumes_are_unchanged(self):
        for fstype, fsroot in (("ext4", "/"), ("xfs", "/"), ("btrfs", "/@")):
            self.mount.update(fstype=fstype, fsroot=fsroot)
            with self.subTest(fstype=fstype, fsroot=fsroot):
                self.assertIsNone(boot.synchronize_boot_root())
                self.assertIn("subvol=@", self.canonical.read_text())

    def test_missing_canonical_entry_can_be_recreated_from_running_recovery(self):
        self.canonical.unlink()
        self.cmdline.unlink()
        self.assertEqual(boot.synchronize_boot_root(recovery_entry=self.recovery.stem), self.canonical.stem)
        self.assertTrue(self.canonical.is_file())
        self.assertIn("subvol=" + SUBVOL, self.cmdline.read_text())


if __name__ == "__main__":
    unittest.main()
