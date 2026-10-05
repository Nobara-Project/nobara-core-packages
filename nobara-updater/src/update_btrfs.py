"""Restore the original root name after Btrfs recovery, without copying the OS.

All boot and mount references are made independent of names before either
rename. A durable journal allows the same operation to resume after interruption.
Only a confirmed, updater-owned recovery root is eligible.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess

from .update_boot import write_boot_file
from .update_state import UpdateError, atomic_json

LOG = logging.getLogger(__name__)
ROOT = Path("/")
BOOT = Path("/boot")
TOP = Path("/run/nobara-updater-root-layout")
JOURNAL = "btrfs-root-layout.json"
MANAGED = re.compile(r"\.nobara-updater/([a-f0-9]{32})/root")


def output(command):
    return subprocess.run(command, check=True, text=True, capture_output=True,
                          env=dict(os.environ, LC_ALL="C.UTF-8")).stdout.strip()


def safe_relative(value):
    value = value.removeprefix("/")
    if (not value or PurePosixPath(value).is_absolute() or not re.fullmatch(r"[A-Za-z0-9_@.+/-]+", value)
            or any(p in {".", ".."} for p in value.split("/"))
            or str(PurePosixPath(value)) != value):
        raise UpdateError("Cannot safely identify the original Btrfs root path.")
    return value


def checked_path(relative):
    path = TOP / safe_relative(relative)
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise UpdateError("A Btrfs root-layout path is a symlink.")
    return path


def subvolume_id(path):
    # rootid alone also accepts ordinary directories within a subvolume.
    if path.stat().st_ino != 256:
        raise UpdateError(f"Not a Btrfs subvolume root: {path}")
    identifier = int(output(["btrfs", "inspect-internal", "rootid", str(path)]))
    if identifier <= 5:
        raise UpdateError("The Btrfs top level cannot be moved as a recovered root.")
    return identifier


def original_root(state_root, state, source, uuid):
    """Follow saved plans across repeated recoveries; never guess '@'."""
    seen = set()
    current = source
    while match := MANAGED.fullmatch(current):
        job = match[1]
        if job in seen:
            raise UpdateError("The saved Btrfs recovery ancestry contains a cycle.")
        seen.add(job)
        plan = state_root / "jobs" / job / "plan.json"
        if plan.is_file() and not plan.is_symlink():
            recovery = json.loads(plan.read_text())["recovery"]
            if recovery.get("uuid") != uuid:
                raise UpdateError("The saved recovery plan belongs to another filesystem.")
            current = safe_relative(recovery["fsroot"])
        elif state.get("recovery_boot", {}).get("job") == job:
            current = safe_relative(state["recovery_boot"]["original_subvolume"])
        else:
            raise UpdateError("The original Btrfs root path is not recorded; leaving the recovered root in place.")
    if current.startswith(".nobara-updater/"):
        raise UpdateError("The original root path is inside updater recovery storage.")
    return current


def replace_selector(flags, selector):
    kept = [f for f in flags.split(",") if f and not f.startswith(("subvol=", "subvolid="))]
    return ",".join([*kept, selector])


def root_options(options, selector):
    tokens = shlex.split(options)
    flags = ",".join(t[10:] for t in tokens if t.startswith("rootflags="))
    tokens = [t for t in tokens if not t.startswith("rootflags=")]
    return shlex.join([*tokens, "rootflags=" + replace_selector(flags, selector)])


def reference_files(journal, environment):
    """Prepare both reference phases completely before writing any file."""
    uuid, source, target, root_id = (journal[k] for k in ("uuid", "source", "target", "root_id"))
    machine = (ROOT / "etc/machine-id").read_text().strip()
    if not re.fullmatch(r"[a-fA-F0-9]{32}", machine):
        raise UpdateError("Cannot identify this installation's boot entries.")
    saved = environment.get("saved_entry", "")
    if not re.fullmatch(r"[A-Za-z0-9_.+~-]+", saved) or saved.startswith("nobara-recovery-"):
        raise UpdateError("Confirm a normal boot entry before restoring the Btrfs root name.")
    files = []
    journal["entry"] = saved
    journal["entries"] = sorted(p.name for p in (BOOT / "loader/entries").glob("*.conf"))

    def add(path, before, stable, after):
        if path.is_symlink():
            raise UpdateError(f"Refusing to replace a symlink during root-layout repair: {path}")
        files.append(dict(path=str(path), before=before, stable=stable, after=after))

    def identify(flags):
        selectors = [f for f in flags.split(",") if f.startswith(("subvol=", "subvolid="))]
        paths = [safe_relative(s[7:]) for s in selectors if s.startswith("subvol=")]
        ids = [int(s[9:]) for s in selectors if s.startswith("subvolid=")]
        if len(paths) > 1 or len(ids) > 1:
            raise UpdateError("Ambiguous Btrfs subvolume selectors in boot or mount configuration.")
        identifier = subvolume_id(checked_path(paths[0])) if paths else (ids[0] if ids else None)
        if ids and ids[0] != identifier:
            raise UpdateError("Btrfs subvolume path and ID disagree in boot or mount configuration.")
        affected = bool(paths and any(paths[0] == p or paths[0].startswith(p + "/") for p in (source, target)))
        return identifier, affected

    fstab = ROOT / "etc/fstab"
    before = fstab.read_text()
    stable_lines, after_lines = [], []
    retained_ids = set()
    roots = 0
    for line in before.splitlines():
        fields = line.split()
        stable = after = line
        if len(fields) >= 4 and not line.lstrip().startswith("#") and fields[2] == "btrfs":
            device = fields[0]
            device_uuid = device[5:] if device.startswith("UUID=") else None
            if device_uuid is None:
                device_path = device if device.startswith("/dev/") else output(["findfs", device])
                device_uuid = output(["blkid", "-s", "UUID", "-o", "value", device_path])
            if fields[1] == "/":
                roots += 1
                if device_uuid != uuid:
                    raise UpdateError("fstab does not identify the running Btrfs root filesystem.")
            if device_uuid == uuid:
                identifier, affected = identify(fields[3])
                if fields[1] == "/":
                    if identifier != root_id:
                        raise UpdateError("fstab does not identify the running recovered subvolume.")
                    affected = True
                elif identifier is not None:
                    retained_ids.add(identifier)
                if affected:
                    fields[3] = replace_selector(fields[3], f"subvolid={identifier}")
                    stable = "\t".join(fields)
                    if fields[1] == "/":
                        fields[3] = replace_selector(fields[3], "subvol=" + target)
                    after = "\t".join(fields)
        stable_lines.append(stable)
        after_lines.append(after)
    if roots != 1:
        raise UpdateError("Expected one Btrfs root entry in fstab.")
    add(fstab, before, "\n".join(stable_lines) + "\n", "\n".join(after_lines) + "\n")

    def transform(options, *, normal=False):
        expanded = options.replace("$kernelopts", environment.get("kernelopts", ""))
        # Normal entries must keep following the selected TuneD profile after
        # the recovered root is renamed. Append the variable after shlex.join
        # so it remains a GRUB substitution, rather than a quoted literal.
        expanded, tuned = re.subn(r"(?<!\S)\$tuned_params(?!\S)", "", expanded)
        suffix = " $tuned_params" if tuned else ""
        if "$" in expanded:
            raise UpdateError("Unsupported variable in Btrfs boot options.")
        tokens = shlex.split(expanded)
        devices = [t for t in tokens if t.startswith("root=")]
        if devices != ["root=UUID=" + uuid]:
            if normal:
                raise UpdateError("The confirmed boot entry does not identify the recovered filesystem.")
            # A LABEL or /dev alias might identify this same filesystem.
            # Do not leave such an entry following a path that changes owners.
            flags = ",".join(t[10:] for t in tokens if t.startswith("rootflags="))
            paths = [safe_relative(f[7:]) for f in flags.split(",") if f.startswith("subvol=")]
            affected = any(p == prefix or p.startswith(prefix + "/") for p in paths for prefix in (source, target))
            if affected and (len(devices) != 1 or not devices[0].startswith("root=UUID=")):
                raise UpdateError("A boot entry uses an ambiguous root device for a subvolume being moved.")
            return options, options
        flags = ",".join(t[10:] for t in tokens if t.startswith("rootflags="))
        identifier, affected = identify(flags)
        if normal:
            if identifier != root_id:
                raise UpdateError("The confirmed boot entry does not identify the recovered subvolume.")
            affected = True
        if not affected:
            return options, options
        stable = root_options(expanded, f"subvolid={identifier}") + suffix
        after = root_options(expanded, "subvol=" + target) + suffix if identifier == root_id else stable
        return stable, after

    from .update_recovery import expand_bls, grub_variables
    variables = grub_variables(environment, boot=BOOT)
    found = False
    for path in sorted((BOOT / "loader/entries").glob("*.conf")):
        before = path.read_text()
        lines = re.findall(r"(?m)^options\s+(.+)$", before)
        if not lines and path.stem != saved:
            continue  # Entries such as memtest need no root filesystem.
        if len(lines) != 1:
            raise UpdateError(f"Cannot identify boot options in {path.name}.")
        normal = path.stem == saved
        stable, after = transform(lines[0], normal=normal)
        add(path, before, re.sub(r"(?m)^options\s+.+$", lambda _: "options " + stable, before),
            re.sub(r"(?m)^options\s+.+$", lambda _: "options " + after, before))
        if normal:
            found = True
            for kind in ("linux", "initrd"):
                images = re.findall(r"(?m)^" + kind + r"\s+(.+)$", before)
                paths = [(BOOT / p.lstrip("/")) for row in images for p in expand_bls(kind, row, variables)]
                if not paths or any(not p.resolve().is_relative_to(BOOT.resolve()) or not p.is_file() for p in paths):
                    raise UpdateError("The confirmed boot entry has missing boot images.")
    if not found:
        raise UpdateError("The confirmed normal boot entry is missing.")

    cmdline = ROOT / "etc/kernel/cmdline"
    if cmdline.is_file():
        before = cmdline.read_text()
        stable, after = transform(before.strip(), normal=True)
        add(cmdline, before, stable + "\n", after + "\n")
    defaults = ROOT / "etc/default/grub"
    before = defaults.read_text()
    # These defaults may omit root=. They belong to the running installation.
    def defaults_for(selector):
        return re.sub(r"rootflags=([^\s'\"]+)", lambda m: "rootflags=" + replace_selector(m[1], selector), before)
    add(defaults, before, defaults_for(f"subvolid={root_id}"), defaults_for("subvol=" + target))
    kernelopts = environment.get("kernelopts", "")
    stable, after = transform(kernelopts, normal=True) if kernelopts else ("", "")
    journal.update(files=files, retained_ids=sorted(retained_ids), kernelopts=dict(before=kernelopts, stable=stable, after=after))


def apply_references(journal, phase):
    if sorted(p.name for p in (BOOT / "loader/entries").glob("*.conf")) != journal["entries"]:
        raise UpdateError("Boot entries changed during root-layout repair.")
    for row in journal["files"]:
        path = Path(row["path"])
        allowed = {ROOT / "etc/fstab", ROOT / "etc/kernel/cmdline", ROOT / "etc/default/grub"}
        if path not in allowed and (path.parent != BOOT / "loader/entries" or path.suffix != ".conf"):
            raise UpdateError("Unexpected file in the Btrfs root-layout journal.")
        if path.is_symlink() or path.read_text() not in {row[k] for k in ("before", "stable", "after")}:
            raise UpdateError(f"Configuration changed during root-layout repair: {path}")
    environment = dict(line.split("=", 1) for line in output(["grub2-editenv", str(BOOT / "grub2/grubenv"), "list"]).splitlines() if "=" in line)
    if (environment.get("saved_entry") != journal["entry"]
            or any(environment.get(key) for key in ("nobara_fallback", "nobara_trial", "next_entry"))):
        raise UpdateError("The boot selection changed during root-layout repair.")
    values = journal["kernelopts"]
    if environment.get("kernelopts", "") not in set(values.values()):
        raise UpdateError("GRUB kernel options changed during root-layout repair.")
    for row in journal["files"]:
        write_boot_file(Path(row["path"]), text=row[phase])
    if values[phase]:
        output(["grub2-editenv", str(BOOT / "grub2/grubenv"), "set", "kernelopts=" + values[phase]])
    os.sync()


def restore_root_layout(state_root, state):
    """Return migration details when completed, or None for an ordinary root.

    The caller holds the updater lock, has confirmed startup/disarmed fallback,
    and must not prune recovery data if this operation fails.
    """
    journal_path = state_root / JOURNAL
    mount = json.loads(output(["findmnt", "--json", "--mountpoint", str(ROOT), "--output", "FSTYPE,FSROOT,UUID"]))["filesystems"][0]
    if mount["fstype"] != "btrfs":
        if journal_path.exists():
            raise UpdateError("A pending Btrfs root-layout repair belongs to another filesystem.")
        return None
    current = safe_relative(mount["fsroot"]) if mount["fsroot"] != "/" else ""
    if not journal_path.exists() and not MANAGED.fullmatch(current):
        return None
    if (ROOT / "system-update").exists() or (ROOT / "system-update").is_symlink():
        raise UpdateError("Finish or cancel the scheduled update before restoring the root layout.")
    if not BOOT.is_mount() or BOOT.stat().st_dev == ROOT.stat().st_dev:
        raise UpdateError("Root-layout repair requires the existing separate /boot filesystem.")
    environment = dict(line.split("=", 1) for line in output(["grub2-editenv", str(BOOT / "grub2/grubenv"), "list"]).splitlines() if "=" in line)
    if any(environment.get(key) for key in ("nobara_fallback", "nobara_trial", "next_entry")):
        raise UpdateError("Root-layout repair waits until the current boot is confirmed.")
    TOP.mkdir(parents=True, exist_ok=True, mode=0o700)
    if TOP.is_symlink() or TOP.is_mount():
        raise UpdateError("The private Btrfs root-layout mountpoint is already in use.")
    output(["mount", "-t", "btrfs", "-o", "subvolid=5", "UUID=" + mount["uuid"], str(TOP)])
    try:
        if journal_path.exists():
            if journal_path.is_symlink():
                raise UpdateError("The root-layout journal is a symlink.")
            journal = json.loads(journal_path.read_text())
        else:
            target = original_root(state_root, state, current, mount["uuid"])
            source_path, target_path = checked_path(current), checked_path(target)
            owner = source_path.parent / "owner.json"
            if owner.is_symlink() or json.loads(owner.read_text()) != dict(owner="nobara-updater", job=MANAGED.fullmatch(current)[1]):
                raise UpdateError("The recovered root has no valid updater ownership record.")
            if not target_path.parent.is_dir():
                raise UpdateError("The original root's parent directory is missing.")
            if source_path.is_relative_to(target_path) or target_path.is_relative_to(source_path):
                raise UpdateError("The original and recovered root paths overlap.")
            root_id = subvolume_id(source_path)
            if root_id != subvolume_id(ROOT):
                raise UpdateError("The recovery subvolume is not the running root.")
            displaced_id = subvolume_id(target_path) if target_path.exists() else None
            if target_path.exists() and (target_path / "etc/machine-id").read_text() != (ROOT / "etc/machine-id").read_text():
                raise UpdateError("The original root path is now used by a different installation.")
            journal = dict(version=1, uuid=mount["uuid"], source=current, target=target,
                           root_id=root_id, displaced_id=displaced_id,
                           displaced=str(PurePosixPath(current).parent / "displaced"))
            if checked_path(journal["displaced"]).exists():
                raise UpdateError("The displaced-root storage path is already in use.")
            reference_files(journal, environment)
            atomic_json(journal_path, journal)
        if journal.get("version") != 1 or journal["uuid"] != mount["uuid"] or subvolume_id(ROOT) != journal["root_id"]:
            raise UpdateError("The root-layout journal does not identify the running filesystem/subvolume.")
        match = MANAGED.fullmatch(journal["source"])
        if not match or journal["displaced"] != str(PurePosixPath(journal["source"]).parent / "displaced"):
            raise UpdateError("Invalid managed root path in the root-layout journal.")
        source, target, displaced = (checked_path(journal[k]) for k in ("source", "target", "displaced"))
        if source.is_relative_to(target) or target.is_relative_to(source) or journal["target"].startswith(".nobara-updater/"):
            raise UpdateError("Invalid destination in the root-layout journal.")
        owner = source.parent / "owner.json"
        if owner.is_symlink() or json.loads(owner.read_text()) != dict(owner="nobara-updater", job=match[1]):
            raise UpdateError("The recovered root has no valid updater ownership record.")
        # Evaluate actual IDs, not a phase flag: a power cut can happen between
        # a rename and a journal write. Never repeat a completed move backwards.
        active_moved = target.exists() and subvolume_id(target) == journal["root_id"]
        if not active_moved:
            if not source.exists() or subvolume_id(source) != journal["root_id"]:
                raise UpdateError("The recovered root moved unexpectedly.")
            old_id = journal["displaced_id"]
            if old_id is not None:
                old = displaced if displaced.exists() else target
                if not old.exists() or subvolume_id(old) != old_id or (displaced.exists() and target.exists()):
                    raise UpdateError("The displaced root changed during layout repair.")
            elif target.exists() or displaced.exists():
                raise UpdateError("The root-layout destination is no longer empty.")
            apply_references(journal, "stable")
            if target.exists():
                os.rename(target, displaced)
                os.sync()
            os.rename(source, target)
            os.sync()
        elif source.exists():
            raise UpdateError("The recovered root's former path has been reused.")
        apply_references(journal, "after")
        # Keep provenance beside the displaced root until safe cleanup. Existing
        # plans retain the original ancestry even after the temporary journal goes.
        result = {key: journal[key] for key in ("uuid", "source", "target", "root_id", "displaced_id", "displaced", "retained_ids")}
        atomic_json(source.parent / "root-layout.json", result)
        journal_path.unlink()
        os.sync()
        LOG.info("Restored the recovered Btrfs system to /%s (subvolume ID %s).", journal["target"], journal["root_id"])
        return result
    finally:
        output(["umount", str(TOP)])
