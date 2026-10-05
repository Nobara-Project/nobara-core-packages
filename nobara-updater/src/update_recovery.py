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
import subprocess
from pathlib import Path, PurePosixPath

from .update_state import UpdateError, atomic_json

SHARED_CONTAINER_PATHS = {"var/lib/machines", "var/lib/portables"}
BOOT = Path("/boot")
BTRFS_TOP = Path("/run/nobara-updater-btrfs")
LOG = logging.getLogger(__name__)


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
    return dict(line.split("=", 1) for line in output(["grub2-editenv", "-", "list"]).splitlines() if "=" in line)


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


def recovery_entry(entry: Path, job_id: str, subvolume: str) -> str:
    """Copy the entry's boot images into the job archive and return its recovery entry.

    GRUB variables are written resolved, so the entry depends only on the archive.
    """
    destination = BOOT / "nobara-updater" / job_id
    destination.mkdir(parents=True, mode=0o700)
    variables = grub_variables()
    entry_lines = []
    for line in entry.read_text().splitlines():
        key, separator, value = line.partition(" ")
        value = value.strip()
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
            line = "options " + replace_subvolume(" ".join(expand_bls(key, value, variables)), subvolume)
        entry_lines.append(line)
    return "\n".join(entry_lines) + "\n"


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
        saved = parent / "saved"
        restored = mountpoint / relative
        subprocess.run(["btrfs", "subvolume", "snapshot", "-r", "/", str(saved)], check=True)
        subprocess.run(["btrfs", "subvolume", "snapshot", str(saved), str(restored)], check=True)
        fstab = restored / "etc/fstab"
        fstab.write_text(recovery_fstab(fstab.read_text(), relative, layout.get("shared_mounts", [])))
        atomic_json(restored / "etc/nobara-updater-recovery.json", {"job": job_id, "original_subvolume": layout["fsroot"]})
    finally:
        subprocess.run(["umount", str(mountpoint)], check=True)
    text = recovery_entry(Path(layout["entry"]), job_id, relative)
    entry_id = "nobara-recovery-" + job_id
    (Path("/boot/loader/entries") / (entry_id + ".conf")).write_text(text)
    os.sync()
    return dict(layout, entry_id=entry_id, subvolume=relative, created=True)


def select_recovery(recovery: dict) -> None:
    if not recovery.get("created"):
        raise UpdateError("Automatic recovery is not available for this update.")
    subprocess.run(["grub2-set-default", "--boot-directory=/boot", recovery["entry_id"]], check=True)
    subprocess.run(["grub2-editenv", "-", "set", "nobara_fallback=" + recovery["entry_id"]], check=True)
    subprocess.run(["grub2-editenv", "-", "unset", "next_entry", "prev_saved_entry", "tmp_saved_entry", "nobara_trial"], check=True)
    if grub_environment().get("nobara_fallback") != recovery["entry_id"]:
        raise UpdateError("Could not arm the recovery boot entry.")
    atomic_json(Path("/boot/nobara-updater/recovery-armed"), {"entry": recovery["entry_id"]})
    os.sync()


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
            if set(p.name for p in parent.iterdir()) <= {"owner.json", "root-layout.json"} and not archive.exists():
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
        if not re.fullmatch(r"[A-Za-z0-9_.+~-]+", entry) or not (Path("/boot/loader/entries") / (entry + ".conf")).is_file():
            raise UpdateError("The trial boot entry is missing or invalid.")
        subprocess.run(["grub2-editenv", "/boot/grub2/grubenv", "set", "nobara_trial=" + entry], check=True)
        if grub_environment().get("nobara_trial") != entry:
            raise UpdateError("Could not arm the updated system's trial boot.")
    os.sync()


def confirm_trial(entry: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_.+~-]+", entry) or not (Path("/boot/loader/entries") / (entry + ".conf")).is_file():
        raise UpdateError("The confirmed boot entry is unavailable.")
    subprocess.run(["grub2-set-default", "--boot-directory=/boot", entry], check=True)
    subprocess.run(["grub2-editenv", "/boot/grub2/grubenv", "unset", "next_entry", "prev_saved_entry", "tmp_saved_entry", "nobara_trial"], check=True)
    if grub_environment().get("saved_entry") != entry:
        raise UpdateError("Could not persist the confirmed boot entry.")
    subprocess.run(["grub2-editenv", "/boot/grub2/grubenv", "unset", "nobara_fallback"], check=True)
    if grub_environment().get("nobara_fallback"):
        raise UpdateError("Could not retire the automatic fallback after confirmation.")
    Path("/boot/nobara-updater/recovery-armed").unlink(missing_ok=True)
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
