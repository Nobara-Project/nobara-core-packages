"""LVM recovery for ext4/XFS roots. Only classic, fully provisioned LVs.

Reserve a full origin's worth of changed blocks, including COW metadata.
Recovery runs from a pre-update initramfs, independent of installed Python.
"""
from __future__ import annotations

import json
from decimal import Decimal
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess

from .update_state import UpdateError

RESERVE_TAG = "nobara.rollback.reserve"
SNAPSHOT_TAG = "nobara.rollback.snapshot"
LV_FIELDS = "vg_name,vg_uuid,lv_name,lv_uuid,lv_path,lv_size,lv_attr,segtype,origin,origin_uuid,lv_tags,data_percent"


def output(args):
    return subprocess.run(args, check=True, text=True, capture_output=True).stdout.strip()


def name(value):
    if not re.fullmatch(r"[A-Za-z0-9_+][A-Za-z0-9_.+-]{0,126}", value or ""):
        raise UpdateError("Invalid LVM volume name.")
    return value


def inventory():
    rows = json.loads(output(["lvm", "lvs", "--reportformat", "json", "--units", "b", "--nosuffix", "-o", LV_FIELDS]))["report"][0]["lv"]
    return [{k: str(v).strip() for k, v in row.items()} for row in rows]


def tags(row):
    return set(filter(None, row["lv_tags"].split(",")))


def snapshot_bytes(origin_bytes):
    # A 64 KiB exception chunk needs much less than 5% metadata. Round up to
    # 4 MiB; lvcreate can cap this to its own maximum COW size for the origin.
    extent = 4 * 1024**2
    return ((origin_bytes * 105 // 100 + 8 * 1024**2 + extent - 1) // extent) * extent


def owned_snapshots(rows, origin):
    return [r for r in rows if r["vg_uuid"] == origin["vg_uuid"]
            and r["origin_uuid"] == origin["lv_uuid"] and SNAPSHOT_TAG in tags(r)
            and re.fullmatch(r"nobara_rollback_[a-f0-9]{32}", r["lv_name"])]


def probe(root):
    if root["fstype"] not in {"ext4", "xfs"}:
        raise UpdateError("LVM recovery supports ext4 and XFS roots.")
    rows = inventory()
    matches = [r for r in rows if r["lv_path"] and os.path.realpath(r["lv_path"]) == os.path.realpath(root["source"])]
    if len(matches) != 1:
        raise UpdateError("The root filesystem is not on a supported LVM logical volume.")
    origin = matches[0]
    if origin["segtype"] != "linear" or origin["origin"] or origin["lv_attr"][0] not in {"-", "o"}:
        raise UpdateError("Recovery currently requires a linear, fully provisioned root LV.")
    # Block rollback must not silently revert user documents created after
    # the snapshot. Installer-managed layouts always have a separate home LV.
    home = json.loads(output(["findmnt", "--json", "--mountpoint", "/home", "--output", "TARGET,SOURCE"]))["filesystems"][0]
    if home["target"] != "/home" or os.path.realpath(home["source"].split("[", 1)[0]) == os.path.realpath(root["source"].split("[", 1)[0]):
        raise UpdateError("LVM recovery requires a separate /home filesystem.")
    if not any(len(f := line.split()) >= 4 and f[1] == "/home" and "noauto" not in f[3].split(",")
               for line in Path("/etc/fstab").read_text().splitlines() if not line.lstrip().startswith("#")):
        raise UpdateError("LVM recovery requires a persistent /home mount.")
    vg, lv = name(origin["vg_name"]), name(origin["lv_name"])
    required = snapshot_bytes(int(Decimal(origin["lv_size"])))
    group = json.loads(output(["lvm", "vgs", "--reportformat", "json", "--units", "b", "--nosuffix", "-o", "vg_uuid,vg_free,vg_extent_size", vg]))["report"][0]["vg"][0]
    if group["vg_uuid"] != origin["vg_uuid"]:
        raise UpdateError("The root volume group identity changed.")
    extent = int(Decimal(group["vg_extent_size"]))
    required = (required + extent - 1) // extent * extent
    reserves = [r for r in rows if r["vg_uuid"] == origin["vg_uuid"] and r["lv_name"] == "nobara_reserve"
                and RESERVE_TAG in tags(r) and r["segtype"] == "linear" and r["lv_attr"][1] == "r"]
    snapshots = owned_snapshots(rows, origin)
    # Only one latest recovery is retained. A new update may reclaim the
    # previous recovery after the running system has been confirmed healthy.
    available = int(Decimal(group["vg_free"])) + sum(int(Decimal(r["lv_size"])) for r in reserves)
    # For classic snapshots lv_size reports the COW allocation (origin_size
    # is the virtual size). Thin snapshots are deliberately unsupported.
    available += sum(int(Decimal(r["lv_size"])) for r in snapshots)
    if available < required:
        raise UpdateError(f"LVM rollback needs {required / 1024**3:.1f} GiB of reserved/free volume-group space; {available / 1024**3:.1f} GiB is available.")
    if not Path("/usr/lib/dracut/modules.d/91nobara-rollback/module-setup.sh").is_file():
        raise UpdateError("The Nobara recovery initramfs module is missing.")
    return dict(backend="lvm", vg=vg, lv=lv, origin_uuid=origin["lv_uuid"], vg_uuid=origin["vg_uuid"],
                origin_path=origin["lv_path"], snapshot_bytes=required, fstype=root["fstype"],
                reclaim=[dict(name=r["lv_name"], uuid=r["lv_uuid"]) for r in reserves + snapshots])


def create(job, layout):
    from .update_recovery import boot_payloads, expand_bls, grub_variables
    job_id = job.name
    vg, lv = name(layout["vg"]), name(layout["lv"])
    origin_path = vg + "/" + lv
    rows = inventory()
    origins = [r for r in rows if r["lv_uuid"] == layout["origin_uuid"] and r["vg_uuid"] == layout["vg_uuid"]]
    if len(origins) != 1 or origins[0]["lv_path"] != layout["origin_path"]:
        raise UpdateError("The root logical volume changed after preparation.")
    for old in layout["reclaim"]:
        match = [r for r in rows if r["vg_uuid"] == layout["vg_uuid"] and r["lv_uuid"] == old["uuid"] and r["lv_name"] == old["name"]]
        if len(match) != 1:
            raise UpdateError("Reserved recovery storage changed after preparation.")
        row = match[0]
        if row not in owned_snapshots(rows, origins[0]) and not (row["lv_name"] == "nobara_reserve" and RESERVE_TAG in tags(row) and row["lv_attr"][1] == "r"):
            raise UpdateError("Refusing to reclaim storage not owned by the updater.")
        output(["lvm", "lvremove", "--yes", vg + "/" + name(old["name"])])
        if old["name"].startswith("nobara_rollback_"):
            old_id = old["name"].removeprefix("nobara_rollback_")
            (Path("/boot/loader/entries") / ("nobara-recovery-" + old_id + ".conf")).unlink(missing_ok=True)
    snap = "nobara_rollback_" + job_id
    # lvcreate suspends the origin while inserting the snapshot target.
    os.sync()
    output(["lvm", "lvcreate", "--snapshot", "--size", str(layout["snapshot_bytes"]) + "B", "--chunksize", "64K",
            "--name", snap, "--addtag", SNAPSHOT_TAG, origin_path])
    row = next(r for r in inventory() if r["vg_name"] == vg and r["lv_name"] == snap)
    if row["origin_uuid"] != layout["origin_uuid"] or row["lv_attr"][0] != "s":
        raise UpdateError("LVM did not create a valid root snapshot.")
    destination = Path("/boot/nobara-updater") / job_id
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    from .update_state import atomic_json
    atomic_json(destination / "owner.json", {"owner": "nobara-updater", "job": job_id, "backend": "lvm"})
    entry = Path(layout["entry"])
    kernel = os.uname().release
    for source in boot_payloads(entry):
        shutil.copy2(source, destination / source.name)
    config = job / "lvm-recovery.conf"
    values = dict(job=job_id, vg=vg, origin=lv, origin_uuid=layout["origin_uuid"], snapshot=snap,
                  snapshot_uuid=row["lv_uuid"], restored_entry="nobara-restored-" + job_id)
    config.write_text("".join(f"NB_{key.upper()}={shlex.quote(value)}\n" for key, value in values.items()))
    image = destination / "recovery.img"
    subprocess.run(["dracut", "--force", "--no-hostonly-cmdline", "--kver", kernel, "--add", "nobara-rollback lvm crypt",
                    "--include", str(config), "/etc/nobara-rollback.conf", str(image)], check=True)
    subprocess.run(["lsinitrd", str(image)], check=True, stdout=subprocess.DEVNULL)
    options = None
    lines = []
    env = grub_variables()
    for line in entry.read_text().splitlines():
        fields = line.split(None, 1)
        if len(fields) != 2:
            lines.append(line)
            continue
        key, value = fields
        if key == "title":
            line = "title Nobara — previous system (" + job_id[:8] + ")"
        elif key in {"linux", "initrd"}:
            line = key + " " + " ".join("/" + str((destination / Path(p).name).relative_to("/boot")) for p in expand_bls(key, value, env))
        elif key == "options":
            options = shlex.split(" ".join(expand_bls(key, value, env)))
            options = [p for p in options if not p.startswith(("nobara.rollback=", "rd.lvm.lv=", "root=", "resume="))]
            options += ["root=/dev/" + origin_path, "rd.lvm.lv=" + origin_path]
            line = "options " + " ".join(options)
        lines.append(line)
    if options is None:
        raise UpdateError("Recovery boot entry is missing root options.")
    # A separate normal entry keeps copied old boot files usable after merge.
    entries = Path("/boot/loader/entries")
    (entries / (values["restored_entry"] + ".conf")).write_text("\n".join(lines) + "\n")
    recovery_lines = [line for line in lines if not line.startswith("initrd ")]
    recovery_lines += ["initrd /" + str(image.relative_to("/boot"))]
    recovery_lines = [("options " + " ".join(options) + " noresume nobara.rollback=" + job_id) if line.startswith("options ") else line for line in recovery_lines]
    entry_id = "nobara-recovery-" + job_id
    (entries / (entry_id + ".conf")).write_text("\n".join(recovery_lines) + "\n")
    os.sync()
    return dict(layout, created=True, entry_id=entry_id, snapshot=snap, snapshot_uuid=row["lv_uuid"], restored_entry=values["restored_entry"])


def finish(layout, job_id, state_root=None):
    """Retire only this confirmed update's snapshot and restore its reserve.

    Classic snapshot origins suppress discards and add write amplification.
    Retention therefore ends after successful boot validation on LVM. Btrfs
    has different retention semantics and is handled separately.
    """
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise UpdateError("Invalid recovery job identifier.")
    from .update_recovery import grub_environment
    from .update_state import STATE_DIR
    state_root = STATE_DIR if state_root is None else state_root
    env = grub_environment()
    if env.get("nobara_fallback"):
        raise UpdateError("Cannot release recovery storage before boot confirmation.")
    rows = inventory()
    origins = [r for r in rows if r["lv_uuid"] == layout["origin_uuid"] and r["vg_uuid"] == layout["vg_uuid"]]
    if len(origins) != 1:
        raise UpdateError("Cannot verify the confirmed root LV.")
    vg = name(origins[0]["vg_name"])
    snapshots = [r for r in owned_snapshots(rows, origins[0]) if r["lv_name"] == "nobara_rollback_" + job_id]
    entry = Path("/boot/loader/entries") / ("nobara-recovery-" + job_id + ".conf")
    if env.get("saved_entry") == entry.stem:
        raise UpdateError("Cannot retire the selected recovery entry.")
    # Remove the menu entry before its backing snapshot, so interruption
    # cannot leave a selectable destructive recovery without a snapshot.
    entry.unlink(missing_ok=True)
    os.sync()
    for snapshot in snapshots:
        if layout.get("snapshot_uuid") and snapshot["lv_uuid"] != layout["snapshot_uuid"]:
            raise UpdateError("The recovery snapshot identity changed.")
        output(["lvm", "lvremove", "--yes", vg + "/" + snapshot["lv_name"]])
    reserves = [r for r in inventory() if r["vg_uuid"] == layout["vg_uuid"] and r["lv_name"] == "nobara_reserve"]
    if reserves:
        if len(reserves) != 1 or RESERVE_TAG not in tags(reserves[0]) or reserves[0]["lv_attr"][1] != "r":
            raise UpdateError("The recovery reserve name is already in use.")
    else:
        output(["lvm", "lvcreate", "--yes", "--size", str(layout["snapshot_bytes"]) + "B", "--name", "nobara_reserve",
                "--permission", "r", "--zero", "n", "--wipesignatures", "n", "--addtag", RESERVE_TAG, vg])
    # Only paths carrying our ownership record are eligible for cleanup.
    # Keep the selected restored kernel/images after a rollback.
    for directory in Path("/boot/nobara-updater").glob("*"):
        if not re.fullmatch(r"[a-f0-9]{32}", directory.name) or directory.is_symlink():
            continue
        owner = directory / "owner.json"
        if not owner.is_file() or json.loads(owner.read_text()) != dict(owner="nobara-updater", job=directory.name, backend="lvm"):
            continue
        identifiers = ["nobara-recovery-" + directory.name, "nobara-restored-" + directory.name]
        # Keep the archives and boot images while another snapshot still
        # depends on them, even if it is not the selected entry.
        if any(r["lv_name"] == "nobara_rollback_" + directory.name for r in owned_snapshots(inventory(), origins[0])):
            continue
        archives = state_root / "jobs" / directory.name
        if not archives.is_symlink():
            for filename in ("boot.tar", "efi.tar"):
                (archives / filename).unlink(missing_ok=True)
        if env.get("saved_entry") in identifiers:
            continue
        for identifier in identifiers:
            (Path("/boot/loader/entries") / (identifier + ".conf")).unlink(missing_ok=True)
        shutil.rmtree(directory)
    os.sync()
