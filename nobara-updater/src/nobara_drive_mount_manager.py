#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, GLib, Gdk

import subprocess, os, grp, getpass, sys
from pathlib import Path
from functools import partial

APP_TITLE = "Nobara Drive Mount Manager"
CONFIG_PATH = "/etc/nobara/automount/enabled.conf"

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
    try:
        out = subprocess.run(
            ["lsblk", "-rno", "NAME,UUID,FSTYPE,MOUNTPOINT"],
            capture_output=True, text=True
        ).stdout
        unmounted, mounted = [], []
        for line in out.splitlines():
            parts = line.split()
            if len(parts) not in (3, 4):
                continue
            name, uuid, fstype = parts[0], parts[1], parts[2]
            name_valid = (not name.startswith("loop")) and ("p" in name or "sd" in name)
            fs_valid = fstype in ("ext3","ext4","exfat","xfs","btrfs","ntfs","f2fs")
            if not (name_valid and fs_valid):
                continue
            if len(parts) == 3:
                unmounted.append((f"/dev/disk/by-uuid/{uuid}", fstype, uuid))
            else:
                mountpoint = parts[3]
                mounted.append((f"/dev/disk/by-uuid/{uuid}", fstype, uuid, mountpoint))
        return unmounted, mounted
    except Exception:
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

def cleanup_xhost():
    try:
        subprocess.run(["xhost", "-si:localuser:root"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
        )
    except Exception:
        pass

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
        mp_by_uuid = {uuid: mnt for (_p, _fs, uuid, mnt) in mounted}

        to_display = unmounted + [(p, fs, uuid) for p, fs, uuid, _ in mounted if p in self.enabled]

        if to_display:
            for partition, fstype, uuid in to_display:
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

                col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
                title = Gtk.Label(label=partition, xalign=0)
                sub_fs = Gtk.Label(label=f"Filesystem: {fstype}", xalign=0)
                sub_fs.get_style_context().add_class("dim-label")
                col.pack_start(title, False, False, 0)
                col.pack_start(sub_fs, False, False, 0)

                # NEW: show mount location ONLY if currently mounted
                if uuid in mp_by_uuid:
                    sub_mp = Gtk.Label(label=f"Mount location: {mp_by_uuid[uuid]} (mounted)", xalign=0)
                    sub_mp.get_style_context().add_class("dim-label")
                    col.pack_start(sub_mp, False, False, 0)

                sw = Gtk.Switch()
                sw.set_active(partition in self.enabled)
                sw.set_size_request(-1, 26)
                sw.set_valign(Gtk.Align.CENTER)
                sw.set_halign(Gtk.Align.CENTER)
                sw.set_hexpand(False); sw.set_vexpand(False)
                sw.connect("notify::active", partial(self.on_switch_toggled, partition))

                row.pack_start(col, True, True, 0)
                row.pack_end(sw, False, False, 0)
                self.parts_list.pack_start(row, False, False, 0)
        else:
            self.parts_list.pack_start(Gtk.Label(label="No eligible partitions found.", xalign=0), False, False, 0)


        any_notice = False
        for partition, fstype, _uuid, mnt in mounted:
            if partition not in self.enabled:
                any_notice = True
                self.notices_box.pack_start(
                    Gtk.Label(label=f"{partition} — Type: {fstype} — Mounted at: {mnt}", xalign=0),
                    False, False, 0
                )
        if not any_notice:
            self.notices_box.pack_start(Gtk.Label(label="No conflicts detected.", xalign=0), False, False, 0)

        self.parts_list.show_all()
        self.notices_box.show_all()

    def on_switch_toggled(self, partition, switch, _pspec):
        active = switch.get_active()
        try:
            set_partition_enabled(partition, active)
            sudo_user = os.environ.get("SUDO_USER")
            if active:
                # try to unmount it first in case it's already mounted or a bad unmount previously occurred
                uuid = partition.rsplit("/", 1)[-1]
                mp = get_mountpoint_for_uuid(uuid)

                # Remove the mount folder if it is the expected temp dir
                user = os.environ.get("SUDO_USER") or getpass.getuser()
                expected_root = os.path.join("/run/media", user)
                expected_path = os.path.join(expected_root, uuid)

                if mp:
                    # Unmount the mountpoint (lazy unmount stays fine)
                    subprocess.run(["umount", "-l", mp], check=True)

                    # Only remove if it’s exactly /run/media/<user>/<UUID> (safety guard)
                    if os.path.normpath(mp) == os.path.normpath(expected_path):
                        try:
                            os.rmdir(mp)  # should be empty after unmount
                        except OSError:
                            # If something re-created it or left files, leave it alone
                            pass
                    self.status(f"Partition {partition} unmounted from {mp}.")
                else:
                    if os.path.ismount(expected_path):
                        subprocess.run(["umount", "-l", expected_path], check=True)
                        # Only remove if it’s exactly /run/media/<user>/<UUID> (safety guard)

                        try:
                            os.rmdir(expected_path)  # should be empty after unmount
                        except OSError:
                            # If something re-created it or left files, leave it alone
                            pass
                        self.status(f"Partition {partition} unmounted from {expected_path}.")

                subprocess.run(
                    ["/usr/libexec/nobara-automount", sudo_user],
                    check=True, env={**os.environ, "USER": sudo_user}
                )
                self.status(f"Partition {partition} mounted.")
            else:
                # try to unmount it first in case it's already mounted or a bad unmount previously occurred
                uuid = partition.rsplit("/", 1)[-1]
                mp = get_mountpoint_for_uuid(uuid)

                # Remove the mount folder if it is the expected temp dir
                user = os.environ.get("SUDO_USER") or getpass.getuser()
                expected_root = os.path.join("/run/media", user)
                expected_path = os.path.join(expected_root, uuid)

                if mp:
                    # Unmount the mountpoint (lazy unmount stays fine)
                    subprocess.run(["umount", "-l", mp], check=True)

                    # Only remove if it’s exactly /run/media/<user>/<UUID> (safety guard)
                    if os.path.normpath(mp) == os.path.normpath(expected_path):
                        try:
                            os.rmdir(mp)  # should be empty after unmount
                        except OSError:
                            # If something re-created it or left files, leave it alone
                            pass
                    self.status(f"Partition {partition} unmounted from {mp}.")
                else:
                    if os.path.ismount(expected_path):
                        subprocess.run(["umount", "-l", expected_path], check=True)
                        # Only remove if it’s exactly /run/media/<user>/<UUID> (safety guard)

                        try:
                            os.rmdir(expected_path)  # should be empty after unmount
                        except OSError:
                            # If something re-created it or left files, leave it alone
                            pass
                        self.status(f"Partition {partition} unmounted from {expected_path}.")

        except PermissionError:
            self.status("Error: Permission denied. Run as administrator."); switch.set_active(not active)
        except FileNotFoundError:
            if active:
                with open(CONFIG_PATH, "w") as f: f.write(partition + "\n")
        except subprocess.CalledProcessError as e:
            self.status(f"Command failed: {e}"); switch.set_active(not active)
        except Exception as e:
            self.status(str(e)); switch.set_active(not active)

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
