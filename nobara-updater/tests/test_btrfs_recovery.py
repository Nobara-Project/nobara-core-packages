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
from nobara_updater import update_btrfs as layout


@unittest.skipUnless(os.environ.get("NOBARA_BTRFS_INTEGRATION") == "1" and os.geteuid() == 0,
                     "Requires explicit Btrfs integration opt-in and root in a private mount namespace")
@unittest.skipUnless(all(shutil.which(tool) for tool in ("mkfs.btrfs", "btrfs", "mount", "umount", "blkid", "findmnt")),
                     "Btrfs integration tools unavailable")
class BtrfsRecoveryTests(unittest.TestCase):
    def root_layout_fixture(self, directory, *, interrupt=None, nested=False, device=None):
        def run(command):
            return subprocess.run(command, check=True, capture_output=True, text=True).stdout.strip()

        image = directory / "filesystem.img"
        with image.open("wb") as stream:
            stream.truncate(256 * 1024 * 1024)
        backing = device or image
        mount_prefix = "" if device else "loop,"
        run(["mkfs.btrfs", "-f", str(backing)])
        uuid = run(["blkid", "-s", "UUID", "-o", "value", str(backing)])
        top, active, boot, booted = [directory / name for name in ("top", "active", "boot", "booted")]
        for path in (top, active, boot, booted):
            path.mkdir()
        mounted = []
        vm = os.environ.get("NOBARA_BTRFS_VM") == "1" and interrupt is None and not nested and device is None
        try:
            run(["mount", "-o", mount_prefix + "subvolid=5", str(backing), str(top)])
            mounted.append(top)
            original = top / "@"
            run(["btrfs", "subvolume", "create", str(original)])
            for path in ("etc/default", "etc/kernel", "var/lib/nobara-updater"):
                (original / path).mkdir(parents=True, exist_ok=True)
            (original / "etc/machine-id").write_text("1" * 32 + "\n")
            (original / "system").write_text("abandoned failed installation")
            if nested:
                (original / "home").mkdir()
                run(["btrfs", "subvolume", "create", str(original / "home/data")])
                (original / "home/data/document").write_text("shared user data")
            job = "a" * 32
            parent = top / ".nobara-updater" / job
            parent.mkdir(parents=True)
            (parent / "owner.json").write_text(json.dumps(dict(owner="nobara-updater", job=job)))
            source = parent / "root"
            relative = str(source.relative_to(top))
            run(["btrfs", "subvolume", "snapshot", str(original), str(source)])
            run(["mount", "-o", mount_prefix + "subvol=" + relative, str(backing), str(active)])
            mounted.append(active)
            (active / "system").write_text("custom labwc repaired in recovery")
            state_root = active / "var/lib/nobara-updater"
            plan = state_root / "jobs" / job / "plan.json"
            plan.parent.mkdir(parents=True)
            plan.write_text(json.dumps(dict(recovery=dict(uuid=uuid, fsroot="/@"))))
            fstab = f"UUID={uuid} / btrfs subvol={relative},relatime,discard=async 0 0\n"
            if nested:
                fstab += f"UUID={uuid} /home/data btrfs subvol=@/home/data,relatime 0 0\n"
            (active / "etc/fstab").write_text(fstab)
            options = f"root=UUID={uuid} ro rd.luks.uuid=preserved rootflags=subvol={relative},compress=zstd:1"
            (active / "etc/kernel/cmdline").write_text(options + "\n")
            (active / "etc/default/grub").write_text(f'GRUB_DEFAULT=saved\nGRUB_CMDLINE_LINUX="{options}"\n')
            run(["mount", "-t", "tmpfs", "tmpfs", str(boot)])
            mounted.append(boot)
            (boot / "grub2").mkdir()
            grubenv = boot / "grub2/grubenv"
            run(["grub2-editenv", str(grubenv), "create"])
            run(["grub2-editenv", str(grubenv), "set", "saved_entry=normal", "kernelopts=" + options])
            entries = boot / "loader/entries"
            entries.mkdir(parents=True)
            for name in ("kernel", "initramfs"):
                (boot / name).write_text(name)
            normal = entries / "normal.conf"
            normal.write_text(f"title Nobara\nlinux /kernel\ninitrd /initramfs\noptions {options}\n")
            root_id = layout.subvolume_id(active)
            native_rename = os.rename
            def interrupted(source, target):
                native_rename(source, target)
                if Path(target).name == interrupt:
                    raise OSError("simulated power loss")

            with patch.object(layout, "ROOT", active), patch.object(layout, "BOOT", boot), \
                 patch.object(layout, "TOP", directory / "repair-top"):
                if interrupt:
                    with patch.object(layout.os, "rename", side_effect=interrupted):
                        with self.assertRaisesRegex(OSError, "simulated power loss"):
                            layout.restore_root_layout(state_root, dict(status="complete"))
                    # A fresh mount using the persisted BLS selector must boot
                    # the repaired system at every intermediate rename boundary.
                    self.assertIn("subvolid=" + str(root_id), normal.read_text())
                    run(["mount", "-o", mount_prefix + f"subvolid={root_id}", str(backing), str(booted)])
                    mounted.append(booted)
                    self.assertEqual((booted / "system").read_text(), "custom labwc repaired in recovery")
                    run(["umount", str(booted)])
                    mounted.remove(booted)
                result = layout.restore_root_layout(state_root, dict(status="complete"))
                self.assertEqual(result["root_id"], root_id)
                self.assertEqual(json.loads(run(["findmnt", "--json", "--mountpoint", str(active), "-o", "FSROOT"]))["filesystems"][0]["fsroot"], "/@")
                self.assertEqual(layout.subvolume_id(original), root_id)
                self.assertIsNone(layout.restore_root_layout(state_root, dict(status="complete")))
            run(["mount", "-o", mount_prefix + "subvol=@", str(backing), str(booted)])
            mounted.append(booted)
            self.assertEqual((booted / "system").read_text(), "custom labwc repaired in recovery")
            self.assertIn("subvol=@", (booted / "etc/fstab").read_text())
            if nested:
                shared_id = layout.subvolume_id(parent / "displaced/home/data")
                self.assertIn("subvolid=" + str(shared_id), (booted / "etc/fstab").read_text())
                self.assertEqual((parent / "displaced/home/data/document").read_text(), "shared user data")
                shared_mount = booted / "home/data"
                run(["mount", "-o", mount_prefix + f"subvolid={shared_id}", str(backing), str(shared_mount)])
                mounted.append(shared_mount)
                self.assertEqual((shared_mount / "document").read_text(), "shared user data")

            def cleanup_output(command):
                if command[:4] == ["findmnt", "--json", "--mountpoint", "/"]:
                    command = list(command)
                    command[3] = str(active)
                return run(command)
            with patch.object(recovery, "BOOT", boot), patch.object(recovery, "BTRFS_TOP", directory / "cleanup-top"), \
                 patch.object(recovery, "grub_environment", return_value=dict(saved_entry="normal")), \
                 patch.object(recovery, "output", side_effect=cleanup_output):
                recovery.prune_recovery(dict(created=True, uuid=uuid), job)
            self.assertEqual((active / "system").read_text(), "custom labwc repaired in recovery")
            self.assertEqual((parent / "displaced").exists(), nested)
            if vm:
                from btrfs_vm_boot import install_root
                install_root(active)
        finally:
            for mount in reversed(mounted):
                run(["umount", str(mount)])
        if vm:
            from btrfs_vm_boot import boot as boot_vm
            boot_vm(directory, image, uuid, root_id, relative)

    def test_recovered_root_resumes_original_name_and_retires_displaced_root(self):
        with tempfile.TemporaryDirectory(prefix="nobara-btrfs-layout-", dir="/tmp") as directory:
            self.root_layout_fixture(Path(directory))

    def test_root_rename_keeps_shared_nested_data(self):
        with tempfile.TemporaryDirectory(prefix="nobara-btrfs-layout-", dir="/tmp") as directory:
            self.root_layout_fixture(Path(directory), nested=True)

    @unittest.skipUnless(shutil.which("cryptsetup"), "LUKS test tool unavailable")
    def test_encrypted_btrfs_root_resumes_original_name(self):
        with tempfile.TemporaryDirectory(prefix="nobara-btrfs-luks-", dir="/tmp") as directory:
            directory = Path(directory)
            image, key = directory / "encrypted.img", directory / "test-key"
            with image.open("wb") as stream:
                stream.truncate(320 * 1024 * 1024)
            key.write_bytes(os.urandom(32))
            name = directory.name
            subprocess.run(["cryptsetup", "luksFormat", "--batch-mode", "--type", "luks2", "--pbkdf", "pbkdf2",
                            "--iter-time", "1", "--key-file", str(key), str(image)], check=True, capture_output=True)
            subprocess.run(["cryptsetup", "open", "--key-file", str(key), str(image), name], check=True, capture_output=True)
            try:
                self.root_layout_fixture(directory, device=Path("/dev/mapper") / name)
            finally:
                subprocess.run(["cryptsetup", "close", name], check=True, capture_output=True)

    def test_btrfs_root_rename_resumes_after_either_interrupted_move(self):
        for destination in ("displaced", "@"):
            with self.subTest(destination=destination), \
                 tempfile.TemporaryDirectory(prefix="nobara-btrfs-layout-", dir="/tmp") as directory:
                self.root_layout_fixture(Path(directory), interrupt=destination)

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
