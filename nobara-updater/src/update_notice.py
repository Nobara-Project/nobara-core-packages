#!/usr/bin/python3
"""Unprivileged recovery notice and report actions, separate from the updater."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time

from nobara_updater.update_report import latest_report, upload_report


def notify(report: dict) -> bool:
    cache = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "nobara-updater"
    seen = cache / "recovery-notified"
    if seen.exists() and seen.read_text().strip() == report["job"]:
        return False
    result = subprocess.run([
        "notify-send", "--app-name=Nobara System Update", "--icon=dialog-warning",
        "--urgency=critical", "--expire-time=0", "--action=details=Details and logs",
        "System update failed — previous system restored" if report.get("recovered") else "System update failed — review required",
        ("The previous system was restored. " if report.get("recovered") else "No automatic rollback was performed. ") +
        "View the error, save a report, or get help.\n"
        "You can also open System Update Recovery from the application menu."], capture_output=True, text=True)
    if result.returncode:
        return True  # Show the details window if notifications are unavailable.
    cache.mkdir(parents=True, exist_ok=True)
    seen.write_text(report["job"] + "\n")
    return result.stdout.strip() == "details"


def show_report(report: dict | None) -> None:
    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import GLib, Gtk

    app = Gtk.Application(application_id="org.nobaraproject.UpdateRecovery")

    def activate(application):
        window = application.get_active_window()
        if window:
            window.present()
            return
        window = Gtk.ApplicationWindow(application=application, title="System Update Recovery")
        window.set_default_size(700, 540)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16,
                      margin_top=24, margin_bottom=24, margin_start=24, margin_end=24)
        window.set_child(box)

        def label(text):
            item = Gtk.Label(label=text, wrap=True, xalign=0, selectable=True)
            box.append(item)
            return item

        if not report:
            label("No recovery report is available.")
            window.present()
            return
        title = label(("The previous system was restored" if report.get("recovered") else "The system update failed")
                      if report.get("active") else "Previous update recovery report")
        title.add_css_class("title-2")
        label("An update failed. You can continue using this recovered system.\n"
              "Review the error below, then check for updates in DNF App Center after addressing it. "
              "If it happens again, share the report with Nobara support."
              if report.get("recovered") else
              "No automatic rollback was performed. Some packages may have changed. "
              "View the log for troubleshooting steps, or save the report for Nobara support. "
              "Reaching the desktop does not mean the update completed."
              if report.get("installation_started") else
              "The update stopped before package installation began. View the log for details, "
              "address the reported error, then check for updates again.")
        scroll = Gtk.ScrolledWindow(vexpand=True, min_content_height=140)
        details = Gtk.TextView(editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        details.get_buffer().set_text(str(report.get("error", "See the update log.")))
        scroll.set_child(details)
        box.append(scroll)
        label("Save logs works offline. Upload logs sends the text report to Nobara's paste service and returns a link. "
              "Review it first: logs may include repository URLs and file paths.")
        result = label("")
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.append(buttons)

        def button(text, action):
            item = Gtk.Button(label=text)
            item.connect("clicked", action)
            buttons.append(item)
            return item

        def view_log(_):
            viewer = Gtk.Window(title="Failed update log", transient_for=window)
            viewer.set_default_size(900, 600)
            content = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
            content.get_buffer().set_text(Path(report["log"]).read_text())
            scroller = Gtk.ScrolledWindow()
            scroller.set_child(content)
            viewer.set_child(scroller)
            viewer.present()

        def save(item):
            item.set_sensitive(False)
            chooser = Gtk.FileChooserNative(title="Save recovery report", transient_for=window,
                                            action=Gtk.FileChooserAction.SAVE,
                                            accept_label="Save", cancel_label="Cancel")
            chooser.set_current_name("nobara-update-report-" + report["job"][:8] + ".tar.gz")
            window._report_chooser = chooser

            def selected(dialog, response):
                if response == Gtk.ResponseType.ACCEPT:
                    try:
                        path = dialog.get_file().get_path()
                        if not path:
                            raise OSError("Choose a local folder to save the report.")
                        shutil.copyfile(report["bundle"], path)
                        result.set_text("Report saved to " + path)
                    except OSError as error:
                        result.set_text("Could not save the report: " + str(error))
                dialog.destroy()
                window._report_chooser = None
                item.set_sensitive(True)
            chooser.connect("response", selected)
            chooser.show()

        def upload(item):
            item.set_sensitive(False)
            result.set_text("Uploading logs…")

            def finish(message):
                result.set_text(message)
                item.set_sensitive(True)
                return False

            def work():
                try:
                    message = "Copy this link into your support request:\n" + upload_report(report)
                except Exception as error:
                    message = str(error) + "\nYou can still save and share the offline report."
                GLib.idle_add(finish, message)
            threading.Thread(target=work, daemon=True).start()

        def open_updater(_):
            try:
                subprocess.Popen(["dnf-app-center", "--update"])
            except OSError as error:
                result.set_text(str(error) + "\nYou can also run sudo nobara-sync cli in a terminal.")

        button("View log", view_log)
        button("Save logs…", save)
        button("Upload logs", upload)
        button("Open App Center", open_updater)
        window.present()

    app.connect("activate", activate)
    app.run([])


def main():
    parser = argparse.ArgumentParser(description="View Nobara update recovery information.")
    parser.add_argument("--notify", action="store_true", help="Notify once per recovered update at desktop login.")
    args = parser.parse_args()
    if args.notify:
        # Confirmation starts after the display manager; desktop autostart
        # can race it. Wait briefly for publication without blocking login.
        for _ in range(30):
            status = subprocess.run(["systemctl", "show", "--value", "--property=ActiveState",
                                     "nobara-updater-confirm.service"], capture_output=True, text=True, timeout=5)
            if status.stdout.strip() not in {"activating", "reloading"}:
                break
            time.sleep(2)
    report = latest_report()
    if args.notify:
        if not report or not report.get("active"):
            return
        try:
            if not notify(report):
                return
        except OSError:
            pass
    show_report(report)


if __name__ == "__main__":
    main()
