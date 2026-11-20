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
from nobara_updater.update_retention import (InboundKernel, boot_room_candidates, excess_kernels,
                                             inbound_kernels, kernels_to_retire)
from nobara_updater.update_state import BootSpaceError


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


def inbound(*builds):
    return inbound_kernels([("kernel-eac-core" if eac else "kernel-core",
                             ("kernel-eac-core-0:" if eac else "kernel-core-0:") + kernel(b, eac))
                            for b, eac in builds])


class BootRoomTests(unittest.TestCase):
    def candidates(self, builds, incoming, running):
        cores = {kernel(b, e): Core(kernel(b, e)) for b, e in builds}
        return boot_room_candidates(cores, inbound(*incoming), running=running)

    def test_inbound_nevras_name_their_kernel_versions(self):
        self.assertEqual(set(inbound((204, False), (204, True))), {kernel(204), kernel(204, True)})
        self.assertEqual(inbound_kernels([("kernel-modules", "kernel-modules-0:" + kernel(204))]), {})

    def test_replaced_standard_kernel_goes_before_the_eac_kernel(self):
        builds = [(200, False), (203, False), (203, True)]
        self.assertEqual(self.candidates(builds, [(204, False), (204, True)], kernel(203)),
                         [kernel(200), kernel(203, True)])

    def test_booted_eac_kernel_is_never_retired(self):
        builds = [(201, False), (202, False), (202, True)]
        self.assertEqual(self.candidates(builds, [(203, False), (203, True)], kernel(202, True)), [kernel(201)])

    def test_without_inbound_kernels_nothing_is_replaced(self):
        builds = [(202, False), (203, False), (203, True)]
        self.assertEqual(self.candidates(builds, [], kernel(203)), [])

    def test_only_as_many_kernels_as_the_shortfall_needs_are_retired(self):
        with tempfile.TemporaryDirectory() as directory:
            boot = Path(directory)
            for build in (200, 201):
                (boot / f"vmlinuz-{kernel(build)}").write_bytes(b"x" * 4096)
                (boot / f"initramfs-{kernel(build)}.img").write_bytes(b"x" * 8192)
            names = [kernel(200), kernel(201)]
            self.assertEqual(kernels_to_retire(names, 10000, boot), [kernel(200)])
            self.assertEqual(kernels_to_retire(names, 20000, boot), names)
            self.assertEqual(kernels_to_retire(names, 10 ** 9, boot), [])


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

    def prepare_with_shortfall(self, freed):
        events = []
        error = BootSpaceError("Not enough space on /boot", shortfall=1, incoming=[("kernel-core", "k")])

        def plan(*args, **kwargs):
            events.append("plan")
            if events.count("plan") == 1:
                raise error
            return {"empty": True}

        with tempfile.TemporaryDirectory() as root, \
             patch.object(backend, "maintain_btrfs_root", return_value=True), \
             patch.object(backend, "check_installed_system"), \
             patch.object(backend, "free_boot_space", side_effect=lambda *a: events.append("room") or freed), \
             patch.object(backend, "policy", return_value={"prepare_attempts": 1, "live_updates": True,
                                                           "require_recovery": False,
                                                           "require_offline_recovery": False}), \
             patch("nobara_updater.update_plan.prepare_transaction", side_effect=plan):
            try:
                backend.prepare(Path(root))
            except BootSpaceError:
                events.append("failed")
            return events, state.read_state(Path(root))

    def test_a_boot_shortfall_retires_replaced_kernels_and_plans_again(self):
        events, result = self.prepare_with_shortfall(["7.3.1-201.nobara.fc44.x86_64"])
        self.assertEqual(events, ["plan", "room", "plan"])
        self.assertEqual(result["status"], "unchanged")

    def test_without_room_to_gain_the_shortfall_is_reported(self):
        events, result = self.prepare_with_shortfall([])
        self.assertEqual(events, ["plan", "room", "failed"])
        self.assertEqual(result["status"], "failed")


if __name__ == "__main__":
    unittest.main()
