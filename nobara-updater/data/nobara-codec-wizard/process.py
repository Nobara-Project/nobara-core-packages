#!/usr/bin/python3
"""Codec wizard progress UI; package work belongs to nobara-sync."""
import json
import os
from pathlib import Path
import subprocess


def completion_result(wait_status, state):
    if not os.WIFEXITED(wait_status) or os.WEXITSTATUS(wait_status) != 0:
        return False, "Preparation failed", "Media codec preparation failed. See the terminal output for details."
    status = state.get("status")
    if status in {"ready", "scheduled", "awaiting-boot"}:
        return True, "Restart required", "Media codecs and system updates are prepared. Save your work and restart to finish installation."
    if status in {"unchanged", "complete", "live-complete", "installer-complete"}:
        return True, "Complete", "Media codecs are installed and up to date."
    return False, "Status unavailable", "Could not confirm the codec update state. Run nobara-sync update-status for details."


def main():
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Vte", "2.91")
    from gi.repository import Gtk, Vte, GLib

    builder = Gtk.Builder()
    builder.add_from_file(str(Path(__file__).with_name("process.ui")))
    window = builder.get_object("main_window")
    action_text = builder.get_object("action_text")
    progress = builder.get_object("progess_bar")
    builder.get_object("topbar_text").set_label("Preparing video playback and encoding packages")
    terminal = Vte.Terminal()
    terminal.set_input_enabled(False)
    builder.get_object("main_box").pack_start(terminal, True, True, 10)
    window.connect("destroy", Gtk.main_quit)

    def finished(term, wait_status):
        state = {}
        if os.WIFEXITED(wait_status) and os.WEXITSTATUS(wait_status) == 0:
            try:
                result = subprocess.run(["/usr/bin/nobara-sync", "update-status", "--json"],
                                        check=True, capture_output=True, text=True, timeout=15)
                state = json.loads(result.stdout)
                if not isinstance(state, dict):
                    state = {}
            except (OSError, subprocess.SubprocessError, ValueError):
                pass
        success, heading, message = completion_result(wait_status, state)
        GLib.source_remove(pulse_id)
        progress.set_fraction(1.0 if success else 0.0)
        action_text.set_label(heading)
        dialog = Gtk.MessageDialog(transient_for=window, modal=True,
                                   message_type=Gtk.MessageType.INFO if success else Gtk.MessageType.ERROR,
                                   buttons=Gtk.ButtonsType.OK, text=heading)
        dialog.format_secondary_text(message)
        dialog.run()
        dialog.destroy()
        # Leave failures and their terminal output visible until the user
        # closes the window, rather than immediately hiding the diagnostics.
        if success:
            Gtk.main_quit()

    def pulsing():
        progress.pulse()
        return True

    def spawned(term, pid, error, *unused):
        if error:
            finished(term, 1 << 8)

    action_text.set_label("Preparing codecs and system updates…")
    progress.set_pulse_step(0.05)
    pulse_id = GLib.timeout_add(150, pulsing)
    terminal.connect("child-exited", finished)
    window.show_all()
    terminal.spawn_async(Vte.PtyFlags.DEFAULT, os.environ.get("HOME", "/"),
                         ["/usr/bin/nobara-sync", "install-codecs"], [],
                         GLib.SpawnFlags.DEFAULT, None, None, -1, None, spawned, None)
    Gtk.main()


if __name__ == "__main__":
    main()
