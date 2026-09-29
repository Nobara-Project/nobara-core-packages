"""Recovery mounts preserve nested container data without excluding OS state."""
import json
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
from nobara_updater.update_state import UpdateError

UUID = "11111111-2222-3333-4444-555555555555"
ROOT = dict(fsroot="/@", uuid=UUID)
FSTAB = f"# original mounts\nUUID={UUID} / btrfs subvol=/@,subvolid=256,compress=zstd:1 0 0\nUUID={UUID} /home btrfs subvol=/@home 0 0\n"
MACHINES = "ID 258 gen 292 top level 256 path @/var/lib/machines"


class RecoveryMountTests(unittest.TestCase):
    def mounts(self, listing=MACHINES, fstab=FSTAB, targets=None):
        def output(command):
            self.assertEqual(command[:3], ["findmnt", "--json", "--target"])
            return json.dumps({"filesystems": [{"target": (targets or {}).get(command[3], "/")}]})
        with patch.object(recovery, "output", side_effect=output):
            return recovery.nested_recovery_mounts(ROOT, listing, fstab)

    def test_nested_machines_are_shared_by_stable_id_without_requiring_empty_data(self):
        mounts = self.mounts()
        self.assertEqual(mounts, [dict(target="/var/lib/machines", subvolid=258, uuid=UUID)])
        # Child guest subvolumes are retained through the parent mount.
        nested = "ID 300 gen 1 top level 258 path @/var/lib/machines/guest\n" + MACHINES
        self.assertEqual(self.mounts(nested), mounts)

    def test_portables_are_supported_but_arbitrary_system_subvolumes_are_not(self):
        listing = MACHINES.replace("var/lib/machines", "var/lib/portables")
        self.assertEqual(self.mounts(listing)[0]["target"], "/var/lib/portables")
        for target in ("var/lib/rpm", "var/lib/dkms", "usr", "var/lib/machines-unrelated"):
            with self.subTest(target=target), self.assertRaisesRegex(UpdateError, "outside root snapshot"):
                self.mounts(MACHINES.replace("var/lib/machines", target))

    def test_real_persistent_container_mount_is_retained_without_duplicate_entry(self):
        fstab = FSTAB + f"UUID={UUID} /var/lib/machines btrfs subvolid=258 0 0\n"
        self.assertEqual(self.mounts(fstab=fstab, targets={"/var/lib/machines": "/var/lib/machines"}), [])
        for table, targets in ((fstab, {}), (FSTAB, {"/var/lib/machines": "/var/lib/machines"}),
                               (fstab.replace("subvolid=258", "noauto,subvolid=258"), {"/var/lib/machines": "/var/lib/machines"})):
            with self.subTest(table=table, targets=targets), self.assertRaisesRegex(UpdateError, "persistent independent mount"):
                self.mounts(fstab=table, targets=targets)

    def test_nested_log_subvolume_still_requires_an_actual_persistent_mount(self):
        listing = MACHINES.replace("var/lib/machines", "var/log")
        with self.assertRaisesRegex(UpdateError, "persistent independent mount"):
            self.mounts(listing)
        fstab = FSTAB + f"UUID={UUID} /var/log btrfs subvolid=258 0 0\n"
        self.assertEqual(self.mounts(listing, fstab, {"/var/log": "/var/log"}), [])

    def test_bad_inventory_and_paths_are_rejected(self):
        for listing in ("not a subvolume record", MACHINES.replace("@/var", "@/../var"),
                        MACHINES.replace("ID 258", "ID 5")):
            with self.subTest(listing=listing), self.assertRaises(UpdateError):
                self.mounts(listing)

    def test_recovery_fstab_keeps_existing_mounts_and_adds_container_storage(self):
        result = recovery.recovery_fstab(FSTAB, ".nobara-updater/job/root", self.mounts())
        self.assertIn("subvol=.nobara-updater/job/root", result)
        self.assertNotIn("subvolid=256", result)
        self.assertIn("compress=zstd:1", result)
        self.assertIn(f"UUID={UUID} /home btrfs subvol=/@home 0 0", result)
        self.assertIn(f"UUID={UUID}\t/var/lib/machines\tbtrfs\tdefaults,subvolid=258\t0 0", result)
        self.assertNotIn("/var/lib/machines", FSTAB)

    def test_conflicting_or_invalid_shared_mount_cannot_overwrite_fstab(self):
        for mount in (dict(target="/etc", subvolid=258, uuid=UUID),
                      dict(target="/var/lib/machines", subvolid=5, uuid=UUID),
                      dict(target="/var/lib/machines", subvolid=258, uuid="invalid\ninjected")):
            with self.subTest(mount=mount), self.assertRaises(UpdateError):
                recovery.recovery_fstab(FSTAB, ".nobara-updater/job/root", [mount])
        with self.assertRaises(UpdateError):
            recovery.recovery_fstab(FSTAB + "tmpfs /var/lib/machines tmpfs defaults 0 0\n", "clone", self.mounts())

    @unittest.skipUnless(shutil.which("findmnt"), "findmnt unavailable")
    def test_native_fstab_parser_recognizes_the_recovery_mount(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fstab"
            path.write_text(recovery.recovery_fstab(FSTAB, ".nobara-updater/job/root", self.mounts()))
            result = subprocess.run(["findmnt", "--fstab", "--tab-file", str(path), "--json", "--output", "SOURCE,TARGET,FSTYPE,OPTIONS"],
                                    check=True, capture_output=True, text=True)
        mounts = json.loads(result.stdout)["filesystems"]
        machines = next(mount for mount in mounts if mount["target"] == "/var/lib/machines")
        self.assertEqual(machines["source"], "UUID=" + UUID)
        self.assertEqual(machines["options"], "defaults,subvolid=258")
        self.assertEqual(machines["fstype"], "btrfs")


if __name__ == "__main__":
    unittest.main()
