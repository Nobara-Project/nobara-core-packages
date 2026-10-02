"""Opt-in real ext4/XFS + plain/LUKS2 LVM creation, snapshot and merge.

Run with sudo unshare --mount --propagation private and
NOBARA_LVM_INTEGRATION=1. Only disposable loop devices are writable. LVM scans
are restricted to the test PV. Recovery script paths are redirected into /tmp;
its LVM operations run unchanged. These are not complete GRUB boot tests.
"""
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
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
if "nobara_updater" not in sys.modules:
    package = types.ModuleType("nobara_updater")
    package.__path__ = [str(PROJECT / "src")]
    sys.modules["nobara_updater"] = package
from nobara_updater import update_lvm as backend, update_recovery


class Storage:
    def __init__(self, values): self.values = values
    def value(self, key): return self.values.get(key)
    def insert(self, key, value): self.values[key] = value


def run(args, **kwargs):
    try:
        result = subprocess.run(args, check=True, capture_output=True, text=True, **kwargs)
    except subprocess.CalledProcessError as error:
        raise RuntimeError(f"{args}:\n{error.stdout}\n{error.stderr}") from error
    return result.stdout.strip()


@unittest.skipUnless(os.environ.get("NOBARA_LVM_INTEGRATION") == "1" and os.geteuid() == 0,
                     "Requires explicit LVM integration opt-in and root in a private mount namespace")
class LvmIntegrationTests(unittest.TestCase):
    def test_plain_ext4(self): self.exercise("ext4", False)
    def test_encrypted_ext4(self): self.exercise("ext4", True)
    def test_plain_xfs(self): self.exercise("xfs", False)
    def test_encrypted_xfs(self): self.exercise("xfs", True)

    def exercise(self, filesystem, encrypted):
        self.assertNotEqual(os.readlink("/proc/self/ns/mnt"), os.readlink("/proc/1/ns/mnt"))
        with tempfile.TemporaryDirectory(prefix="nobara-lvm-test-", dir="/tmp") as tmp:
            directory = Path(tmp)
            image = directory / "disk.img"
            with image.open("wb") as stream: stream.truncate(44 * 1024**3)
            loop = run(["losetup", "--find", "--show", str(image)])
            mapper = "nobara_test_" + directory.name.rsplit("-", 1)[-1]
            vg = None
            mounts = []
            original_env = os.environ.get("LVM_SYSTEM_DIR")
            conf = directory / "lvm"
            conf.mkdir()
            os.environ["LVM_SYSTEM_DIR"] = str(conf)
            pv = "/dev/mapper/" + mapper if encrypted else loop
            (conf / "lvm.conf").write_text('devices { use_devicesfile=0 filter=["a|^' + pv + '$|", "r|.*|"] }\n'
                                           'activation { monitoring=0 }\n')
            try:
                if encrypted:
                    key = directory / "key"
                    key.write_text("disposable-test-key")
                    run(["cryptsetup", "luksFormat", "--batch-mode", "--type", "luks2", "--pbkdf", "pbkdf2", "--iter-time", "10", "--key-file", str(key), loop])
                    run(["cryptsetup", "open", "--allow-discards", "--key-file", str(key), loop, mapper])
                run(["lvm", "pvcreate", "--yes", pv])
                root = dict(device=loop, mountPoint="/", fs="lvm2 pv", claimed=True)
                if encrypted:
                    root.update(luksMapperName=mapper, luksUuid=run(["cryptsetup", "luksUUID", loop]))
                gs = Storage(dict(nobaraLvmFileSystem=filesystem,
                     nobaraLvmPlan=dict(rootBytes=16 * 1024**3, homeBytes=4 * 1024**3,
                                       reserveBytes=backend.snapshot_bytes(16 * 1024**3), swapBytes=512 * 1024**2),
                     partitions=[root], filesystem_use={}))
                fake = types.SimpleNamespace(globalstorage=gs, job=types.SimpleNamespace(configuration={}))
                with patch.dict(sys.modules, {"libcalamares": fake}):
                    if not os.environ.get("CALAMARES_SOURCE"):
                        self.skipTest("Requires CALAMARES_SOURCE with the patched installer")
                    spec = importlib.util.spec_from_file_location("nobaralvm_test", Path(os.environ["CALAMARES_SOURCE"]) / "src/modules/nobaralvm/main.py")
                    installer = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(installer)
                    installer.create()
                vg = gs.value("nobaraLvmCreated")["vg"]
                partitions = gs.value("partitions")
                self.assertEqual(len([p for p in partitions if p["mountPoint"] == "/"]), 1)
                actual_root = next(p for p in partitions if p["mountPoint"] == "/")
                self.assertNotIn("luksMapperName", actual_root)
                self.assertEqual(actual_root["fs"], filesystem)
                if filesystem == "ext4":
                    for name in ("root", "home"):
                        features = run(["tune2fs", "-l", "/dev/" + vg + "/" + name])
                        line = next(line for line in features.splitlines() if line.startswith("Filesystem features:"))
                        self.assertIn("metadata_csum", line.split())
                self.assertEqual(bool(next(p for p in partitions if p["device"] == loop).get("luksMapperName")), encrypted)
                mounted_root, home = directory / "root", directory / "home"
                for path, name in ((mounted_root, "root"), (home, "home")):
                    path.mkdir()
                    run(["mount", "/dev/" + vg + "/" + name, str(path)])
                    mounts.append(path)
                (mounted_root / "etc").mkdir()
                (mounted_root / "etc/version").write_text("old operating system")
                (home / "document").write_text("before update")
                if os.environ.get("NOBARA_LVM_VM") == "1":
                    from lvm_vm_boot import install_root
                    install_root(mounted_root)
                boot = directory / "boot"
                entry = boot / "loader/entries/normal.conf"
                entry.parent.mkdir(parents=True)
                (boot / "vmlinuz-test").write_text("retained kernel fixture")
                (boot / "initramfs-test.img").write_text("retained initrd fixture")
                entry.write_text("title Nobara\nversion test\nlinux /vmlinuz-test\ninitrd /initramfs-test.img\noptions root=/dev/" + vg + "/root\n")
                job = mounted_root / "jobs" / ("a" * 32)
                job.mkdir(parents=True)
                fstab = mounted_root / "etc/fstab"
                fstab.write_text("/dev/" + vg + "/home /home " + filesystem + " defaults 0 0\n")
                real_output = backend.output
                def output(args):
                    if args[0] == "findmnt":
                        return json.dumps(dict(filesystems=[dict(target="/home", source="/dev/" + vg + "/home")]))
                    return real_output(args)
                real_path = Path
                def path(value):
                    value = str(value)
                    if value == "/etc/fstab": return fstab
                    if value.startswith("/boot"): return boot / value[6:]
                    if value.startswith("/usr/lib/dracut/modules.d/91nobara-rollback"):
                        return PROJECT / "data/dracut/91nobara-rollback/module-setup.sh"
                    return real_path(value)
                with patch.object(backend, "Path", side_effect=path), patch.object(backend, "output", side_effect=output):
                    layout = backend.probe(dict(fstype=filesystem, source="/dev/" + vg + "/root"))
                # The initramfs itself is validated separately; this test runs
                # the actual LV creation and the shipped merge shell program.
                layout.update(entry=str(entry), available=True)
                real_run = subprocess.run
                def command(args, **kwargs):
                    if args[0] in {"dracut", "lsinitrd"}:
                        if args[0] == "dracut": Path(args[-1]).write_text("initrd fixture")
                        return subprocess.CompletedProcess(args, 0)
                    return real_run(args, **kwargs)
                # create() constructs /boot-relative BLS paths; use a private
                # bind mount so those paths and boot payload copying are real.
                run(["mount", "--bind", str(boot), "/boot"])
                mounts.append(Path("/boot"))
                layout["entry"] = "/boot/loader/entries/normal.conf"
                with patch.object(backend.subprocess, "run", side_effect=command), \
                     patch.object(update_recovery, "grub_environment", return_value={}):
                    saved = backend.create(job, layout)
                config = directory / "rollback.conf"
                shutil.copy2(job / "lvm-recovery.conf", config)
                (mounted_root / "etc/version").write_text("broken new operating system")
                (home / "document").write_text("new user data")
                run(["fstrim", "--verbose", str(home)])
                # Some snapshot-origin targets deliberately suppress discards
                # until the snapshot is retired; test and document that case.
                trim = subprocess.run(["fstrim", "--verbose", str(mounted_root)], capture_output=True, text=True)
                if trim.returncode and "not supported" not in trim.stderr:
                    raise RuntimeError(trim.stderr)
                os.sync()
                run(["umount", str(mounted_root)])
                mounts.remove(mounted_root)
                if os.environ.get("NOBARA_LVM_VM") == "1":
                    from lvm_vm_boot import boot as vm_boot
                    run(["umount", str(home)])
                    mounts.remove(home)
                    run(["lvm", "vgchange", "-an", vg])
                    if encrypted:
                        run(["cryptsetup", "close", mapper])
                    vm_boot(directory, image, vg, config, root.get("luksUuid"), directory / "key")
                    if encrypted:
                        run(["cryptsetup", "open", "--allow-discards", "--key-file", str(directory / "key"), loop, mapper])
                    run(["lvm", "vgchange", "-ay", vg])
                    run(["mount", "/dev/" + vg + "/home", str(home)])
                    mounts.append(home)
                else:
                    script = (PROJECT / "data/dracut/91nobara-rollback/rollback.sh").read_text()
                    script = script.replace('. /lib/dracut-lib.sh', 'getarg() { printf "%s" ' + "a" * 32 + '; }')
                    script = script.replace('/etc/nobara-rollback.conf', str(config)).replace('/run/nobara-rollback', str(directory / "completed"))
                    script = script.replace('/sysroot', str(mounted_root))
                    executable = directory / "rollback.sh"
                    executable.write_text(script)
                    run(["bash", "-x", str(executable)], timeout=180)
                    self.assertTrue((directory / "completed/completed").is_file())
                    # Simulate a power interruption after merge but before the
                    # restored root's first boot. Merge intent allows safe retry.
                    run(["bash", str(executable)], timeout=30)
                run(["mount", "/dev/" + vg + "/root", str(mounted_root)])
                mounts.append(mounted_root)
                self.assertEqual((mounted_root / "etc/version").read_text(), "old operating system")
                self.assertEqual((home / "document").read_text(), "new user data")
                self.assertFalse(any(r["lv_name"] == saved["snapshot"] for r in backend.inventory()))
                # A restored system reserves storage again and can trim its
                # root once the old snapshot has merged away.
                with patch.object(update_recovery, "grub_environment", return_value={"saved_entry": saved["restored_entry"]}):
                    backend.finish(saved, "a" * 32, state_root=directory)
                reserve = next(r for r in backend.inventory() if r["lv_name"] == "nobara_reserve")
                self.assertIn(backend.RESERVE_TAG, backend.tags(reserve))
                self.assertEqual(reserve["lv_attr"][1], "r")
                run(["fstrim", "--verbose", str(mounted_root)])
                # A subsequent successful update retires its still-existing
                # snapshot after confirmation, preserving the updated root.
                with patch.object(backend, "Path", side_effect=path), patch.object(backend, "output", side_effect=output):
                    next_layout = backend.probe(dict(fstype=filesystem, source="/dev/" + vg + "/root"))
                next_layout.update(entry="/boot/loader/entries/normal.conf", available=True)
                next_job = mounted_root / "jobs" / ("b" * 32)
                next_job.mkdir(parents=True)
                with patch.object(backend.subprocess, "run", side_effect=command), \
                     patch.object(update_recovery, "grub_environment", return_value={}):
                    next_saved = backend.create(next_job, next_layout)
                (mounted_root / "etc/version").write_text("validated new system")
                with patch.object(update_recovery, "grub_environment", return_value={"saved_entry": "normal"}):
                    backend.finish(next_saved, "b" * 32, state_root=directory)
                self.assertEqual((mounted_root / "etc/version").read_text(), "validated new system")
                self.assertFalse(backend.owned_snapshots(backend.inventory(), next(r for r in backend.inventory() if r["lv_name"] == "root")))
                self.assertFalse((Path("/boot/nobara-updater") / ("a" * 32)).exists())
                self.assertFalse((Path("/boot/nobara-updater") / ("b" * 32)).exists())
                run(["fstrim", "--verbose", str(mounted_root)])
            finally:
                for mount in reversed(mounts): run(["umount", str(mount)])
                if encrypted and not Path(pv).exists():
                    run(["cryptsetup", "open", "--allow-discards", "--key-file", str(directory / "key"), loop, mapper])
                # Query only this isolated inventory, in case create() failed
                # after vgcreate but before returning its GS result.
                for row in json.loads(run(["lvm", "vgs", "--reportformat", "json", "-o", "vg_name"]))["report"][0]["vg"]:
                    name = row["vg_name"].strip()
                    if name.startswith("nobara_"):
                        run(["lvm", "vgremove", "--yes", name])
                if encrypted: run(["cryptsetup", "close", mapper])
                run(["losetup", "--detach", loop])
                if original_env is None: os.environ.pop("LVM_SYSTEM_DIR", None)
                else: os.environ["LVM_SYSTEM_DIR"] = original_env


if __name__ == "__main__": unittest.main()
