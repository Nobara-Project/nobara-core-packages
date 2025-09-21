#!/usr/bin/python3
# -*- coding: utf-8 -*-

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, GLib, Gdk

import subprocess, os, grp, getpass, sys, textwrap, pwd, grp, shlex, threading, tempfile, shutil, time
from pathlib import Path
from functools import partial
import configparser
from typing import Optional
from contextlib import contextmanager

APP_TITLE = "Nobara Drive Mount Manager"
CONFIG_PATH = "/etc/nobara/automount/enabled.conf"
SHORTCUTS_CONFIG_PATH = "/etc/nobara/automount/desktop_shortcuts.conf"
_LUKS_PW_TTL = 600  # seconds
ENV_DIR = "/etc/nobara/automount/"

RULE_TEMPLATE = ( 'ACTION=="add|change", SUBSYSTEM=="block", ' 'ENV{{ID_FS_USAGE}}=="filesystem", ' 'ENV{{ID_FS_TYPE}}!="crypto_LUKS", ' 'ENV{{ID_FS_UUID}}=="{uuid}", ' 'TAG+="systemd", ' 'ENV{{SYSTEMD_WANTS}}+="nobara-automount@%E{{ID_FS_UUID}}.service"\n' )

# -----------------------------
# Theme & CSS (pulled from Flatpost)
# -----------------------------
def init_theme_and_css():
    settings = Gtk.Settings.get_default()
    # Same theme hints Flatpost uses
    settings.set_property("gtk-theme-name", "adw-gtk3-dark")
    settings.set_property("gtk-application-prefer-dark-theme", True)

    css = """
    .top-bar {
        margin: 0px; padding: 0px; border: 0px;
        background-color: @sidebar_backdrop_color;
        border-bottom: 1px solid mix(currentColor,@window_bg_color,0.86);
    }
    .category-panel {
        padding: 0 6px; margin: 12px; border-radius: 4px;
        background-color: @sidebar_backdrop_color;
        border: 1px solid mix(currentColor,@window_bg_color,0.86);
    }
    .category-group-header { margin: 0; font-weight: bold; }
    .category-button {
        border: 0px; padding: 12px 8px; margin: 0; background: none;
        transition: margin-left 0.2s cubic-bezier(0.040,0.455,0.215,0.995),
                    padding 0.2s cubic-bezier(0.040,0.455,0.215,0.995);
    }
    .category-button.active {
        padding: 12px 14px; margin-left: 4px;
        background-color: mix(currentColor,@window_bg_color,0.9);
        border-radius: 4px; font-weight: bold;
    }
    .app-panel { margin: 12px; }
    .app-list { border: 0px; background: none; }
    .app-list-item {
        padding: 24px 32px; margin: 10px 0; border-radius: 16px;
        background-color: alpha(@card_bg_color,0.6);
        border: 1px solid mix(currentColor,@window_bg_color,0.9);
        transition: background-color 0.2s ease-out;
    }
    .dim-label { opacity: 0.7; }
    .mono { font-family: monospace; }
    .title-1 { font-weight: 800; font-size: 20px; }
    .title-2 { font-weight: 700; font-size: 16px; }
    """
    provider = Gtk.CssProvider()
    provider.load_from_data(css.encode("utf-8"))
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(),
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 600
    )

# -----------------------------
# Elevation
# -----------------------------
def relaunch_with_pkexec():
    script_path = Path(__file__).resolve()
    user = getpass.getuser()
    if os.geteuid() != 0:
        subprocess.run(["xhost", "si:localuser:root"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
        )
        os.execvp(
            "pkexec",
            [
                "pkexec",
                "--disable-internal-agent",
                "env",
                f"DISPLAY={os.environ.get('DISPLAY','')}",
                f"XAUTHORITY={os.environ.get('XAUTHORITY','')}",
                f"SUDO_USER={user}",
                "NO_AT_BRIDGE=1",
                "G_MESSAGES_DEBUG=none",
                sys.executable,
                str(script_path),
            ] + sys.argv[1:],
        )

def is_luks_mapper_present(uuid: str) -> bool:
    # cryptsetup default mapper name pattern we’ll use below
    return Path(f"/dev/mapper/luks-{uuid}").exists()

def prompt_password(parent: Optional[Gtk.Window], title="Unlock encrypted drive") -> Optional[str]:
    dlg = Gtk.Dialog(
        title=title,
        transient_for=parent,
        modal=True,
        destroy_with_parent=True
    )
    dlg.set_resizable(False)
    box = dlg.get_content_area()
    box.set_border_width(12)

    v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    lab = Gtk.Label(label="Enter passphrase:", xalign=0)
    entry = Gtk.Entry()
    entry.set_visibility(False)
    entry.set_invisible_char("•")
    entry.set_activates_default(True)

    v.pack_start(lab, False, False, 0)
    v.pack_start(entry, False, False, 0)
    box.add(v)

    ok_btn = dlg.add_button("_Unlock", Gtk.ResponseType.OK)
    cancel_btn = dlg.add_button("_Cancel", Gtk.ResponseType.CANCEL)
    ok_btn.get_style_context().add_class("suggested-action")
    dlg.set_default_response(Gtk.ResponseType.OK)

    dlg.show_all()
    resp = dlg.run()
    text = entry.get_text() if resp == Gtk.ResponseType.OK else None
    try:
        dlg.destroy()
    except Exception:
        pass
    return text if text else None

def _cryptsetup_is_luks(path: str) -> bool:
    if not path:
        return False
    return subprocess.run(
        ["cryptsetup", "isLuks", path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    ).returncode == 0


def _is_outer_luks(name: str, uuid: str, fstype: str, typ: str) -> bool:
    """Detect a *locked* LUKS container device (the outer), not the mapper."""
    # If lsblk already told us, trust it.
    if (fstype or "").lower() == "crypto_luks":
        return True
    # Never treat the mapper (TYPE=crypt or luks-*) as an outer.
    if typ == "crypt" or (name or "").startswith("luks-"):
        return False
    # Prefer /dev/disk/by-uuid if we have a UUID; else fall back to /dev/<name>.
    # Some distros/filesystems don’t expose the outer UUID via lsblk reliably.
    cand = f"/dev/disk/by-uuid/{uuid}" if uuid else (f"/dev/{name}" if name else "")
    return _cryptsetup_is_luks(cand)

def ensure_auto_unlock(uuid: str, passphrase: str, mapper_name: str | None = None, status_fn=print):
    """
    Idempotently set up auto-unlock for a LUKS container identified by UUID.
    - Creates /etc/cryptsetup-keys.d/<UUID>.key if missing (0400, root).
    - Adds that key to the LUKS keyslots (using the provided passphrase).
    - Ensures a matching /etc/crypttab entry exists.
    """
    mapper = mapper_name or f"luks-{uuid}"
    keydir = "/etc/cryptsetup-keys.d"
    keyfile = os.path.join(keydir, f"{uuid}.key")
    dev = f"/dev/disk/by-uuid/{uuid}"

    # 1) Create keyfile if missing (256 random bytes)
    if not os.path.exists(keyfile):
        os.makedirs(keydir, exist_ok=True)
        # create empty with correct perms first
        with open(keyfile, "wb") as f:
            pass
        os.chmod(keyfile, 0o400)
        os.chown(keyfile, 0, 0)  # root:root
        # fill with random bytes
        with open("/dev/urandom", "rb") as r, open(keyfile, "r+b") as f:
            f.write(r.read(256))
        # SELinux contexts (best-effort)
        subprocess.run(["restorecon", "-v", keyfile], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # 2) Enroll keyfile as a new keyslot (using the existing passphrase on stdin)
    #    cryptsetup reads the *existing* passphrase from stdin when --key-file=- is used.
    add_cmd = ["cryptsetup", "luksAddKey", "--key-file=-", dev, keyfile]
    proc = subprocess.run(add_cmd, input=passphrase.encode("utf-8"))
    if proc.returncode != 0:
        status_fn("Could not add auto-unlock key (wrong passphrase or policy).")
        return False

    # 3) Ensure /etc/crypttab line exists (update if an older line exists)
    crypttab_line = f"{mapper}\tUUID={uuid}\t{keyfile}\tluks,discard\n"
    try:
        # read current file
        try:
            with open("/etc/crypttab", "r", encoding="utf-8") as f:
                lines = f.readlines()
        except FileNotFoundError:
            lines = []

        # remove pre-existing lines for this UUID or mapper
        new_lines = [l for l in lines if (uuid not in l and not l.strip().startswith(mapper))]
        new_lines.append(crypttab_line)

        with open("/etc/crypttab", "w", encoding="utf-8") as f:
            f.writelines(new_lines)
    except Exception as e:
        status_fn(f"Failed to update /etc/crypttab: {e}")
        return False

    # 4) Reload systemd units so the crypttab is seen by the manager now
    subprocess.run(["systemctl", "daemon-reload"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    status_fn("Auto-unlock configured: will unlock at boot using a keyfile.")
    return True

def _rows_by_name(rows):
    return {(r.get("NAME") or ""): r for r in rows}

def _pkname_of(name, rows_map):
    return (rows_map.get(name) or {}).get("PKNAME") or ""

def _base_disk(name, rows_map):
    seen = set(); cur = name
    while True:
        if cur in seen:            # safety
            return cur
        seen.add(cur)
        pk = _pkname_of(cur, rows_map)
        if not pk:
            return cur
        cur = pk

def _pretty_model(s):
    return " ".join((s or "").replace("_", " ").split()) or "Unknown"

def _probe_uuid_fstype(devnode):
    uuid = fstype = None
    try:
        uuid = subprocess.run(["blkid", "-o", "value", "-s", "UUID", devnode],
                              text=True, capture_output=True, check=False).stdout.strip() or None
        fstype = subprocess.run(["blkid", "-o", "value", "-s", "TYPE", devnode],
                                text=True, capture_output=True, check=False).stdout.strip() or None
    except Exception:
        pass
    # fallback to udev if needed
    if not uuid or not fstype:
        try:
            out = subprocess.run(["udevadm", "info", "--query=property", "--name", devnode],
                                 text=True, capture_output=True, check=False).stdout
            if not uuid:
                for line in out.splitlines():
                    if line.startswith("ID_FS_UUID="):
                        uuid = line.split("=",1)[1].strip() or None
                        break
            if not fstype:
                for line in out.splitlines():
                    if line.startswith("ID_FS_TYPE="):
                        fstype = line.split("=",1)[1].strip() or None
                        break
        except Exception:
            pass
    return uuid, (fstype.lower() if fstype else None)

def _size_bytes(devnode):
    try:
        # blockdev --getsize64 returns bytes
        out = subprocess.run(["blockdev", "--getsize64", devnode],
                             text=True, capture_output=True, check=False).stdout.strip()
        return int(out) if out else 0
    except Exception:
        return 0

# -----------------------------
# Env
# -----------------------------
def ensure_env_file(unit_or_instance: str, uuid: str, user: str, fstype: str, opts: str) -> str:
    """
    Create /etc/nobara/automount/%I.env where %I is the unescaped instance string
    (e.g. run/media/<user>/<uuid>.mount). Accepts either:
      - escaped unit name (e.g. run-media-user-UUID.mount)  -> will unescape to %I
      - unescaped instance/path (e.g. run/media/user/UUID.mount or /run/media/user/UUID.mount)
    Returns the absolute path to the env file.
    """
    inst = unit_or_instance.strip()

    # Convert escaped unit name to an unescaped instance (%I) using systemd-escape -u
    if inst.endswith(".mount") and not inst.startswith("/"):
        # Looks like a unit name (run-media-… .mount)
        try:
            inst_unescaped = subprocess.run(
                ["systemd-escape", "-u", inst],
                check=True, text=True, capture_output=True
            ).stdout.strip()
        except Exception:
            # Fallback best-effort: de-escape \x2d and turn dashes into slashes
            tmp = inst[:-6]  # drop ".mount"
            tmp = tmp.replace("\\x2d", "-")
            inst_unescaped = "/" + tmp.replace("-", "/")
            inst_unescaped += ".mount"
    else:
        # Already an instance/path; normalize leading slash and ensure trailing ".mount"
        inst_unescaped = inst
        if not inst_unescaped.startswith("/"):
            inst_unescaped = "/" + inst_unescaped
        if not inst_unescaped.endswith(".mount"):
            inst_unescaped += ".mount"
        inst_unescaped = inst_unescaped.replace("-", "x2d")

    # Build /etc/nobara/automount/%I.env (strip leading "/" so we create subdirs under ENV_DIR)
    rel = inst_unescaped.lstrip("/")
    env_path = Path(ENV_DIR) / f"{rel}.env"

    # Ensure parent dirs exist (because %I contains slashes)
    env_path.parent.mkdir(parents=True, exist_ok=True)

    rw_uid = pwd.getpwnam(user).pw_uid
    rw_gid = pwd.getpwnam(user).pw_gid

    # Write the env file (include RWUSER and UUID)
    tmp = env_path.with_suffix(env_path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(f"RWUSER={user}\n")
        f.write(f"RW_UID={rw_uid}\n")
        f.write(f"RW_GID={rw_gid}\n")
        f.write(f"UUID={uuid}\n")
        f.write(f"FSTYPE={fstype}\n")
        f.write(f"OPTS={opts}\n")
    os.replace(tmp, env_path)
    os.chmod(env_path, 0o644)

    return str(env_path)

# -----------------------------
# Core logic
# -----------------------------

def _read_first_line(path):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.readline().strip()
    except Exception:
        return ""

def _udev_model_for(name: str) -> str:
    try:
        out = subprocess.run(
            ["udevadm", "info", "--query=property", "--name", f"/dev/{name}"],
            check=False, text=True, capture_output=True
        ).stdout
        props = {}
        for line in out.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                props[k.strip()] = v.strip()
        return props.get("ID_MODEL_FROM_DATABASE") or props.get("ID_MODEL") or ""
    except Exception:
        return ""

def read_shortcut_partitions():
    try:
        with open(SHORTCUTS_CONFIG_PATH, "r") as f:
            return set(l.strip() for l in f.readlines())
    except FileNotFoundError:
        return set()

def write_shortcut_partitions(lines):
    # mirror the same safety filter as enabled.conf
    clean = [l for l in lines if l.strip().startswith(("/dev/disk/by-uuid/", "#"))]
    os.makedirs(os.path.dirname(SHORTCUTS_CONFIG_PATH), exist_ok=True)
    with open(SHORTCUTS_CONFIG_PATH, "w") as f:
        f.writelines(clean)

def set_shortcut_enabled(partition, enabled):
    try:
        try:
            with open(SHORTCUTS_CONFIG_PATH, "r") as f:
                lines = f.readlines()
        except FileNotFoundError:
            lines = []

        devnode = os.path.realpath(partition)
        uuid, fstype = _probe_uuid_fstype(devnode)

        if enabled:
            if partition + "\n" not in lines:
                lines.append(partition + "\n")
                partition_uuid = partition.rsplit("/", 1)[-1]
                write_shortcut_partitions(lines)
                subprocess.run(
                    ["/usr/libexec/nobara-automount", "--mount", "--uuid", f"{partition_uuid}", "--fstype", f"{fstype}"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
        else:
            lines = [l for l in lines if l.strip() != partition]
            partition_uuid = partition.rsplit("/", 1)[-1]
            write_shortcut_partitions(lines)
            subprocess.run(
                ["/usr/libexec/nobara-automount", "--cleanup", "--uuid", f"{partition_uuid}"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
    except Exception as e:
        raise RuntimeError(f"Error updating desktop shortcut config: {e}")

def check_wheel_group(status_fn):
    user = os.environ.get("SUDO_USER")
    try:
        groups = [g.gr_name for g in grp.getgrall() if user in g.gr_mem]
        if "wheel" not in groups:
            status_fn("Permission Denied: You must be in the 'wheel' group to run this application.")
            GLib.timeout_add(1500, Gtk.main_quit)
            return False
        return True
    except Exception as e:
        status_fn(f"Error: Failed to check group membership: {e}")
        GLib.timeout_add(1500, Gtk.main_quit)
        return False

def read_enabled_partitions():
    try:
        with open(CONFIG_PATH, "r") as f:
            return set(l.strip() for l in f.readlines())
    except FileNotFoundError:
        return set()

def write_enabled_partitions(lines):
    clean = [l for l in lines if l.strip().startswith(("/dev/disk/by-uuid/", "#"))]
    with open(CONFIG_PATH, "w") as f:
        f.writelines(clean)

def set_partition_enabled(partition, enabled):
    try:
        try:
            with open(CONFIG_PATH, "r") as f:
                lines = f.readlines()
        except FileNotFoundError:
            lines = []
        if enabled:
            if partition + "\n" not in lines:
                lines.append(partition + "\n")
        else:
            lines = [l for l in lines if l.strip() != partition]
        write_enabled_partitions(lines)
    except Exception as e:
        raise RuntimeError(f"Error updating config: {e}")

def get_partitions():
    """
    Returns:
      unmounted:   [(path, fstype, uuid, size_str, model_str)]
      mounted:     [(path, fstype, uuid, mountpoint, size_str, model_str)]
      luks_locked: [(path, uuid, size_str, model_str)]  # outer crypto_LUKS that need unlock
    """
    try:
        out = subprocess.run(
            ["lsblk", "-b", "-P", "-o", "NAME,TYPE,PKNAME,UUID,FSTYPE,SIZE,MODEL,MOUNTPOINT"],
            capture_output=True, text=True, check=True
        ).stdout

        rows = []
        for line in out.splitlines():
            if not line.strip():
                continue
            fields = {}
            for tok in shlex.split(line):
                if "=" in tok:
                    k, v = tok.split("=", 1)
                    fields[k] = v.strip('"')
            rows.append(fields)

        rows_map = _rows_by_name(rows)

        # parent->MODEL for inheritance
        model_by_name = {}
        for f in rows:
            nm  = f.get("NAME") or ""
            mdl = (f.get("MODEL") or "").strip()
            if mdl:
                model_by_name[nm] = mdl

        def model_for(name):
            base = _base_disk(name, rows_map)
            return _pretty_model(model_by_name.get(base) or model_by_name.get(name) or "")

        # --- helpers local to this function ---
        def _cryptsetup_is_luks(path: str) -> bool:
            if not path:
                return False
            return subprocess.run(
                ["cryptsetup", "isLuks", path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            ).returncode == 0

        def _is_outer_luks(name: str, uuid: str, fstype: str, typ: str) -> bool:
            # Never treat mapper as outer
            if (typ or "").lower() == "crypt" or (name or "").startswith("luks-"):
                return False
            # If lsblk said crypto_luks, trust it
            if (fstype or "").lower() == "crypto_luks":
                return True
            # Fall back to probing: prefer UUID path, else /dev/<name>
            cand = f"/dev/disk/by-uuid/{uuid}" if uuid else (f"/dev/{name}" if name else "")
            return _cryptsetup_is_luks(cand)

        # --- precompute locked/unlocked outers ---
        # An outer is considered "unlocked" iff a TYPE=crypt child exists with PKNAME=<outer NAME>
        # Build a set of crypt PKNAMEs for quick membership checks.
        crypt_children_of = { (r.get("PKNAME") or "") for r in rows if (r.get("TYPE") or "").lower() == "crypt" }
        locked_outer_names = set()
        unlocked_outer_names = set()
        outer_uuid_by_name = {}

        for f in rows:
            name   = f.get("NAME") or ""
            typ    = (f.get("TYPE") or "").lower()
            uuid   = f.get("UUID") or ""
            fstype = (f.get("FSTYPE") or "").lower()

            if not name:
                continue

            if _is_outer_luks(name, uuid, fstype, typ):
                if name in crypt_children_of:
                    unlocked_outer_names.add(name)
                else:
                    locked_outer_names.add(name)
                # remember (maybe empty) outer UUID (lsblk can omit it)
                outer_uuid_by_name[name] = uuid

        # Now we can bucket while suppressing children of locked outers.
        unmounted, mounted, luks_locked = [], [], []

        for f in rows:
            name       = f.get("NAME") or ""
            typ        = (f.get("TYPE") or "").lower()
            pkname     = f.get("PKNAME") or ""
            uuid       = f.get("UUID") or ""
            fstype     = (f.get("FSTYPE") or "").lower()
            size_bytes = f.get("SIZE") or "0"
            mnt        = f.get("MOUNTPOINT") or ""

            # Only consider real disks/parts or mappers
            is_real_part = name.startswith(("sd", "nvme", "mmcblk")) and not name.startswith(("loop", "dm-"))
            is_mapper    = (typ == "crypt") or name.startswith("luks-")
            if not (is_real_part or is_mapper):
                continue

            # 1) If this row is the OUTER:
            if name in locked_outer_names or name in unlocked_outer_names:
                # If locked -> show parent in luks_locked (and do NOT show any child)
                if name in locked_outer_names:
                    ouuid = outer_uuid_by_name.get(name, "") or uuid
                    # force devpath even when outer uuid is missing
                    devpath = f"/dev/disk/by-uuid/{ouuid}" if ouuid else f"/dev/{name}"
                    size_str = human_size(size_bytes)
                    luks_locked.append((devpath, ouuid or "", size_str, model_for(name)))
                # If unlocked -> hide the parent (children/mapper will be processed below)
                continue

            # 2) Suppress any child whose PKNAME is a *locked* outer (hide ext4/xfs until unlocked)
            if pkname in locked_outer_names:
                continue

            # 3) For mappers or rows missing UUID/FSTYPE, probe
            if is_mapper and (not uuid or not fstype):
                devnode = f"/dev/mapper/{name}" if (typ == "crypt" or name.startswith("luks-")) else f"/dev/{name}"
                puuid, ptype = _probe_uuid_fstype(devnode)
                uuid = uuid or (puuid or "")
                fstype = fstype or (ptype or "")

            if not uuid:
                continue
            if fstype and fstype not in ALLOWED_FS:
                continue

            devpath  = f"/dev/disk/by-uuid/{uuid}"
            size_str = human_size(size_bytes)
            mdl      = model_for(name)

            if mnt:
                mounted.append((devpath, fstype, uuid, mnt, size_str, mdl))
            else:
                unmounted.append((devpath, fstype, uuid, size_str, mdl))

        # 4) Second pass: synthesize inner FS for unlocked outers when lsblk omitted it
        names_in_rows = { r.get("NAME") or "" for r in rows }
        for outer_name in unlocked_outer_names:
            # Find the crypt child name (mapper) for this outer, if any
            # (there can be exactly one mapper per outer; pick the first)
            mapper_names = [ (r.get("NAME") or "")
                             for r in rows
                             if (r.get("TYPE") or "").lower() == "crypt"
                             and (r.get("PKNAME") or "") == outer_name ]
            mapper_name = mapper_names[0] if mapper_names else ""
            mapper_node = f"/dev/mapper/{mapper_name}" if mapper_name else ""

            if not mapper_node:
                # As a last resort, try the conventional luks-<UUID> name
                ouuid = outer_uuid_by_name.get(outer_name, "")
                if ouuid:
                    cand = f"/dev/mapper/luks-{ouuid}"
                    if os.path.exists(cand):
                        mapper_node = cand

            if not mapper_node:
                continue  # nothing to synthesize

            # If mapped but not listed / not mounted, probe inner FS and add to unmounted
            mp = subprocess.run(["findmnt", "-n", "-o", "TARGET", mapper_node],
                                text=True, capture_output=True, check=False).stdout.strip()
            if mp:
                continue  # already mounted

            in_uuid, in_fstype = _probe_uuid_fstype(mapper_node)
            if not in_uuid or not in_fstype or in_fstype not in ALLOWED_FS:
                continue

            devpath = f"/dev/disk/by-uuid/{in_uuid}"
            if not any(p == devpath for (p, *_rest) in unmounted):
                size_b  = _size_bytes(mapper_node)
                size_str = human_size(size_b if size_b else 0)
                mdl      = model_for(outer_name)
                unmounted.append((devpath, in_fstype, in_uuid, size_str, mdl))

        return unmounted, mounted, luks_locked

    except Exception:
        return [], [], []


def get_mountpoint_for_uuid(uuid):
    """Return the current mountpoint for the given UUID, or None."""
    try:
        out = subprocess.run(
            ["lsblk", "-rno", "UUID,MOUNTPOINT"],
            capture_output=True, text=True, check=True
        ).stdout
        for line in out.splitlines():
            parts = line.split(None, 1)
            if not parts:
                continue
            u = parts[0]
            mp = parts[1].strip() if len(parts) == 2 else ""
            if u == uuid and mp:
                return mp
    except Exception:
        pass
    return None

def find_desktop_name_for_mount(mountpoint: str) -> str | None:
    """
    Return the Name= from a .desktop file that refers to this mountpoint, or None.
    We check Desktop Entry fields Path=, URL= (file://), and Exec= containing the mountpoint.
    """
    user = os.environ.get("SUDO_USER")
    user_home = Path(pwd.getpwnam(user).pw_dir)
    candidates = [
        user_home / "Desktop",
    ]

    for base in candidates:
        try:
            if not base.is_dir():
                continue
            for desktop in base.glob("*.desktop"):
                cp = configparser.ConfigParser(interpolation=None)
                try:
                    cp.read(desktop, encoding="utf-8")
                except Exception:
                    continue
                if "Desktop Entry" not in cp:
                    continue
                de = cp["Desktop Entry"]
                name = de.get("Name")
                url  = de.get("URL")

                # URL=file://... exact match
                if mountpoint in str(url):
                    return str(name)

        except Exception:
            # ignore unreadable entries/dirs
            continue

    return None

def cleanup_xhost():
    try:
        subprocess.run(["xhost", "-si:localuser:root"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
        )
    except Exception:
        pass

ALLOWED_FS = {
    "ext3","ext4","exfat","xfs","btrfs","ntfs","ntfs3","f2fs","vfat","fat","fat32"
}

def human_size(nbytes_str):
    try:
        n = float(nbytes_str)
    except Exception:
        return "—"
    units = ["B","KiB","MiB","GiB","TiB","PiB"]
    i = 0
    while n >= 1024 and i < len(units)-1:
        n /= 1024.0
        i += 1
    return f"{n:.1f} {units[i]}" if i >= 2 else f"{int(n)} {units[i]}"

def _is_partition_name(name: str) -> bool:
    return (
        name.startswith(("sd", "nvme", "mmcblk"))
        and not name.startswith(("loop", "dm-"))
    )

def compute_mount_opts(uuid: str, fstype: str, rw_uid: int, rw_gid: int, rwuser: str):
    """
    Returns (fstype_out, opts) for systemd-mount.
    Mirrors the old bash case, with safe btrfs probing for subvol=@.
    """
    devpath = f"/dev/disk/by-uuid/{uuid}"
    fs = fstype or ""

    # normalize ntfs -> ntfs-3g for systemd-mount
    if fs == "ntfs":
        fs_out = "lowntfs-3g"
    else:
        fs_out = fs

    # defaults
    base = "rw,noatime,lazytime"

    if fs in ("ext4", "xfs", "ext3", "ext2"):
        opts = base

    elif fs == "f2fs":
        # ensure f2fs is listed in /etc/filesystems (optional parity)
        try:
            need = True
            if os.path.exists("/etc/filesystems"):
                with open("/etc/filesystems", "r", encoding="utf-8", errors="ignore") as f:
                    need = ("f2fs" not in f.read().split())
            if need:
                with open("/etc/filesystems", "a") as f:
                    f.write("f2fs\n")
        except Exception:
            pass
        opts = base + ",compress_algorithm=zstd,compress_chksum,atgc,gc_merge"

    elif fs == "btrfs":
        opts = base + ",compress-force=zstd,space_cache=v2,autodefrag,ssd_spread"

        # Probe for a default subvolume '@' safely
        tmpmp = None
        try:
            tmpmp = tempfile.mkdtemp(prefix=".btrfs_probe_", dir=f"/run/media/{rwuser}")
            # read-only probe mount
            subprocess.run(["mount", "-t", "btrfs", "-o", "ro", devpath, tmpmp],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            sub = "@"
            # if @ exists and is a subvolume, include it
            if os.path.isdir(os.path.join(tmpmp, sub)):
                # btrfs subvolume show returns 0 for a subvolume
                if subprocess.run(["btrfs", "subvolume", "show", os.path.join(tmpmp, sub)],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                    opts += f",subvol={sub}"
        except Exception:
            pass
        finally:
            # best-effort cleanup
            try:
                subprocess.run(["umount", "-l", tmpmp], check=False,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
            if tmpmp:
                try: os.rmdir(tmpmp)
                except Exception: pass

    elif fs == "vfat":
        opts = f"{base},uid={rw_uid},gid={rw_gid},utf8=1"

    elif fs == "exfat":
        opts = f"{base},uid={rw_uid},gid={rw_gid}"

    elif fs == "ntfs":  # (handled via fs_out above → ntfs-3g)
        opts = f"{base},uid={rw_uid},gid={rw_gid},big_writes,umask=0022"

    else:
        opts = base

    return fs_out or fstype, opts

# -----------------------------
# UI helpers (Flatpost-style)
# -----------------------------
def card(title):
    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    outer.get_style_context().add_class("app-list-item")
    header = Gtk.Label(label=title)
    header.set_xalign(0)
    header.get_style_context().add_class("title-2")
    outer.pack_start(header, False, False, 0)
    return outer

def bullet(label_text):
    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    dot = Gtk.Label(label="•")
    lab = Gtk.Label(label=label_text)
    lab.set_xalign(0); lab.set_line_wrap(True)
    row.pack_start(dot, False, False, 0)
    row.pack_start(lab, True, True, 0)
    return row

# -----------------------------
# Main Window
# -----------------------------
class MainWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title=APP_TITLE)
        self.set_default_size(1100, 650)
        self._luks_pw_cache = {}
        self.status_label = Gtk.Label(xalign=0)
        self.set_icon_name("drive-harddisk")
        self.sidebar_rows = {}
        self.enabled = set()

        self._inflight = set()          # partitions currently being changed
        self._cooldown_ms = 1500        # adjust to taste (1.5s)

        # main stack
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_hexpand(True)
        self.stack.set_vexpand(True)

        # top bar (matches Flatpost bar)
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        top.get_style_context().add_class("top-bar")
        top.set_margin_start(0); top.set_margin_end(0)
        title = Gtk.Label(label=APP_TITLE); title.set_xalign(0)
        title.get_style_context().add_class("title-2")
        refresh_btn = Gtk.Button()
        refresh_btn.set_relief(Gtk.ReliefStyle.NONE)
        refresh_btn.set_image(Gtk.Image.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.BUTTON))
        refresh_btn.connect("clicked", self.on_refresh_clicked)
        filler = Gtk.Box(); filler.set_hexpand(True)
        top.pack_start(title, False, False, 8)
        top.pack_start(filler, True, True, 0)
        top.pack_end(refresh_btn, False, False, 8)

        # left sidebar (Flatpost “category-panel” look)
        self.sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.sidebar.get_style_context().add_class("category-panel")
        self.sidebar.set_size_request(260, -1)

        def add_side_button(key, text):
            eb = Gtk.EventBox()
            lbl = Gtk.Label(label=text); lbl.set_xalign(0)
            lbl.get_style_context().add_class("category-button")
            eb.add(lbl)
            eb.connect("button-release-event", lambda *_: self.show_page(key))
            self.sidebar.pack_start(eb, False, False, 0)
            self.sidebar_rows[key] = lbl

        add_side_button("usage", "Usage Information")
        add_side_button("parts", "Auto-mount")
        add_side_button("notices", "External Mounts")

        # pages -> each page gets a “card”
        # --- Usage Information (merged from Notes + Configuration) ---
        usage_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, border_width=12)

        usage_card = card("Usage Information")
        # bullets (from Notes)
        for line in [
            "Partitions with auto-mount enabled will be mounted at user login.",
            "Nobara’s automount does NOT dynamically mount USB storage and SD cards when they are plugged in.",
            "Toggling enables auto-mount on login and also mounts/unmounts immediately.",
            "Only /dev/disk/by-uuid/ device paths are supported for stability across reboots.",
        ]:
            usage_card.pack_start(bullet(line), False, False, 0)

        # config details (from Configuration)
        usage_card.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 6)
        usage_card.pack_start(Gtk.Label(label="Configuration file used by Nobara automount:", xalign=0), False, False, 0)
        p = Gtk.Label(label=CONFIG_PATH, xalign=0); p.get_style_context().add_class("mono"); p.set_selectable(True)
        usage_card.pack_start(p, False, False, 0)

        usage_card.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 6)
        user = os.environ.get("SUDO_USER")
        usage_card.pack_start(Gtk.Label(label="Mount location template:", xalign=0), False, False, 0)
        m = Gtk.Label(label=f"/run/media/{user}/(UUID)", xalign=0); m.get_style_context().add_class("mono"); m.set_selectable(True)
        usage_card.pack_start(m, False, False, 0)

        usage_box.pack_start(usage_card, False, False, 0)
        self.stack.add_titled(usage_box, "usage", "Usage Information")


        # --- size groups for column alignment (create BEFORE header uses them) ---
        self.sg_auto_col  = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        self.sg_short_col = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)

        # Partitions
        parts_outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, border_width=12)
        parts_card  = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        parts_card.get_style_context().add_class("app-list-item")
        hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        title_lbl = Gtk.Label(label="Enable auto-mount on available drives")
        title_lbl.set_xalign(0)
        title_lbl.get_style_context().add_class("title-2")
        hdr.pack_start(title_lbl, False, False, 0)

        # flexible spacer pushes the right columns to the far edge
        hdr_filler = Gtk.Box()
        hdr_filler.set_hexpand(True)
        hdr.pack_start(hdr_filler, True, True, 0)

        hdr_automount = Gtk.Label(label="auto-mount")
        hdr_automount.get_style_context().add_class("dim-label")
        hdr_automount.set_xalign(1.0)

        hdr_auto_col = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        hdr_auto_col.set_halign(Gtk.Align.END)
        hdr_auto_col.set_hexpand(False)
        hdr_auto_col.pack_end(hdr_automount, False, False, 0)
        self.sg_auto_col.add_widget(hdr_auto_col)
        hdr.pack_start(hdr_auto_col, False, False, 0)

        # fixed gap between columns
        gap = Gtk.Box(); gap.set_size_request(16, -1)
        hdr.pack_start(gap, False, False, 0)

        # header "desktop shortcut"
        hdr_shortcut = Gtk.Label(label="desktop shortcut")
        hdr_shortcut.get_style_context().add_class("dim-label")
        hdr_shortcut.set_xalign(1.0)

        hdr_short_col = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        hdr_short_col.set_halign(Gtk.Align.END)
        hdr_short_col.set_hexpand(False)
        hdr_short_col.pack_end(hdr_shortcut, False, False, 0)
        self.sg_short_col.add_widget(hdr_short_col)
        hdr.pack_start(hdr_short_col, False, False, 0)

        parts_card.pack_start(hdr, False, False, 0)

        # Scroller + list
        sc = Gtk.ScrolledWindow()
        sc.set_hexpand(True)
        sc.set_vexpand(True)
        self.parts_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        sc.add(self.parts_list)
        parts_card.pack_start(sc, True, True, 0)

        parts_outer.pack_start(parts_card, True, True, 0)
        self.stack.add_titled(parts_outer, "parts", "Auto-mount")

        # Notices
        notice_outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, border_width=12)
        notice_card = card("Currently mounted by other processes")
        self.notices_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        notice_card.pack_start(self.notices_box, False, False, 0)
        notice_outer.pack_start(notice_card, False, False, 0)
        self.stack.add_titled(notice_outer, "notices", "External Mounts")

        # bottom status
        status_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        status_row.set_border_width(8)
        dim = Gtk.Label(label="STATUS:", xalign=0); dim.get_style_context().add_class("dim-label")
        status_row.pack_start(dim, False, False, 0)
        status_row.pack_start(self.status_label, True, True, 0)

        # main layout
        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        main.pack_start(top, False, True, 0)

        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        content.pack_start(self.sidebar, False, False, 0)
        content.pack_start(self.stack, True, True, 0)

        main.pack_start(content, True, True, 0)
        main.pack_end(status_row, False, False, 0)

        # ===== Overlay wiring (key change) =====
        self._overlay = Gtk.Overlay()
        self._overlay.add(main)  # main UI as the underlay

        self._busy_spinner = Gtk.Spinner()
        self._busy_spinner.set_halign(Gtk.Align.CENTER)
        self._busy_spinner.set_valign(Gtk.Align.CENTER)
        self._busy_spinner.set_size_request(48, 48)  # optional sizing
        self._busy_spinner.hide()  # hidden by default

        self._overlay.add_overlay(self._busy_spinner)
        self.add(self._overlay)   # add overlay (not 'main') to the window
        self.show_all()  # ensure overlay & children are realized/visible

        # perms & initial data
        if not check_wheel_group(self.status):  # exits if not wheel
            return
        self.refresh()
        GLib.idle_add(self.show_page, "parts")

        # Keep sidebar highlight in sync with stack page changes
        self.stack.connect("notify::visible-child-name", self.on_stack_changed)

    def _run_with_overlay(self, work_fn, finish_fn, min_show_ms: int = 250):
        """
        Show overlay, run work_fn() in a background thread (NO GTK inside),
        then call finish_fn(result, error) on GTK thread and hide overlay.
        Keeps the overlay visible for at least min_show_ms to ensure it paints.
        """
        self._show_busy_overlay()
        shown_at = time.monotonic()

        def _worker():
            result, error = None, None
            try:
                result = work_fn()
            except Exception as e:
                error = e

            def _finish():
                # ensure overlay stays visible at least min_show_ms
                elapsed = (time.monotonic() - shown_at) * 1000.0
                if elapsed < min_show_ms:
                    GLib.timeout_add(int(min_show_ms - elapsed), lambda: (self._hide_busy_overlay(), finish_fn(result, error), False)[2])
                    return False
                # normal path
                try:
                    self._hide_busy_overlay()
                finally:
                    try:
                        finish_fn(result, error)
                    except Exception:
                        pass
                return False

            GLib.idle_add(_finish)

        threading.Thread(target=_worker, daemon=True).start()

    def on_shortcut_state_set(self, partition, sw_auto, sw_short, requested_state: bool):
        """
        Desktop shortcut toggle. Only works if automount is active.
        Return True so we fully control the visual flip (no double-toggle).
        """
        try:
            # Guard: only allowed if automount is on
            if not sw_auto.get_active():
                self.status("Enable auto-mount first to use desktop shortcut.")
                with self._temporarily_block(sw_short):
                    sw_short.set_state(requested_state)
                sw_short.set_state(False)
                sw_short.set_sensitive(False)
                sw_short.queue_draw()
                with self._temporarily_block(sw_short):
                    sw_short.set_state(requested_state)
                return True  # stop default handler

            # Persist to config
            set_shortcut_enabled(partition, requested_state)
            self.status(f"{'Enabled' if requested_state else 'Disabled'} desktop shortcut for {partition}.")

            # Flip immediately (this is what fixes the "color needs refresh" issue)
            with self._temporarily_block(sw_short):
                sw_short.set_state(requested_state)
            sw_short.set_state(requested_state)
            sw_short.set_sensitive(True)  # keep enabled so 'on' style renders correctly
            sw_short.queue_draw()         # repaint now
            with self._temporarily_block(sw_short):
                sw_short.set_state(requested_state)

            return True  # prevent GTK's default toggle (we already did it)

        except Exception as e:
            self.status(f"Error: {e}")
            # Revert visually if something failed
            try:
                with self._temporarily_block(sw_short):
                    sw_short.set_state(requested_state)
                sw_short.set_state(not requested_state)
                sw_short.queue_draw()
            finally:
                with self._temporarily_block(sw_short):
                    sw_short.set_state(requested_state)
            return True


    # ------------- behavior -------------
    def _refresh_when_mounted(self, uuid, attempts=10, interval_ms=150):
        """Poll for the new mountpoint; refresh as soon as it exists."""
        def _tick(count):
            mp = get_mountpoint_for_uuid(uuid)
            if mp or count <= 0:
                self.refresh()
                return False  # stop polling
            GLib.timeout_add(interval_ms, _tick, count - 1)
            return False
        GLib.timeout_add(0, _tick, attempts)

    def on_stack_changed(self, *_):
        # whenever stack page changes, update the sidebar highlight
        name = self.stack.get_visible_child_name()
        if name:
            self.mark_active_sidebar(name)

    def mark_active_sidebar(self, key):
        for k, lbl in self.sidebar_rows.items():
            ctx = lbl.get_style_context()
            if k == key: ctx.add_class("category-button"); ctx.add_class("active")
            else:
                ctx.remove_class("active"); ctx.add_class("category-button")

    def show_page(self, key):
        self.stack.set_visible_child_name(key)
        self.mark_active_sidebar(key)

    def on_refresh_clicked(self, *_):
        self.refresh()
        self.status("Partition lists refreshed.")

    def refresh(self):
        # clear lists
        for c in list(self.parts_list.get_children()):
            self.parts_list.remove(c)
        for c in list(self.notices_box.get_children()):
            self.notices_box.remove(c)

        self.enabled = read_enabled_partitions()
        unmounted, mounted, luks_locked = get_partitions()   # <-- now 3 lists
        self.shortcuts_enabled = read_shortcut_partitions()

        # ----------------------------------------------------
        # 1) Locked LUKS section (Unlock button)
        # ----------------------------------------------------
        for devpath, luks_uuid, size_str, model_str in luks_locked:
            row  = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            col  = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)

            title = Gtk.Label(label=f"{devpath} (encrypted)", xalign=0)
            sub1  = Gtk.Label(label=f"Size: {size_str}", xalign=0);  sub1.get_style_context().add_class("dim-label")
            clean_model = " ".join((model_str or "").replace("_"," ").split()) or "Unknown"
            sub2  = Gtk.Label(label=f"Model: {clean_model}", xalign=0); sub2.get_style_context().add_class("dim-label")

            col.pack_start(title, False, False, 0)
            col.pack_start(sub1,   False, False, 0)
            col.pack_start(sub2,   False, False, 0)
            row.pack_start(col, True, True, 0)

            btn = Gtk.Button.new_with_label("Unlock")
            btn.set_valign(Gtk.Align.START)
            btn.set_halign(Gtk.Align.END)
            btn.get_style_context().add_class("suggested-action")

            def _on_unlock(_btn, _uuid=luks_uuid):
                # Helper: find OUTER device NAME from UUID (non-crypt row with this UUID)
                def _outer_name_from_uuid(u):
                    try:
                        out = subprocess.run(
                            ["lsblk", "-P", "-o", "NAME,TYPE,UUID,PKNAME"],
                            text=True, capture_output=True, check=False
                        ).stdout
                        for line in out.splitlines():
                            fields = {}
                            for tok in shlex.split(line):
                                if "=" in tok:
                                    k, v = tok.split("=", 1)
                                    fields[k] = v.strip('"')
                        # pick row where UUID matches and TYPE != crypt (i.e., the physical outer)
                            if (fields.get("UUID") or "") == u and (fields.get("TYPE") or "").lower() != "crypt":
                                return fields.get("NAME") or ""
                    except Exception:
                        pass
                    return ""

                # Helper: is the outer already unlocked? (has TYPE=crypt child with PKNAME==outer_name)
                def _outer_has_crypt_child(outer_name: str) -> bool:
                    if not outer_name:
                        return False
                    try:
                        out = subprocess.run(
                            ["lsblk", "-P", "-o", "NAME,TYPE,PKNAME"],
                            text=True, capture_output=True, check=False
                        ).stdout
                        for line in out.splitlines():
                            fields = {}
                            for tok in shlex.split(line):
                                if "=" in tok:
                                    k, v = tok.split("=", 1)
                                    fields[k] = v.strip('"')
                            if (fields.get("TYPE") or "").lower() == "crypt" and (fields.get("PKNAME") or "") == outer_name:
                                return True
                    except Exception:
                        pass
                    return False

                outer_name = _outer_name_from_uuid(_uuid)
                mapper_base = f"luks-{_uuid}"

                # Robust "already unlocked?" check — ONLY trust the PKNAME relationship.
                if _outer_has_crypt_child(outer_name):
                    self.status("Already unlocked.")
                    self.refresh()
                    return

                # If /dev/mapper/luks-<uuid> exists but isn't our child, it's a name collision.
                mapper = mapper_base
                if os.path.exists(f"/dev/mapper/{mapper}") and not _outer_has_crypt_child(outer_name):
                    # choose a unique fallback mapper name
                    mapper = f"{mapper_base}-{os.getpid()}"

                pw = prompt_password(self, title="Unlock encrypted drive")
                if not pw:
                    self.status("Unlock cancelled.")
                    return

                self._show_busy_overlay()
                try:
                    outer_symlink = f"/dev/disk/by-uuid/{_uuid}"
                    proc = subprocess.run(
                        ["cryptsetup", "luksOpen", outer_symlink, mapper],
                        input=pw.encode("utf-8"),
                        check=False
                    )
                    if proc.returncode != 0:
                        self.status("Unlock failed (bad passphrase or device).")
                        return

                    # Let udev settle so lsblk sees the mapper and inner FS
                    subprocess.run(["udevadm", "settle", "-t", "5"], check=False)
                    outer_uuid = _uuid  # from the closure
                    # store passphrase in-memory for 10 minutes

                    if pw and outer_uuid:
                        self._cache_luks_pass(outer_uuid, pw)
                    # Post-verify with the ONLY source of truth we trust: PKNAME relation
                    if not _outer_has_crypt_child(outer_name):
                        self.status("Unlocked, but mapping not visible yet — try Refresh.")
                    else:
                        self.status("Unlocked.")
                    self.refresh()
                finally:
                    self._hide_busy_overlay()


            btn.connect("clicked", _on_unlock)
            row.pack_end(btn, False, False, 0)

            # separator + row
            self.parts_list.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, True, 4)
            self.parts_list.pack_start(row, False, False, 0)

        # ----------------------------------------------------
        # 2) Auto-mount list = ONLY UNMOUNTED
        #    (includes unlocked-but-not-mounted LUKS mapper)
        # ----------------------------------------------------
        # Normalize to (path, fstype, uuid, size, model) for display
        # normalize to (path, fstype, uuid, size, model) for display
        mounted_by_uuid = {}
        for partition, fstype_m, uuid_m, mnt_m, size_m, model_m in mounted:
            mounted_by_uuid[uuid_m] = mnt_m

        # Normalize entries to (path, fstype, uuid, size, model)
        to_display = list(unmounted)

        # Add mounted items that are enabled (but avoid duplicates)
        enabled_set = set(self.enabled)
        already = {p for (p, *_rest) in to_display}
        for partition, fstype_m, uuid_m, mnt_m, size_m, model_m in mounted:
            if partition in enabled_set and partition not in already:
                to_display.append((partition, fstype_m, uuid_m, size_m, model_m))

        # Sort: keep enabled ones first (same as before), then by path
        to_display.sort(key=lambda t: (t[0] not in enabled_set, t[0]))

        if to_display:
            self.partition_info = {}
            for partition, fstype, uuid, size_str, model_str in to_display:
                self.partition_info[partition] = {
                    "fstype": fstype,
                    "uuid": uuid,
                    "size": size_str,
                    "model": model_str,
                }
                if fstype == "ntfs":
                    fstype = "lowntfs-3g"

                sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
                self.parts_list.pack_start(sep, False, False, 0)
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
                title = Gtk.Label(label=partition, xalign=0)

                sub_fs = Gtk.Label()
                sub_fs.set_use_markup(True)
                sub_fs.set_xalign(0)
                sub_fs.set_line_wrap(True)
                sub_fs.set_markup(
                    f"Filesystem: <span foreground='#ccc'>{fstype}</span>\nSize: <span foreground='#ccc'>{size_str}</span>"
                )
                sub_fs.get_style_context().add_class("dim-label")

                sub_model = Gtk.Label(label=f"Model: {model_str}", xalign=0)
                sub_model.get_style_context().add_class("dim-label")

                sub_model = Gtk.Label()
                sub_model.set_use_markup(True)
                sub_model.set_xalign(0)
                sub_model.set_line_wrap(True)
                sub_model.set_markup(
                    f"Model: <span foreground='#ccc'>{model_str}</span>"
                )
                sub_model.get_style_context().add_class("dim-label")

                col.pack_start(title, False, False, 0)
                col.pack_start(sub_fs, False, False, 0)
                col.pack_start(sub_model, False, False, 0)

                # switches on the right: auto-mount then desktop shortcut
                sw_auto = Gtk.Switch()
                sw_auto.set_active(partition in self.enabled)
                sw_auto.set_valign(Gtk.Align.START)
                sw_auto.set_halign(Gtk.Align.END)
                sw_auto.set_hexpand(False)

                auto_col = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
                auto_col.set_valign(Gtk.Align.START)
                auto_col.set_halign(Gtk.Align.END)
                auto_col.set_hexpand(False)
                auto_col.pack_end(sw_auto, False, False, 0)
                self.sg_auto_col.add_widget(auto_col)   # <- group the column container, not the switch

                # desktop shortcut switch
                sw_short = Gtk.Switch()
                short_active = ((partition + "\n") in self.shortcuts_enabled) or (partition in self.shortcuts_enabled)
                sw_short.set_active(short_active)
                sw_short.set_valign(Gtk.Align.START)
                sw_short.set_halign(Gtk.Align.END)
                sw_short.set_hexpand(False)
                sw_short.set_sensitive(sw_auto.get_active())

                short_col = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
                short_col.set_valign(Gtk.Align.START)
                short_col.set_halign(Gtk.Align.END)
                short_col.set_hexpand(False)
                short_col.pack_end(sw_short, False, False, 0)
                self.sg_short_col.add_widget(short_col)  # <- same idea here

                # wire handlers (unchanged)
                hid_auto = sw_auto.connect("state-set", partial(self.on_switch_state_set, partition, sw_short))
                sw_auto._state_set_hid = hid_auto
                hid_short = sw_short.connect("state-set", partial(self.on_shortcut_state_set, partition, sw_auto))
                sw_short._state_set_hid = hid_short

                # ---- packing: left details expand, then spacer, then the two column boxes ----
                row.pack_start(col, True, True, 0)

                spacer = Gtk.Box(); spacer.set_hexpand(True)
                row.pack_start(spacer, True, True, 0)

                row.pack_start(auto_col,  False, False, 0)
                gap = Gtk.Box(); gap.set_size_request(16, -1)
                row.pack_start(gap, False, False, 0)
                row.pack_start(short_col, False, False, 0)

                self.parts_list.pack_start(row, False, False, 0)
        else:
            # Only show fallback if *also* no locked LUKS shown
            if not luks_locked:
                self.parts_list.pack_start(
                    Gtk.Label(label="No eligible partitions found.", xalign=0),
                    False, False, 0
                )

        # ----------------------------------------------------
        # 3) External Mounts (unchanged)
        # ----------------------------------------------------
        any_notice = False
        for partition, fstype, _uuid, mnt, size_str, model_str in mounted:
            if partition in self.enabled:
                continue
            any_notice = True
            sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            self.notices_box.pack_start(sep, False, True, 6)
            row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            title = Gtk.Label(label=partition, xalign=0)
            fs_sz = Gtk.Label(label=f"Filesystem: {fstype} • Size: {size_str}", xalign=0)
            fs_sz.get_style_context().add_class("dim-label")
            clean_model = " ".join((model_str or "").replace("_"," ").split()) or "Unknown"
            model_lbl = Gtk.Label(label=f"Model: {clean_model}", xalign=0)
            model_lbl.get_style_context().add_class("dim-label")
            mp_lbl = Gtk.Label(label=f"Mounted at: {mnt}", xalign=0)
            mp_lbl.get_style_context().add_class("dim-label")
            row.pack_start(title, False, False, 0)
            row.pack_start(fs_sz, False, False, 0)
            row.pack_start(model_lbl, False, False, 0)
            row.pack_start(mp_lbl, False, False, 0)
            self.notices_box.pack_start(row, False, False, 6)

        if not any_notice:
            self.notices_box.pack_start(Gtk.Label(label="No conflicts detected.", xalign=0), False, False, 0)

        self.parts_list.show_all()
        self.notices_box.show_all()


    def _show_busy_overlay(self):
        self._busy_spinner.show()
        self._busy_spinner.start()
        # let the overlay paint a frame before blocking
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)

    def _hide_busy_overlay(self):
        try:
            self._busy_spinner.stop()
            self._busy_spinner.hide()
        except Exception:
            pass

    def status(self, text):
        self.status_label.set_text(text)

    def _cache_luks_pass(self, outer_uuid: str, pw: str):
        # always use monotonic; never mix with time.time()
        self._luks_pw_cache[outer_uuid] = (pw, time.monotonic() + _LUKS_PW_TTL)

    def _get_cached_luks_pass(self, outer_uuid: str):
        tup = self._luks_pw_cache.get(outer_uuid)
        if not tup:
            return None
        pw, exp = tup
        if time.monotonic() < exp:
            return pw
        # expired
        try:
            self._luks_pw_cache.pop(outer_uuid, None)
        except Exception:
            pass
        return None

    def _handler_ok(self, widget, hid_attr="_state_set_hid"):
        hid = getattr(widget, hid_attr, None)
        if hid is None:
            return None
        try:
            if not hasattr(widget, "handler_is_connected") or not widget.handler_is_connected(hid):
                return None
            return hid
        except Exception:
            return None

    def _handler_is_blocked(self, widget, hid):
        try:
            return hasattr(widget, "handler_is_blocked") and widget.handler_is_blocked(hid)
        except Exception:
            return False

    def _block_handler(self, widget, hid_attr="_state_set_hid"):
        hid = self._handler_ok(widget, hid_attr)
        if hid is None:
            return None
        if not self._handler_is_blocked(widget, hid):
            try:
                widget.handler_block(hid)
            except Exception:
                return None
        return hid

    def _unblock_handler(self, widget, hid_attr="_state_set_hid"):
        hid = self._handler_ok(widget, hid_attr)
        if hid is None:
            return
        if self._handler_is_blocked(widget, hid):
            try:
                widget.handler_unblock(hid)
            except Exception:
                pass

    @contextmanager
    def _temporarily_block(self, widget, hid_attr="_state_set_hid"):
        hid = self._block_handler(widget, hid_attr)
        try:
            yield
        finally:
            # only unblock if we actually blocked it here
            if hid is not None:
                self._unblock_handler(widget, hid_attr)

    def on_switch_state_set(self, partition, sw_short, switch, requested_state: bool):
        if partition in self._inflight:
            return True
        self._inflight.add(partition)

        def _safe_unblock(widget, hid_attr="_state_set_hid"):
            hid = getattr(widget, hid_attr, None)
            if hid is None:
                return
            try:
                if hasattr(widget, "handler_is_connected") and widget.handler_is_connected(hid):
                    widget.handler_unblock(hid)
            except Exception:
                pass

        uuid = partition.rsplit("/", 1)[-1]
        sudo_user = os.environ.get("SUDO_USER") or getpass.getuser()
        info = self.partition_info.get(partition, {}) or {}
        fstype = info.get("fstype", "")
        mountpoint = f"/run/media/{sudo_user}/{uuid}"

        # instance name (cheap)
        instance = subprocess.run(
            ["systemd-escape", "-p", "--suffix=mount", mountpoint],
            check=True, text=True, capture_output=True
        ).stdout.strip()

        # ---------------- worker: NO GTK HERE ----------------
        def work():
            result = {
                "ok": False,
                "messages": [],
                "requested_state": requested_state,
                "shortcut_on": False,
                "needs_crypttab": None,   # dict when we must prompt later
                "crypttab_ready": False,  # true after crypttab ensured
                "instance": instance,
                "partition": partition,
                "uuid": uuid,
                "sudo_user": sudo_user,
            }

            if requested_state:
                # compute opts + write env (cheap)
                rw_uid = pwd.getpwnam(sudo_user).pw_uid
                rw_gid = grp.getgrnam(sudo_user).gr_gid if hasattr(grp, "getgrnam") else rw_uid
                fstype_out, opts = compute_mount_opts(uuid, fstype, rw_uid, rw_gid, sudo_user)
                ensure_env_file(instance, uuid, sudo_user, fstype_out, opts)

                # detect LUKS parent and try to ensure crypttab with cached pw (no prompt here)
                try:
                    out = subprocess.run(
                        ["lsblk", "-P", "-o", "NAME,TYPE,UUID,PKNAME"],
                        text=True, capture_output=True, check=False
                    ).stdout
                    rows = []
                    for line in out.splitlines():
                        if not line.strip(): continue
                        fields = {}
                        for tok in shlex.split(line):
                            if "=" in tok:
                                k, v = tok.split("=", 1)
                                fields[k] = v.strip('"')
                        rows.append(fields)

                    child = next((r for r in rows if (r.get("UUID") or "") == uuid), None)
                    need_pw = None
                    crypttab_ready = True  # assume non-LUKS by default

                    if child:
                        mapper_name = child.get("PKNAME") or ""
                        if (child.get("TYPE") or "").lower() == "crypt":
                            mapper_name = child.get("NAME") or mapper_name
                        crypt_row = next((r for r in rows
                                        if (r.get("NAME") or "") == mapper_name and (r.get("TYPE") or "").lower() == "crypt"), None)
                        if crypt_row:
                            outer_name = crypt_row.get("PKNAME") or ""
                            unlocked = any((r.get("TYPE") or "").lower() == "crypt" and (r.get("PKNAME") or "") == outer_name
                                        for r in rows)
                            if outer_name and unlocked:
                                outer_uuid = subprocess.run(
                                    ["blkid", "-o", "value", "-s", "UUID", f"/dev/{outer_name}"],
                                    text=True, capture_output=True, check=False
                                ).stdout.strip()

                                have_crypttab = False
                                try:
                                    with open("/etc/crypttab", "r", encoding="utf-8") as f:
                                        have_crypttab = outer_uuid and any(f"UUID={outer_uuid}" in line for line in f)
                                except FileNotFoundError:
                                    have_crypttab = False

                                if outer_uuid and not have_crypttab:
                                    # need to add a key and write crypttab
                                    keydir  = "/etc/cryptsetup-keys.d"
                                    keyfile = f"{keydir}/{outer_uuid}.key"
                                    dev     = f"/dev/disk/by-uuid/{outer_uuid}"
                                    mapper  = f"luks-{outer_uuid}"

                                    if not os.path.exists(keyfile):
                                        os.makedirs(keydir, exist_ok=True)
                                        with open(keyfile, "wb") as kf: pass
                                        os.chmod(keyfile, 0o400); os.chown(keyfile, 0, 0)
                                        with open("/dev/urandom", "rb") as r, open(keyfile, "r+b") as kf:
                                            kf.write(r.read(256))
                                        subprocess.run(["restorecon", "-v", keyfile], check=False,
                                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                                    used_cached = False
                                    pw_cached = self._get_cached_luks_pass(outer_uuid)
                                    if pw_cached:
                                        proc = subprocess.run(
                                            ["cryptsetup", "luksAddKey", "--key-file=-", dev, keyfile],
                                            input=pw_cached.encode("utf-8"),
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                                        )
                                        if proc.returncode == 0:
                                            used_cached = True

                                    if used_cached:
                                        # write crypttab now
                                        line = f"{mapper}\tUUID={outer_uuid}\t{keyfile}\tluks,discard\n"
                                        try:
                                            try:
                                                with open("/etc/crypttab", "r", encoding="utf-8") as f:
                                                    lines = f.readlines()
                                            except FileNotFoundError:
                                                lines = []
                                            new_lines = [l for l in lines if (f"UUID={outer_uuid}" not in l and not l.strip().startswith(mapper))]
                                            new_lines.append(line)
                                            with open("/etc/crypttab", "w", encoding="utf-8") as f:
                                                f.writelines(new_lines)
                                            subprocess.run(["systemctl", "daemon-reload"],
                                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                                            result["messages"].append("Auto-unlock set up. This drive will unlock at boot.")
                                            crypttab_ready = True
                                        except Exception as e:
                                            result["messages"].append(f"Failed to update /etc/crypttab: {e}")
                                            crypttab_ready = False
                                    else:
                                        # must prompt later; not ready yet
                                        need_pw = {"outer_uuid": outer_uuid, "dev": dev, "keyfile": keyfile, "mapper": mapper}
                                        crypttab_ready = False
                            # if there is a crypt mapper but it's locked (no unlocked child), we still need a key
                            elif crypt_row:
                                # treat as requiring prompt
                                need_pw = {"outer_uuid": None, "dev": None, "keyfile": None, "mapper": None}
                                crypttab_ready = False

                    result["crypttab_ready"] = crypttab_ready
                    result["needs_crypttab"] = need_pw
                except Exception as e:
                    result["messages"].append(f"Auto-unlock setup skipped: {e}")
                    result["crypttab_ready"] = True  # don’t block the user if detection failed

                # Only proceed to enable/start and mark enabled when crypttab is ready
                if result["crypttab_ready"]:
                    # mark enabled now (persist)
                    set_partition_enabled(partition, True)
                    subprocess.run(["systemctl", "add-wants", instance, f"nobara-automount@{instance}.service"],
                                check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    subprocess.run(["systemctl", "enable", f"nobara-automount@{instance}.service"],
                                check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    subprocess.run(["systemctl", "start", f"nobara-automount@{instance}.service"],
                                check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    result["shortcut_on"] = True
                    result["messages"].append(f"Enabled automount for {partition}.")
                else:
                    # not flipping yet; finish() will prompt and complete or cancel
                    result["messages"].append("Waiting for auto-unlock configuration…")
            else:
                # DISABLE path
                # mark disabled first (persist)
                set_partition_enabled(partition, False)

                subprocess.run(["systemctl", "stop", f"nobara-automount@{instance}.service"],
                            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(["systemctl", "disable", f"nobara-automount@{instance}.service"],
                            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(["systemctl", "remove-wants", instance, f"nobara-automount@{instance}.service"],
                            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                try:
                    os.remove(f"/etc/nobara/automount/run/media/{sudo_user}/{uuid}.mount.env")
                except FileNotFoundError:
                    pass

                # auto-unlock teardown (unchanged)
                try:
                    out = subprocess.run(
                        ["lsblk", "-P", "-o", "NAME,TYPE,UUID,PKNAME"],
                        text=True, capture_output=True, check=False
                    ).stdout
                    rows = []
                    for line in out.splitlines():
                        if not line.strip(): continue
                        f = {}
                        for tok in shlex.split(line):
                            if "=" in tok:
                                k, v = tok.split("=", 1)
                                f[k] = v.strip('"')
                        rows.append(f)

                    def _outer_for_inner(_uuid):
                        child = next((r for r in rows if (r.get("UUID") or "") == _uuid), None)
                        if not child: return "", ""
                        mapper_name = child.get("PKNAME") or ""
                        if (child.get("TYPE") or "").lower() == "crypt":
                            mapper_name = child.get("NAME") or mapper_name
                        crypt_row = next((r for r in rows
                                        if (r.get("NAME") or "") == mapper_name and (r.get("TYPE") or "").lower() == "crypt"), None)
                        if not crypt_row: return "", ""
                        outer_name = crypt_row.get("PKNAME") or ""
                        outer_uuid2 = subprocess.run(
                            ["blkid", "-o", "value", "-s", "UUID", f"/dev/{outer_name}"],
                            text=True, capture_output=True, check=False
                        ).stdout.strip() if outer_name else ""
                        if outer_uuid2 and pw:
                            self._cache_luks_pass(outer_uuid2, pw)
                        return outer_name, outer_uuid2

                    remaining_enabled = set(read_enabled_partitions())
                    if partition in remaining_enabled:
                        remaining_enabled.discard(partition)

                    _, disabled_outer_uuid = _outer_for_inner(uuid)

                    keep = False
                    if disabled_outer_uuid:
                        for p in list(remaining_enabled):
                            other_uuid = p.rsplit("/", 1)[-1]
                            _, ou = _outer_for_inner(other_uuid)
                            if ou and ou == disabled_outer_uuid:
                                keep = True
                                break

                    if disabled_outer_uuid and not keep:
                        keyfile = f"/etc/cryptsetup-keys.d/{disabled_outer_uuid}.key"
                        dev2 = f"/dev/disk/by-uuid/{disabled_outer_uuid}"

                        try:
                            try:
                                with open("/etc/crypttab", "r", encoding="utf-8") as f:
                                    lines = f.readlines()
                            except FileNotFoundError:
                                lines = []
                            new_lines = [l for l in lines if f"UUID={disabled_outer_uuid}" not in l]
                            if new_lines != lines:
                                with open("/etc/crypttab", "w", encoding="utf-8") as f:
                                    f.writelines(new_lines)
                        except Exception:
                            pass

                        if os.path.exists(keyfile):
                            subprocess.run(["cryptsetup", "luksRemoveKey", dev2, keyfile],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                            try: os.remove(keyfile)
                            except Exception: pass

                        subprocess.run(["systemctl", "daemon-reload"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        result["messages"].append("Auto-unlock disabled for this encrypted drive.")
                    elif keep:
                        result["messages"].append("Auto-unlock kept (other enabled partitions use the same encrypted parent).")
                except Exception as e:
                    result["messages"].append(f"Auto-unlock teardown skipped: {e}")

                try:
                    set_shortcut_enabled(partition, False)
                except Exception:
                    pass

                result["shortcut_on"] = False
                result["messages"].append(f"Disabled automount for {partition}.")

            result["ok"] = True
            return result
        # ---------------- end work() ----------------

        def finish(result, error):
            try:
                if error:
                    self.status(f"Error: {error}")
                    return

                for m in result.get("messages", []):
                    self.status(m)

                # If enabling and crypttab not ready yet, prompt now; ONLY flip on success
                need = result.get("needs_crypttab")
                if requested_state and need:
                    # Hide spinner so the prompt is usable
                    self._hide_busy_overlay()
                    pw = prompt_password(self, title="Enable auto-unlock for this encrypted drive?")
                    if pw:
                        # Run the *post-prompt* steps in a background thread and show overlay
                        instance2 = result["instance"]
                        def post_prompt_work():
                            msgs = []
                            # 1) Add the key using the provided passphrase
                            proc = subprocess.run(
                                ["cryptsetup", "luksAddKey", "--key-file=-", need["dev"], need["keyfile"]],
                                input=pw.encode("utf-8"),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                            )
                            if proc.returncode != 0:
                                return {"ok": False, "messages": ["Could not add auto-unlock key (wrong passphrase or policy)."]}

                            # 2) Write/ensure crypttab
                            line = f"{need['mapper']}\tUUID={need['dev'].split('/')[-1]}\t{need['keyfile']}\tluks,discard\n"
                            try:
                                try:
                                    with open("/etc/crypttab", "r", encoding="utf-8") as f:
                                        lines = f.readlines()
                                except FileNotFoundError:
                                    lines = []
                                new_lines = [l for l in lines if need["keyfile"] not in l and not l.strip().startswith(need["mapper"])]
                                new_lines.append(line)
                                with open("/etc/crypttab", "w", encoding="utf-8") as f:
                                    f.writelines(new_lines)
                                subprocess.run(["systemctl", "daemon-reload"],
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                                msgs.append("Auto-unlock set up. This drive will unlock at boot.")
                            except Exception as e:
                                return {"ok": False, "messages": [f"Failed to update /etc/crypttab: {e}"]}

                            # 3) Persist enable + start unit
                            set_partition_enabled(partition, True)
                            subprocess.run(["systemctl", "add-wants", instance2, f"nobara-automount@{instance2}.service"],
                                        check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                            subprocess.run(["systemctl", "enable", f"nobara-automount@{instance2}.service"],
                                        check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                            subprocess.run(["systemctl", "start", f"nobara-automount@{instance2}.service"],
                                        check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                            msgs.append(f"Enabled automount for {partition}.")
                            return {"ok": True, "messages": msgs}

                        def post_prompt_finish(res, err):
                            # back on GTK thread
                            for m in (res or {}).get("messages", []):
                                self.status(m)
                            ok = (res or {}).get("ok", False) and not err
                            # Flip toggles only on success
                            with self._temporarily_block(switch):
                                switch.set_state(True)
                            # auto-enable the shortcut as well
                            try:
                                set_shortcut_enabled(partition, True)
                            except Exception:
                                pass
                            with self._temporarily_block(sw_short):
                                sw_short.set_state(True)
                            sw_short.set_sensitive(True)
                            sw_short.queue_draw()

                            # Refresh rows
                            GLib.idle_add(self.refresh)

                        # show overlay during the work
                        self._run_with_overlay(post_prompt_work, post_prompt_finish, min_show_ms=250)
                        return  # we’re done handling the prompted path here

                    else:
                        # user cancelled → keep OFF, do not enable/start
                        self.status("Unlock failed for auto-mounting encrypted disk. User cancelled or password invalid.")
                        with self._temporarily_block(switch):
                                switch.set_state(True)
                        try:
                            sw_short.set_sensitive(False)
                            with self._temporarily_block(sw_short):
                                sw_short.set_state(result.get("shortcut_on", False))
                            sw_short.set_state(False)
                            sw_short.queue_draw()
                        except Exception:
                            pass
                        GLib.idle_add(self.refresh)
                        return

                else:
                    # No prompt needed → flip according to crypttab_ready
                    with self._temporarily_block(switch):
                            switch.set_state(True)
                    if result.get("requested_state") and result.get("crypttab_ready"):
                        try:
                            set_shortcut_enabled(partition, True)
                        except Exception:
                            pass
                        with self._temporarily_block(sw_short):
                            sw_short.set_state(True)
                        sw_short.set_sensitive(True)
                        sw_short.queue_draw()

                GLib.idle_add(self.refresh)
            finally:
                def _reenable():
                    try:
                        switch.set_sensitive(True)
                    finally:
                        self._inflight.discard(partition)
                    return False
                GLib.timeout_add(self._cooldown_ms, _reenable)

        # run it (shows overlay inside, with min-show)
        self._run_with_overlay(work, finish, min_show_ms=250)
        return True  # we’ll set the visual state ourselves


# -----------------------------
# Entrypoint
# -----------------------------
def main():
    relaunch_with_pkexec()
    init_theme_and_css()
    win = MainWindow()
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    Gtk.main()

if __name__ == "__main__":
    try:
        main()
    finally:
        cleanup_xhost()
