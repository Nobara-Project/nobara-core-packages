"""Opt-in real Btrfs test, run as root inside a private mount namespace.

All mounts/snapshots/data live in one disposable loopback filesystem in /tmp.
"""
import json
import os
from pathlib import Path
import shutil
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


@unittest.skipUnless(os.environ.get("NOBARA_BTRFS_INTEGRATION") == "1" and os.geteuid() == 0,
                     "Requires explicit Btrfs integration opt-in and root in a private mount namespace")
@unittest.skipUnless(all(shutil.which(tool) for tool in ("mkfs.btrfs", "btrfs", "mount", "umount", "blkid", "findmnt")),
                     "Btrfs integration tools unavailable")
class BtrfsRecoveryTests(unittest.TestCase):
    def test_confirmed_recovery_retires_real_snapshots_and_keeps_mounted_system(self):
        def run(command):
            return subprocess.run(command, check=True, capture_output=True, text=True).stdout.strip()

        with tempfile.TemporaryDirectory(prefix="nobara-btrfs-retirement-", dir="/tmp") as directory:
            directory = Path(directory)
            image = directory / "filesystem.img"
            with image.open("wb") as stream:
                stream.truncate(256 * 1024 * 1024)
            run(["mkfs.btrfs", "-f", str(image)])
            uuid = run(["blkid", "-s", "UUID", "-o", "value", str(image)])
            top, active = directory / "top", directory / "active"
            top.mkdir()
            active.mkdir()
            mounted = []
            try:
                run(["mount", "-o", "loop,subvolid=5", str(image), str(top)])
                mounted.append(top)
                original = top / "@"
                run(["btrfs", "subvolume", "create", str(original)])
                (original / "system").write_text("original system")
                boot = directory / "boot"
                entries = boot / "loader/entries"
                entries.mkdir(parents=True)
                jobs = ["a" * 32, "b" * 32, "c" * 32]
                for job in jobs:
                    parent = top / ".nobara-updater" / job
                    parent.mkdir(parents=True)
                    (parent / "owner.json").write_text(json.dumps(dict(owner="nobara-updater", job=job)))
                    run(["btrfs", "subvolume", "snapshot", "-r", str(original), str(parent / "saved")])
                    run(["btrfs", "subvolume", "snapshot", str(parent / "saved"), str(parent / "root")])
                    archive = boot / "nobara-updater" / job
                    archive.mkdir(parents=True)
                    (archive / "kernel").write_text("old kernel")
                    (entries / ("nobara-recovery-" + job + ".conf")).write_text("title previous system\n")
                relative = ".nobara-updater/" + jobs[0] + "/root"
                run(["mount", "-o", "loop,subvol=" + relative, str(image), str(active)])
                mounted.append(active)
                (active / "system").write_text("reinstalled custom labwc")
                (boot / "kernel").write_text("normal kernel")
                (boot / "initramfs").write_text("normal initramfs")
                (entries / "normal.conf").write_text("title Nobara Linux\nlinux /kernel\ninitrd /initramfs\noptions root=UUID=" + uuid + " rootflags=subvol=" + relative + "\n")

                def output(command):
                    if command[:4] == ["findmnt", "--json", "--mountpoint", "/"]:
                        command = list(command)
                        command[3] = str(active)
                    return run(command)

                with patch.object(recovery, "BOOT", boot), patch.object(recovery, "BTRFS_TOP", directory / "cleanup-top"), \
                     patch.object(recovery, "grub_environment", return_value={"saved_entry": "normal"}), \
                     patch.object(recovery, "output", side_effect=output):
                    recovery.prune_recovery(dict(created=True, uuid=uuid), jobs[1])
                    recovery.prune_recovery(dict(created=True, uuid=uuid), jobs[1])
                self.assertEqual((active / "system").read_text(), "reinstalled custom labwc")
                self.assertEqual((original / "system").read_text(), "original system")
                self.assertFalse(list(entries.glob("nobara-recovery-*.conf")))
                remaining = run(["btrfs", "subvolume", "list", str(top)])
                self.assertIn(relative, remaining)
                self.assertNotIn("/saved", remaining)
                self.assertNotIn(jobs[1], remaining)
                self.assertNotIn(jobs[2], remaining)
                self.assertTrue((entries / "normal.conf").is_file())
            finally:
                for mount in reversed(mounted):
                    run(["umount", str(mount)])

    def test_container_contents_remain_shared_after_snapshot_recovery(self):
        def run(command):
            return subprocess.run(command, check=True, capture_output=True, text=True).stdout.strip()

        with tempfile.TemporaryDirectory(prefix="nobara-btrfs-recovery-", dir="/tmp") as directory:
            directory = Path(directory)
            image = directory / "filesystem.img"
            with image.open("wb") as stream:
                stream.truncate(256 * 1024 * 1024)
            run(["mkfs.btrfs", "-f", str(image)])
            uuid = run(["blkid", "-s", "UUID", "-o", "value", str(image)])
            top = directory / "filesystem"
            top.mkdir()
            mounted = []
            try:
                run(["mount", "-o", "loop,subvolid=5", str(image), str(top)])
                mounted.append(top)
                original = top / "@"
                run(["btrfs", "subvolume", "create", str(original)])
                machines = original / "var/lib/machines"
                machines.parent.mkdir(parents=True)
                run(["btrfs", "subvolume", "create", str(machines)])
                run(["btrfs", "subvolume", "create", str(machines / "guest")])
                (machines / "guest/data").write_text("original container data")
                (original / "etc").mkdir()
                fstab = f"UUID={uuid} / btrfs subvol=/@,compress=zstd:1 0 0\n"
                (original / "etc/fstab").write_text(fstab)
                listing = run(["btrfs", "subvolume", "list", "-o", str(original)])

                def find_mount(command):
                    self.assertEqual(command, ["findmnt", "--json", "--target", "/var/lib/machines", "--output", "TARGET"])
                    actual = json.loads(run(["findmnt", "--json", "--target", str(machines), "--output", "TARGET"]))
                    self.assertEqual(actual["filesystems"][0]["target"], str(top))
                    actual["filesystems"][0]["target"] = "/"
                    return json.dumps(actual)

                with patch.object(recovery, "output", side_effect=find_mount):
                    mounts = recovery.nested_recovery_mounts(dict(fsroot="/@", uuid=uuid), listing, fstab)
                self.assertEqual(len(mounts), 1)
                saved, restored = top / "saved", top / "restored"
                run(["btrfs", "subvolume", "snapshot", "-r", str(original), str(saved)])
                run(["btrfs", "subvolume", "snapshot", str(saved), str(restored)])
                target = restored / "var/lib/machines"
                self.assertFalse((target / "guest/data").exists(), "Root snapshots must actually omit nested contents")
                restored_fstab = recovery.recovery_fstab((restored / "etc/fstab").read_text(), "restored", mounts)
                (restored / "etc/fstab").write_text(restored_fstab)
                self.assertIn(f"defaults,subvolid={mounts[0]['subvolid']}", restored_fstab)
                run(["mount", "-o", f"loop,subvolid={mounts[0]['subvolid']}", str(image), str(target)])
                mounted.append(target)
                self.assertEqual((target / "guest/data").read_text(), "original container data")
                (target / "guest/data").write_text("changed after recovery")
                self.assertEqual((machines / "guest/data").read_text(), "changed after recovery")
                self.assertEqual((original / "etc/fstab").read_text(), fstab)
            finally:
                for mount in reversed(mounted):
                    run(["umount", str(mount)])


if __name__ == "__main__":
    unittest.main()
