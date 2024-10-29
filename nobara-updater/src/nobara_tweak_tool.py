#!/usr/bin/env python3

import tkinter as tk
import subprocess
import os
import grp
import getpass
import sys
from pathlib import Path

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

# Status bar class
class StatusBar(tk.Frame):
    def __init__(self, master):
        super().__init__(master)
        self.label = tk.Label(self, bd=1, relief=tk.SUNKEN, anchor='w')
        self.label.pack(fill=tk.X)

    def set(self, message):
        self.label.config(text=f"STATUS: {message}")
        self.label.update_idletasks()

    def clear(self):
        self.label.config(text="")
        self.label.update_idletasks()

# Global variable for status bar
status_bar = None

# Function to check if the user is in the wheel group
def check_wheel_group():
    user = os.environ.get('SUDO_USER')
    try:
        groups = [g.gr_name for g in grp.getgrall() if user in g.gr_mem]
        if 'wheel' not in groups:
            status_bar.set("Permission Denied: You must be in the 'wheel' group to run this application.")
            sys.exit(1)
    except Exception as e:
        status_bar.set(f"Error: Failed to check group membership: {e}")
        sys.exit(1)

# Function to toggle automatic updates
def toggle_autoupdate():
    config_path = '/etc/nobara/gamescope/autoupdate.conf'
    config_dir = os.path.dirname(config_path)

    if not autoupdate_var.get():
        try:
            # Ensure the directory exists
            os.makedirs(config_dir, exist_ok=True)

            # Write to the file
            with open(config_path, 'w') as f:
                f.write('disabled\n')
            status_bar.set("Automatic updates disabled.")
        except PermissionError:
            status_bar.set("Error: Permission denied. Run as administrator.")
        except Exception as e:
            status_bar.set(f"Error: {e}")
    else:
        try:
            # Remove the file if it exists
            os.remove(config_path)
            status_bar.set("Automatic updates enabled.")
        except FileNotFoundError:
            # File doesn't exist, which is fine
            status_bar.set("Automatic updates enabled.")
        except PermissionError:
            status_bar.set("Error: Permission denied. Run as administrator.")
        except Exception as e:
            status_bar.set(f"Error: {e}")

def toggle_handheld_packages():
    config_path = '/etc/nobara/handheld_packages/autoupdate.conf'
    config_dir = os.path.dirname(config_path)

    if not handheld_var.get():
        try:
            os.makedirs(config_dir, exist_ok=True)
            with open(config_path, 'w') as f:
                f.write('disabled\n')
            status_bar.set("Controller input packages configuration disabled.")
        except PermissionError:
            status_bar.set("Error: Permission denied. Run as administrator.")
        except Exception as e:
            status_bar.set(f"Error: {e}")
    else:
        try:
            os.remove(config_path)
            status_bar.set("Controller input packages configuration enabled.")
        except FileNotFoundError:
            status_bar.set("Controller input packages configuration enabled.")
        except PermissionError:
            status_bar.set("Error: Permission denied. Run as administrator.")
        except Exception as e:
            status_bar.set(f"Error: {e}")

def toggle_decky_loader():
    config_path = '/etc/nobara/decky_loader/autoupdate.conf'
    config_dir = os.path.dirname(config_path)

    if not decky_var.get():
        try:
            os.makedirs(config_dir, exist_ok=True)
            with open(config_path, 'w') as f:
                f.write('disabled\n')
            status_bar.set("Decky Loader disabled.")
        except PermissionError:
            status_bar.set("Error: Permission denied. Run as administrator.")
        except Exception as e:
            status_bar.set(f"Error: {e}")
    else:
        try:
            os.remove(config_path)
            status_bar.set("Decky Loader enabled.")
        except FileNotFoundError:
            status_bar.set("Decky Loader enabled.")
        except PermissionError:
            status_bar.set("Error: Permission denied. Run as administrator.")
        except Exception as e:
            status_bar.set(f"Error: {e}")

# Function to toggle partition mount
def toggle_partition(partition):
    partition_changed[partition] = True
    try:
        with open('/etc/nobara/automount/enabled.conf', 'r+') as f:
            lines = f.readlines()
            f.seek(0)
            f.truncate()
            if partition_vars[partition].get():
                if partition not in lines:
                    lines.append(partition + '\n')
            else:
                lines = [line for line in lines if line.strip() != partition]
            f.writelines(lines)

        # Get the SUDO_USER environment variable
        sudo_user = os.environ.get('SUDO_USER')

        if partition_vars[partition].get():
            # Mount the partition
            subprocess.run(
                ['/usr/libexec/nobara-automount', sudo_user],
                check=True,
                env={**os.environ, 'USER': sudo_user}
            )
            status_bar.set(f"Partition {partition} mounted.")
        else:
            # Unmount the partition
            subprocess.run(['umount', partition], check=True)
            status_bar.set(f"Partition {partition} unmounted.")
    except PermissionError:
        status_bar.set("Error: Permission denied. Run as administrator.")
    except FileNotFoundError:
        if partition_vars[partition].get():
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
            name_valid = 'p' in name or 'sd' in name # (e.g. (e.g. nvme0n1p1) or sda1)
            part_valid = fstype == 'ext3' or fstype == 'ext4' or fstype == 'xfs' or fstype == 'btrfs' or fstype == 'ntfs'
            if len(parts) == 2:  # No mount point
                name, fstype = parts
                if not name.startswith('loop') and name_valid and part_valid:
                    unmounted_partitions.append((f"/dev/{name}", fstype))
            elif len(parts) == 3:  # With mount point
                mountpoint = parts[2]
                if not name.startswith('loop') and name_valid and part_valid:
                    mounted_partitions.append((f"/dev/{name}", fstype, mountpoint))
        return unmounted_partitions, mounted_partitions
    except Exception as e:
        status_bar.set(f"Error: Failed to get partitions: {e}")
        return [], []

# Function to run before closing the application
def on_closing():
    root.destroy()

# Main function
def main():
    global status_bar

    # Relaunch with pkexec if not running as root
    relaunch_with_pkexec()

    # Create the main window
    global root
    root = tk.Tk()
    root.title("Nobara Tweak Tool")

    # Create and pack the status bar
    status_bar = StatusBar(root)
    status_bar.pack(side=tk.BOTTOM, fill=tk.X)

    # Check if the user is in the wheel group
    check_wheel_group()

    # Create frames for sections
    updates_frame = tk.LabelFrame(root, text="Enable gamescope session automatic updates (recommended)", padx=10, pady=10)
    updates_frame.pack(fill="both", expand=True, padx=10, pady=5)

    handheld_frame = tk.LabelFrame(root, text="Enable auto-configuring of controller input packages (recommended)", padx=10, pady=10)
    handheld_frame.pack(fill="both", expand=True, padx=10, pady=5)

    decky_frame = tk.LabelFrame(root, text="Enable DeckyLoader in gamescope session (recommended)", padx=10, pady=10)
    decky_frame.pack(fill="both", expand=True, padx=10, pady=5)

    partitions_frame = tk.LabelFrame(root, text="Enable auto-mounting on available disk partitions", padx=10, pady=10)
    partitions_frame.pack(fill="both", expand=True, padx=10, pady=5)

    # Add configuration file location for automatic updates
    updates_config_note = "Configuration file: /etc/nobara/gamescope/autoupdate.conf"
    tk.Label(updates_frame, text=updates_config_note).pack(anchor='w')

    # Automatic Updates Section
    global autoupdate_var
    autoupdate_var = tk.BooleanVar(value=True)
    autoupdate_checkbox = tk.Checkbutton(updates_frame, text="Enable", variable=autoupdate_var, command=toggle_autoupdate)
    autoupdate_checkbox.pack(anchor='w')

    # Add configuration file location for handheld package updates
    handheld_config_note = "Configuration file: /etc/nobara/handheld_packages/autoupdate.conf"
    tk.Label(handheld_frame, text=handheld_config_note).pack(anchor='w')

    global handheld_var
    handheld_var = tk.BooleanVar(value=True)
    handheld_checkbox = tk.Checkbutton(handheld_frame, text="Enable", variable=handheld_var, command=toggle_handheld_packages)
    handheld_checkbox.pack(anchor='w')

    # Add configuration file location for Decky Loader
    decky_config_note = "Configuration file: /etc/nobara/decky_loader/autoupdate.conf"
    tk.Label(decky_frame, text=decky_config_note).pack(anchor='w')

    global decky_var
    decky_var = tk.BooleanVar(value=True)
    decky_checkbox = tk.Checkbutton(decky_frame, text="Enable", variable=decky_var, command=toggle_decky_loader)
    decky_checkbox.pack(anchor='w')

    # Add configuration file location for automount
    partitions_config_note = "Configuration file: /etc/nobara/automount/enabled.conf"
    tk.Label(partitions_frame, text=partitions_config_note).pack(anchor='w')

    # Mountable Partitions Section
    global partition_vars, partition_changed
    partition_vars = {}
    partition_changed = {}

    # Read enabled partitions from the configuration file
    enabled_partitions = set()
    try:
        with open('/etc/nobara/automount/enabled.conf', 'r') as f:
            enabled_partitions = set(line.strip() for line in f.readlines())
    except FileNotFoundError:
        pass

    # Add note about mount location
    mount_note = f"Notes:"
    tk.Label(partitions_frame, text=mount_note).pack(anchor='w')

    # Add note about mount location
    mount_note2 = f"Partitions with auto-mount enabled will be mounted at user login."
    tk.Label(partitions_frame, text=mount_note2).pack(anchor='w')

    # Add note about mount location
    mount_note3 = f"Nobara's automount does NOT dynamically mount USB storage and SD cards when they are plugged in."
    tk.Label(partitions_frame, text=mount_note3).pack(anchor='w')

    # Add note about mount location
    mount_note4 = f"Checking the box enables auto-mounting on login for the partition and will also mount it."
    tk.Label(partitions_frame, text=mount_note4).pack(anchor='w')

    # Add note about mount location
    user = getpass.getuser()
    mount_note5 = f"Partitions will be mounted at /run/media/{user}/(partition_name)"
    tk.Label(partitions_frame, text=mount_note5).pack(anchor='w')

    # Get partitions
    unmounted_partitions, mounted_partitions = get_partitions()

    # Combine unmounted and enabled mounted partitions
    partitions_to_display = unmounted_partitions + [(p, fstype) for p, fstype, _ in mounted_partitions if p in enabled_partitions]

    if partitions_to_display:
        for partition, fstype in partitions_to_display:
            var = tk.BooleanVar(value=partition in enabled_partitions)
            partition_vars[partition] = var
            partition_changed[partition] = False
            checkbox_text = f"    {partition} ({fstype})" if fstype else partition
            checkbox = tk.Checkbutton(partitions_frame, text=checkbox_text, variable=var, command=lambda p=partition: toggle_partition(p))
            checkbox.pack(anchor='w')

    # Add note about current partitions
    mount_note = "\nThese partitions cannot be mounted by Nobara's automount system because some other process has already mounted them:"
    tk.Label(partitions_frame, text=mount_note).pack(anchor='w')

    # Display mounted partitions not in the enabled list
    for partition, fstype, mountpoint in mounted_partitions:
        if partition not in enabled_partitions:
            mounted_text = f"    {partition} ({fstype}) mounted at {mountpoint}"
            tk.Label(partitions_frame, text=mounted_text).pack(anchor='w')


    # Set the protocol for window close event
    root.protocol("WM_DELETE_WINDOW", on_closing)

    # Run the application
    root.mainloop()

if __name__ == '__main__':
    main()

