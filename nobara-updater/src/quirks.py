#!/usr/bin/python3
import logging
import threading
import os
import subprocess
import shutil
import pwd
import re
from datetime import datetime
from pathlib import Path

import gi  # type: ignore[import]
from packaging.version import parse as parse_version

gi.require_version("Gtk", "3.0")
gi.require_version("GLib", "2.0")
gi.require_version("Flatpak", "1.0")

from yumex.constants import BACKEND  # type: ignore[import]

if BACKEND == "DNF5":
    from nobara_updater.dnf5 import PackageUpdater, updatechecker  # type: ignore[import]
else:
    from nobara_updater.dnf4 import PackageUpdater, updatechecker  # type: ignore[import]


class QuirkFixup:
    def __init__(self, logger=None):
        self.logger = logger if logger else logging.getLogger("nobara-updater.quirks")

    def system_quirk_fixup(self):
        package_names = updatechecker()
        action = "upgrade"
        perform_kernel_actions = 0
        perform_reboot_request = 0
        perform_refresh = 0

        # QUIRK 1: Make sure to update the updater itself and refresh before anything
        update_self = [
            "nobara-welcome",
            "nobara-updater",
        ]
        if any(pkg in package_names for pkg in update_self):
            update_self = [
                pkg for pkg in package_names if "nobara-welcome" in pkg or "nobara-updater" in pkg
            ]
            log_message = "An update for the Update System app has been detected, updating self...\n"
            self.update_core_packages(update_self, action, log_message)
            perform_refresh = 1

        # QUIRK 2: Make sure to refresh the repositories and gpg-keys before anything
        critical_packages = [
            "fedora-gpg-keys",
            "nobara-gpg-keys",
            "fedora-repos",
            "nobara-repos",
        ]
        if any(pkg in package_names for pkg in critical_packages):
            critical_updates = [
                pkg for pkg in package_names if pkg in critical_packages
            ]
            log_message = "Updates for repository packages detected: {}. Updating these first...\n".format(
                ", ".join(critical_updates)
            )
            self.update_core_packages(critical_updates, action, log_message)
            perform_refresh = 1

        # QUIRK 3: Make sure to reinstall rpmfusion repos if they do not exist
        if (
            self.check_and_install_rpmfusion(
                "/etc/yum.repos.d/rpmfusion-free.repo", "rpmfusion-free-release"
            )
            == 1
        ):
            perform_refresh = 1
        if (
            self.check_and_install_rpmfusion(
                "/etc/yum.repos.d/rpmfusion-free-updates.repo", "rpmfusion-free-release"
            )
            == 1
        ):
            perform_refresh = 1
        if (
            self.check_and_install_rpmfusion(
                "/etc/yum.repos.d/rpmfusion-nonfree.repo", "rpmfusion-nonfree-release"
            )
            == 1
        ):
            perform_refresh = 1
        if (
            self.check_and_install_rpmfusion(
                "/etc/yum.repos.d/rpmfusion-nonfree-updates.repo",
                "rpmfusion-nonfree-release",
            )
            == 1
        ):
            perform_refresh = 1

        # QUIRK 4: Don't allow kde discover to manage system packages or updates
        # Check if the specified package is installed
        discover_packagekit_name = "plasma-discover-packagekit"
        result = subprocess.run(
            ["rpm", "-q", discover_packagekit_name], capture_output=True, text=True
        )
        if result.returncode == 0:  # Check if the package is installed
            self.logger.info("%s is installed.\n", discover_packagekit_name)
            self.logger.info(
                "%s does not follow repository priority settings, it is not safe to use for managing system packages. Removing...\n",
                discover_packagekit_name,
            )
            self.logger.info("Discover should only be used for flatpak management.\n")
            updater_thread = threading.Thread(
                target=self.run_package_updater,
                args=([discover_packagekit_name], "remove"),
            )
            updater_thread.start()
            updater_thread.join()

        # QUIRK 5: Make sure to run both dracut and akmods if any kmods  or kernel packages were updated.
        # Check if any packages contain "kernel" or "akmod"
        kernel_kmod_packages = [
            pkg for pkg in package_names if "kernel" in pkg or "akmod" in pkg
        ]
        if kernel_kmod_packages:
            perform_kernel_actions = 1
            perform_reboot_request = 1

        # QUIRK 6: If kwin or mutter are being updated, ask for a reboot.
        de_update_packages = [
            pkg for pkg in package_names if "kwin" in pkg or "mutter" in pkg
        ]
        if de_update_packages:
            perform_reboot_request = 1

        # QUIRK 7: Install HHD or InputPlumber and handheld packages for Legion Go and ROG Ally, cleanup old packages
        gamescope_hh = "gamescope-handheld-common"
        check_gamescope_hh = subprocess.run(
            ["rpm", "-q", gamescope_hh], capture_output=True, text=True
        )

        hhd_name = "hhd"
        check_hhd = subprocess.run(
            ["rpm", "-q", hhd_name], capture_output=True, text=True
        )

        ip_name = "inputplumber"
        check_ip = subprocess.run(
            ["rpm", "-q", ip_name], capture_output=True, text=True
        )

        rogfw_name = "rogally-firmware"
        check_rogfw = subprocess.run(
            ["rpm", "-q", rogfw_name], capture_output=True, text=True
        )

        check_legion = subprocess.run(
            "dmesg | grep 'Legion Go'", capture_output=True, text=True, shell=True
        )
        check_ally = subprocess.run(
            "dmesg | grep 'ROG Ally'", capture_output=True, text=True, shell=True
        )

        legion_detected = check_legion.returncode == 0
        ally_detected = check_ally.returncode == 0

        hhd_notinstalled = check_hhd.returncode != 0
        ip_notinstalled = check_ip.returncode != 0
        rogfw_notinstalled = check_rogfw.returncode != 0
        gamescope_hh_notinstalled = check_gamescope_hh.returncode != 0

        if legion_detected or ally_detected:
            self.logger.info(
                "Found Legion Go or ROG Ally, performing old package cleanup and installing hhd (Handheld Daemon)"
            )
            remove_names = []
            updatelist  = []

            check_handygccs = subprocess.run(
                ["rpm", "-q", "HandyGCCS"], capture_output=True, text=True
            )
            if check_handygccs.returncode == 0:
                subprocess.run(
                    ["systemctl", "disable", "--now", "handycon"],
                    capture_output=True,
                    text=True,
                )
                remove_names.append("HandyGCCS")

            check_lgcd = subprocess.run(
                ["rpm", "-q", "lgcd"], capture_output=True, text=True
            )
            if check_lgcd.returncode == 0:
                remove_names.append("lgcd")

            check_rogue_enemy = subprocess.run(
                ["rpm", "-q", "rogue-enemy"], capture_output=True, text=True
            )
            if check_rogue_enemy.returncode == 0:
                remove_names.append("rogue-enemy")

            if legion_detected:
                if hhd_notinstalled:
                    updatelist.append(hhd_name)

            if ally_detected:
                if not hhd_notinstalled:
                    remove_names.append(hhd_name)
                if ip_notinstalled:
                    updatelist.append(ip_name)
                if rogfw_notinstalled:
                    updatelist.append(rogfw_name)

            if gamescope_hh_notinstalled:
                updatelist.append(gamescope_hh)

            if remove_names:
                PackageUpdater(remove_names, "remove", None)

            if updatelist:
                PackageUpdater(updatelist, "install", None)

        # Also check if device is steamdeck, if so install jupiter packages
        check_galileo = subprocess.run(
            "dmesg | grep 'Galileo'", capture_output=True, text=True, shell=True
        )
        check_jupiter = subprocess.run(
            "dmesg | grep 'Jupiter'", capture_output=True, text=True, shell=True
        )

        jupiter_hw = "jupiter-hw-support"
        check_jupiter_hw = subprocess.run(
            ["rpm", "-q", jupiter_hw], capture_output=True, text=True
        )

        jupiter_fan = "jupiter-fan-control"
        check_jupiter_fan = subprocess.run(
            ["rpm", "-q", jupiter_fan], capture_output=True, text=True
        )

        steamdeck_dsp = "steamdeck-dsp"
        check_steamdeck_dsp = subprocess.run(
            ["rpm", "-q", steamdeck_dsp], capture_output=True, text=True
        )

        steamdeck_firmware = "steamdeck-firmware"
        check_steamdeck_firmware = subprocess.run(
            ["rpm", "-q", steamdeck_firmware], capture_output=True, text=True
        )

        galileo_detected = check_galileo.returncode == 0
        jupiter_detected = check_jupiter.returncode == 0
        jupiter_hw_installed = check_jupiter_hw.returncode != 0
        jupiter_fan_installed = check_jupiter_fan.returncode != 0
        steamdeck_dsp_installed = check_steamdeck_dsp.returncode != 0
        steamdeck_firmware_installed = check_steamdeck_firmware.returncode != 0

        jupiter_install = []
        if (galileo_detected or jupiter_detected):

            if jupiter_hw_installed:
                jupiter_install.append(jupiter_hw)
            if jupiter_fan_installed:
                jupiter_install.append(jupiter_fan)
            if steamdeck_dsp_installed:
                jupiter_install.append(steamdeck_dsp)
            if steamdeck_firmware_installed:
                jupiter_install.append(steamdeck_firmware)

            PackageUpdater(jupiter_install, "install", None)

        # QUIRK 8: winehq-staging packaging changed in version 9.9. We need to completely remove older versions first.
        result = subprocess.run(
            ["rpm", "-q", "winehq-staging"], capture_output=True, text=True, check=True
        )
        wine_version = result.stdout.strip().split("-")[2]
        if wine_version and parse_version(wine_version) < parse_version("9.9"):
            self.logger.info("Upstream wine packaging has changed, fixing conflicts")
            remove_names = []

            check_winehq_staging = subprocess.run(
                ["rpm", "-q", "winehq-staging"], capture_output=True, text=True
            )
            if check_winehq_staging.returncode == 0:
                remove_names.append("winehq-staging")

            check_wine_staging64 = subprocess.run(
                ["rpm", "-q", "wine-staging64"], capture_output=True, text=True
            )
            if check_wine_staging64.returncode == 0:
                remove_names.append("wine-staging64")

            check_wine_staging_common = subprocess.run(
                ["rpm", "-q", "wine-staging-common"], capture_output=True, text=True
            )
            if check_wine_staging_common.returncode == 0:
                remove_names.append("wine-staging-common")

            if remove_names:
                PackageUpdater(remove_names, "remove", None)

            PackageUpdater(["winehq-staging"], "install", None)

        # QUIRK 9: Obsolete package cleanup
        obsolete = [
            "kf5-baloo-file",
            "supergfxctl-plasmoid",
            "layer-shell-qt5",
            "herqq",
            "hfsutils",
            "okular5-libs",
            "fedora-workstation-repositories",
            "gnome-shell-extension-supergfxctl-gex",
            "mesa-demos",
            "okular5-part",
        ]
        obsolete_names = []
        for package in obsolete:
            obsolete_check = subprocess.run(
                ["rpm", "-q", package], capture_output=True, text=True
            )
            if obsolete_check.returncode == 0:
                obsolete_names.append(package)
        if obsolete_names != []:
            self.logger.info("Found obsolete packages, removing...")
            PackageUpdater(obsolete_names, "remove", None)

        # QUIRK 10: Problematic package cleanup
        problematic = [
            "unrar",
            "qt5-qtwebengine-freeworld",
            "qt6-qtwebengine-freeworld",
            "qgnomeplatform-qt6",
            "qgnomeplatform-qt5",
            "musescore",
            "okular5-libs",
            "fedora-workstation-repositories",
            "gnome-shell-extension-supergfxctl-gex",
            "mesa-demos",
        ]
        problematic_names = []
        for package in problematic:
            problematic_check = subprocess.run(
                ["rpm", "-q", package], capture_output=True, text=True
            )
            if problematic_check.returncode == 0:
                problematic_names.append(package)
        if problematic_names != []:
            self.logger.info("Found problematic packages, removing...")
            PackageUpdater(problematic_names, "remove", None)

        # QUIRK 11: Cleanup incompatible package versions from n39 kde6:
        incompat = [
            "kf5-kxmlgui-5.116.0-2.fc39.x86_64",
            "kf5-kirigami2-5.116.0-2.fc39.x86_64",
            "kf5-kiconthemes-5.116.0-2.fc39.x86_64",
            "kf5-ki18n-5.116.0-2.fc39.x86_64"
        ]
        incompat_names = []
        for package in incompat:
            incompat_check = subprocess.run(
                ["rpm", "-q", package], capture_output=True, text=True
            )
            if incompat_check.returncode == 0:
                incompat_names.append(package.replace("-5.116.0-2.fc39.x86_64", ""))
        if incompat_names != []:
            self.logger.info("Found incompatible package versions, fixing them...")
            for package in incompat:
                incompat_check = subprocess.run(
                    ["rpm", "-e", "--nodeps", package], capture_output=True, text=True
                )
        reinstall = []
        for package in incompat:
            incompat_check = subprocess.run(
                ["rpm", "-q", package.replace("-5.116.0-2.fc39.x86_64", "")], capture_output=True, text=True
            )
            if incompat_check.returncode != 0:
                reinstall.append(package.replace("-5.116.0-2.fc39.x86_64", ""))
        if reinstall != []:
            PackageUpdater(reinstall, "install", None)

        # QUIRK 12: Media fixup
        media = [
            "ffmpeg-libs.x86_64",
            "ffmpeg-libs.i686",
            "x264.x86_64",
            "x265.x86_64",
            "noopenh264.x86_64",
            "noopenh264.i686",
            "libavcodec-free.x86_64",
            "libavcodec-free.i686",
            "mesa-va-drivers.x86_64",
            "mesa-vdpau-drivers.x86_64",
            "mesa-va-drivers-freeworld.x86_64",
            "mesa-vdpau-drivers-freeworld.x86_64",
            "gstreamer1-plugins-bad-free-extras.x86_64",
            "gstreamer1-plugins-bad-free-extras.i686",
            "mozilla-openh264.x86_64",
            "mesa-demos",
        ]

        media_fixup = 0
        for package in media:
            media_fixup_check = subprocess.run(
                ["rpm", "-q", package], capture_output=True, text=True
            )

            if package == "libavcodec-free.x86_64":
                libavcodec_freeworld_check = subprocess.run(
                    ["rpm", "-q", "libavcodec-freeworld.x86_64"],
                    capture_output=True,
                    text=True,
                )
                if (
                    libavcodec_freeworld_check.returncode == 0
                    and media_fixup_check.returncode != 0
                ):
                    media_fixup = 1
                    break
            elif package == "libavcodec-free.i686":
                libavcodec_freeworld_check = subprocess.run(
                    ["rpm", "-q", "libavcodec-freeworld.i686"],
                    capture_output=True,
                    text=True,
                )
                if (
                    libavcodec_freeworld_check.returncode == 0
                    and media_fixup_check.returncode != 0
                ):
                    media_fixup = 1
                    break
            elif package in [
                "mesa-va-drivers-freeworld.x86_64",
                "mesa-vdpau-drivers-freeworld.x86_64",
                "gstreamer1-plugins-bad-free-extras.x86_64",
                "gstreamer1-plugins-bad-free-extras.i686",
                "mozilla-openh264.x86_64",
            ]:
                if media_fixup_check.returncode != 0:
                    media_fixup = 1
                    break
            else:
                if media_fixup_check.returncode == 0:
                    media_fixup = 1
                    break

        # Remove newinstall needs-update tracker
        if Path.exists(Path("/etc/nobara/newinstall")):
            try:
                # Remove the file
                Path("/etc/nobara/newinstall").unlink()
            except OSError as e:
                logger.error("Error: %s", e.strerror)

        return (
            perform_kernel_actions,
            perform_reboot_request,
            media_fixup,
            perform_refresh,
        )

        # QUIRK 13: Clear plasmashell cache if a plasma-workspace update is available
        # Function to run the rpm command and get the output
        def get_rpm_info():
            try:
                result = subprocess.run(['rpm', '-qi', 'plasma-workspace'], capture_output=True, text=True, check=True)
                return result.stdout
            except subprocess.CalledProcessError as e:
                print(f"Failed to run rpm command: {e}")
                return None

        # Function to parse the rpm output
        def parse_rpm_info(rpm_output):
            version_pattern = re.compile(r"Version\s+:\s+(\d+\.\d+\.\d+)")
            install_date_pattern = re.compile(r"Install Date\s+:\s+(.+)")
            
            version_match = version_pattern.search(rpm_output)
            install_date_match = install_date_pattern.search(rpm_output)
            
            if version_match and install_date_match:
                version = version_match.group(1)
                install_date_str = install_date_match.group(1)
                install_date = datetime.strptime(install_date_str, "%a %d %b %Y %I:%M:%S %p %Z")
                return version, install_date
            else:
                return None, None

        # Function to get the list of all user home directories
        def get_all_user_home_directories():
            home_directories = []
            for user in pwd.getpwall():
                if user.pw_uid >= 1000:  # Filter out system users
                    home_directories.append(user.pw_dir)
            return home_directories

        # Function to check and delete qmlcache folder if older than install date
        def check_and_delete_qmlcache(home_dir, install_date):
            qmlcache_dir = os.path.join(home_dir, ".cache", "plasmashell", "qmlcache")
            if os.path.exists(qmlcache_dir):
                qmlcache_mtime = datetime.fromtimestamp(os.path.getmtime(qmlcache_dir))
                if qmlcache_mtime < install_date:
                    try:
                        shutil.rmtree(qmlcache_dir)
                        print(f"Deleted '{qmlcache_dir}' directory successfully")
                    except Exception as e:
                        print(f"Failed to delete '{qmlcache_dir}': {e}")
                else:
                    print(f"'{qmlcache_dir}' is not older than the install date")
            else:
                print(f"Directory '{qmlcache_dir}' does not exist")

        # Main script execution
        rpm_output = get_rpm_info()
        if rpm_output:
            version, install_date = parse_rpm_info(rpm_output)
            if version == "6.1.3" and install_date:
                for home_dir in get_all_user_home_directories():
                    check_and_delete_qmlcache(home_dir, install_date)
            else:
                print("Version is not 6.1.3 or failed to parse install date")
        else:
            print("Failed to get rpm info")

    def update_core_packages(
        self, package_list: list[str], action: str, log_message: str
    ) -> None:
        self.logger.info(log_message)

        # Run the updater in a separate thread and wait for it to finish
        updater_thread = threading.Thread(
            target=self.run_package_updater, args=(package_list, action)
        )
        updater_thread.start()
        updater_thread.join()  # Wait for the updater thread to finish

    def check_and_install_rpmfusion(self, filepath: str, packagename: str) -> int:
        # Check if the specified file exists
        if not Path(filepath).exists():
            self.logger.info(
                "%s not found. Checking for %s package...\n", filepath, packagename
            )

            # Check if the specified package is installed
            result = subprocess.run(
                ["rpm", "-q", packagename], capture_output=True, text=True
            )
            if result.returncode == 0:
                self.logger.info("%s is installed. Reinstalling...\n", packagename)
                updater_thread = threading.Thread(
                    target=self.run_package_updater, args=([packagename], "remove")
                )
                updater_thread.start()
                updater_thread.join()
                updater_thread = threading.Thread(
                    target=self.run_package_updater, args=([packagename], "install")
                )
                updater_thread.start()
                updater_thread.join()
            else:
                self.logger.info("%s is not installed. Installing...\n", packagename)
                updater_thread = threading.Thread(
                    target=self.run_package_updater, args=([packagename], "install")
                )
                updater_thread.start()
                updater_thread.join()
            return 1
        return 0

    def run_package_updater(self, package_names: list[str], action: str) -> None:
        # Initialize the PackageUpdater
        PackageUpdater(package_names, action, None)

