#! /usr/bin/python3
import gi
import sys
from pathlib import Path
gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")
from gi.repository import Gtk, GObject, Vte, GLib
import os, subprocess, time, threading

def is_running_with_sudo_or_pkexec() -> int:
    # Check environment variables first
    if "SUDO_USER" in os.environ:
        return 1
    if "PKEXEC_UID" in os.environ:
        return 2

    return 0

class Application(Gtk.ApplicationWindow):
	### MAIN WINDOW ###
	def __init__(self):
		script_path = Path(__file__).resolve()
		if is_running_with_sudo_or_pkexec() == 0:
			os.execvp(
				"pkexec",
				[
					"pkexec",
					"--disable-internal-agent",
					"env",
					f"DISPLAY={os.environ['DISPLAY']}",
					f"XAUTHORITY={os.environ.get('XAUTHORITY', '')}",
					f"XDG_CURRENT_DESKTOP={os.environ.get('XDG_CURRENT_DESKTOP', '').lower()}",
					f"ORIGINAL_USER_HOME={Path('~').expanduser()!s}",
					f"ORIG_USER={os.getuid()!s}",
					f"PKEXEC_UID={os.getuid()!s}",
					"NO_AT_BRIDGE=1",
					"G_MESSAGES_DEBUG=none",
					sys.executable,
					str(script_path),
				]
				+ sys.argv[1:],
			)
 
		self.column_names = False
		self.drop_nan = False
		self.df = None
		application_id="nobara.multimedia"

		self.builder = Gtk.Builder()
		self.builder.add_from_file(str(script_path.parent) + "/main.ui")
		self.builder.connect_signals(self)

		self.window = self.builder.get_object("main_window")
		self.window.show()

		self.main_box = self.builder.get_object("main_box")
		self.top_box = self.builder.get_object("top_box")
		self.buttom_box = self.builder.get_object("buttom_box")
		self.center_text = self.builder.get_object("center_text")
		self.btn_decline = self.builder.get_object("btn_decline")
		self.btn_accept = self.builder.get_object("btn_accept")
		self.status_logo = self.builder.get_object("status_logo")

		win = self.builder.get_object("main_window")
		win.connect("destroy", Gtk.main_quit)

		self.window = self.builder.get_object("main_window")
		self.window.show()
		
		self.center_text.set_label("Due to U.S. patent laws we are not able to include some important video playback and encoding\n packages on the Nobara installation media, -HOWEVER- these are freely\navailable to download and install with your consent, which we are asking for now! \n\nPlease note that without these packages installed, video playback in some games, browsers,\nand media players will not work correctly.\n\n Additionally, without these packages you will\nbe unable to use video encoding in OBS studio and Blender.\n\n\nWould you like to install the required video playback and encoding packages now to resolve\nthe issue? (strongly recommended)")
		self.status_logo.set_from_icon_name("media-tape", 64)
		self.btn_decline.set_sensitive(True)
		self.btn_accept.set_sensitive(True)

 
	def on_btn_decline_pressed(self, widget):
		self.builder.get_object("main_window").set_visible(False)
		Gtk.main_quit()
	def on_btn_accept_pressed(self, widget):
		self.builder.get_object("main_window").set_visible(False)
		def install():
			script_path = Path(__file__).resolve()
			os.system("python3 " + str(script_path.parent) + "/process.py install")
		t1 = threading.Thread(target=install)
		t1.start()
		Gtk.main_quit()

Application()
Gtk.main()
