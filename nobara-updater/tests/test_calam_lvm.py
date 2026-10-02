"""Installer capacity and target configuration contract tests."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

PROJECT = Path(__file__).resolve().parents[1]
CALAMARES = Path(os.environ.get("CALAMARES_SOURCE", "/nonexistent")) / "src/modules"


class Storage:
    def __init__(self, values): self.values = values
    def value(self, key): return self.values.get(key)
    def insert(self, key, value): self.values[key] = value


@unittest.skipUnless((CALAMARES / "nobaralvm/main.py").is_file(), "Requires CALAMARES_SOURCE with the patched installer")
class InstallerConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "etc/default").mkdir(parents=True)
        (self.root / "etc/default/grub").write_text('GRUB_DEFAULT=0\nGRUB_TIMEOUT=5\n')
        timer = self.root / "usr/lib/systemd/system/fstrim.timer"
        timer.parent.mkdir(parents=True)
        timer.touch()
        self.gs = Storage(dict(rootMountPoint=str(self.root), nobaraGuidedStorage=True))
        self.fake = types.SimpleNamespace(globalstorage=self.gs, job=types.SimpleNamespace(configuration={}),
                                         utils=types.SimpleNamespace(target_env_call=Mock(return_value=0)))
        with patch.dict(sys.modules, {"libcalamares": self.fake}):
            spec = importlib.util.spec_from_file_location("installer_storage_test", CALAMARES / "nobaralvm/main.py")
            self.module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.module)
        self.addCleanup(patch.stopall)
        patch.object(Path, "is_mount", return_value=True).start()
        self.output = patch.object(self.module, "output", return_value="").start()
        patch.object(self.module.os, "sync").start()

    def test_btrfs_guided_install_requires_recovery_without_lvm_configuration(self):
        self.module.configure()
        policy = json.loads((self.root / "etc/nobara-updater/offline.json").read_text())
        self.assertTrue(policy["require_offline_recovery"])
        self.assertNotIn("live_updates", policy)
        self.fake.utils.target_env_call.assert_not_called()
        self.assertIn("GRUB_DEFAULT=saved", (self.root / "etc/default/grub").read_text())
        self.assertIn("GRUB_TIMEOUT=5", (self.root / "etc/default/grub").read_text())

    def test_encrypted_lvm_configures_initramfs_trim_and_password_unlock(self):
        self.gs.insert("nobaraLvmFileSystem", "ext4")
        self.gs.insert("nobaraLvmCreated", dict(vg="nobara_test", pv="/dev/mapper/luks-test", encrypted=True))
        self.gs.insert("partitions", [dict(luksMapperName="luks-test", luksUuid="test-uuid")])
        self.gs.insert("nobaraLvmKernelParams", ["rd.lvm.lv=nobara_test/root", "rd.luks.uuid=test-uuid", "rd.luks.options=test-uuid=discard,x-initrd.attach"])
        (self.root / "etc/crypttab").write_text("# keep this comment\nluks-test UUID=test-uuid none\n")
        self.module.configure()
        self.assertEqual((self.root / "etc/crypttab").read_text(), "# keep this comment\nluks-test UUID=test-uuid none luks,discard,x-initrd.attach\n")
        conf = (self.root / "etc/dracut.conf.d/90-nobara-lvm.conf").read_text()
        self.assertIn('systemd-cryptsetup', conf)
        self.assertIn('rd.lvm.lv=nobara_test/root', conf)
        self.assertIn('rd.luks.options=test-uuid=discard,x-initrd.attach', conf)
        self.output.assert_called_once_with(["systemctl", "--root=" + str(self.root), "enable", "fstrim.timer"])
        self.fake.utils.target_env_call.assert_called_once_with(["lvmdevices", "--adddev", "/dev/mapper/luks-test"])

    def test_incomplete_or_already_executed_plan_cannot_start_formatting(self):
        self.gs.insert("nobaraLvmFileSystem", "ext4")
        self.gs.insert("nobaraLvmCreated", {"vg": "existing"})
        with self.assertRaisesRegex(RuntimeError, "already executed"):
            self.module.create()
        self.output.assert_not_called()
        self.gs.insert("nobaraLvmCreated", None)
        self.gs.insert("nobaraLvmPlan", dict(rootBytes=16 * 1024**3, homeBytes=4 * 1024**3,
                                           reserveBytes=1024**3, swapBytes=0))
        with self.assertRaisesRegex(RuntimeError, "Insufficient"):
            self.module.create()
        self.output.assert_not_called()

    def test_manual_install_does_not_invent_a_volume_plan(self):
        self.gs.insert("nobaraGuidedStorage", False)
        self.module.configure()
        self.assertFalse((self.root / "etc/nobara-updater/offline.json").exists())


@unittest.skipUnless(shutil.which("g++"), "C++ compiler unavailable")
@unittest.skipUnless((CALAMARES / "partition/core/NobaraLvmLayout.h").is_file(), "Requires CALAMARES_SOURCE with the patched installer")
class LayoutSizingTests(unittest.TestCase):
    def test_real_installer_sizing_reserves_full_root_and_refuses_small_disks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            cpp = path / "layout.cpp"
            cpp.write_text('#include "' + str(CALAMARES / "partition/core/NobaraLvmLayout.h") + '''"
#include <cassert>
int main() {
    using namespace NobaraLvm;
    assert(!plan(24*GiB, 0, 11*GiB).valid);
    assert(!plan(37*GiB, 9*GiB, 11*GiB).valid);
    for(auto capacity : {61*GiB, 125*GiB, 1021*GiB}) {
        auto p = plan(capacity, 9*GiB, 11*GiB);
        assert(p.valid);
        assert(p.home >= 4*GiB);
        assert(p.root >= 16*GiB && p.root <= 96*GiB);
        assert(p.reserve > p.root);
        assert(p.root+p.home+p.reserve+p.swap <= capacity-64*MiB);
    }
    assert(plan(1021*GiB, 9*GiB, 11*GiB).root == 96*GiB);
}
''')
            subprocess.run(["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror", str(cpp), "-o", str(path / "layout")], check=True)
            subprocess.run([str(path / "layout")], check=True)


@unittest.skipUnless(os.environ.get("CALAMARES_SOURCE"), "Requires the patched Calamares source tree")
class GeneratedMountConfigurationTests(unittest.TestCase):
    def setUp(self):
        import yaml
        self.source = Path(os.environ["CALAMARES_SOURCE"]) / "src/modules"
        fake = types.SimpleNamespace(globalstorage=Storage(dict(btrfsSubvolumes=[
            dict(mountPoint="/", subvolume="@"), dict(mountPoint="/home", subvolume="@home")])), utils=types.SimpleNamespace(
            gettext_path=lambda: "/nonexistent", gettext_languages=lambda: [], debug=lambda *args: None))
        for name in ("mount", "fstab"):
            with patch.dict(sys.modules, {"libcalamares": fake}):
                spec = importlib.util.spec_from_file_location("calamares_" + name, self.source / name / "main.py")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                setattr(self, name, module)
        self.configuration = yaml.safe_load((self.source / "mount/mount.conf").read_text())

    def test_generated_fstab_uses_exactly_one_trim_strategy_and_relatime(self):
        for fs in ("ext4", "xfs", "btrfs"):
            for ssd in (True, False):
                with self.subTest(filesystem=fs, ssd=ssd), tempfile.TemporaryDirectory() as target:
                    partitions, mount_options = [], []
                    for point in ("/", "/home"):
                        partition = dict(fs=fs, device="/dev/mapper/test-root", mountPoint=point,
                                         uuid="filesystem-uuid", claimed=True)
                        with patch.object(self.mount, "is_ssd_disk", return_value=ssd):
                            options = self.mount.get_mount_options(fs, self.configuration["mountOptions"], partition)
                        mount_options.append(dict(mountpoint=point, option_string=options))
                        if fs != "btrfs" or point == "/":
                            partitions.append(partition)
                    generator = self.fstab.FstabGenerator(partitions, target, mount_options, "", [])
                    generator.generate_fstab()
                    lines = [line.split() for line in (Path(target) / "etc/fstab").read_text().splitlines()
                             if line and not line.startswith("#")]
                    self.assertEqual(len(lines), 2)
                    for fields in lines:
                        options = fields[3].split(",")
                        self.assertIn("relatime", options)
                        self.assertNotIn("noatime", options)
                        self.assertNotIn("discard", options)
                        if fs == "btrfs":
                            self.assertIn("discard=async", options)
                            self.assertIn("X-fstrim.notrim", options)
                            self.assertNotIn("nodiscard", options)
                        else:
                            self.assertIn("nodiscard", options)
                            self.assertNotIn("discard=async", options)
                            self.assertNotIn("X-fstrim.notrim", options)

    def test_new_encrypted_containers_pass_trim_without_unlock_key(self):
        for fs in ("btrfs", "lvm2 pv"):
            with self.subTest(filesystem=fs), tempfile.TemporaryDirectory() as target:
                partition = dict(fs=fs, device="/dev/test", mountPoint="/" if fs == "btrfs" else "",
                                 claimed=True, luksMapperName="luks-test", luksUuid="luks-uuid")
                generator = self.fstab.FstabGenerator([partition], target, [], "", [])
                generator.generate_crypttab()
                fields = (Path(target) / "etc/crypttab").read_text().splitlines()[-1].split()
                self.assertEqual(fields, ["luks-test", "UUID=luks-uuid", "none", "luks,discard,x-initrd.attach"])

    def test_btrfs_swap_mount_also_skips_periodic_trim(self):
        options = self.mount.get_mount_options("btrfs_swap", self.configuration["mountOptions"],
                                               dict(device="/dev/test", mountPoint="/swap"))
        self.assertIn("relatime", options.split(","))
        self.assertIn("discard=async", options.split(","))
        self.assertIn("X-fstrim.notrim", options.split(","))


if __name__ == "__main__": unittest.main()
