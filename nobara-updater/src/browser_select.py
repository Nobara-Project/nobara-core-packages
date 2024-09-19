#!/usr/bin/python3

import gi
import threading
import tkinter as tk
import subprocess
import gi
import cairo
from PIL import Image, ImageTk

gi.require_version('Gio', '2.0')
from gi.repository import GLib
import xml.etree.ElementTree as ET
import evdev
from evdev import ecodes, InputDevice, list_devices
import os
import sys
import vdf
import binascii
import argparse
import time
import logging

browser_labels = []
signal_match = None
bluetooth_device = None
controller_devices = {}
device_names = {}

def install_browser(browser_name, flatpak_package, png_path):
    """Install the selected browser using Flatpak."""

    label.destroy()

    # Remove the list of browsers from the frame
    for widget in browser_frame.winfo_children():
        widget.destroy()

    # Add a label to indicate the installation process
    status_label = tk.Label(browser_frame, text=f"Installing {browser_name}, please wait...",
                            font=("Arial", 20), fg="white", bg="black")
    status_label.grid(row=0, column=0, padx=10, pady=20)

    # Update the UI before running the subprocess
    browser_frame.update_idletasks()

    try:
        # Run the Flatpak installation command as the current user
        subprocess.run(["flatpak", "--user", "install", "-y", flatpak_package], check=True)

        # If installation succeeds, add the browser to Steam using steamtinkerlaunch
        add_to_steam(browser_name, "/usr/bin/flatpak", "run " + flatpak_package)

        # Update the label to indicate success
        status_label.config(text=f"{browser_name} installed successfully!")
        safe_exit()
    except subprocess.CalledProcessError as e:
        # If installation fails, update the label to show the failure message
        status_label.config(text=f"Installation of {browser_name} failed: {e}")
        safe_exit()

def generate_preliminary_id(name):
    exe = "/usr/bin/flatpak"
    unique_id = "".join([exe, name])
    top = binascii.crc32(str.encode(unique_id, "utf-8")) | 0x80000000
    return (top << 32) | 0x02000000

def generate_shortcut_id(name):
    return (generate_preliminary_id(name) >> 32) - 0x100000000

def add_to_steam(name, exe, args):
    # Locate the Steam userdata directory
    steam_path = os.path.expanduser("~/.steam/steam/userdata")
    user_dirs = [d for d in os.listdir(steam_path) if os.path.isdir(os.path.join(steam_path, d))]

    if not user_dirs:
        # print("No Steam user directories found.")
        return

    # Assuming the first user directory is the one we want
    user_id = user_dirs[0]
    shortcuts_path = os.path.join(steam_path, user_id, "config", "shortcuts.vdf")

    new_entry = {
        "appid": generate_shortcut_id(name),
        "AppName": name,
        "Exe": exe,
        "StartDir": os.path.dirname(exe),
        "icon": "",
        "LaunchOptions": args,
        "IsHidden": 0,
        "AllowDesktopConfig": 1,
        "AllowOverlay": 1,
        "OpenVR": 0,
        "Devkit": 0,
        "DevkitOverrideAppID": 0,
        "LastPlayTime": 0,
    }

    print(f"Creating Steam shortcut for {name}")

    if os.path.exists(shortcuts_path):
        with open(shortcuts_path, "rb") as shortcut_file:
            shortcuts = vdf.binary_loads(shortcut_file.read())["shortcuts"].values()
    else:
        shortcuts = []

    # Convert shortcuts to a list and append the new entry
    shortcuts = list(shortcuts)
    shortcuts.append(new_entry)

    # Update the shortcuts dictionary
    updated_shortcuts = {"shortcuts": {str(index): elem for index, elem in enumerate(shortcuts)}}
    with open(shortcuts_path, "wb") as shortcut_file:
        shortcut_file.write(vdf.binary_dumps(updated_shortcuts))


def handle_wired_input_event(event_name, event_value):
    """Handle input events from the wired controller."""

    # Map the input events to navigation or selection
    if event_name == "ui_up" and event_value == 1.0:
        navigate_listbox(-1)  # Move up
    elif event_name == "ui_down" and event_value == 1.0:
        navigate_listbox(1)  # Move down
    elif event_name == "ui_accept" and event_value == 1.0:
        select_browser()  # Select the current item
    elif event_name == "ui_back" and event_value == 1.0:
        safe_exit()  # Exit the application

def navigate_listbox(direction):
    """Navigate the listbox using the D-pad."""
    global browser_labels  # Access the global browser_labels
    # Find the currently selected label
    selected_index = None
    for i, (label, _, _, _) in enumerate(browser_labels):
        if label.cget("bg") == "#33d1ff":  # Check if the label is selected (blue background)
            selected_index = i
            break

    # If no selection, select the first item
    if selected_index is None:
        selected_index = 0
    else:
        # Calculate the new selection index
        selected_index = (selected_index + direction) % len(browser_labels)

    # Clear the current selection and set the new one
    for label, _, _, _ in browser_labels:
        label.config(bg='black')  # Reset background color
    browser_labels[selected_index][0].config(bg='#33d1ff')  # Highlight the selected label

def select_browser():
    """Select the currently highlighted browser and trigger installation."""
    global browser_labels  # Access the global browser_labels
    # Find the currently selected label
    for label, browser_name, flatpak_package, png_path in browser_labels:
        if label.cget("bg") == "#33d1ff":  # Check if the label is selected (blue background)
            install_browser(browser_name, flatpak_package, png_path)  # Pass png_path here
            break

def find_wireless_controllers():
    """Find all gamepad devices using evdev."""
    devices = [InputDevice(path) for path in list_devices()]
    gamepads = []

    for device in devices:
        capabilities = device.capabilities()

        # Extract absolute axis codes and key codes
        abs_codes = capabilities.get(ecodes.EV_ABS, [])
        key_codes = capabilities.get(ecodes.EV_KEY, [])

        for code in abs_codes:
            if isinstance(code, tuple):
                axis_code, abs_info = code

        has_buttons = any(
            code in key_codes for code in [ecodes.BTN_A, ecodes.BTN_B, ecodes.BTN_X, ecodes.BTN_Y]
        )

        if has_buttons:
            gamepads.append(device)

    return gamepads

def handle_controller(device):
    """Handle input events from the wireless controller using evdev."""
    try:
        for event in device.read_loop():
            # Handle D-pad (EV_ABS) events
            if event.type == ecodes.EV_ABS:
                if event.code == ecodes.ABS_HAT0Y:
                    if event.value == -1:
                        navigate_listbox(-1)  # Move up
                    elif event.value == 1:
                        navigate_listbox(1)  # Move down
                if event.code == ecodes.ABS_Y:
                    if event.value == 0:
                        navigate_listbox(-1)  # Move up
                    elif event.value == 255:
                        navigate_listbox(1)  # Move down
            # Handle button (EV_KEY) events
            elif event.type == ecodes.EV_KEY:
                key_event = evdev.categorize(event)
                # Handle key events (e.g., button presses) here
                if key_event.keystate == 1:  # Key press
                    if 'BTN_A' in key_event.keycode:
                        select_browser()  # Select the current item
                    elif 'BTN_B' in key_event.keycode:
                        safe_exit()  # exit
    except OSError as e:
        if e.errno == 19:  # No such device
            pass
        else:
            raise  # Re-raise other OSError exceptions

def monitor_controller_devices():
    """Monitor wireless controllers and handle their input events."""
    global controller_devices
    controller_devices = {}

    while True:
        current_devices = find_wireless_controllers()

        # Add new devices
        for device in current_devices:
            if device.path not in controller_devices:
                print(f"Controller connected: {device.name}")
                controller_devices[device.path] = threading.Thread(target=handle_controller, args=(device,), daemon=True)
                controller_devices[device.path].start()
                device_names[device.path] = device.name  # Store the name along with the path

        # Remove disconnected devices
        for path in list(controller_devices.keys()):
            if path not in [device.path for device in current_devices]:
                print(f"Controller disconnected: {device_names[path]}")  # Print the stored name
                controller_devices[path].join()  # Ensure the thread is properly terminated
                del controller_devices[path]
                del device_names[path]

        time.sleep(5)  # Check every 5 seconds

# Start a thread to monitor DBus for wireless device plug-in and unplug events
def start_controller_monitor_thread():
    threading.Thread(target=monitor_controller_devices, daemon=True).start()

def create_browser_selection_window():
    """Create a full-screen window with a list of browsers for the user to select."""
    global browser_frame, browser_labels, label, root  # Make browser_frame accessible in other functions

    root = tk.Tk()
    root.title("Select a Browser")

    # Make the window full-screen
    root.attributes('-fullscreen', True)

    # Set the background color to black
    root.configure(bg='black')

    # Create an invisible frame (the "box") to hold the label and progress bar
    frame = tk.Frame(root, bg='black')

    # Center the frame in the middle of the window
    frame.place(relx=0.5, rely=0.5, anchor=tk.CENTER)

    # Create a label widget for the text above the progress bar
    label = tk.Label(frame, text="Select a Browser to Install",
                     font=("Arial", 20), fg="white", bg="black")
    label.grid(row=0, column=0, sticky="W", padx=10, pady=5)

    # Create a frame to hold the browser list
    browser_frame = tk.Frame(frame, bg='black')
    browser_frame.grid(row=1, column=0, sticky="W", padx=10, pady=5)

    # List of browsers and their corresponding SVG icons
    browsers = [
        ("Google Chrome", "/usr/share/nobara-gamescope/browser-select/chrome_icon.png", "com.google.Chrome"),
        ("Mozilla Firefox", "/usr/share/nobara-gamescope/browser-select/firefox_icon.png", "org.mozilla.firefox"),
        ("Microsoft Edge", "/usr/share/nobara-gamescope/browser-select/edge_icon.png", "com.microsoft.Edge"),
        ("Brave Browser", "/usr/share/nobara-gamescope/browser-select/brave_icon.png", "com.brave.Browser")
    ]

    # Store references to the labels and icons for selection handling
    icons = []

    # Load and display each browser with its SVG icon
    for i, (browser_name, png_path, flatpak_package) in enumerate(browsers):

        # Load the PNG image using PIL
        img = Image.open(png_path)
        icon = ImageTk.PhotoImage(img)
        icons.append(icon)  # Keep a reference to avoid garbage collection

        # Create a label for the icon
        icon_label = tk.Label(browser_frame, image=icon, bg='black')
        icon_label.grid(row=i, column=0, padx=10, pady=5)

        # Create a label for the browser name
        browser_label = tk.Label(browser_frame, text=browser_name, font=("Arial", 20),
                                 bg='black', fg='white',  # Background and text color
                                 padx=10, pady=5)
        browser_label.grid(row=i, column=1, sticky="ew")

        # Bind a click event to the browser label
        browser_label.bind("<Button-1>", lambda e, index=i: on_label_click(index))

        # Bind hover events to change the background color
        browser_label.bind("<Enter>", lambda e, label=browser_label: label.config(bg='#33d1ff'))
        browser_label.bind("<Leave>", lambda e, label=browser_label: label.config(bg='black'))

        # Store the label for selection handling
        browser_labels.append((browser_label, browser_name, flatpak_package, png_path))  # Include png_path

    # Bind the Escape key to exit full-screen mode
    root.bind("<Escape>", lambda e: safe_exit())

    # Bind keyboard keys for navigation and selection (case insensitive)
    root.bind("<w>", lambda e: navigate_listbox(-1))
    root.bind("<W>", lambda e: navigate_listbox(-1))
    root.bind("<s>", lambda e: navigate_listbox(1))
    root.bind("<S>", lambda e: navigate_listbox(1))
    root.bind("<Up>", lambda e: navigate_listbox(-1))
    root.bind("<Down>", lambda e: navigate_listbox(1))
    root.bind("<Return>", lambda e: select_browser())

    # Start monitoring for controller devices
    start_controller_monitor_thread()

    # Schedule focus_window to be called after 1 second (1000 ms)
    root.after(1000, focus_window)

    # Start the Tkinter event loop
    root.mainloop()

def on_label_click(index):
    """Handle mouse click on a browser label."""
    # Clear the current selection and set the new one
    for i, (label, _, _, _) in enumerate(browser_labels):
        if i == index:
            label.config(bg='#33d1ff')  # Highlight the selected label
        else:
            label.config(bg='black')  # Reset other labels

    # Call select_browser to trigger installation
    select_browser()

def focus_window():
    # Step 1: Get the window ID using xwininfo
    xwininfo_command = ["xwininfo", "-d", ":0", "-root", "-tree"]
    grep_command = ["grep", "Select a Browser"]
    cut_command = ["cut", "-d", " ", "-f", "6"]

    # Run the xwininfo command and pipe it through grep and cut
    xwininfo_process = subprocess.Popen(xwininfo_command, stdout=subprocess.PIPE)
    grep_process = subprocess.Popen(grep_command, stdin=xwininfo_process.stdout, stdout=subprocess.PIPE)
    cut_process = subprocess.Popen(cut_command, stdin=grep_process.stdout, stdout=subprocess.PIPE)

    # Get the window ID from the output
    window_id = cut_process.communicate()[0].strip().decode()

    # Step 2: Run the xprop command with the window ID
    xprop_command = [
        "xprop", "-d", ":0", "-id", window_id, "-f", "STEAM_GAME", "32co",
        "-set", "STEAM_GAME", "12346"
    ]

    # Run the xprop command
    subprocess.run(xprop_command, check=True)

def safe_exit():
    root.quit()

# Run the browser selection window
create_browser_selection_window()
