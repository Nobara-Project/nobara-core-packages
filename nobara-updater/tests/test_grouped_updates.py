from __future__ import annotations

import io
import logging
import subprocess
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path


SOURCE_DIR = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SOURCE_DIR))

from grouped_updates import (  # noqa: E402
    FAILURE_MARKER,
    GROUP_ORDER,
    SUCCESS_MARKER,
    PackageGroup,
    ValidationOutcome,
    load_package_groups,
    log_failure_report,
    module_package_kinds,
    parse_package_groups,
    partition_pending_updates,
    run_grouped_updates,
    validate_kernel_modules,
)


@dataclass(frozen=True)
class FakeTransactionOutcome:
    success: bool
    transaction_id: int | None = None
    packages: tuple[str, ...] = ()
    error: str | None = None
    changed: bool = False
    interrupted: bool = False


def package_group_map() -> dict[str, frozenset[str]]:
    return {
        "kernel": frozenset({"kernel"}),
        "graphic stack": frozenset({"mesa-dri-drivers", "dkms-nvidia"}),
        "system core packages": frozenset({"systemd-udev"}),
        "desktop environment": frozenset({"kwin"}),
        "non-essential packages": frozenset({"gamescope"}),
    }


def test_logger() -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    logger = logging.Logger("grouped-update-test")
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return logger, stream


class PackageGroupParsingTests(unittest.TestCase):
    def test_real_inventory_loads_and_arches_match_base_names(self) -> None:
        inventory = Path(__file__).resolve().parents[1] / "data" / "package-groups.txt"
        groups = load_package_groups(inventory)
        self.assertTrue(
            {"zlib", "zlib-ng", "zlib-ng-compat"}.issubset(
                groups["system core packages"]
            )
        )

        partition = partition_pending_updates(
            [
                "totally-unlisted",
                "kwin",
                "systemd-udev",
                "libnvidia-ml",
                "kernel",
            ],
            groups,
        )

        self.assertEqual(tuple(group.key for group in partition), GROUP_ORDER)
        self.assertEqual(partition[0].packages, ("kernel",))
        self.assertEqual(partition[1].packages, ("libnvidia-ml",))
        self.assertEqual(partition[2].packages, ("systemd-udev",))
        self.assertEqual(partition[3].packages, ("kwin",))
        self.assertEqual(partition[4].packages, ("totally-unlisted",))

    def test_missing_or_unknown_sections_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "missing required sections"):
            parse_package_groups(["[kernel]", "kernel"])

        with self.assertRaisesRegex(ValueError, "Unknown package group"):
            parse_package_groups(["[kernal]", "kernel"])

    def test_module_package_patterns(self) -> None:
        self.assertEqual(module_package_kinds(["dkms-nvidia"]), {"dkms"})
        self.assertEqual(module_package_kinds(["akmod-nvidia"]), {"akmod"})
        self.assertEqual(module_package_kinds(["nvidia-kmod-common"]), {"kmod"})
        self.assertEqual(module_package_kinds(["mesa-dri-drivers"]), set())


class GroupedUpdateRunnerTests(unittest.TestCase):
    def test_updates_only_pending_targets_in_priority_order(self) -> None:
        logger, stream = test_logger()
        transaction_calls: list[tuple[str, tuple[str, ...]]] = []
        next_id = 10

        def transaction_runner(packages, group):
            nonlocal next_id
            next_id += 1
            transaction_calls.append((group.key, tuple(packages)))
            return FakeTransactionOutcome(
                True,
                next_id,
                tuple(packages),
                changed=True,
            )

        summary = run_grouped_updates(
            [
                "unknown-package",
                "gamescope",
                "kwin",
                "kernel",
                "systemd-udev",
            ],
            package_group_map(),
            transaction_runner,
            lambda transaction_id, group: FakeTransactionOutcome(True),
            lambda group, packages, after_rollback: ValidationOutcome(True),
            logger,
        )

        self.assertTrue(summary.success)
        self.assertEqual(
            transaction_calls,
            [
                ("kernel", ("kernel",)),
                ("system core packages", ("systemd-udev",)),
                ("desktop environment", ("kwin",)),
                (
                    "non-essential packages",
                    ("gamescope", "unknown-package"),
                ),
            ],
        )
        self.assertNotIn("mesa-dri-drivers", str(transaction_calls))
        self.assertEqual(stream.getvalue().count(SUCCESS_MARKER), 5)

    def test_validation_failure_reverts_exact_transaction_and_stops(self) -> None:
        logger, stream = test_logger()
        transaction_calls: list[str] = []
        rollback_calls: list[tuple[int, str]] = []
        validation_calls: list[tuple[str, bool]] = []

        def transaction_runner(packages, group):
            transaction_calls.append(group.key)
            return FakeTransactionOutcome(
                True,
                41 if group.key == "kernel" else 42,
                tuple(packages),
                changed=True,
            )

        def rollback_runner(transaction_id, group):
            rollback_calls.append((transaction_id, group.key))
            return FakeTransactionOutcome(True, 99, changed=True)

        def validator(group, packages, after_rollback):
            validation_calls.append((group.key, after_rollback))
            if group.key == "kernel" and not after_rollback:
                return ValidationOutcome(False, "dkms failed with exit code 10")
            return ValidationOutcome(True)

        summary = run_grouped_updates(
            ["kernel", "kwin"],
            package_group_map(),
            transaction_runner,
            rollback_runner,
            validator,
            logger,
        )
        log_failure_report(summary, logger)

        self.assertFalse(summary.success)
        self.assertEqual(rollback_calls, [(41, "kernel")])
        self.assertEqual(
            validation_calls,
            [("kernel", False), ("kernel", True)],
        )
        self.assertNotIn("desktop environment", transaction_calls)
        self.assertTrue(summary.results[0].rollback_success)
        self.assertTrue(summary.results[0].post_rollback_validation_success)
        self.assertIn("dkms failed", stream.getvalue())
        self.assertIn(FAILURE_MARKER, stream.getvalue())
        self.assertIn("Package update failure report", stream.getvalue())

    def test_resolve_failure_without_changes_does_not_attempt_rollback(self) -> None:
        logger, _ = test_logger()
        rollback_calls: list[int] = []
        transaction_calls: list[str] = []

        def transaction_runner(packages, group):
            transaction_calls.append(group.key)
            if group.key == "system core packages":
                return FakeTransactionOutcome(False, error="Package conflicts")
            return FakeTransactionOutcome(True, 8, tuple(packages), changed=True)

        summary = run_grouped_updates(
            ["systemd-udev", "kwin"],
            package_group_map(),
            transaction_runner,
            lambda transaction_id, group: rollback_calls.append(transaction_id),
            lambda group, packages, after_rollback: ValidationOutcome(True),
            logger,
        )

        core_result = summary.results[2]
        self.assertFalse(core_result.success)
        self.assertFalse(core_result.rollback_attempted)
        self.assertEqual(rollback_calls, [])
        self.assertNotIn("desktop environment", transaction_calls)

    def test_rollback_failure_stops_later_groups(self) -> None:
        logger, _ = test_logger()
        transaction_calls: list[str] = []

        def transaction_runner(packages, group):
            transaction_calls.append(group.key)
            return FakeTransactionOutcome(
                False,
                55,
                tuple(packages),
                error="RPM transaction failed",
                changed=True,
            )

        summary = run_grouped_updates(
            ["kernel", "kwin", "unknown-package"],
            package_group_map(),
            transaction_runner,
            lambda transaction_id, group: FakeTransactionOutcome(
                False, error="undo unavailable"
            ),
            lambda group, packages, after_rollback: ValidationOutcome(True),
            logger,
        )

        self.assertEqual(transaction_calls, ["kernel"])
        self.assertFalse(summary.results[0].rollback_success)
        self.assertTrue(summary.results[3].skipped)
        self.assertTrue(summary.results[4].skipped)

    def test_dependency_module_package_triggers_validation_in_any_group(self) -> None:
        logger, _ = test_logger()
        validation_calls: list[tuple[str, tuple[str, ...], bool]] = []

        def transaction_runner(packages, group):
            affected = tuple(packages)
            if group.key == "system core packages":
                affected += ("akmod-example",)
            return FakeTransactionOutcome(True, 71, affected, changed=True)

        def validator(group, packages, after_rollback):
            validation_calls.append((group.key, tuple(packages), after_rollback))
            return ValidationOutcome(True)

        summary = run_grouped_updates(
            ["systemd-udev"],
            package_group_map(),
            transaction_runner,
            lambda transaction_id, group: FakeTransactionOutcome(True),
            validator,
            logger,
        )

        self.assertTrue(summary.success)
        self.assertEqual(len(validation_calls), 1)
        self.assertEqual(validation_calls[0][0], "system core packages")
        self.assertIn("akmod-example", validation_calls[0][1])

    def test_interrupted_group_rolls_back_and_skips_later_groups(self) -> None:
        logger, _ = test_logger()
        transaction_calls: list[str] = []
        rollback_calls: list[int] = []

        def transaction_runner(packages, group):
            transaction_calls.append(group.key)
            return FakeTransactionOutcome(
                False,
                81,
                tuple(packages),
                error="DNF transaction was interrupted",
                changed=True,
                interrupted=True,
            )

        def rollback_runner(transaction_id, group):
            rollback_calls.append(transaction_id)
            return FakeTransactionOutcome(True, 82, changed=True)

        summary = run_grouped_updates(
            ["kernel", "kwin"],
            package_group_map(),
            transaction_runner,
            rollback_runner,
            lambda group, packages, after_rollback: ValidationOutcome(True),
            logger,
        )

        self.assertEqual(rollback_calls, [81])
        self.assertEqual(transaction_calls, ["kernel"])
        self.assertTrue(summary.results[0].interrupted)
        self.assertTrue(summary.results[3].skipped)


class ModuleValidationTests(unittest.TestCase):
    def test_kernel_validation_builds_modules_before_dracut(self) -> None:
        logger, _ = test_logger()
        commands: list[tuple[str, ...]] = []

        def command_runner(command, **kwargs):
            commands.append(tuple(command))
            return subprocess.CompletedProcess(command, 0, "ok", "")

        outcome = validate_kernel_modules(
            PackageGroup("kernel", "Kernel", ("kernel",)),
            ("kernel",),
            logger,
            command_finder=lambda command: f"/usr/bin/{command}",
            command_runner=command_runner,
        )

        self.assertTrue(outcome.success)
        self.assertEqual(
            commands,
            [
                ("dkms", "autoinstall"),
                ("akmods", "--force"),
                ("dracut", "-f", "--regenerate-all"),
            ],
        )

    def test_module_build_failure_fails_validation_before_dracut(self) -> None:
        logger, _ = test_logger()
        commands: list[tuple[str, ...]] = []

        def command_runner(command, **kwargs):
            commands.append(tuple(command))
            return subprocess.CompletedProcess(command, 7, "", "build failed")

        outcome = validate_kernel_modules(
            PackageGroup("graphic stack", "Graphic stack", ("dkms-nvidia",)),
            ("dkms-nvidia",),
            logger,
            command_finder=lambda command: f"/usr/bin/{command}",
            command_runner=command_runner,
        )

        self.assertFalse(outcome.success)
        self.assertEqual(commands, [("dkms", "autoinstall")])
        self.assertIn("exit code 7", outcome.error or "")


if __name__ == "__main__":
    unittest.main()
