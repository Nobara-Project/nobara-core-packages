"""Nobara keeps two standard kernels and the EAC build of the booted kernel."""
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
from nobara_updater import update_backend as backend, update_state as state
from nobara_updater.update_retention import excess_kernels


class Core:
    def __init__(self, uname):
        version, rest = uname.split("-", 1)
        release, self.arch = rest.rsplit(".", 1)
        self.name = "kernel-eac-core" if ".eac." in uname else "kernel-core"
        self.version, self.release = version, release

    def get_name(self): return self.name
    def get_arch(self): return self.arch
    def get_epoch(self): return "0"
    def get_version(self): return self.version
    def get_release(self): return self.release


def kernel(build, eac=False):
    return f"7.3.1-{build}.{'eac.' if eac else ''}nobara.fc44.x86_64"


def excess(builds, running):
    cores = {kernel(b, e): Core(kernel(b, e)) for b, e in builds}
    return excess_kernels(cores, running=running)


class RetentionPolicyTests(unittest.TestCase):
    def test_before_an_update_the_booted_and_previous_kernels_are_kept(self):
        builds = [(201, False), (202, False), (201, True), (202, True)]
        self.assertEqual(excess(builds, kernel(202)), {kernel(201, True)})

    def test_after_booting_the_new_kernel_it_replaces_the_oldest(self):
        builds = [(201, False), (202, False), (203, False), (202, True), (203, True)]
        self.assertEqual(excess(builds, kernel(203)), {kernel(201), kernel(202, True)})

    def test_eac_follows_the_booted_kernel_until_the_new_kernel_boots(self):
        # A newer EAC kernel waiting for its first boot is not yet the one kept.
        builds = [(201, False), (202, False), (203, False), (202, True), (203, True)]
        self.assertEqual(excess(builds, kernel(202)), {kernel(201), kernel(203, True)})

    def test_booted_eac_kernel_is_kept_and_counts_as_the_eac_kernel(self):
        builds = [(201, False), (202, False), (201, True), (202, True)]
        self.assertEqual(excess(builds, kernel(202, True)), {kernel(201, True)})

    def test_without_a_matching_eac_build_the_newest_eac_kernel_is_kept(self):
        builds = [(202, False), (203, False), (200, True), (201, True)]
        self.assertEqual(excess(builds, kernel(203)), {kernel(200, True)})

    def test_an_older_booted_kernel_is_kept_beside_the_two_newest(self):
        builds = [(200, False), (201, False), (202, False), (203, False)]
        self.assertEqual(excess(builds, kernel(200)), {kernel(201)})


class PreparationCleanupTests(unittest.TestCase):
    def setUp(self):
        self.prune = patch.object(backend, "prune_kernel_packages").start()
        patch.object(backend, "grub_environment", return_value={}).start()
        patch.object(Path, "exists", return_value=False).start()
        patch.object(Path, "is_symlink", return_value=False).start()
        self.addCleanup(patch.stopall)

    def test_idle_and_unstarted_failed_preparations_are_cleaned_up_first(self):
        backend.cleanup_kernel_retention({"status": "idle"})
        backend.cleanup_kernel_retention({"status": "failed", "started": False, "job": "a" * 32})
        self.assertEqual(self.prune.call_count, 2)

    def test_failed_installations_are_left_for_repair(self):
        backend.cleanup_kernel_retention({"status": "failed", "started": True, "job": "a" * 32})
        self.prune.assert_not_called()

    def test_prepare_runs_cleanup_before_planning(self):
        events = []
        self.prune.side_effect = lambda **kw: events.append("cleanup")
        with tempfile.TemporaryDirectory() as root, \
             patch.object(backend, "maintain_btrfs_root", return_value=True), \
             patch.object(backend, "check_installed_system"), \
             patch.object(backend, "policy", return_value={"prepare_attempts": 1, "live_updates": True,
                                                           "require_recovery": False,
                                                           "require_offline_recovery": False}), \
             patch("nobara_updater.update_plan.prepare_transaction",
                   side_effect=lambda *a, **kw: events.append("plan") or {"empty": True}):
            backend.prepare(Path(root))
            self.assertEqual(state.read_state(Path(root))["status"], "unchanged")
        self.assertEqual(events, ["cleanup", "plan"])


if __name__ == "__main__":
    unittest.main()
