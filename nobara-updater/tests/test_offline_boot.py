"""Exercise the installed systemd generator inside a disposable chroot.

Run with unshare --user --map-root-user; no host triggers or services change.
"""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

GENERATOR = Path("/usr/lib/systemd/system-generators/systemd-system-update-generator")


@unittest.skipUnless(os.geteuid() == 0 and GENERATOR.is_file(),
                     "Requires systemd and a root user namespace for the disposable chroot")
class OfflineBootGeneratorTests(unittest.TestCase):
    def test_only_a_real_trigger_routes_boot_into_offline_target(self):
        with tempfile.TemporaryDirectory(prefix="nobara-boot-generator-") as directory:
            root = Path(directory)
            paths = {str(GENERATOR)}
            dependencies = subprocess.run(["ldd", str(GENERATOR)], capture_output=True, text=True, check=True).stdout
            paths.update(re.findall(r"(/[^\s()]+)", dependencies))
            for filename in paths:
                destination = root / filename.lstrip("/")
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(filename, destination)
            (root / "proc").mkdir()
            (root / "proc/cmdline").write_text("")
            state = root / "var/lib/nobara-updater"
            state.mkdir(parents=True)
            (state / "state.json").write_text('{"status":"scheduled"}')
            normal, early, late = (root / name for name in ("normal", "early", "late"))
            for path in (normal, early, late):
                path.mkdir()
            command = ["chroot", str(root), str(GENERATOR), "/normal", "/early", "/late"]
            def generate():
                result = subprocess.run(command, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
            generate()
            self.assertFalse((early / "default.target").is_symlink())
            (root / "system-update").symlink_to("/var/lib/nobara-updater")
            generate()
            self.assertEqual((early / "default.target").readlink().name, "system-update.target")
            # Ordinary boots resume when the updater consumes its trigger.
            (root / "system-update").unlink()
            (early / "default.target").unlink()
            generate()
            self.assertFalse((early / "default.target").is_symlink())


if __name__ == "__main__":
    unittest.main()
