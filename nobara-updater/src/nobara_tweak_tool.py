#!/usr/bin/python3

import gi
import subprocess
import os
import grp
import getpass
import sys
from pathlib import Path

gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, GLib

def relaunch_with_pkexec():
    script_path = Path(__file__).resolve()
    user = getpass.getuser()
    if os.geteuid() != 0:

        # Ensure DISPLAY and XAUTHORITY are set
        os.execvp(
            "pkexec",
            [
                "pkexec",
                "--disable-internal-agent",
                "env",
                f"DISPLAY={os.environ['DISPLAY']}",
                f"XAUTHORITY={os.environ.get('XAUTHORITY', '')}",
                f"SUDO_USER={user}",
                "NO_AT_BRIDGE=1",
                "G_MESSAGES_DEBUG=none",
                sys.executable,
                str(script_path),
            ]
        + sys.argv[1:],
        )

class NobaraTweakTool(Gtk.Application):
    def __init__(self):
        super().__init__(application_id='org.example.NobaraTweakTool')
        self.window = None
        self.status_bar = None
        self.context_id = None
        self.enabled_partitions = set()  # Initialize enabled_partitions
        self.partition_changed = {}  # Initialize partition_changed

    def do_activate(self):
        if not self.window:
            self.window = Gtk.ApplicationWindow(application=self)
            self.window.set_title("Nobara Tweak Tool")
            self.window.set_default_size(800, 600)

            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            self.window.add(vbox)

            # Status bar
            self.status_bar = Gtk.Statusbar()
            self.context_id = self.status_bar.get_context_id("status")
            vbox.pack_end(self.status_bar, False, False, 0)

            # Check if the user is in the wheel group
            self.check_wheel_group()

            # Create frames for sections with 5px margin
            updates_frame = Gtk.Frame(label="Enable gamescope session automatic updates (recommended)")
            updates_frame.set_border_width(10)
            updates_frame.set_label_align(0.02, 0.5)
            vbox.pack_start(updates_frame, True, True, 5)

            handheld_frame = Gtk.Frame(label="Enable auto-configuring of controller input packages (recommended)")
            handheld_frame.set_border_width(10)
            handheld_frame.set_label_align(0.02, 0.5)
            vbox.pack_start(handheld_frame, True, True, 5)

            decky_frame = Gtk.Frame(label="Enable DeckyLoader in gamescope session (recommended)")
            decky_frame.set_border_width(10)
            decky_frame.set_label_align(0.02, 0.5)
            vbox.pack_start(decky_frame, True, True, 5)

            partitions_frame = Gtk.Frame(label="Enable auto-mounting on available disk partitions")
            partitions_frame.set_border_width(10)
            partitions_frame.set_label_align(0.02, 0.5)
            vbox.pack_start(partitions_frame, True, True, 5)

            # Automatic Updates Section
            autoupdate_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            updates_frame.add(autoupdate_box)
            autoupdate_box.pack_start(Gtk.Box(), False, False, 2)
            autoupdate_label = Gtk.Label(label="    Configuration file: /etc/nobara/gamescope/autoupdate.conf")
            autoupdate_label.set_xalign(0.0)
            autoupdate_box.pack_start(autoupdate_label, False, False, 0)
            autoupdate_alignment = Gtk.Alignment(xalign=0.1, yalign=0.5, xscale=0, yscale=0)
            self.autoupdate_var = Gtk.CheckButton(label=" Enable")
            self.autoupdate_var.set_active(self.read_config_state('/etc/nobara/gamescope/autoupdate.conf'))
            self.autoupdate_var.connect("toggled", self.toggle_autoupdate)
            autoupdate_box.pack_start(self.autoupdate_var, False, False, 0)
            autoupdate_box.pack_start(Gtk.Box(), False, False, 2)

            # Handheld Packages Section
            handheld_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            handheld_frame.add(handheld_box)
            handheld_box.pack_start(Gtk.Box(), False, False, 2)
            handheld_label = Gtk.Label(label="    Configuration file: /etc/nobara/handheld_packages/autoupdate.conf")
            handheld_label.set_xalign(0.0)
            handheld_box.pack_start(handheld_label, False, False, 0)
            self.handheld_var = Gtk.CheckButton(label=" Enable")
            self.handheld_var.set_active(self.read_config_state('/etc/nobara/handheld_packages/autoupdate.conf'))
            self.handheld_var.connect("toggled", self.toggle_handheld_packages)
            handheld_box.pack_start(self.handheld_var, False, False, 0)
            handheld_box.pack_start(Gtk.Box(), False, False, 2)

            # Decky Loader Section
            decky_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            decky_frame.add(decky_box)
            decky_box.pack_start(Gtk.Box(), False, False, 2)
            decky_label = Gtk.Label(label="    Configuration file: /etc/nobara/decky_loader/autoupdate.conf")
            decky_label.set_xalign(0.0)
            decky_box.pack_start(decky_label, False, False, 0)
            self.decky_var = Gtk.CheckButton(label=" Enable")
            self.decky_var.set_active(self.read_config_state('/etc/nobara/decky_loader/autoupdate.conf'))
            self.decky_var.connect("toggled", self.toggle_decky_loader)
            decky_box.pack_start(self.decky_var, False, False, 0)
            decky_box.pack_start(Gtk.Box(), False, False, 2)

            # Partitions Section
            partitions_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            partitions_frame.add(partitions_box)

            partitions_label = Gtk.Label(label="    Configuration file: /etc/nobara/automount/enabled.conf")
            partitions_label.set_xalign(0.0)
            partitions_box.pack_start(partitions_label, False, False, 0)

            # Add notes about mount location
            notes = [
                "    Notes:",
                "    Partitions with auto-mount enabled will be mounted at user login.",
                "    Nobara's automount does NOT dynamically mount USB storage and SD cards when they are plugged in.",
                "    Checking the box enables auto-mounting on login for the partition and will also mount it.",
                f"    Partitions will be mounted at /run/media/(username)(/(partition_name)"
            ]
            for note in notes:
                note_label = Gtk.Label(label=note)
                note_label.set_xalign(0.0)
                partitions_box.pack_start(note_label, False, False, 0)

            # Read enabled partitions from configuration file
            self.read_enabled_partitions()

            # Get partitions
            unmounted_partitions, mounted_partitions = self.get_partitions()

            # Combine unmounted and enabled mounted partitions
            partitions_to_display = unmounted_partitions + [(p, fstype) for p, fstype, _ in mounted_partitions if p in self.enabled_partitions]

            if partitions_to_display:
                for partition, fstype in partitions_to_display:
                    var = Gtk.CheckButton(label=f"  {partition} ({fstype})" if fstype else partition)
                    var.set_active(partition in self.enabled_partitions)
                    var.connect("toggled", self.toggle_partition, partition)
                    partitions_box.pack_start(var, False, False, 0)

            # Add note about current partitions
            mount_note = "\n    These partitions cannot be mounted by Nobara's automount system because\n    some other process has already mounted them:\n"
            mount_note_label = Gtk.Label(label=mount_note)
            mount_note_label.set_xalign(0.0)
            partitions_box.pack_start(mount_note_label, False, False, 0)

            # Display mounted partitions not in the enabled list
            for partition, fstype, mountpoint in mounted_partitions:
                if partition not in self.enabled_partitions:
                    mounted_text = f"        {partition} ({fstype}) mounted at {mountpoint}"
                    mounted_label = Gtk.Label(label=mounted_text)
                    mounted_label.set_xalign(0.0)
                    partitions_box.pack_start(mounted_label, False, False, 0)

            partitions_box.pack_start(Gtk.Box(), False, False, 2)
            self.window.show_all()


    def read_enabled_partitions(self):
        try:
            with open('/etc/nobara/automount/enabled.conf', 'r') as f:
                self.enabled_partitions = set(line.strip() for line in f.readlines())
        except FileNotFoundError:
            self.enabled_partitions = set()
        except Exception as e:
            self.set_status(f"Error reading enabled partitions: {e}")

    def check_wheel_group(self):
        user = os.environ.get('SUDO_USER')
        try:
            groups = [g.gr_name for g in grp.getgrall() if user in g.gr_mem]
            if 'wheel' not in groups:
                self.set_status("Permission Denied: You must be in the 'wheel' group to run this application.")
                sys.exit(1)
        except Exception as e:
            self.set_status(f"Error: Failed to check group membership: {e}")
            sys.exit(1)

    def set_status(self, message):
        self.status_bar.push(self.context_id, f"STATUS: {message}")

    def toggle_autoupdate(self, widget):
        config_path = '/etc/nobara/gamescope/autoupdate.conf'
        if not widget.get_active():
            try:
                os.makedirs(os.path.dirname(config_path), exist_ok=True)
                with open(config_path, 'w') as f:
                    f.write('disabled\n')
                self.set_status("Automatic updates disabled.")
            except PermissionError:
                self.set_status("Error: Permission denied. Run as administrator.")
            except Exception as e:
                self.set_status(f"Error: {e}")
        else:
            try:
                os.remove(config_path)
                self.set_status("Automatic updates enabled.")
            except FileNotFoundError:
                self.set_status("Automatic updates enabled.")
            except PermissionError:
                self.set_status("Error: Permission denied. Run as administrator.")
            except Exception as e:
                self.set_status(f"Error: {e}")

    def toggle_handheld_packages(self, widget):
        config_path = '/etc/nobara/handheld_packages/autoupdate.conf'
        if not widget.get_active():
            try:
                os.makedirs(os.path.dirname(config_path), exist_ok=True)
                with open(config_path, 'w') as f:
                    f.write('disabled\n')
                self.set_status("Controller input packages configuration disabled.")
            except PermissionError:
                self.set_status("Error: Permission denied. Run as administrator.")
            except Exception as e:
                self.set_status(f"Error: {e}")
        else:
            try:
                os.remove(config_path)
                self.set_status("Controller input packages configuration enabled.")
            except FileNotFoundError:
                self.set_status("Controller input packages configuration enabled.")
            except PermissionError:
                self.set_status("Error: Permission denied. Run as administrator.")
            except Exception as e:
                self.set_status(f"Error: {e}")

    def toggle_decky_loader(self, widget):
        config_path = '/etc/nobara/decky_loader/autoupdate.conf'
        if not widget.get_active():
            try:
                os.makedirs(os.path.dirname(config_path), exist_ok=True)
                with open(config_path, 'w') as f:
                    f.write('disabled\n')
                self.set_status("Decky Loader disabled.")
            except PermissionError:
                self.set_status("Error: Permission denied. Run as administrator.")
            except Exception as e:
                self.set_status(f"Error: {e}")
        else:
            try:
                os.remove(config_path)
                self.set_status("Decky Loader enabled.")
            except FileNotFoundError:
                self.set_status("Decky Loader enabled.")
            except PermissionError:
                self.set_status("Error: Permission denied. Run as administrator.")
            except Exception as e:
                self.set_status(f"Error: {e}")

    def toggle_partition(self, widget, partition):
        self.partition_changed[partition] = True
        try:
            with open('/etc/nobara/automount/enabled.conf', 'r+') as f:
                lines = f.readlines()
                f.seek(0)
                f.truncate()
                if widget.get_active():
                    if partition not in lines:
                        lines.append(partition + '\n')
                else:
                    lines = [line for line in lines if line.strip() != partition]
                f.writelines(lines)

            sudo_user = os.environ.get('SUDO_USER')

            if widget.get_active():
                subprocess.run(
                    ['/usr/libexec/nobara-automount', sudo_user],
                    check=True,
                    env={**os.environ, 'USER': sudo_user}
                )
                self.set_status(f"Partition {partition} mounted.")
            else:
                subprocess.run(['umount', partition], check=True)
                self.set_status(f"Partition {partition} unmounted.")
        except PermissionError:
            self.set_status("Error: Permission denied. Run as administrator.")
        except FileNotFoundError:
            if widget.get_active():
                with open('/etc/nobara/automount/enabled.conf', 'w') as f:
                    f.write(partition + '\n')

    # Function to get list of partitions with their file systems and mount points
    def get_partitions():
        try:
            result = subprocess.run(['lsblk', '-rno', 'NAME,FSTYPE,MOUNTPOINT'], capture_output=True, text=True)
            unmounted_partitions = []
            mounted_partitions = []
            for line in result.stdout.splitlines():
                parts = line.split()
                if len(parts) not in [2,3]:
                    continue  # not a device we are interested in (no filesystem)
                name = parts[0]
                fstype = parts[1]
                name_valid = not name.startswith('loop') and 'p' in name or 'sd' in name # e.g. nvme0n1p1 or sda1
                part_valid = fstype == 'ext3' or fstype == 'ext4' or fstype == 'xfs' or fstype == 'btrfs' or fstype == 'ntfs'
                if len(parts) == 2 and name_valid and part_valid:  # No mount point
                        unmounted_partitions.append((f"/dev/{name}", fstype))
                elif len(parts) == 3 and name_valid and part_valid:  # With mount point
                    mountpoint = parts[2]
                    mounted_partitions.append((f"/dev/{name}", fstype, mountpoint))
            return unmounted_partitions, mounted_partitions
        except Exception as e:
            status_bar.set(f"Error: Failed to get partitions: {e}")
            return [], []

    def read_config_state(self, config_path):
        try:
            with open(config_path, 'r') as f:
                content = f.read().strip()
                return content != 'disabled'
        except FileNotFoundError:
            return True
        except Exception as e:
            self.set_status(f"Error reading config: {e}")
            return True

def main():
    # Relaunch with pkexec if not running as root
    relaunch_with_pkexec()
    app = NobaraTweakTool()
    app.run(sys.argv)

if __name__ == '__main__':
    main()

