#! /usr/bin/python3
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")
from gi.repository import Gtk, GObject, Vte, GLib
import os, subprocess, time, threading, sys

print(sys.argv[1])

column_names = False
drop_nan = False
df = None
application_id="resolve.wizard"
        
builder = Gtk.Builder()
builder.add_from_file("/etc/nobara/scripts/nobara-davinci/process.ui")
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
        
def install():
	progress_bar.pulse()
	progress_bar.set_pulse_step(100.0)
	action_text.set_label("Installing...")
	terminal.spawn_sync(
		Vte.PtyFlags.DEFAULT,
		os.environ['HOME'],
		["/etc/nobara/scripts/nobara-davinci/install.sh"],
		[],
		GLib.SpawnFlags.DO_NOT_REAP_CHILD,
		None,
		None,
		)
install()
    
            
Gtk.main()
