#! /usr/bin/python3
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")
from gi.repository import Gtk, GObject, Vte, GLib
import os, subprocess, time, threading



nvgpupresent = subprocess.run(["/etc/nobara/scripts/cosmo-nvidia-wizard/gpu-utils detecthw"], shell=True)
nvkernmodpresent = subprocess.run(["/etc/nobara/scripts/cosmo-nvidia-wizard/gpu-utils detectdriver"], shell=True)
if (nvgpupresent.returncode) == 0:
    nvgpuname = subprocess.check_output(["/etc/nobara/scripts/cosmo-nvidia-wizard/gpu-utils getname"], stderr=subprocess.STDOUT, shell=True)
nvdriverpresent = subprocess.run(["rpm -q nvidia-driver"], shell=True)

class Application(Gtk.ApplicationWindow):
    ### MAIN WINDOW ###
    def __init__(self):
         
        self.column_names = False
        self.drop_nan = False
        self.df = None
        application_id="cosmo.nvidia.wizard"
        
        self.builder = Gtk.Builder()
        self.builder.add_from_file("/etc/nobara/scripts/cosmo-nvidia-wizard/main.ui")
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
        self.topbar_text = self.builder.get_object("topbar_text")
        
        win = self.builder.get_object("main_window")
        win.connect("destroy", Gtk.main_quit)
        
        self.window = self.builder.get_object("main_window")
        self.window.show()
 
        if (nvgpupresent.returncode) == 0:
            self.topbar_text.set_label(f"{nvgpuname.decode('ascii')} detected!")
            if (nvkernmodpresent.returncode) != 0:
                self.center_text.set_label("We have Detected an NVIDIA GPU on your system\nAdditional Proprietary Drivers are required!\nNote:\nBy Installing these driver you agree to:\nhttps://www.nvidia.com/content/DriverDownloads/licence.php")
                self.status_logo.set_from_icon_name("nobara-nvidia", 64)
                self.btn_decline.set_sensitive(True)
                self.btn_accept.set_sensitive(True)
                def accept():
                    os.system("zenity --info --title=NVIDIA GPU Setup Wizard' --width=600 --text='We will not ask again until your next reboot.'")
            else:
                self.center_text.set_label("Your NVIDIA GPU is fully setup and ready to GO!\nNo further action is required!\nAre you trying to remove them?")
                self.status_logo.set_from_icon_name("nobara-nvidia", 64)
                self.btn_decline.set_sensitive(True)
                self.btn_accept.set_sensitive(True) 
        else:
                if (nvdriverpresent.returncode) == 0:
                    self.center_text.set_label("We have Detected the NVIDIA GPU Driver on your system\nBut not an NVIDIA GPU\nWould you like to remove it?")
                    self.status_logo.set_from_icon_name("user-trash", 64)
                    self.btn_decline.set_sensitive(True)
                    self.btn_accept.set_sensitive(True)                    

    def on_btn_decline_pressed(self, widget):
        self.builder.get_object("main_window").set_visible(False)
        os.system("zenity --info --title='NVIDIA GPU Setup Wizard' --width=600 --text='We will not ask again until your next reboot'.")
        Gtk.main_quit()
    
    if (nvgpupresent.returncode) == 0:
        if (nvkernmodpresent.returncode) != 0:
            def on_btn_accept_pressed(self, widget):
                self.builder.get_object("main_window").set_visible(False)
                def install():
                    os.system("python3 /etc/nobara/scripts/cosmo-nvidia-wizard/process.py install")    
                t1 = threading.Thread(target=install)
                t1.start()
                Gtk.main_quit()
        else:
            def on_btn_accept_pressed(self, widget):
                self.builder.get_object("main_window").set_visible(False)
                def remove():
                    os.system("python3 /etc/nobara/scripts/cosmo-nvidia-wizard/process.py remove")    
                t1 = threading.Thread(target=remove)
                t1.start()
                Gtk.main_quit()
    else:
            if (nvdriverpresent.returncode) == 0:
                self.builder.get_object("main_window").set_visible(False)
                def remove():
                    os.system("python3 /etc/nobara/scripts/cosmo-nvidia-wizard/process.py remove")    
                t1 = threading.Thread(target=remove)
                t1.start()
                Gtk.main_quit()
        
        
        
 
Application()
Gtk.main()
