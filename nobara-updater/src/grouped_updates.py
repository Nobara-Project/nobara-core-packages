from __future__ import annotations

import logging
import shutil
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


GROUP_ORDER = (
    "kernel",
    "graphic stack",
    "system core packages",
    "desktop environment",
    "non-essential packages",
)

GROUP_LABELS = {
    "kernel": "Kernel",
    "graphic stack": "Graphic stack",
    "system core packages": "System core",
    "desktop environment": "Desktop environment",
    "non-essential packages": "Non-essential packages",
}

SUCCESS_MARKER = "<span foreground='#00FF00'>[OK]</span>"
FAILURE_MARKER = "<span foreground='#FF0000'>[X]</span>"

_RPM_ARCHES = frozenset(
    {
        "aarch64",
        "armv5tel",
        "armv6hl",
        "armv7hl",
        "i386",
        "i486",
        "i586",
        "i686",
        "noarch",
        "ppc",
        "ppc64",
        "ppc64le",
        "riscv64",
        "s390",
        "s390x",
        "src",
        "x86_64",
    }
)


@dataclass(frozen=True)
class PackageGroup:
    key: str
    label: str
    packages: tuple[str, ...]


class TransactionOutcome(Protocol):
    success: bool
    transaction_id: int | None
    packages: tuple[str, ...]
    error: str | None
    changed: bool
    interrupted: bool


@dataclass(frozen=True)
class ValidationOutcome:
    success: bool
    error: str | None = None
    interrupted: bool = False


@dataclass(frozen=True)
class GroupUpdateResult:
    key: str
    label: str
    packages: tuple[str, ...]
    success: bool
    changed: bool = False
    transaction_id: int | None = None
    failure_reason: str | None = None
    rollback_attempted: bool = False
    rollback_success: bool | None = None
    post_rollback_validation_success: bool | None = None
    skipped: bool = False
    interrupted: bool = False


@dataclass(frozen=True)
class GroupedUpdateSummary:
    results: tuple[GroupUpdateResult, ...]

    @property
    def success(self) -> bool:
        return all(result.success for result in self.results)

    @property
    def kernel_or_module_update_applied(self) -> bool:
        return any(
            result.success
            and result.changed
            and (
                result.key == "kernel"
                or requires_module_validation(result.packages)
            )
            for result in self.results
        )


def normalize_package_name(package_spec: str) -> str:
    """Return an RPM name suitable for matching an updatechecker result.

    Package group entries may be arch-qualified (for example, ``foo.i686``),
    while libdnf5's update checker returns only ``foo``.  Only known RPM arch
    suffixes are removed so dots that are part of a package name are retained.
    """

    package_name = package_spec.strip()
    base, separator, suffix = package_name.rpartition(".")
    if separator and suffix in _RPM_ARCHES:
        return base
    return package_name


def is_kernel_package(name: str) -> bool:
    name = normalize_package_name(name)
    return name == "kernel" or name.startswith("kernel-")


def parse_package_groups(lines: Iterable[str]) -> dict[str, frozenset[str]]:
    sections: dict[str, set[str]] = {}
    current_section: str | None = None

    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        if line.startswith("[") and line.endswith("]"):
            section = " ".join(line[1:-1].strip().lower().split())
            if section not in GROUP_ORDER:
                raise ValueError(
                    f"Unknown package group [{line[1:-1]}] on line {line_number}."
                )
            if section in sections:
                raise ValueError(
                    f"Duplicate package group [{line[1:-1]}] on line {line_number}."
                )
            sections[section] = set()
            current_section = section
            continue

        # The inventory contains an explanatory preamble before its first
        # section.  Once a section starts, every non-comment line is a package.
        if current_section is not None:
            package_name = normalize_package_name(line.split("#", 1)[0])
            if package_name:
                sections[current_section].add(package_name)

    missing = [section for section in GROUP_ORDER if section not in sections]
    if missing:
        raise ValueError(
            "Package group file is missing required sections: " + ", ".join(missing)
        )

    return {key: frozenset(value) for key, value in sections.items()}


def load_package_groups(path: Path) -> dict[str, frozenset[str]]:
    with path.open("r", encoding="utf-8") as group_file:
        return parse_package_groups(group_file)


def default_package_groups_path() -> Path:
    candidates = (
        Path("/usr/share/nobara-updater/package-groups.txt"),
        Path(__file__).resolve().parent.parent / "data" / "package-groups.txt",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "Could not find the nobara-updater package-groups.txt data file."
    )


def partition_pending_updates(
    pending_packages: Iterable[str],
    package_groups: Mapping[str, frozenset[str]],
) -> tuple[PackageGroup, ...]:
    """Partition only pending updates, with every unmatched item in residuals."""

    pending_by_name: dict[str, str] = {}
    for package in pending_packages:
        package_name = normalize_package_name(package)
        if package_name:
            pending_by_name.setdefault(package_name, package)

    assigned: set[str] = set()
    result: list[PackageGroup] = []

    for key in GROUP_ORDER[:-1]:
        configured_names = set(package_groups[key])
        if key == "kernel":
            configured_names.update(name for name in pending_by_name if is_kernel_package(name))
        if key == "system core packages":
            configured_names.update(
                name for name in pending_by_name
                if name in {"glibc", "rpm", "dnf5", "libdnf5", "dracut", "systemd", "python3"}
                or name.startswith(("glibc-", "rpm-", "dnf5-", "libdnf5-", "dracut-", "systemd-"))
            )
        names = tuple(
            sorted(
                original
                for normalized, original in pending_by_name.items()
                if normalized in configured_names and normalized not in assigned
            )
        )
        assigned.update(normalize_package_name(name) for name in names)
        result.append(PackageGroup(key, GROUP_LABELS[key], names))

    residuals = tuple(
        sorted(
            original
            for normalized, original in pending_by_name.items()
            if normalized not in assigned
        )
    )
    result.append(
        PackageGroup(
            "non-essential packages",
            GROUP_LABELS["non-essential packages"],
            residuals,
        )
    )
    return tuple(result)


def module_package_kinds(package_names: Iterable[str]) -> frozenset[str]:
    kinds: set[str] = set()
    for package in package_names:
        name = normalize_package_name(package).lower()
        if (
            name.startswith("dkms-")
            or name.endswith("-dkms")
            or "-dkms-" in name
        ):
            kinds.add("dkms")
        if (
            name.startswith("akmod-")
            or name.endswith("-akmod")
            or "-akmod-" in name
        ):
            kinds.add("akmod")
        if (
            name.startswith("kmod-")
            or name.endswith("-kmod")
            or "-kmod-" in name
        ):
            kinds.add("kmod")
    return frozenset(kinds)


def requires_module_validation(package_names: Iterable[str]) -> bool:
    names = tuple(package_names)
    return any(is_kernel_package(name) for name in names) or bool(module_package_kinds(names))


def _log_command_output(
    logger: logging.Logger, command_name: str, stdout: str, stderr: str
) -> None:
    if stdout.strip():
        logger.info("%s output:\n%s", command_name, stdout.rstrip())
    if stderr.strip():
        logger.info("%s diagnostic output:\n%s", command_name, stderr.rstrip())


def validate_kernel_modules(
    group: PackageGroup,
    package_names: Sequence[str],
    logger: logging.Logger,
    *,
    after_rollback: bool = False,
    dracut_enabled: bool = True,
    command_finder: Callable[[str], str | None] = shutil.which,
    command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> ValidationOutcome:
    """Build applicable out-of-tree modules, then regenerate initramfs."""

    kinds = module_package_kinds(package_names)
    commands: list[list[str]] = []
    phase = "post-rollback" if after_rollback else "post-update"

    # A new kernel must be checked against every installed module framework.
    # For other groups, run only the framework implicated by that transaction.
    if group.key == "kernel" or "dkms" in kinds:
        if command_finder("dkms") is not None:
            commands.append(["dkms", "autoinstall"])
        elif "dkms" in kinds:
            return ValidationOutcome(False, "dkms is required but was not found")

    if group.key == "kernel" or "akmod" in kinds:
        if command_finder("akmods") is not None:
            commands.append(["akmods", "--force"])
        elif "akmod" in kinds:
            return ValidationOutcome(False, "akmods is required but was not found")

    if dracut_enabled:
        if command_finder("dracut") is None:
            return ValidationOutcome(False, "dracut is required but was not found")
        commands.append(["dracut", "-f", "--regenerate-all"])
    else:
        logger.info("Skipping dracut because this kernel image type is unsupported.")

    logger.info(
        "Running %s kernel module/initramfs validation for %s...",
        phase,
        group.label,
    )
    for command in commands:
        command_name = command[0]
        logger.info("Running: %s", " ".join(command))
        try:
            result = command_runner(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except KeyboardInterrupt:
            logger.error("%s was interrupted.", command_name)
            return ValidationOutcome(
                False, f"{command_name} was interrupted", interrupted=True
            )
        except OSError as error:
            logger.error("Could not run %s: %s", command_name, error)
            return ValidationOutcome(False, f"{command_name} could not start: {error}")

        stdout = result.stdout or ""
        stderr = result.stderr or ""
        _log_command_output(logger, command_name, stdout, stderr)
        if result.returncode != 0:
            logger.error("%s failed with exit code %s.", command_name, result.returncode)
            return ValidationOutcome(
                False, f"{command_name} failed with exit code {result.returncode}"
            )

    return ValidationOutcome(True)


def _result_status_detail(result: GroupUpdateResult) -> str:
    if result.skipped:
        return "skipped"
    if result.success and not result.changed:
        return "no pending updates"
    if result.success:
        return f"{len(result.packages)} transaction package(s) applied"
    if result.rollback_attempted and result.rollback_success:
        return "failed; package changes rolled back"
    if result.rollback_attempted:
        return "failed; rollback failed"
    return "failed before package changes"


def _log_group_status(logger: logging.Logger, result: GroupUpdateResult) -> None:
    marker = SUCCESS_MARKER if result.success else FAILURE_MARKER
    logger.info("%s %s - %s", marker, result.label, _result_status_detail(result))


def _append_failure_detail(primary: str, detail: str) -> str:
    if detail in primary:
        return primary
    return f"{primary}; {detail}"


def run_grouped_updates(
    pending_packages: Iterable[str],
    package_groups: Mapping[str, frozenset[str]],
    transaction_runner: Callable[[Sequence[str], PackageGroup], TransactionOutcome],
    rollback_runner: Callable[[int, PackageGroup], TransactionOutcome],
    validator: Callable[[PackageGroup, Sequence[str], bool], ValidationOutcome],
    logger: logging.Logger,
) -> GroupedUpdateSummary:
    groups = partition_pending_updates(pending_packages, package_groups)
    results: list[GroupUpdateResult] = []
    halt_reason: str | None = None

    for group in groups:
        logger.info("")
        logger.info("Package update group: %s", group.label)

        if halt_reason is not None:
            result = GroupUpdateResult(
                key=group.key,
                label=group.label,
                packages=group.packages,
                success=False,
                failure_reason=f"Skipped because {halt_reason}",
                skipped=True,
            )
            results.append(result)
            _log_group_status(logger, result)
            continue

        if not group.packages:
            result = GroupUpdateResult(
                key=group.key,
                label=group.label,
                packages=(),
                success=True,
            )
            results.append(result)
            _log_group_status(logger, result)
            continue

        logger.info("Pending targets:\n%s", "\n".join(group.packages))
        transaction = transaction_runner(group.packages, group)
        affected_packages = tuple(
            sorted(set(group.packages).union(transaction.packages))
        )
        validation_required = group.key == "kernel" or requires_module_validation(
            affected_packages
        )
        interrupted = bool(getattr(transaction, "interrupted", False))
        failure_reason = transaction.error or "Package transaction failed"

        if transaction.success and validation_required:
            validation = validator(group, affected_packages, False)
            if validation.success:
                failure_reason = ""
            else:
                failure_reason = validation.error or "Kernel module validation failed"
                interrupted = validation.interrupted
        elif transaction.success:
            failure_reason = ""

        if not failure_reason:
            result = GroupUpdateResult(
                key=group.key,
                label=group.label,
                packages=affected_packages,
                success=True,
                changed=transaction.changed,
                transaction_id=transaction.transaction_id,
            )
            results.append(result)
            _log_group_status(logger, result)
            continue

        logger.error("%s failed: %s", group.label, failure_reason)
        rollback_attempted = False
        rollback_success: bool | None = None
        post_rollback_validation_success: bool | None = None

        if transaction.changed:
            rollback_attempted = True
            if transaction.transaction_id is None:
                rollback_success = False
                failure_reason = _append_failure_detail(
                    failure_reason, "could not identify the DNF transaction to roll back"
                )
            else:
                logger.info(
                    "Reverting DNF transaction %s for %s...",
                    transaction.transaction_id,
                    group.label,
                )
                rollback = rollback_runner(transaction.transaction_id, group)
                rollback_success = rollback.success
                if not rollback.success:
                    failure_reason = _append_failure_detail(
                        failure_reason, rollback.error or "rollback failed"
                    )

        old_state_available = not transaction.changed or rollback_success is True
        if validation_required and old_state_available:
            post_validation = validator(group, affected_packages, True)
            post_rollback_validation_success = post_validation.success
            if not post_validation.success:
                failure_reason = _append_failure_detail(
                    failure_reason,
                    post_validation.error
                    or "post-rollback kernel module validation failed",
                )
            interrupted = interrupted or post_validation.interrupted

        if interrupted:
            halt_reason = f"the {group.label} operation was interrupted"
        elif rollback_success is False:
            halt_reason = f"the {group.label} rollback did not complete"
        elif post_rollback_validation_success is False:
            halt_reason = f"the restored state after {group.label} did not validate"
        elif group.key != "non-essential packages":
            halt_reason = f"the {group.label} operation failed"

        result = GroupUpdateResult(
            key=group.key,
            label=group.label,
            packages=affected_packages,
            success=False,
            changed=transaction.changed,
            transaction_id=transaction.transaction_id,
            failure_reason=failure_reason,
            rollback_attempted=rollback_attempted,
            rollback_success=rollback_success,
            post_rollback_validation_success=post_rollback_validation_success,
            interrupted=interrupted,
        )
        results.append(result)
        _log_group_status(logger, result)

    return GroupedUpdateSummary(tuple(results))


def log_failure_report(summary: GroupedUpdateSummary, logger: logging.Logger) -> None:
    failures = [result for result in summary.results if not result.success]
    if not failures:
        logger.info("")
        logger.info("All package update groups completed successfully.")
        return

    logger.error("")
    logger.error("Package update failure report:")
    for result in failures:
        logger.error("  - %s: %s", result.label, result.failure_reason)
