#! /usr/bin/python3
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")
from gi.repository import Gtk, GObject, Vte, GLib
import os, subprocess, time, threading, sys
from pathlib import Path

script_path = Path(__file__).resolve()
column_names = False
drop_nan = False
df = None
application_id="nobara.multimedia"
        
builder = Gtk.Builder()
builder.add_from_file(str(script_path.parent) + "/process.ui")
#builder.connect_signals()
        
window = builder.get_object("main_window")
window.show()
        
main_box = builder.get_object("main_box")
top_box = builder.get_object("top_box")
buttom_box = builder.get_object("buttom_box")
action_text = builder.get_object("action_text")
progress_bar = builder.get_object("progess_bar")
media_logo = builder.get_object("media_logo")
topbar_text = builder.get_object("topbar_text")
        
terminal=Vte.Terminal()
terminal.set_input_enabled(False)
main_box.pack_start(terminal, True, True, 10)
        
win = builder.get_object("main_window")
win.connect("destroy", Gtk.main_quit)
win.show_all()

def on_child_exited(term, status, progress_bar, action_text):

	# status is the child's exit status (like waitpid)
	# Show success/failure as you prefer:
	if os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0:
		progress_bar.set_fraction(1.0)
		action_text.set_label("Complete!")
		# Flush GTK so changes are visible *before* zenity pops up
		while Gtk.events_pending():
			Gtk.main_iteration()
		subprocess.run([
			"zenity", "--info",
			"--title=Video Playback and Encoding enablement",
			"--width=600",
			"--text=Media codecs package installation complete!"
		])
	else:
		progress_bar.set_fraction(1.0)
		action_text.set_label("Error.")
		# Flush GTK so changes are visible *before* zenity pops up
		while Gtk.events_pending():
			Gtk.main_iteration()
		subprocess.run([
			"zenity", "--error",
			"--title=Video Playback and Encoding enablement",
			"--width=600",
			"--text=Media codecs package installation failed."
		])
	Gtk.main_quit()

def install():
	progress_bar.pulse()
	progress_bar.set_pulse_step(100.0)
	action_text.set_label("Installing...")

	terminal.connect(
		"child-exited",
		lambda term, status: on_child_exited(term, status, progress_bar, action_text)
	)

	# Start the process in the terminal (async, non-blocking)
	terminal.spawn_async(
		Vte.PtyFlags.DEFAULT,
		os.environ["HOME"],
		["/usr/bin/nobara-sync", "install-codecs"],
		[],
		GLib.SpawnFlags.DO_NOT_REAP_CHILD,
		None, None,
		-1, None, None, None
	)

install()
Gtk.main()
