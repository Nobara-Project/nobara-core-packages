"""Recovery for Btrfs or ext4/XFS on LVM, separate /boot and GRUB/BLS.

Unsupported layouts are reported, never guessed. Btrfs snapshots live outside
the active root subvolume. Boot payloads are copied, so kernel cleanup cannot erase
the recovery entry's images. Other layouts retain offline staging protection.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

from .update_state import UpdateError, atomic_json
from .update_boot import write_boot_file

SHARED_CONTAINER_PATHS = {"var/lib/machines", "var/lib/portables"}
BOOT = Path("/boot")
BTRFS_TOP = Path("/run/nobara-updater-btrfs")
ACTIVE_SWAPS = Path("/proc/swaps")
FSTAB = Path("/etc/fstab")
LOG = logging.getLogger(__name__)


def check_snapshot_swapfiles(layout: dict) -> None:
    """Refuse root snapshots containing active or configured Btrfs swapfiles.

    Do not turn this into an unavailable-recovery result: an otherwise supported
    system must report the repair instead of silently losing rollback coverage.
    No swap is disabled and neither files nor their configuration are changed.
    """
    if not layout.get("available") or layout.get("backend") == "lvm":
        return

    def unescape(value: str) -> str:
        # procfs and fstab escape whitespace/backslashes as octal, not shell
        # quotes. Decode once so a literal backslash followed by digits survives.
        return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)

    candidates = {}
    inspecting = "the swap configuration"
    try:
        lines = ACTIVE_SWAPS.read_text().splitlines()
        if not lines or lines[0].split() != ["Filename", "Type", "Size", "Used", "Priority"]:
            raise ValueError("Unrecognized /proc/swaps header")
        for line in lines[1:]:
            fields = line.split()
            if len(fields) != 5 or fields[1] not in {"file", "partition"}:
                raise ValueError("Unrecognized /proc/swaps entry")
            if fields[1] == "file":
                candidates[unescape(fields[0])] = True
        for line in FSTAB.read_text().splitlines():
            fields = line.split()
            if not fields or fields[0].startswith("#"):
                continue
            if len(fields) >= 4 and fields[2] == "swap" and fields[0].startswith("/"):
                # Include noauto entries too: snapshotting an inactive swapfile
                # can prevent the user from activating it again afterwards.
                candidates.setdefault(unescape(fields[0]), False)
        if not candidates:
            return
        root_id = None
        blockers = []
        for name, active in sorted(candidates.items()):
            inspecting = f"swapfile {name!r}"
            path = Path(name)
            if not path.is_absolute():
                raise ValueError("Swapfile path is not absolute")
            try:
                mode = path.stat().st_mode
            except FileNotFoundError:
                if active:
                    raise
                # A missing inactive fstab target cannot be snapshotted.
                continue
            if not stat.S_ISREG(mode):
                if active:
                    raise ValueError("Active swapfile is not a regular file")
                continue
            mount = json.loads(output(["findmnt", "--json", "--target", name,
                                       "--output", "FSTYPE,UUID"]))["filesystems"][0]
            if mount["fstype"] != "btrfs":
                continue
            if not mount.get("uuid"):
                raise ValueError("Cannot identify swapfile filesystem UUID")
            if mount["uuid"].lower() != layout["uuid"].lower():
                continue
            if root_id is None:
                root_id = int(output(["btrfs", "inspect-internal", "rootid", "/"]))
                if root_id < 5:
                    raise ValueError("Invalid root subvolume ID")
            # Mount paths alone cannot distinguish an unmounted nested subvolume
            # or a bind mount alias of a file still belonging to the root.
            subvol_id = int(output(["btrfs", "inspect-internal", "rootid", name]))
            if subvol_id < 5:
                raise ValueError("Invalid swapfile subvolume ID")
            if subvol_id == root_id:
                blockers.append(f"{name!r} ({'active' if active else 'configured in /etc/fstab'})")
    except (OSError, subprocess.CalledProcessError, ValueError, KeyError, IndexError, TypeError) as error:
        raise UpdateError(f"Cannot verify Btrfs snapshot safety while checking {inspecting}: {error}. "
                          "Review the swap configuration before retrying the update.") from error
    if blockers:
        raise UpdateError(
            "Btrfs recovery snapshot blocked by swapfile(s) in the root subvolume:\n  "
            + "\n  ".join(blockers)
            + "\nAn active swapfile prevents snapshot creation; snapshotting an inactive configured "
              "swapfile can prevent its later activation. Create a replacement in a separate, persistently "
              "mounted Btrfs subvolume (for example @swap at /swap, with /swap/swapfile), then update "
              "/etc/fstab and any custom swap unit or hibernation resume settings. Ensure enough RAM "
              "or replacement swap is available before disabling the old swapfile; remove it only after "
              "the replacement works. A directory named /swap alone is not sufficient. "
              "See RECOVERY.md for relocation guidance, then run the updater again. "
              "The updater has not disabled or modified swap.")


def nested_recovery_mounts(root: dict, listing: str, fstab: str) -> list[dict]:
    """Keep container data reachable from a root snapshot via explicit mounts.

    Btrfs snapshots omit nested subvolume contents. Container/portable images
    are shared data, not host OS state; mount their original subvolume by ID
    in the recovery clone, including any guest subvolumes below that parent.
    """
    prefix = root["fsroot"].lstrip("/") + "/"
    nested = []
    for line in listing.splitlines():
        match = re.fullmatch(r"ID (\d+) .* path (.+)", line)
        if not match:
            raise UpdateError("Cannot interpret the nested Btrfs subvolume inventory.")
        relative = match[2].removeprefix(prefix)
        path = PurePosixPath(relative)
        if path.is_absolute() or ".." in path.parts or str(path) != relative or int(match[1]) <= 5:
            raise UpdateError("Invalid nested Btrfs subvolume path or ID.")
        nested.append((relative, int(match[1])))
    entries = [line.split() for line in fstab.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    mounts = []
    covered = []
    for relative, subvolid in sorted(nested, key=lambda item: (len(PurePosixPath(item[0]).parts), item[0])):
        if any(relative.startswith(parent + "/") for parent in covered):
            continue
        target = "/" + relative
        configured = [fields for fields in entries if len(fields) >= 4 and fields[1] == target]
        mount = json.loads(output(["findmnt", "--json", "--target", target, "--output", "TARGET"]))["filesystems"][0]
        if relative in SHARED_CONTAINER_PATHS and mount["target"] == "/" and not configured:
            mounts.append(dict(target=target, subvolid=subvolid, uuid=root["uuid"]))
            covered.append(relative)
            continue
        if not (relative in SHARED_CONTAINER_PATHS or any(relative == p or relative.startswith(p + "/")
                                                        for p in ("home", "var/log", "var/cache", "tmp", ".snapshots"))):
            raise UpdateError(f"Nested subvolume {relative} is outside root snapshot coverage.")
        if mount["target"] != target or len(configured) != 1 or "noauto" in configured[0][3].split(","):
            raise UpdateError(f"Nested subvolume {relative} needs a persistent independent mount for recovery.")
        covered.append(relative)
    return mounts


def recovery_fstab(text: str, subvolume: str, shared_mounts: list[dict]) -> str:
    """Change only the recovery clone's mounts; never edit the running root."""
    lines = []
    targets = set()
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 4 and not line.lstrip().startswith("#"):
            targets.add(fields[1])
            if fields[1] == "/":
                flags = [flag for flag in fields[3].split(",") if not flag.startswith(("subvol=", "subvolid="))]
                fields[3] = ",".join(flags + ["subvol=" + subvolume])
                line = "\t".join(fields)
        lines.append(line)
    for mount in shared_mounts:
        target, subvolid, uuid = mount["target"], mount["subvolid"], mount["uuid"]
        if (target.lstrip("/") not in SHARED_CONTAINER_PATHS or target in targets
                or type(subvolid) is not int or subvolid <= 5
                or not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", uuid or "")):
            raise UpdateError("Cannot safely preserve the container-storage mount in recovery.")
        lines.append(f"UUID={uuid}\t{target}\tbtrfs\tdefaults,subvolid={subvolid}\t0 0")
        targets.add(target)
    return "\n".join(lines) + "\n"

def output(command: list[str]) -> str:
    return subprocess.run(command, check=True, text=True, capture_output=True).stdout.strip()


def grub_environment() -> dict[str, str]:
    return dict(line.split("=", 1) for line in output(["grub2-editenv", str(BOOT / "grub2/grubenv"), "list"]).splitlines() if "=" in line)


# Variables GRUB substitutes into BLS fields that recovery understands. TuneD's
# kernel-install hook appends $tuned_params to options and $tuned_initrd to
# initrd on every entry. Anything else is refused, not guessed.
BLS_VARIABLES = {"options": {"$kernelopts", "$tuned_params"}, "initrd": {"$tuned_initrd"}}
# GRUB lexes options again after substitution; refuse values it would reinterpret.
PLAIN_GRUB_VALUE = re.compile(r"[^$\"'\\`;|&<>{}()\x00-\x1f]*")


def grub_variables(environment: dict[str, str] | None = None, *, boot: Path | None = None) -> dict[str, str]:
    """grubenv, plus TuneD's values exactly as GRUB uses them at boot.

    00_header loads grubenv; TuneD's later 00_tuned block in grub.cfg then sets
    tuned_params and tuned_initrd, so it wins. An unset variable expands to nothing.
    """
    variables = dict(grub_environment() if environment is None else environment)
    config = (BOOT if boot is None else boot) / "grub2/grub.cfg"
    text = config.read_text() if config.exists() else ""
    block = re.search(r"(?ms)^### BEGIN /etc/grub\.d/00_tuned ###$(.*?)^### END /etc/grub\.d/00_tuned ###$", text)
    for name in ("tuned_params", "tuned_initrd"):
        if not re.search(r"(?m)^\s*set\s+" + name + "=", text):
            continue
        values = re.findall(r'(?m)^set ' + name + r'="(.*)"$', block[1]) if block else []
        if (len(re.findall(r"(?m)^\s*set\s+" + name + "=", text)) != 1 or len(values) != 1
                or "load_env" in text[block.end():]):
            raise UpdateError(f"GRUB sets {name} in a way recovery cannot verify.")
        variables[name] = values[0]
    for name in ("tuned_params", "tuned_initrd"):
        if not PLAIN_GRUB_VALUE.fullmatch(variables.get(name, "")):
            raise UpdateError(f"GRUB variable {name} has a value recovery cannot verify.")
    return variables


def expand_bls(key: str, value: str, variables: dict[str, str]) -> list[str]:
    """Words of a BLS field as GRUB boots them, for whole-word known variables only."""
    words = []
    for word in value.split():
        if word in BLS_VARIABLES.get(key, ()):
            word = variables.get(word[1:], "")
        elif "$" in word:
            raise UpdateError(f"Recovery cannot interpret {word} in the BLS {key} line.")
        words.extend(word.split())
    if any("$" in word for word in words):
        raise UpdateError(f"The BLS {key} line expands to an unsupported variable.")
    return words


def boot_files(key: str, value: str, variables: dict[str, str]) -> list[Path]:
    files = []
    for name in expand_bls(key, value, variables):
        source = BOOT / name.lstrip("/")
        if not source.resolve().is_relative_to(BOOT.resolve()) or not source.is_file():
            raise UpdateError(f"Recovery boot image is unavailable: {name}")
        files.append(source)
    return files


def replace_subvolume(options: str, subvolume: str) -> str:
    tokens = shlex.split(options)
    flags = []
    kept = []
    for token in tokens:
        if token.startswith("rootflags="):
            flags.extend(flag for flag in token[10:].split(",") if not flag.startswith(("subvol=", "subvolid=")))
        else:
            kept.append(token)
    flags.append("subvol=" + subvolume)
    kept.append("rootflags=" + ",".join(flags))
    return " ".join(kept)


def boot_payloads(entry: Path) -> list[Path]:
    fields = {}
    for line in entry.read_text().splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            fields.setdefault(parts[0], []).append(parts[1])
    if any(key not in fields for key in ("linux", "initrd", "options")):
        raise UpdateError("Recovery requires a split kernel/initramfs BLS entry with explicit options.")
    variables = grub_variables()
    options = " ".join(expand_bls("options", fields["options"][0], variables))
    if not any(token.startswith("root=") for token in shlex.split(options)):
        raise UpdateError("Recovery BLS options do not identify a usable root device.")
    return [source for key in ("linux", "initrd") for value in fields[key] for source in boot_files(key, value, variables)]


def recovery_entry(entry: Path, job_id: str, subvolume: str, *, recovery: dict) -> str:
    """Copy the entry's boot images into the job archive and return its recovery entry.

    GRUB variables are written resolved, so the entry depends only on the archive.
    """
    destination = BOOT / "nobara-updater" / job_id
    destination.mkdir(parents=True, mode=0o700)
    variables = grub_variables()
    entry_lines = []
    for line in entry.read_text().splitlines():
        fields = line.split(None, 1)
        if len(fields) != 2:
            entry_lines.append(line)
            continue
        key, value = fields
        if key == "title":
            line = "title Nobara — previous system (" + job_id[:8] + ")"
        elif key in {"linux", "initrd"}:
            paths = []
            for index, source in enumerate(boot_files(key, value, variables)):
                target = destination / f"{key}-{index}-{source.name}"
                shutil.copy2(source, target)
                paths.append("/" + str(target.relative_to(BOOT)))
            line = key + " " + " ".join(paths)
        elif key == "options":
            if "$tuned_params" in value.split():
                # A normal entry rebuilt from this one after a rollback must not
                # pin TuneD's current values; synchronize_boot_root() restores this.
                atomic_json(destination / "bls-source.json", {"options": value})
            line = "options " + recovery_options(" ".join(expand_bls(key, value, variables)), recovery)
        entry_lines.append(line)
    return "\n".join(entry_lines) + "\n"


def recovery_archive(recovery: dict) -> Path:
    match = re.fullmatch(r"nobara-recovery-([a-f0-9]{32})", recovery.get("entry_id", ""))
    if not match:
        raise UpdateError("Invalid recovery boot entry identifier.")
    archive = BOOT / "nobara-updater" / match[1]
    if archive.is_symlink() or archive.parent.is_symlink():
        raise UpdateError("The recovery boot archive is a symlink.")
    return archive


def recovery_options(options: str, recovery: dict, *, restored: bool = False) -> str:
    """Reconstruct only selectors whose identity is recorded by this job."""
    job = recovery_archive(recovery).name
    tokens = shlex.split(options)
    tokens = [token for token in tokens if not token.startswith(("root=", "nobara.rollback="))]
    if recovery.get("backend") == "lvm":
        from .update_lvm import name
        origin = name(recovery["vg"]) + "/" + name(recovery["lv"])
        tokens = [token for token in tokens if not token.startswith(("rd.lvm.lv=", "resume="))]
        tokens += ["root=/dev/" + origin, "rd.lvm.lv=" + origin]
        if not restored:
            tokens = [token for token in tokens if token != "noresume"]
            tokens += ["noresume", "nobara.rollback=" + job]
        return " ".join(tokens)
    uuid = recovery.get("uuid", "")
    if not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", uuid):
        raise UpdateError("Invalid recovery filesystem UUID.")
    subvolume = ".nobara-updater/" + job + "/root"
    if recovery.get("subvolume", subvolume) != subvolume:
        raise UpdateError("The recovery subvolume does not match its job.")
    return replace_subvolume(" ".join(tokens + ["root=UUID=" + uuid]), subvolume)


def validate_recovery_entry(text: str, recovery: dict, *, restored: bool = False) -> None:
    """A fallback must use this job's saved images and exact root selectors."""
    fields = {}
    for line in text.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            fields.setdefault(parts[0], []).append(parts[1])
    if any(len(fields.get(key, [])) != 1 for key in ("linux", "options", "version")) or not fields.get("initrd"):
        raise UpdateError("The recovery boot entry is incomplete or ambiguous.")
    options = fields["options"][0]
    if "$" in options:
        raise UpdateError("Recovery root options must not depend on GRUB variables.")
    # Comparing parsed tokens permits harmless spacing/order differences, but
    # detects duplicate roots, stale subvolume IDs and removed LVM merge flags.
    if sorted(shlex.split(options)) != sorted(shlex.split(recovery_options(options, recovery, restored=restored))):
        raise UpdateError("The recovery boot entry no longer selects its saved system.")
    archive = recovery_archive(recovery)
    for key in ("linux", "initrd"):
        for value in fields[key]:
            if "$" in value:
                raise UpdateError("Recovery boot images must not depend on GRUB variables.")
            paths = boot_files(key, value, {})
            if not paths or (key == "linux" and len(paths) != 1):
                raise UpdateError("The recovery boot entry is missing saved boot images.")
            if any(not p.resolve().is_relative_to(archive.resolve()) for p in paths):
                raise UpdateError("The recovery boot entry references images outside its own archive.")


def save_recovery_entry(recovery: dict, text: str, *, restored_text: str | None = None) -> None:
    """Keep trusted copies outside the BLS directory rewritten by RPM hooks."""
    archive = recovery_archive(recovery)
    validate_recovery_entry(text, recovery)
    if restored_text is not None:
        validate_recovery_entry(restored_text, recovery, restored=True)
        write_boot_file(archive / "restored-entry.conf", text=restored_text)
        write_boot_file(BOOT / "loader/entries" / ("nobara-restored-" + archive.name + ".conf"), text=restored_text)
    write_boot_file(archive / "recovery-entry.conf", text=text)
    write_boot_file(BOOT / "loader/entries" / (recovery["entry_id"] + ".conf"), text=text)


def restore_recovery_entry(recovery: dict, *, verify_images: bool = False) -> None:
    archive = recovery_archive(recovery)
    identifiers = [(recovery["entry_id"], "recovery-entry.conf", False)]
    if recovery.get("backend") == "lvm":
        identifiers.append(("nobara-restored-" + archive.name, "restored-entry.conf", True))
    replacements = []
    for identifier, filename, restored in identifiers:
        saved = archive / filename
        entry = BOOT / "loader/entries" / (identifier + ".conf")
        if saved.is_symlink() or entry.is_symlink():
            raise UpdateError("The recovery boot entry is a symlink.")
        if saved.exists():
            text = saved.read_text()
        else:
            # Older jobs have no private copy. Their saved image paths and
            # recorded filesystem/LV identify the target without guessing '@'.
            text = entry.read_text()
            options = re.findall(r"(?m)^options\s+(.+)$", text)
            if len(options) != 1:
                raise UpdateError("Cannot repair an ambiguous legacy recovery entry.")
            variables = grub_variables()
            desired = recovery_options(" ".join(expand_bls("options", options[0], variables)),
                                       recovery, restored=restored)
            text = re.sub(r"(?m)^options\s+.+$", lambda _: "options " + desired, text)
            for key in ("linux", "initrd"):
                text = re.sub(r"(?m)^" + key + r"\s+(.+)$",
                              lambda match: key + " " + " ".join(expand_bls(key, match[1], variables)), text)
        validate_recovery_entry(text, recovery, restored=restored)
        if verify_images:
            for line in text.splitlines():
                fields = line.split(None, 1)
                if len(fields) != 2 or fields[0] not in {"linux", "initrd"}:
                    continue
                for image in boot_files(fields[0], fields[1], {}):
                    if not image.stat().st_size:
                        raise UpdateError(f"The archived recovery boot image is empty: {image.name}")
                    if fields[0] == "initrd":
                        subprocess.run(["lsinitrd", str(image)], check=True, timeout=60,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        replacements.append((saved, entry, text))
    for saved, entry, text in replacements:
        if not saved.exists():
            write_boot_file(saved, text=text)
        if not entry.exists() or entry.read_text() != text:
            LOG.warning("Restoring recovery boot entry %s after its contents changed.", entry.name)
            write_boot_file(entry, text=text)


def recovery_reboot_attempted(recovery: dict) -> bool:
    attempt = recovery_archive(recovery) / "reboot-attempted"
    return attempt.exists() or attempt.is_symlink()


def validate_saved_recovery(recovery: dict, job: str) -> None:
    """Check the existing rollback target without creating or merging snapshots."""
    archive = recovery_archive(recovery)
    if archive.name != job or not recovery.get("created"):
        raise UpdateError("The saved recovery does not belong to the interrupted update.")
    if not BOOT.is_mount() or BOOT.stat().st_dev == Path("/").stat().st_dev:
        raise UpdateError("Mount the separate /boot filesystem before retrying recovery.")
    current = json.loads(output(["findmnt", "--json", "--mountpoint", "/",
                                 "--output", "FSTYPE,UUID,FSROOT,SOURCE"]))["filesystems"][0]
    if current.get("uuid") != recovery.get("uuid") or not current.get("uuid"):
        raise UpdateError("The running root filesystem differs from this update's recorded root.")
    if Path("/etc/nobara-updater-recovery.json").exists():
        raise UpdateError("This system already has a recovery marker. Resolve startup confirmation before retrying recovery.")
    if recovery.get("backend") == "lvm":
        from .update_lvm import validate_saved_snapshot
        validate_saved_snapshot(recovery, job, current)
        return
    if recovery.get("backend") not in {None, "btrfs"} or current.get("fstype") != "btrfs":
        raise UpdateError("Unsupported saved recovery filesystem.")
    if current.get("fsroot") != recovery.get("fsroot"):
        raise UpdateError("The running Btrfs subvolume differs from this update's original root.")
    # Validate the recorded UUID and exact job subvolume before mounting it.
    recovery_options("", recovery)
    top = Path(tempfile.mkdtemp(prefix="nobara-recovery-check-", dir="/run"))
    mounted = False
    try:
        subprocess.run(["mount", "-t", "btrfs", "-o", "ro,subvolid=5",
                        "UUID=" + recovery["uuid"], str(top)], check=True)
        mounted = True
        parent = top / ".nobara-updater" / job
        snapshot = parent / "root"
        marker = snapshot / "etc/nobara-updater-recovery.json"
        paths = [parent.parent, parent, parent / "owner.json", parent / "origin.json",
                 snapshot, snapshot / "etc", marker]
        if any(path.is_symlink() for path in paths):
            raise UpdateError("The saved recovery snapshot contains a symlink in its identity paths.")
        owner = json.loads((parent / "owner.json").read_text())
        origin = json.loads((parent / "origin.json").read_text())
        saved_marker = json.loads(marker.read_text())
        if (owner != dict(owner="nobara-updater", job=job)
                or origin != dict(version=1, job=job, uuid=recovery["uuid"], fsroot=recovery["fsroot"])
                or saved_marker != dict(job=job, original_subvolume=recovery["fsroot"])):
            raise UpdateError("The saved recovery snapshot identity or marker does not match this update.")
        # An ordinary directory at this path is not a bootable snapshot.
        subprocess.run(["btrfs", "subvolume", "show", str(snapshot)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if output(["btrfs", "property", "get", "-ts", str(snapshot), "ro"]) != "ro=false":
            raise UpdateError("The saved recovery root is not writable.")
    finally:
        if mounted:
            subprocess.run(["umount", str(top)], check=True)
        # Never recursively clean up a mountpoint, including on umount failure.
        top.rmdir()


def reserve_manual_recovery_attempt(recovery: dict) -> None:
    """A user-requested restart must not re-enable automatic restart loops."""
    attempt = recovery_archive(recovery) / "reboot-attempted"
    if attempt.is_symlink() or (attempt.exists() and not attempt.is_dir()):
        raise UpdateError("The recovery restart record is invalid; preserve it for support.")
    attempt.mkdir(mode=0o700, exist_ok=True)
    os.sync()


def probe_recovery() -> dict:
    try:
        root = json.loads(output(["findmnt", "--json", "--mountpoint", "/", "--output", "FSTYPE,FSROOT,UUID,SOURCE"]))["filesystems"][0]
        lvm = root["fstype"] in {"ext4", "xfs"}
        if not lvm and (root["fstype"] != "btrfs" or root["fsroot"] == "/"):
            return {"available": False, "reason": "Automatic recovery needs a Btrfs root subvolume or an ext4/XFS root on LVM."}
        if not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", root.get("uuid") or ""):
            return {"available": False, "reason": "Cannot identify the root filesystem UUID for recovery."}
        if not Path("/boot").is_mount() or Path("/boot").stat().st_dev == Path("/").stat().st_dev:
            return {"available": False, "reason": "Automatic recovery currently needs a separate /boot filesystem."}
        grub = Path("/etc/default/grub").read_text()
        if not re.search(r"(?m)^GRUB_DEFAULT=['\"]?saved['\"]?\s*$", grub):
            return {"available": False, "reason": "Automatic recovery needs GRUB_DEFAULT=saved."}
        if "blscfg" not in Path("/boot/grub2/grub.cfg").read_text():
            return {"available": False, "reason": "Automatic recovery needs GRUB with BLS entries."}
        for program in (("lvm", "dracut", "lsinitrd") if lvm else ("btrfs",)) + ("mount", "umount", "grub2-set-default", "grub2-editenv"):
            if shutil.which(program) is None:
                return {"available": False, "reason": f"Recovery tool {program} is missing."}
        # External system-state mounts/subvolumes cannot be recovered by a
        # root snapshot. User data/log/cache mounts are deliberately excluded.
        for path in ("/usr", "/usr/local", "/etc", "/opt", "/var", "/var/spool", "/var/lib", "/var/lib/dkms", "/var/lib/rpm", "/usr/lib/sysimage", "/usr/lib/modules"):
            if Path(path).exists():
                mount = json.loads(output(["findmnt", "--json", "--target", path, "--output", "TARGET"]))["filesystems"][0]
                if mount["target"] != "/":
                    return {"available": False, "reason": f"Separate system-state mount {mount['target']} needs its own recovery backend."}
        extra = {}
        if lvm:
            from .update_lvm import probe
            extra = probe(root)
        shared_mounts = [] if lvm else nested_recovery_mounts(root, output(["btrfs", "subvolume", "list", "-o", "/"]), Path("/etc/fstab").read_text())
        kernel = os.uname().release
        entries = []
        for path in Path("/boot/loader/entries").glob("*.conf"):
            text = path.read_text()
            if re.search(r"(?m)^version\s+" + re.escape(kernel) + r"\s*$", text):
                if path.name.startswith("nobara-recovery-") and (lvm or "subvol=" + root["fsroot"].lstrip("/") not in text):
                    continue
                entries.append(path)
        selected = grub_environment().get("saved_entry", "")
        preferred = [p for p in entries if p.stem == selected]
        if preferred:
            entries = preferred
        if len(entries) != 1:
            return {"available": False, "reason": "Cannot identify one BLS entry for the currently running kernel."}
        boot_payloads(entries[0])
        return dict(available=True, uuid=root["uuid"], fsroot=root["fsroot"], entry=str(entries[0]), saved_entry=selected,
                    shared_mounts=shared_mounts, **extra)
    except (OSError, subprocess.CalledProcessError, ValueError, KeyError, UpdateError) as error:
        return {"available": False, "reason": f"Recovery layout could not be verified: {error}"}


def recovery_boot_space(layout: dict) -> int:
    if not layout.get("available"):
        return 0
    payloads = boot_payloads(Path(layout["entry"]))
    required = sum(p.stat().st_size for p in set(payloads))
    if layout.get("backend") == "lvm":
        # LVM additionally builds a recovery initramfs with merge support.
        required += max((p.stat().st_size for p in payloads), default=128 * 1024**2) + 64 * 1024**2
    return required


def check_recovery_space(layout: dict, boot_space: dict) -> None:
    if not layout.get("available"):
        return
    required = recovery_boot_space(layout) + boot_space.get("/boot", 32 * 1024**2)
    free = shutil.disk_usage("/boot").free
    if free < required:
        raise UpdateError(f"Not enough space on /boot for recovery and this update: need {required // 1024**2} MiB free, have {free // 1024**2} MiB. Free space before retrying; no update has been installed.")


def create_recovery(job: Path, layout: dict, *, boot_space: dict | None = None) -> dict:
    if not layout.get("available"):
        return layout
    if layout.get("backend") != "lvm" and layout.get("fsroot", "").lstrip("/").startswith(".nobara-updater/"):
        raise UpdateError("Restore the normal Btrfs root layout before creating another rollback snapshot. "
                          "The current recovery system remains usable; inspect the updater log for the root-layout repair error.")
    job_id = job.name
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise UpdateError("Invalid recovery job identifier.")
    # Recheck immediately before snapshotting, after all downloads and after
    # removing /system-update to avoid a recovery boot re-running an update.
    actual = probe_recovery()
    if not actual.get("available") or actual["uuid"] != layout["uuid"] or actual["fsroot"] != layout["fsroot"]:
        raise UpdateError("The recovery layout changed after preparation.")
    for key in ("backend", "origin_uuid", "vg_uuid"):
        if actual.get(key) != layout.get(key):
            raise UpdateError("The recovery volume changed after preparation.")
    check_snapshot_swapfiles(actual)
    ensure_recovery_bootloader()
    layout = actual
    check_recovery_space(layout, boot_space or {})
    archive_size = sum(int(output(["du", "-sx", "--block-size=1", str(mount)]).split()[0])
                       for mount in (Path("/boot"), Path("/boot/efi")) if mount.is_mount())
    if shutil.disk_usage(job).free < archive_size + 512 * 1024**2:
        raise UpdateError("There is not enough space for the boot recovery archives.")
    for mount, filename in ((Path("/boot"), "boot.tar"), (Path("/boot/efi"), "efi.tar")):
        if mount.is_mount():
            subprocess.run(["tar", "--one-file-system", "--xattrs", "--acls", "--selinux", "--exclude=./nobara-updater", "-cpf", str(job / filename), "-C", str(mount), "."], check=True)
    if layout.get("backend") == "lvm":
        from .update_lvm import create
        return create(job, layout)
    mountpoint = Path("/run/nobara-updater-btrfs")
    mountpoint.mkdir(mode=0o700, exist_ok=True)
    subprocess.run(["mount", "-t", "btrfs", "-o", "subvolid=5", "UUID=" + layout["uuid"], str(mountpoint)], check=True)
    relative = f".nobara-updater/{job_id}/root"
    try:
        parent = mountpoint / ".nobara-updater" / job_id
        parent.mkdir(parents=True, mode=0o700)
        atomic_json(parent / "owner.json", {"owner": "nobara-updater", "job": job_id})
        # Keep ancestry outside either snapshot and state.json so a later
        # restore or update cannot lose the route back to the normal root.
        atomic_json(parent / "origin.json", dict(version=1, job=job_id,
                                                 uuid=layout["uuid"], fsroot=layout["fsroot"]))
        saved = parent / "saved"
        restored = mountpoint / relative
        # Recheck after archiving /boot, immediately before the first snapshot.
        check_snapshot_swapfiles(layout)
        subprocess.run(["btrfs", "subvolume", "snapshot", "-r", "/", str(saved)], check=True)
        subprocess.run(["btrfs", "subvolume", "snapshot", str(saved), str(restored)], check=True)
        fstab = restored / "etc/fstab"
        fstab.write_text(recovery_fstab(fstab.read_text(), relative, layout.get("shared_mounts", [])))
        atomic_json(restored / "etc/nobara-updater-recovery.json", {"job": job_id, "original_subvolume": layout["fsroot"]})
    finally:
        subprocess.run(["umount", str(mountpoint)], check=True)
    entry_id = "nobara-recovery-" + job_id
    result = dict(layout, entry_id=entry_id, subvolume=relative, created=True)
    text = recovery_entry(Path(layout["entry"]), job_id, relative, recovery=result)
    save_recovery_entry(result, text)
    os.sync()
    return result


def select_recovery(recovery: dict, *, verify_images: bool = False) -> None:
    if not recovery.get("created"):
        raise UpdateError("Automatic recovery is not available for this update.")
    # Package hooks can regenerate grub.cfg and replace every BLS options
    # line. Recheck the integration, then restore saved entries last.
    ensure_recovery_bootloader()
    try:
        restore_recovery_entry(recovery, verify_images=verify_images)
    except Exception:
        # The native fallback must not boot a copy Python just rejected.
        (BOOT / "nobara-updater/recovery-armed").unlink(missing_ok=True)
        os.sync()
        raise
    subprocess.run(["grub2-set-default", "--boot-directory=" + str(BOOT), recovery["entry_id"]], check=True)
    subprocess.run(["grub2-editenv", str(BOOT / "grub2/grubenv"), "set", "nobara_fallback=" + recovery["entry_id"]], check=True)
    subprocess.run(["grub2-editenv", str(BOOT / "grub2/grubenv"), "unset", "next_entry", "prev_saved_entry", "tmp_saved_entry", "nobara_trial"], check=True)
    environment = grub_environment()
    if (environment.get("nobara_fallback") != recovery["entry_id"]
            or environment.get("saved_entry") != recovery["entry_id"]
            or any(environment.get(key) for key in ("next_entry", "prev_saved_entry", "tmp_saved_entry", "nobara_trial"))):
        raise UpdateError("Could not arm the recovery boot entry.")
    atomic_json(BOOT / "nobara-updater/recovery-armed", {"entry": recovery["entry_id"]})
    os.sync()


def prune_boot_archives(state: dict, state_root: Path) -> tuple[int, int]:
    """Remove obsolete job tarballs, never snapshots, boot images or reports.

    Called under the updater lock. Older Btrfs cleanup retired the snapshots
    and /boot job directories but left these separate tarballs behind.
    Remaining boot data is treated as a reference, even for an older job.
    """
    from .update_state import TRIGGER
    from .update_btrfs import JOURNAL
    if state.get("status") not in {"complete", "recovered", "live-complete", "unchanged"}:
        raise UpdateError("Finish the pending update or recovery before cleaning update storage.")
    blockers = (TRIGGER, Path("/etc/system-update"), Path("/run/nobara-updater-offline"),
                Path("/etc/nobara-updater-recovery.json"), state_root / JOURNAL,
                BOOT / "nobara-updater/recovery-armed")
    if any(p.exists() or p.is_symlink() for p in blockers):
        raise UpdateError("Update or recovery work is still pending; keeping update storage.")
    jobs = state_root / "jobs"
    if state_root.is_symlink() or jobs.is_symlink():
        raise UpdateError("Refusing cleanup through a symlinked update directory.")
    candidates = []
    for job in jobs.glob("*"):
        if (not re.fullmatch(r"[a-f0-9]{32}", job.name) or job.is_symlink()
                or not job.is_dir() or job.is_mount()):
            continue
        for name in ("boot.tar", "efi.tar"):
            archive = job / name
            if archive.is_file() and not archive.is_symlink() and not archive.is_mount():
                candidates.append(archive)
    if not candidates:
        return 0, 0
    entries = BOOT / "loader/entries"
    boot_jobs = BOOT / "nobara-updater"
    if (not BOOT.is_mount() or BOOT.is_symlink() or boot_jobs.is_symlink()
            or (BOOT / "loader").is_symlink() or entries.is_symlink() or not entries.is_dir()):
        raise UpdateError("Mount the separate /boot filesystem with its normal boot entries before cleaning boot archives.")
    environment = grub_environment()
    selected = environment.get("saved_entry", "")
    if (any(environment.get(key) for key in ("nobara_fallback", "nobara_trial", "next_entry"))
            or not re.fullmatch(r"[A-Za-z0-9_.+~-]+", selected)
            or selected.startswith("nobara-recovery-")
            or not (entries / (selected + ".conf")).is_file()):
        raise UpdateError("A confirmed normal boot must be selected before cleaning boot archives.")
    removed_bytes = kept = 0
    for archive in candidates:
        job_id = archive.parent.name
        references = (boot_jobs / job_id, entries / ("nobara-recovery-" + job_id + ".conf"),
                      entries / ("nobara-restored-" + job_id + ".conf"))
        if (any(p.exists() or p.is_symlink() for p in references)
                or (job_id == state.get("job") and state.get("recovery", {}).get("created")
                    and not state.get("recovery_cleanup_complete"))):
            kept += 1
            continue
        size = archive.stat().st_size
        archive.unlink()
        removed_bytes += size
    if removed_bytes:
        os.sync()
    return removed_bytes, kept


def prune_recovery(recovery: dict, confirmed_job: str, *, state_root=None) -> None:
    """Retire rollback data after confirmation, preserving roots/images in use."""
    if not recovery.get("created"):
        return
    if recovery.get("backend") == "lvm":
        from .update_lvm import finish
        finish(recovery, confirmed_job, state_root=state_root)
        return
    current = json.loads(output(["findmnt", "--json", "--mountpoint", "/", "--output", "FSTYPE,UUID,FSROOT"]))["filesystems"][0]
    if current["fstype"] != "btrfs" or current["uuid"] != recovery["uuid"]:
        raise UpdateError("Cannot verify the confirmed Btrfs root for recovery cleanup.")
    environment = grub_variables()
    if any(environment.get(key) for key in ("nobara_fallback", "nobara_trial", "next_entry")):
        raise UpdateError("Cannot remove recovery snapshots while a boot trial is armed.")
    selected = environment.get("saved_entry", "")
    if not re.fullmatch(r"[A-Za-z0-9_.+~-]+", selected) or selected.startswith("nobara-recovery-"):
        raise UpdateError("A normal boot entry must be selected before recovery cleanup.")
    entries = BOOT / "loader/entries"
    selected_path = entries / (selected + ".conf")
    if not selected_path.is_file():
        raise UpdateError("The confirmed normal boot entry is missing.")
    selected_text = selected_path.read_text()
    if (len(re.findall(r"(?m)^options\s+.+$", selected_text)) != 1
            or len(re.findall(r"(?m)^linux\s+\S+\s*$", selected_text)) != 1
            or not re.search(r"(?m)^initrd\s+\S+", selected_text)):
        raise UpdateError("The confirmed normal boot entry is incomplete.")
    mounts = json.loads(output(["findmnt", "--json", "--list", "--output", "UUID,FSROOT"]))["filesystems"]
    protected = {current["fsroot"]}
    protected.update(m["fsroot"] for m in mounts if m.get("uuid") == current["uuid"] and m.get("fsroot"))
    mountpoint = BTRFS_TOP
    mountpoint.mkdir(mode=0o700, exist_ok=True)
    subprocess.run(["mount", "-t", "btrfs", "-o", "subvolid=5", "UUID=" + recovery["uuid"], str(mountpoint)], check=True)
    try:
        parents = []
        managed = mountpoint / ".nobara-updater"
        if not managed.exists():
            return
        if managed.is_symlink():
            raise UpdateError("Recovery snapshot directory is a symlink.")
        for parent in managed.iterdir():
            identifier = parent.name
            if not re.fullmatch(r"[a-f0-9]{32}", identifier) or parent.is_symlink() or not parent.is_dir():
                continue
            marker = parent / "owner.json"
            if marker.is_symlink() or not marker.is_file() or json.loads(marker.read_text()) != {"owner": "nobara-updater", "job": identifier}:
                continue
            parents.append(parent)
        retired_entries = {entries / ("nobara-recovery-" + p.name + ".conf") for p in parents}
        referenced_images = set()
        for entry in entries.glob("*.conf"):
            if entry in retired_entries:
                continue
            for line in entry.read_text().splitlines():
                fields = line.split(None, 1)
                if len(fields) != 2:
                    continue
                key, value = fields
                if key in {"linux", "initrd", "efi"}:
                    # Unknown variables still stop cleanup; it is retried later.
                    images = {(BOOT / name.lstrip("/")).resolve() for name in expand_bls(key, value, environment)}
                    if entry.stem == selected and any(not image.is_file() for image in images):
                        raise UpdateError("The confirmed normal boot entry has missing boot images.")
                    referenced_images.update(images)
                elif key == "options":
                    tokens = shlex.split(value.replace("$kernelopts", environment.get("kernelopts", "")))
                    if entry.stem == selected and current["fsroot"].startswith("/.nobara-updater/"):
                        flags = [flag for token in tokens if token.startswith("rootflags=") for flag in token[10:].split(",")]
                        subvolumes = ["/" + flag[7:].strip("/") for flag in flags if flag.startswith("subvol=")]
                        if "root=UUID=" + current["uuid"] not in tokens or subvolumes != [current["fsroot"]]:
                            raise UpdateError("The normal boot entry does not point to the active recovered root.")
                    if "root=UUID=" + current["uuid"] not in tokens:
                        continue
                    for token in tokens:
                        if token.startswith("rootflags="):
                            for flag in token[10:].split(","):
                                if flag.startswith("subvolid="):
                                    identifier = flag[9:]
                                    if not identifier.isdecimal() or int(identifier) <= 5:
                                        raise UpdateError("A boot entry has an invalid recovery subvolume ID.")
                                    relative = output(["btrfs", "inspect-internal", "subvolid-resolve", identifier, str(mountpoint)])
                                    if not relative or ".." in PurePosixPath(relative).parts:
                                        raise UpdateError("Cannot resolve a boot entry's recovery subvolume ID.")
                                    protected.add("/" + relative.strip("/"))
                                if flag.startswith("subvol="):
                                    protected.add("/" + flag[7:].strip("/"))
        # Persist menu removal before deleting any backing snapshots. Retain
        # ownership markers until cleanup finishes so a later boot can retry.
        for entry in retired_entries:
            entry.unlink(missing_ok=True)
        os.sync()
        pending = False
        for parent in parents:
            for name in ("root", "saved", "displaced"):
                path = parent / name
                relative = "/" + str(path.relative_to(mountpoint))
                if any(p == relative or p.startswith(relative + "/") for p in protected):
                    # A displaced original root may still contain shared
                    # container/home data or belong to an independent boot
                    # entry. Retaining that storage is expected, not failed
                    # rollback-snapshot cleanup.
                    pending |= name != "displaced" and relative != current["fsroot"] and path.exists()
                    continue
                if path.exists() and not path.is_symlink():
                    if name == "displaced":
                        # This was the old original root before the recovered
                        # system resumed its name. Never delete shared nested
                        # data, even when it is temporarily unmounted.
                        from .update_btrfs import subvolume_id
                        record = parent / "root-layout.json"
                        if record.is_symlink() or not record.is_file():
                            continue
                        layout = json.loads(record.read_text())
                        if (layout.get("uuid") != current["uuid"]
                                or layout.get("displaced") != relative.lstrip("/")
                                or layout.get("displaced_id") != subvolume_id(path)):
                            raise UpdateError("Cannot verify the displaced root for cleanup.")
                        if layout["displaced_id"] in layout.get("retained_ids", []):
                            LOG.info("Keeping displaced root storage referenced by the recovered system's mount configuration: %s", relative)
                            continue
                        if output(["btrfs", "subvolume", "list", "-o", str(path)]):
                            LOG.info("Keeping displaced root storage containing nested subvolumes: %s", relative)
                            continue
                    subprocess.run(["btrfs", "subvolume", "delete", str(path)], check=True)
            archive = BOOT / "nobara-updater" / parent.name
            if not any(p.is_relative_to(archive.resolve()) for p in referenced_images):
                if archive.exists() and not archive.is_symlink():
                    shutil.rmtree(archive)
            # A promoted recovery root is the live OS, not a rollback copy.
            # Keep its ownership record so future cleanups can identify it.
            if set(p.name for p in parent.iterdir()) <= {"owner.json", "origin.json", "root-layout.json"} and not archive.exists():
                (parent / "origin.json").unlink(missing_ok=True)
                (parent / "root-layout.json").unlink(missing_ok=True)
                (parent / "owner.json").unlink()
                parent.rmdir()
        os.sync()
        if pending:
            raise UpdateError("Some rollback subvolumes are still mounted or referenced by another boot entry; cleanup will retry.")
    finally:
        subprocess.run(["umount", str(mountpoint)], check=True)


def trial_boot(recovery: dict, entry: str | None = None) -> None:
    """Persist recovery as default before writes; allow one updated boot."""
    if not recovery.get("created"):
        return
    select_recovery(recovery)
    if entry:
        if not re.fullmatch(r"[A-Za-z0-9_.+~-]+", entry) or not (BOOT / "loader/entries" / (entry + ".conf")).is_file():
            raise UpdateError("The trial boot entry is missing or invalid.")
        subprocess.run(["grub2-editenv", str(BOOT / "grub2/grubenv"), "set", "nobara_trial=" + entry, "next_entry=" + entry], check=True)
        environment = grub_environment()
        if environment.get("nobara_trial") != entry or environment.get("next_entry") != entry:
            raise UpdateError("Could not arm the updated system's trial boot.")
        LOG.info("Armed updated boot entry %s; fallback is %s.", entry, recovery["entry_id"])
    os.sync()


def confirm_trial(entry: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_.+~-]+", entry) or not (BOOT / "loader/entries" / (entry + ".conf")).is_file():
        raise UpdateError("The confirmed boot entry is unavailable.")
    subprocess.run(["grub2-set-default", "--boot-directory=" + str(BOOT), entry], check=True)
    subprocess.run(["grub2-editenv", str(BOOT / "grub2/grubenv"), "unset", "next_entry", "prev_saved_entry", "tmp_saved_entry", "nobara_trial"], check=True)
    if grub_environment().get("saved_entry") != entry:
        raise UpdateError("Could not persist the confirmed boot entry.")
    subprocess.run(["grub2-editenv", str(BOOT / "grub2/grubenv"), "unset", "nobara_fallback"], check=True)
    if grub_environment().get("nobara_fallback"):
        raise UpdateError("Could not retire the automatic fallback after confirmation.")
    (BOOT / "nobara-updater/recovery-armed").unlink(missing_ok=True)
    os.sync()


def ensure_recovery_bootloader() -> None:
    """Use private GRUB variables that kernel-install cannot overwrite.

    The late fragment overrides ordinary saved/next_entry while an update is
    unconfirmed. Its one-shot trial is consumed by GRUB before loading Linux.
    """
    from .update_boot import ensure_saved_default, atomic_text
    ensure_saved_default()
    config = Path("/boot/grub2/grub.cfg")
    fragment = Path("/etc/grub.d/42_nobara_update")
    if not fragment.is_file():
        raise UpdateError("The Nobara GRUB recovery integration is missing.")
    marker = "# Nobara update fallback v1"
    if marker not in config.read_text():
        import tempfile
        fd, filename = tempfile.mkstemp(prefix=".nobara-grub-", dir=config.parent)
        os.close(fd)
        temporary = Path(filename)
        try:
            output(["grub2-mkconfig", "--no-grubenv-update", "-o", str(temporary)])
            output(["grub2-script-check", str(temporary)])
            text = temporary.read_text()
            if marker not in text:
                raise UpdateError("GRUB configuration did not include the recovery integration.")
            atomic_text(config, text)
        finally:
            temporary.unlink(missing_ok=True)
