#!/usr/bin/python3

import tkinter as tk
from tkinter import ttk
import subprocess
import os
import threading

# List of lines to monitor in the log file
progress_lines = [
    "INFO - Checking repositories...",
    "INFO - Checking for various known problems to repair, please do not turn off your computer...",
    "INFO - Starting package updates, please do not turn off your computer...",
    "INFO - All Updates complete!"
]

log_file_path = os.path.expanduser("~/.nobara-updater-gamescope.log")

# Set to track lines that have already been processed
processed_lines = set()

def clean_log_line(line):
    """Remove everything before the first occurrence of 'INFO'."""
    if "INFO" in line:
        return "INFO " + line.split("INFO", 1)[1].strip()  # Keep everything after 'INFO'
    return line.strip()  # If 'INFO' is not found, return the original line

def monitor_log_file(progress_bar, progress_var, root):
    """Monitor the log file and update the progress bar based on its content."""
    try:
        with open(log_file_path, "r") as log_file:
            log_lines = log_file.readlines()  # Read all lines in the log file

            # Check for each progress line in the log file
            for line in log_lines:
                cleaned_line = clean_log_line(line)  # Remove everything before 'INFO'

                # Only process the line if it hasn't been processed before
                if cleaned_line in progress_lines and cleaned_line not in processed_lines:
                    # Update the progress bar when a matching line is found
                    print(cleaned_line)
                    current_value = progress_var.get()
                    current_value += 100 // len(progress_lines)  # Increment based on the number of lines
                    progress_var.set(current_value)
                    root.update_idletasks()  # Update the GUI

                    # Mark the line as processed
                    processed_lines.add(cleaned_line)

    except FileNotFoundError:
        print(f"Log file not found: {log_file_path}")

    # Schedule the next check after 1 second (1000 ms)
    root.after(5000, monitor_log_file, progress_bar, progress_var, root)

def run_command(progress_var, root):
    """Run the command in a separate thread and check progress after completion."""
    log_file = open(log_file_path, "w")  # Open the log file for writing
    try:
        # Run the command and wait for it to complete
        subprocess.run(
            ["pkexec", "/usr/bin/nobara-updater", "cli", os.getenv("USER")],
            stdout=log_file, stderr=subprocess.STDOUT, check=True
        )
    except subprocess.CalledProcessError as e:
        print(f"Command failed with error: {e}")
    finally:
        log_file.close()  # Ensure the log file is closed

    # After the command completes, schedule a check for the progress bar in the main thread
    root.after(5000, check_progress_and_quit, progress_var, root)

def check_progress_and_quit(progress_var, root):
    """Check if the progress bar is at 100% and quit the application if it is."""
    if progress_var.get() == 100:
        print("Progress is 100%, quitting the application.")
        root.quit()  # Quit the Tkinter application
    else:
        print("Command completed, but progress is not 100% yet.")

def run_command_and_monitor(progress_bar, progress_var, root):
    """Run the command and monitor the log file."""
    # Start the command in a separate thread
    threading.Thread(target=lambda: run_command(progress_var, root)).start()

    # Start monitoring the log file
    root.after(5000, monitor_log_file, progress_bar, progress_var, root)

def create_fullscreen_window():
    """Create a full-screen window with a centered progress bar and black background."""
    root = tk.Tk()
    root.title("Nobara Updater")

    # Get screen width and height
    width = root.winfo_screenwidth()
    height = root.winfo_screenheight()

    # Set the window size to the screen size
    root.geometry(f"{width}x{height}")

    # Set the background color to black
    root.configure(bg='black')

    # Create a style and set the progress bar color to purple, with white borders on the left and right
    style = ttk.Style()
    style.theme_use('clam')  # Use the 'clam' theme to allow color customization

    # Configure the progress bar style
    style.configure("Purple.Horizontal.TProgressbar",
                    troughcolor='#232323',  # Background of the progress bar trough
                    background='#33d1ff',  # Color of the progress bar fill
                    bordercolor='black',  # Remove the outer border by setting it to black
                    lightcolor='black',   # Set the left and top border to white
                    darkcolor='black',    # Set the right and bottom border to white
                    relief='flat')        # Make the progress bar flat (no 3D effect)

    # Create an invisible frame (the "box") to hold the label and progress bar
    frame = tk.Frame(root, bg='black')

    # Center the frame in the middle of the window
    frame.place(relx=0.5, rely=0.5, anchor=tk.CENTER)

    # Create a label widget for the text above the progress bar
    label = tk.Label(frame, text="Performing Nobara updates, please wait...",
                     font=("Arial", 20), fg="white", bg="black")

    # Create a progress bar variable
    progress_var = tk.IntVar()

    # Create a progress bar widget with the custom style
    progress_bar = ttk.Progressbar(frame, orient="horizontal", length=600, mode="determinate",
                                   variable=progress_var, style="Purple.Horizontal.TProgressbar")

    # Use grid to align the label and progress bar to the left of the frame
    label.grid(row=1, column=0, sticky="W", padx=10, pady=5)
    progress_bar.grid(row=0, column=0, sticky="W", padx=10, pady=5)

    # For demo/dev purposes so we can see where our progress bar is.
    #progress_var.set(25)
    #root.update_idletasks()  # Update the GUI

    # Start monitoring the command output in a separate thread
    run_command_and_monitor(progress_bar, progress_var, root)

    # Bind the Escape key to exit full-screen mode
    root.bind("<Escape>", lambda e: root.quit())

    # Schedule focus_window to be called after 1 second (1000 ms)
    root.after(1000, focus_window)

    # Start the Tkinter event loop
    root.mainloop()


def focus_window():
    # Step 1: Get the window ID using xwininfo
    xwininfo_command = ["xwininfo", "-d", ":0", "-root", "-tree"]
    grep_command = ["grep", "Nobara Updater"]
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


create_fullscreen_window()

