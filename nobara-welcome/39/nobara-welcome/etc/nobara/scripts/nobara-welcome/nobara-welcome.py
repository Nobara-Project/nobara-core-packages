import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gio
import subprocess
import os
import os.path
from pathlib import Path

import time
import threading
import subprocess

settings = Gio.Settings.new("org.nobara.welcome")

proc1 = subprocess.Popen(['ps', 'aux'], stdout=subprocess.PIPE)
proc2 = subprocess.Popen(['grep', '/etc/nobara/scripts/nobara-welcome/nobara-welcome.py'], stdin=proc1.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
proc3 = subprocess.Popen(['grep', '-v', 'grep'], stdin=proc2.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
proc4 = subprocess.Popen(['wc', '-l'], stdin=proc3.stdout, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
result = proc4.stdout.read()
print(int(result))
if int(result) > 1:
    quit()

class Application:
    
    ### MAIN WINDOW ###
    def __init__(self):
        
        self.column_names = False
        self.drop_nan = False
        self.df = None
        application_id="org.nobara.welcome"
        
        self.builder = Gtk.Builder()
        self.builder.add_from_file("/etc/nobara/scripts/nobara-welcome/nobara-welcome.ui")
        self.builder.connect_signals(self)
        win = self.builder.get_object("main_Window")
        win.connect("destroy", Gtk.main_quit)
        
        self.window = self.builder.get_object("main_Window")
        self.window.show()

        self.main_Sidebar = self.builder.get_object("main_Sidebar")
        

        ### app state refresh ###
        global app_state_refresh
        app_state_refresh = True
        global window_state_refresh
        window_state_refresh = True
        
        def app_state_refresh_func(): 
            blender_install_button = self.builder.get_object("blender_install_button")
            blender_remove_button = self.builder.get_object("blender_remove_button")
            discord_install_button = self.builder.get_object("discord_install_button")
            discord_remove_button = self.builder.get_object("discord_remove_button")
            kdenlive_install_button = self.builder.get_object("kdenlive_install_button")
            kdenlive_remove_button = self.builder.get_object("kdenlive_remove_button")
            obs_install_button = self.builder.get_object("obs_install_button")
            obs_remove_button = self.builder.get_object("obs_remove_button")
            while app_state_refresh == True:
                blender_output = subprocess.run(["flatpak list | grep org.blender.Blender"], shell=True, stdout=subprocess.DEVNULL)
                if (blender_output.returncode) == 0:
                    blender_install_button.set_sensitive(False)
                    blender_remove_button.set_sensitive(True)
                else:
                    blender_install_button.set_sensitive(True)
                    blender_remove_button.set_sensitive(False)
                discord_output = subprocess.run(["flatpak list | grep com.discordapp.Discord"], shell=True, stdout=subprocess.DEVNULL)
                if (discord_output.returncode) == 0:
                    discord_install_button.set_sensitive(False)
                    discord_remove_button.set_sensitive(True)
                else:
                    discord_install_button.set_sensitive(True)
                    discord_remove_button.set_sensitive(False)
                kdenlive_output = subprocess.run(["rpm -q kdenlive"], shell=True, stdout=subprocess.DEVNULL)
                if (kdenlive_output.returncode) == 0:
                    kdenlive_install_button.set_sensitive(False)
                    kdenlive_remove_button.set_sensitive(True)
                else:
                    kdenlive_install_button.set_sensitive(True)
                    kdenlive_remove_button.set_sensitive(False)
                obs_output = subprocess.run(["rpm -q obs-studio"], shell=True, stdout=subprocess.DEVNULL)
                if (obs_output.returncode) == 0:
                    obs_install_button.set_sensitive(False)
                    obs_remove_button.set_sensitive(True)
                else:
                    obs_install_button.set_sensitive(True)
                    obs_remove_button.set_sensitive(False)                
                time.sleep(1)
        
        t1 = threading.Thread(target=app_state_refresh_func)
        t1.start()
        
        self.main_Sidebar = self.builder.get_object("main_Sidebar")
        self.Stack = self.builder.get_object("Stack")
        self.sidebar_btn = self.builder.get_object("sidebar_btn")
        
        def resolution_refresh_func():
            while window_state_refresh == True:
                        time.sleep(1)
                        if self.window.get_size()[0] < 650:
                                                self.sidebar_btn.show()
                                                if self.sidebar_btn.get_active() == False:
                                                        self.Stack.show()
                                                        self.main_Sidebar.hide()
                                                else:
                                                        self.main_Sidebar.show()
                                                        self.Stack.hide()
                                                        self.main_Sidebar.set_hexpand(True)
                        else:
                                                        self.sidebar_btn.hide()
                                                        self.Stack.show()
                                                        self.main_Sidebar.show()
                                                        self.main_Sidebar.set_hexpand(False)
        
        t2 = threading.Thread(target=resolution_refresh_func)
        t2.start()
        
        def app_state_refresh_kill(self):
            global app_state_refresh
            global window_state_refresh
            app_state_refresh = False
            window_state_refresh = False
        
        win.connect("destroy", app_state_refresh_kill)
        
    ### Start up Switch ###
        
        startup_switch = self.builder.get_object("startup_switch")
        
        startup_switch.set_active(settings.get_boolean("startup-show"))
    
        startup_switch.connect("toggled", lambda btn: settings.set_boolean("startup-show", btn.get_active()))
    
    
    ### Hardcoded icons ###
        if (settings.get_boolean("use-system-icons")) == True:
            update_logo = self.builder.get_object("distrosync_logo_2")
            nvidia_logo = self.builder.get_object("nvidia_logo")
            software_logo = self.builder.get_object("software_logo")
            webapps_logo = self.builder.get_object("webapps_logo")
            
            blender_install_logo = self.builder.get_object("blender_install_logo")
            kdenlive_install_logo = self.builder.get_object("kdenlive_install_logo")
            obs_install_logo = self.builder.get_object("obs_install_logo")
            discord_install_logo = self.builder.get_object("discord_install_logo")
            
            amd_logo = self.builder.get_object("amd_logo")
            rocm_logo = self.builder.get_object("rocm_logo")
            xone_logo = self.builder.get_object("xone_logo")
            protonup_logo = self.builder.get_object("protonup_logo")
            resolve_logo = self.builder.get_object("resolve_logo")
            steamfix_logo = self.builder.get_object("steamfix_logo")

            dm_logo = self.builder.get_object("dm_logo")
            pling_logo = self.builder.get_object("pling_logo")
            
            troubleshoot_logo = self.builder.get_object("troubleshoot_logo")
            doc_logo = self.builder.get_object("doc_logo")
            distrosync_logo = self.builder.get_object("distrosync_logo")
            
            discord_logo = self.builder.get_object("discord_logo")
            reddit_logo = self.builder.get_object("reddit_logo")
            
            patreon_logo = self.builder.get_object("patreon_logo")
            design_logo = self.builder.get_object("design_logo")
            ge_gitlab_logo = self.builder.get_object("ge_gitlab_logo")
            ge_github_logo = self.builder.get_object("ge_github_logo")
            cosmo_github_logo = self.builder.get_object("cosmo_github_logo")
            nobara_project_logo = self.builder.get_object("nobara_project_logo")
            
            ###
            
            update_logo.set_from_icon_name("system-software-update", 80)
            nvidia_logo.set_from_icon_name("nvidia", 80)
            software_logo.set_from_icon_name("media-floppy", 80)
            webapps_logo.set_from_icon_name("applications-internet", 80)
            
            blender_install_logo.set_from_icon_name("blender", 80)
            kdenlive_install_logo.set_from_icon_name("kdenlive", 80)
            obs_install_logo.set_from_icon_name("obs", 80)
            discord_install_logo.set_from_icon_name("discord", 80)
            
            amd_logo.set_from_icon_name("amd", 80)
            rocm_logo.set_from_icon_name("amd", 80)
            xone_logo.set_from_icon_name("input-gaming", 80)
            protonup_logo.set_from_icon_name("net.davidotek.pupgui2", 80)
            resolve_logo.set_from_icon_name("DaVinci_Resolve", 80)

            dm_logo.set_from_icon_name("applications-graphics", 80)
            pling_logo.set_from_icon_name("applications-graphics", 80)
            
            troubleshoot_logo.set_from_icon_name("applications-graphics", 80)
            doc_logo.set_from_icon_name("applications-graphics", 80)
            distrosync_logo.set_from_icon_name("system-software-update", 80)
            
            discord_logo.set_from_icon_name("discord", 80)
            reddit_logo.set_from_icon_name("reddit", 80)
            
            update_logo = self.builder.get_object("distrosync_logo_2")
            nvidia_logo = self.builder.get_object("nvidia_logo")
            software_logo = self.builder.get_object("software_logo")
            webapps_logo = self.builder.get_object("webapps_logo")
            
            blender_install_logo = self.builder.get_object("blender_install_logo")
            kdenlive_install_logo = self.builder.get_object("kdenlive_install_logo")
            obs_install_logo = self.builder.get_object("obs_install_logo")
            discord_install_logo = self.builder.get_object("discord_install_logo")
            
            amd_logo = self.builder.get_object("amd_logo")
            rocm_logo = self.builder.get_object("rocm_logo")
            xone_logo = self.builder.get_object("xone_logo")
            resolve_logo = self.builder.get_object("resolve_logo")

            dm_logo = self.builder.get_object("dm_logo")
            pling_logo = self.builder.get_object("pling_logo")
            
            troubleshoot_logo = self.builder.get_object("troubleshoot_logo")
            doc_logo = self.builder.get_object("doc_logo")
            distrosync_logo = self.builder.get_object("distrosync_logo")
            
            discord_logo = self.builder.get_object("discord_logo")
            reddit_logo = self.builder.get_object("reddit_logo")
            
            patreon_logo = self.builder.get_object("patreon_logo")
            design_logo = self.builder.get_object("design_logo")
            ge_gitlab_logo = self.builder.get_object("ge_gitlab_logo")
            ge_github_logo = self.builder.get_object("ge_github_logo")
            cosmo_github_logo = self.builder.get_object("cosmo_github_logo")
            nobara_project_logo = self.builder.get_object("nobara_project_logo")
            
            update_logo.set_from_icon_name("system-software-update", 80)
            nvidia_logo.set_from_icon_name("nvidia", 80)
            software_logo.set_from_icon_name("media-floppy", 80)
            webapps_logo.set_from_icon_name("applications-internet", 80)
            
            blender_install_logo.set_from_icon_name("blender", 80)
            kdenlive_install_logo.set_from_icon_name("kdenlive", 80)
            obs_install_logo.set_from_icon_name("obs", 80)
            discord_install_logo.set_from_icon_name("discord", 80)
            
            amd_logo.set_from_icon_name("amd", 80)
            rocm_logo.set_from_icon_name("amd", 80)
            xone_logo.set_from_icon_name("input-gaming", 80)
            resolve_logo.set_from_icon_name("DaVinci_Resolve", 80)

            dm_logo.set_from_icon_name("emblem-readonly", 80)
            pling_logo.set_from_icon_name("emblem-downloads", 80)
            
            troubleshoot_logo.set_from_icon_name("emblem-important", 80)
            doc_logo.set_from_icon_name("emblem-documents", 80)
            distrosync_logo.set_from_icon_name("system-software-update", 80)
            
            discord_logo.set_from_icon_name("discord", 80)
            reddit_logo.set_from_icon_name("reddit", 80)
            
            patreon_logo.set_from_icon_name("emblem-favorite", 80)
            design_logo.set_from_icon_name("applications-graphics", 80)
            ge_gitlab_logo.set_from_icon_name("gitlab", 80)
            ge_github_logo.set_from_icon_name("github", 80)
            cosmo_github_logo.set_from_icon_name("github", 80)
            design_logo.set_from_icon_name("applications-graphics", 80)
            ge_gitlab_logo.set_from_icon_name("gitlab", 80)
            ge_github_logo.set_from_icon_name("github", 80)
            cosmo_github_logo.set_from_icon_name("github", 80)
            nobara_project_logo.set_from_icon_name("fedora-logo-icon", 80)
        pass
        
    def on_sidebar_btn_pressed(self, widget):
        time.sleep(1)
        if self.window.get_size()[0] < 650:
                if self.Stack.get_visible() == True:
                        self.sidebar_btn.set_active(False) 
                        self.main_Sidebar.show()
                        self.Stack.hide()
                else:
                        self.sidebar_btn.set_active(True)
                        self.Stack.show()
                        self.main_Sidebar.hide()
    
    
    ### ENTER LOOK WINDOW ###
    def enter_add_software(self, widget):
        install_window =  self.builder.get_object("install_Window")
        install_window.show()
    ### EXIT LOOK WINDOW ###
    def close_add_software(self, widget, event):
        return self.builder.get_object("install_Window").hide_on_delete()
    
    ##### FIRST STEPS ENTRIES #####
    
    #### DRIVER ENTRIES ####
    
    ### NVIDIA ###
    def enter_nvidia(self, widget):
        subprocess.Popen(["/etc/nobara/scripts/nobara-welcome/nvidia.sh"], shell=True)
    ### ROCm ###
    def enter_rocm(self, widget):
        subprocess.Popen(["/usr/lib/pika/welcome/xdg-terminal '/usr/lib/pika/welcome/pkcon-install.sh install nobara-rocm-meta'"], shell=True)
    ### XONE ###
    def enter_xone(self, widget):
        subprocess.Popen(["/usr/bin/nobara-controller-config"], shell=True)
    ### PROTONUP ###
    def enter_protonup(self, widget):
        subprocess.Popen(["/usr/bin/protonup-qt"], shell=True) 
    ### DAVINCI_RESOLVE ###
    def enter_resolve(self, widget):
        subprocess.Popen(["/usr/bin/nobara-resolve-wizard"], shell=True)
    ### STEAM_FIXES ###
    def enter_steamfix(self, widget):
        subprocess.Popen(["/usr/bin/nobara-steam-fixes"], shell=True)

    #### Apps Entries ####
   
    ### APPS ###
    def enter_apps(self, widget):
        subprocess.Popen(["/etc/nobara/scripts/nobara-welcome/apps.sh"], shell=True)
    ### WEBAPPS ###
    def enter_webapps(self, widget):
        subprocess.Popen(["/usr/bin/webapp-manager"], shell=True)

    ##### QUICK SETUP ENTRIES #####
    
    ### LOGIN MANAGER ###
    def enter_dm(self, widget):
        subprocess.Popen(["/usr/bin/nobara-login-config"], shell=True)
    ### PLING ###
    def enter_pling(self, widget):
        subprocess.Popen(["xdg-open https://pling.com/"], shell=True)

    #### TROUBLESHOOT ENTRIES ####
    
    ### Troubleshoot ###
    def enter_troubleshoot(self, widget):
        subprocess.Popen(["xdg-open https://feed.nobaraproject.org/en"], shell=True)
    ### Docs ###
    def enter_doc(self, widget):
        subprocess.Popen(["xdg-open https://nobaraproject.org/docs/"], shell=True)
    ### Distro Sync ###
    def enter_distrosync(self, widget):
        subprocess.Popen(["/usr/bin/nobara-sync"], shell=True)


    #### COMMUNITY ENTRIES ####
    
    ### discord ###
    def enter_discord(self, widget):
        subprocess.Popen(["xdg-open https://discord.gg/6y3BdzC"], shell=True)
    ### reddit ###
    def enter_reddit(self, widget):
        subprocess.Popen(["xdg-open https://www.reddit.com/r/NobaraProject/"], shell=True)
        
    #### Contribute ENTRIES ####
    
    ### patreon ###
    def enter_patreon(self, widget):
        subprocess.Popen(["xdg-open https://www.patreon.com/gloriouseggroll"], shell=True)
    ### design ###
    def enter_design(self, widget):
        subprocess.Popen(["xdg-open https://discord.com/channels/110175050006577152/1015154123114549309"], shell=True)
    ### GE GITLAB ###
    def enter_ge_gitlab(self, widget):
        subprocess.Popen(["xdg-open https://gitlab.com/GloriousEggroll"], shell=True)
    ### GE GITHUB ###
    def enter_ge_github(self, widget):
        subprocess.Popen(["xdg-open https://github.com/GloriousEggroll"], shell=True)
    ### COSMO GITHUB ###
    def enter_cosmo_github(self, widget):
        subprocess.Popen(["xdg-open https://github.com/CosmicFusion"], shell=True)
    ### NOBARA GITHUB ###
    def enter_nobara_github(self, widget):
        subprocess.Popen(["xdg-open https://github.com/Nobara-Project"], shell=True)
    ###############################################################
    #### Install Window ####
    
    ### Blender ###
    def enter_install_blender(self, widget):
        subprocess.Popen(["/etc/nobara/scripts/nobara-welcome/xdg-terminal '/etc/nobara/scripts/nobara-welcome/flatpak-install.sh install org.blender.Blender'"], shell=True)
    def enter_remove_blender(self, widget):
        subprocess.Popen(["/etc/nobara/scripts/nobara-welcome/xdg-terminal '/etc/nobara/scripts/nobara-welcome/flatpak-install.sh uninstall org.blender.Blender'"], shell=True)
    
    ### KDENLIVE ###
    def enter_install_kdenlive(self, widget):
        subprocess.Popen(["/etc/nobara/scripts/nobara-welcome/xdg-terminal '/etc/nobara/scripts/nobara-welcome/pkcon-install.sh install kdenlive'"], shell=True)
    def enter_remove_kdenlive(self, widget):
        subprocess.Popen(["/etc/nobara/scripts/nobara-welcome/xdg-terminal '/etc/nobara/scripts/nobara-welcome/pkcon-install.sh remove kdenlive catdoc dvdauthor kdenlive kf5-kfilemetadata kf5-knotifyconfig qt5-qtnetworkauth'"], shell=True)
    ### OBS STUDIO ###
    def enter_install_obs(self, widget):
        subprocess.Popen(["/etc/nobara/scripts/nobara-welcome/xdg-terminal '/etc/nobara/scripts/nobara-welcome/pkcon-install.sh install obs-studio'"], shell=True)
    def enter_remove_obs(self, widget):
        subprocess.Popen(["/etc/nobara/scripts/nobara-welcome/xdg-terminal '/etc/nobara/scripts/nobara-welcome/pkcon-install.sh remove obs-studio'"], shell=True)
    ### DISCORD ###
    def enter_install_discord(self, widget):
        subprocess.Popen(["/etc/nobara/scripts/nobara-welcome/xdg-terminal '/etc/nobara/scripts/nobara-welcome/flatpak-install.sh install com.discordapp.Discord'"], shell=True)
    def enter_remove_discord(self, widget):
        subprocess.Popen(["/etc/nobara/scripts/nobara-welcome/xdg-terminal '/etc/nobara/scripts/nobara-welcome/flatpak-install.sh uninstall com.discordapp.Discord'"], shell=True)
        
Application()
Gtk.main()
