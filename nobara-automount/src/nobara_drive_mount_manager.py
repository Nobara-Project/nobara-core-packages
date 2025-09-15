#!/usr/bin/python3
# -*- coding: utf-8 -*-

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, GLib, Gdk

import subprocess, os, grp, getpass, sys, textwrap, pwd, grp, shlex, threading, tempfile, shutil
from pathlib import Path
from functools import partial
import configparser

APP_TITLE = "Nobara Drive Mount Manager"
CONFIG_PATH = "/etc/nobara/automount/enabled.conf"

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
      unmounted: (path, fstype, uuid, size_str, model_str)
      mounted:   (path, fstype, uuid, mountpoint, size_str, model_str)
    """
    try:
        # -P for key="value" (handles spaces), -b for bytes
        out = subprocess.run(
            ['lsblk', '-b', '-P', '-o', 'NAME,PKNAME,UUID,FSTYPE,SIZE,MODEL,MOUNTPOINT'],
            capture_output=True, text=True, check=True
        ).stdout

        rows = []
        for line in out.splitlines():
            if not line.strip():
                continue
            fields = {}
            for tok in shlex.split(line):
                if '=' in tok:
                    k, v = tok.split('=', 1)
                    fields[k] = v.strip('"')
            rows.append(fields)

        # Build a map: NAME -> MODEL for parent devices
        model_by_name = {}
        for f in rows:
            name  = f.get('NAME') or ''
            model = (f.get('MODEL') or '').strip()
            if model:
                model_by_name[name] = model

        unmounted, mounted = [], []

        for f in rows:
            name       = f.get('NAME') or ''
            pkname     = f.get('PKNAME') or ''
            uuid       = f.get('UUID') or ''
            fstype     = (f.get('FSTYPE') or '').lower()
            size_bytes = f.get('SIZE') or '0'
            mnt        = f.get('MOUNTPOINT') or ''
            model      = (f.get('MODEL') or '').strip()

            if not _is_partition_name(name):
                continue
            if not uuid:
                continue
            if fstype and fstype not in ALLOWED_FS:
                continue

            # Inherit model from parent if missing
            if not model:
                model = model_by_name.get(pkname, '') or model_by_name.get(name, '')
            model_str = model if model else "Unknown"

            size_str = human_size(size_bytes)
            devpath  = f"/dev/disk/by-uuid/{uuid}"

            if mnt:
                mounted.append((devpath, fstype, uuid, mnt, size_str, model_str))
            else:
                unmounted.append((devpath, fstype, uuid, size_str, model_str))

        return unmounted, mounted

    except Exception as e:
        try:
            self.status(f"Failed to scan partitions: {e}")
        except Exception:
            pass
        return [], []

    except Exception as e:
        # If you have a status() method, surface the error; otherwise silently return empties.
        try:
            self.status(f"Failed to scan partitions: {e}")
        except Exception:
            pass
        return [], []

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
        fs_out = "ntfs-3g"
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

        self.status_label = Gtk.Label(xalign=0)
        self.set_icon_name("drive-harddisk")
        self.sidebar_rows = {}
        self.enabled = set()

        self._inflight = set()          # partitions currently being changed
        self._cooldown_ms = 1500        # adjust to taste (1.5s)

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

        # main stack
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_hexpand(True); self.stack.set_vexpand(True)

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

        # Partitions
        parts_outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, border_width=12)
        parts_card = card("Enable auto-mount on available drives")
        sc = Gtk.ScrolledWindow(); sc.set_hexpand(True); sc.set_vexpand(True)
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
        self.add(main)

        # perms & initial data
        if not check_wheel_group(self.status):  # exits if not wheel
            return
        self.refresh()
        GLib.idle_add(self.show_page, "parts")

        # Keep sidebar highlight in sync with stack page changes
        self.stack.connect("notify::visible-child-name", self.on_stack_changed)

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
        unmounted, mounted = get_partitions()
        mp_by_uuid = {uuid: mnt for (_p, _fs, uuid, mnt, _sz, _model) in mounted}

        # normalize to (path, fstype, uuid, size, model) for display
        to_display = list(unmounted) + [
            (p, fs, uuid, sz, model)
            for (p, fs, uuid, _mnt, sz, model) in mounted
            if p in self.enabled
        ]
        to_display.sort(key=lambda t: (t[0] not in self.enabled, t[0]))
        if to_display:
            self.partition_info = {}
            for partition, fstype, uuid, size_str, model_str in to_display:
                self.partition_info[partition] = {
                    "fstype": fstype,
                    "uuid": uuid,
                    "size": size_str,
                    "model": model_str,
                }
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)

                title = Gtk.Label(label=partition, xalign=0)

                sub_fs = Gtk.Label(label=f"Filesystem: {fstype} • Size: {size_str}", xalign=0)
                sub_fs.get_style_context().add_class("dim-label")

                sub_model = Gtk.Label(label=f"Model: {model_str}", xalign=0)
                sub_model.get_style_context().add_class("dim-label")

                col.pack_start(title, False, False, 0)
                col.pack_start(sub_fs, False, False, 0)
                col.pack_start(sub_model, False, False, 0)

                if uuid in mp_by_uuid:
                    mountpoint = mp_by_uuid[uuid]
                    sub_mp = Gtk.Label(label=f"Mount location: {mp_by_uuid[uuid]} (mounted)", xalign=0)
                    sub_mp.get_style_context().add_class("dim-label")
                    col.pack_start(sub_mp, False, False, 0)
                    # add Name= from any desktop shortcut that points to this mount
                    desktop_name = find_desktop_name_for_mount(mountpoint)
                    if desktop_name:
                        sub_dn = Gtk.Label(label=f"Desktop shortcut: {desktop_name}", xalign=0)
                        sub_dn.get_style_context().add_class("dim-label")
                        col.pack_start(sub_dn, False, False, 0)

                sw = Gtk.Switch()
                sw.set_active(partition in self.enabled)
                sw.set_size_request(-1, 26)
                sw.set_valign(Gtk.Align.CENTER)
                sw.set_halign(Gtk.Align.CENTER)
                sw.set_hexpand(False); sw.set_vexpand(False)
                hid = sw.connect("state-set", partial(self.on_switch_state_set, partition))
                sw._state_set_hid = hid

                row.pack_start(col, True, True, 0)
                # Add a separator line right after the row
                sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
                self.parts_list.pack_start(sep, False, True, 4)  # extra spacing of 4px
                row.pack_end(sw, False, False, 0)
                self.parts_list.pack_start(row, False, False, 0)
        else:
            self.parts_list.pack_start(Gtk.Label(label="No eligible partitions found.", xalign=0), False, False, 0)


        any_notice = False
        # mounted tuples are: (path, fstype, uuid, mnt, size_str, model_str)
        for partition, fstype, _uuid, mnt, size_str, model_str in mounted:
            if partition in self.enabled:
                continue  # skip ones managed by automount

            any_notice = True

            # separator before each entry
            sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            self.notices_box.pack_start(sep, False, True, 6)

            row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)

            # Title (path)
            title = Gtk.Label(label=partition, xalign=0)

            # FS + Size
            fs_sz = Gtk.Label(label=f"Filesystem: {fstype} • Size: {size_str}", xalign=0)
            fs_sz.get_style_context().add_class("dim-label")

            # Model
            model_lbl = Gtk.Label(label=f"Model: {model_str}", xalign=0)
            model_lbl.get_style_context().add_class("dim-label")

            # Mountpoint
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

    def _show_busy(self, text="Applying changes, please wait…"):
        dlg = Gtk.Dialog(
            title=None, transient_for=self, modal=True, destroy_with_parent=True,
            flags=Gtk.DialogFlags.MODAL
        )
        box = dlg.get_content_area()
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        spinner = Gtk.Spinner(); spinner.start()
        label = Gtk.Label(label=text, xalign=0)
        row.pack_start(spinner, False, False, 0)
        row.pack_start(label, True, True, 0)
        box.set_border_width(12)
        box.pack_start(row, True, True, 0)
        dlg.set_resizable(False)
        dlg.show_all()

        # ensure it renders before we block on subprocess.run()
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)

        return dlg, spinner

    def _close_busy(self, dlg, spinner):
        try:
            spinner.stop()
        except Exception:
            pass
        try:
            dlg.destroy()
        except Exception:
            pass

    def on_switch_state_set(self, partition, switch, requested_state: bool):
        """
        requested_state=True => user asked to enable
        requested_state=False => user asked to disable
        Return True to prevent GTK from toggling automatically.
        """
        if partition in self._inflight:
            return

        self._inflight.add(partition)

        uuid = partition.rsplit("/", 1)[-1]
        sudo_user = os.environ.get("SUDO_USER") or getpass.getuser()
        info = self.partition_info.get(partition, {})
        fstype = info.get("fstype", "")
        switch.set_sensitive(True)  # or False if you want to block while working
        mountpoint = f"/run/media/{sudo_user}/{uuid}"

        instance = subprocess.run(
            ["systemd-escape", "-p", "--suffix=mount", mountpoint],
            check=True,
            text=True,
            capture_output=True
        ).stdout.strip()

        # Prevent repeated clicks on this switch
        switch.set_sensitive(False)
        busy, spin = self._show_busy("Applying changes, please wait…")

        try:
            set_partition_enabled(partition, requested_state)

            if requested_state:
                rw_uid = pwd.getpwnam(sudo_user).pw_uid
                rw_gid = grp.getgrnam(sudo_user).gr_gid if hasattr(grp, "getgrnam") else rw_uid
                fstype_out, opts = compute_mount_opts(uuid, fstype, rw_uid, rw_gid, sudo_user)

                ensure_env_file(instance, uuid, sudo_user, fstype_out, opts)

                # Make the .mount WANT the helper (creates wants/ symlink)
                subprocess.run(
                    ["systemctl", "add-wants", instance, f"nobara-automount@{instance}.service"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )

                # Enable and start the helper for this instance
                subprocess.run(
                    ["systemctl", "enable", f"nobara-automount@{instance}.service"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )

                # Enable and start the helper for this instance
                subprocess.run(
                    ["systemctl", "start", f"nobara-automount@{instance}.service"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )

                self.status(f"Enabled automount for {partition}.")
            else:
                # Enable and start the helper for this instance
                subprocess.run(
                    ["systemctl", "stop", f"nobara-automount@{instance}.service"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )

                subprocess.run(
                    ["systemctl", "disable", f"nobara-automount@{instance}.service"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )

                subprocess.run(
                    ["systemctl", "remove-wants", instance, f"nobara-automount@{instance}.service"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )

                try:
                    os.remove(f"/etc/nobara/automount/run/media/{sudo_user}/{uuid}.mount.env")
                except FileNotFoundError:
                    pass

                self.status(f"Disabled automount for {partition}.")

            # === VISUAL STATE ===
            # Prevent re-entry when we set_active() ourselves
            if hasattr(switch, "_state_set_hid"):
                switch.handler_block(switch._state_set_hid)

            switch.set_active(requested_state)
            # <-- color updates immediately

            if hasattr(switch, "_state_set_hid"):
                switch.handler_unblock(switch._state_set_hid)

            # Rebuild list so "Mount location:" and "Desktop shortcut:" appear/disappear now
            # If you want to keep UI snappy, schedule it on idle:
            GLib.idle_add(self.refresh)

            return True

        except PermissionError:
            self.status("Error: Permission denied. Run as administrator.")
            # Do NOT flip the switch here; leaving it as-is keeps UX honest

        except subprocess.CalledProcessError as e:
            self.status(f"Command failed: {e}")
            print(f"{e}")

        except Exception as e:
            self.status(f"Error: {e}")
            # Stop GTK from toggling automatically; we set it ourselves on success
            return True

        finally:
            # Close busy UI and re-enable after a short cooldown
            self._close_busy(busy, spin)

            def _reenable():
                try:
                    switch.set_sensitive(True)
                finally:
                    self._inflight.discard(partition)
                return False
            GLib.timeout_add(self._cooldown_ms, _reenable)

    def status(self, text):
        self.status_label.set_text(text)

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
