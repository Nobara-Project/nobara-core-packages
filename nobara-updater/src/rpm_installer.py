#!/usr/bin/env python3

import tkinter as tk
from tkinter import messagebox
from tkinter import ttk
import subprocess
import os
import sys
import rpm
import logging
import datetime
import time
import threading
import queue

def validate_rpm_file(rpm_file):
    if not rpm_file:
        return False

    # Convert to lowercase for case-insensitive comparison
    rpm_file = rpm_file.lower()

    # Check if it ends with .rpm but not .src.rpm
    if rpm_file.endswith('.rpm') and not rpm_file.endswith('.src.rpm'):
        return True
    else:
        return False

# Set up logging
def setup_logging():
    log_dir = "~/.local/share/nobara-updater/rpm-installer/"
    log_file = f"log_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    # Expand tilde to home directory
    log_dir = os.path.expanduser(log_dir)
    log_file = os.path.join(log_dir, log_file)

    # Create log directory if it doesn't exist
    os.makedirs(log_dir, exist_ok=True)

    logging.basicConfig(
        level=logging.ERROR,
        filename=log_file,
        format='%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    return log_file

# Function to get package name and version from RPM file
def get_rpm_info(rpm_file):
    try:
        # Get RPM name
        rpm_name_command = ["rpm", "-qp", "--qf", "%{NAME}\n", rpm_file]
        rpm_name_process = subprocess.Popen(rpm_name_command, stdout=subprocess.PIPE, text=True)
        rpm_name = rpm_name_process.communicate()[0].strip()

        # Get RPM version
        rpm_version_command = ["rpm", "-qp", "--qf", "%{VERSION}\n", rpm_file]
        rpm_version_process = subprocess.Popen(rpm_version_command, stdout=subprocess.PIPE, text=True)
        rpm_version = rpm_version_process.communicate()[0].strip()

        return rpm_name, rpm_version
    except Exception as e:
        messagebox.showerror("Error", f"Failed to get RPM info: {str(e)}")
        return None, None

def get_installed_package_info(rpm_name):
    try:
        # Check if package is installed
        result = subprocess.run(["rpm", "-q", rpm_name], capture_output=True, text=True)

        if result.returncode != 0:
            return False, None

        # Get currently installed version
        result = subprocess.run(["rpm", "-q", "--qf", "%{VERSION}\n", rpm_name], capture_output=True, text=True)
        current_version = result.stdout.strip()

        return True, current_version

    except Exception as e:
        messagebox.showerror("Error", f"Failed to check installed version: {str(e)}")
        return False, None

# Function to check if package is installed and compare versions
def check_installed_version(rpm_name, version):
    installed, current_version = get_installed_package_info(rpm_name)

    if not installed:
        return False, version
    else:
        return True, current_version

class NoticeDialog:
    def __init__(self, master, message):
        self.dialog = tk.Toplevel(master)
        self.dialog.title("Install/Update in progress...")
        self.dialog.geometry("300x100")
        self.dialog.resizable(False, False)
        print(message)

        self.label = tk.Label(self.dialog, text=message, wraplength=280, justify=tk.CENTER)
        self.label.pack(padx=10, pady=10)

        self.close_function = None

    def set_close_function(self, func):
        self.close_function = func

    def close(self):
        self.dialog.destroy()

def close_dialog():
    # Assuming there's only one NoticeDialog open at a time
    dialog = globals().get('notice')
    if dialog:
        dialog.close()

def show_notice(message):
    global notice
    notice = NoticeDialog(root, message)
    notice.dialog.update_idletasks()  # Force the dialog to appear

def show_waiting_dialog(rpm_name, status, rpm_file):
    if status == "install":
        status_text_complete = "Install"
    else:
        status_text_complete = "Update"

    try:
        result = subprocess.run(["pkexec", "dnf", status, "-y", rpm_file],
                                capture_output=True, text=True)

        if result.returncode != 0:
            raise subprocess.CalledProcessError(result.returncode, result.args, output=result.stdout, stderr=result.stderr)
        close_dialog()
        messagebox.showinfo("Success", f"{status_text_complete} successful for {rpm_name}", parent=root)
        sys.exit(0)
    except subprocess.CalledProcessError as e:
        error_message = f"Failed to {status} {rpm_name}\n\nAn error occurred:\n{e}"
        logging.error(error_message)
        close_dialog()
        messagebox.showerror("Error", error_message + "\n\nLog file location: " + setup_logging(), parent=root)
        sys.exit(0)

# Main function
def main():
    global root
    root = tk.Tk()
    root.withdraw()

    rpm_file = sys.argv[1]

    if not validate_rpm_file(rpm_file):
        messagebox.showerror("Invalid RPM file", "The specified file must end with '.rpm' but not '.src.rpm'")
        sys.exit(1)

    # Get RPM information
    rpm_info = get_rpm_info(rpm_file)
    if not rpm_info:
        return

    rpm_name, version = rpm_info

    # Check if package is installed
    installed, current_version = check_installed_version(rpm_name, version)

    if not installed:
        # Package not installed, ask user if they want to install it
        confirm_install = messagebox.askyesno("Confirm Installation",
                                              f"Do you want to install {rpm_name} version {version}?")
        status = "install"

        if confirm_install:
            status_text = "Installing"
            show_notice(f"{status_text}, please wait")
            root.update_idletasks()
            root.update()
            time.sleep(2)
            show_waiting_dialog(rpm_name, status, rpm_file)
        else:
            messagebox.showinfo("Cancelled", f"Installation of {rpm_name} cancelled.")
            sys.exit(0)

    else:
        # Package installed, check version

        if version > current_version:
            update = messagebox.askyesno("Update Confirmation",
                                        f"{rpm_name} version ({current_version}) is already installed, but the RPM version you've opened is more recent ({version}). Do you want to update {rpm_name} {current_version} -> {version}?")
            status = "update"

            if update:
                status_text = "Updating"
                show_notice(f"{status_text}, please wait")
                root.update_idletasks()
                root.update()
                time.sleep(2)
                show_waiting_dialog(rpm_name, status, rpm_file)
            else:
                messagebox.showinfo("Cancelled", f"Update of {rpm_name} cancelled.")
                sys.exit(0)
        else:
            if version <= current_version:
                messagebox.showinfo("Version Information",
                                f"{rpm_name} is already installed and the version you are trying to install ({version}) is older or the same as the current version ({current_version}).")
                sys.exit(0)

    root.mainloop()

if __name__ == "__main__":
    main()
