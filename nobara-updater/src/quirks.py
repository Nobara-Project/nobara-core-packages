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
        # START QUIRKS LIST
        # QUIRK 1: Make sure to update the updater itself and refresh before anything
        self.logger.info("QUIRK: Make sure to update the updater itself and refresh before anything.")
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
        self.logger.info("QUIRK: Make sure to refresh the repositories and gpg-keys before anything.")
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
        self.logger.info("QUIRK: Make sure to reinstall rpmfusion repos if they do not exist.")
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
        self.logger.info("QUIRK: Don't allow kde discover to manage system packages or updates.")
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
        self.logger.info("QUIRK: Make sure to run both dracut and akmods if any kmods  or kernel packages were updated.")
        # Check if any packages contain "kernel" or "akmod"
        kernel_kmod_packages = [
            pkg for pkg in package_names if "kernel" in pkg or "akmod" in pkg
        ]
        if kernel_kmod_packages:
            perform_kernel_actions = 1
            perform_reboot_request = 1

        # QUIRK 6: If kwin or mutter are being updated, ask for a reboot.
        self.logger.info("QUIRK: If kwin or mutter are being updated, ask for a reboot.")
        de_update_packages = [
            pkg for pkg in package_names if "kwin" in pkg or "mutter" in pkg
        ]
        if de_update_packages:
            perform_reboot_request = 1

        # QUIRK 7: Install HHD for Controller input, install steam firmware for steamdecks. Cleanup old packages.
        remove_names = []
        updatelist  = []
        controller_update_config_path = '/etc/nobara/handheld_packages/autoupdate.conf'
        controller_update_config = True

        # Check if the file exists
        if os.path.exists(controller_update_config_path):
            try:
                # Open the file and read its contents
                with open(controller_update_config_path, 'r') as file:
                    content = file.read().strip()
                    if content == "disabled":
                        controller_update_config = False
            except Exception as e:
                self.logger.info(f"An error occurred while reading the file: {e}")

        if controller_update_config == True:
            self.logger.info("QUIRK: Install InputPlumber for Controller input, install steam firmware for steamdecks. Cleanup old packages.")

            # Remove any deprecated controller input handlers
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

            check_hhd = subprocess.run(
                ["rpm", "-q", "hhd"], capture_output=True, text=True
            )
            if check_hhd.returncode == 0:
                remove_names.append("hhd")

            check_hhd_ui = subprocess.run(
                ["rpm", "-q", "hhd-ui"], capture_output=True, text=True
            )
            if check_hhd_ui.returncode == 0:
                remove_names.append("hhd-ui")

            check_hhd_adjustor = subprocess.run(
                ["rpm", "-q", "adjustor"], capture_output=True, text=True
            )
            if check_hhd_adjustor.returncode == 0:
                remove_names.append("adjustor")

            # Install InputPlumber
            check_ip = subprocess.run(
                ["rpm", "-q", "inputplumber"], capture_output=True, text=True
            )
            if check_ip.returncode != 0:
                updatelist.append("inputplumber")

            # Install ROG Ally/X firmware if needed
            check_ally = subprocess.run(
                "dmesg | grep 'ROG Ally'", capture_output=True, text=True, shell=True
            )
            ally_detected = check_ally.returncode == 0
            if ally_detected:
                self.logger.info(
                    "Found ROG Ally, installing firmware"
                )

                rogfw_name = "rogally-firmware"
                check_rogfw = subprocess.run(
                    ["rpm", "-q", rogfw_name], capture_output=True, text=True
                )
                rogfw_notinstalled = check_rogfw.returncode != 0
                if rogfw_notinstalled:
                    updatelist.append(rogfw_name)

        check_gamescope_htpc = subprocess.run(
            ["rpm", "-q", "gamescope-htpc-common"], capture_output=True, text=True
        )
        gamescope_htpc_installed = check_gamescope_htpc.returncode == 0

        check_gamescope_session_common = subprocess.run(
            ["rpm", "-q", "gamescope-session-common"], capture_output=True, text=True
        )
        gamescope_session_common_installed = check_gamescope_session_common.returncode == 0
        if gamescope_htpc_installed:
            if not gamescope_session_common_installed:
                # Return to normal grub + plymouth first.
                plymouth_scripts_name = "plymouth-plugin-script"
                check_plymouth_scripts = subprocess.run(
                    ["rpm", "-q", plymouth_scripts_name], capture_output=True, text=True
                )
                plymouth_scripts_notinstalled = check_plymouth_scripts.returncode != 0
                if plymouth_scripts_notinstalled:
                    PackageUpdater(["plymouth-plugin-script"], "install", None)

                # Run the 'plymouth-set-default-theme' command and capture its output
                check_theme = subprocess.run(
                    ["plymouth-set-default-theme"],
                    capture_output=True,
                    text=True
                )
                if 'steamos' in check_theme.stdout:
                    # Fixup grub so it's more steamos-like
                    subprocess.run(
                        ["plymouth-set-default-theme", "bgrt"],
                        capture_output=True,
                        text=True,
                    )

                    subprocess.run(["dracut", "-f"], check=True)

                    # Path to the grub configuration file
                    grub_file_path = "/etc/default/grub"  # Use the test file path

                    # Function to calculate SHA256 checksum
                    def calculate_sha256(file_path):
                        result = subprocess.run(
                            ["sha256sum", file_path], capture_output=True, text=True
                        )
                        return result.stdout.split()[0]  # Extract the checksum from the output

                    # Calculate SHA256 checksum before changes
                    sha256_before = calculate_sha256(grub_file_path)

                    # Fixup grub so it's more steamos-like
                    subprocess.run(
                        ["sed", "-i", "s/GRUB_TIMEOUT='0'/GRUB_TIMEOUT='5'/g", "/etc/default/grub"],
                        capture_output=True,
                        text=True,
                    )

                    # Lines to remove from the file
                    lines_to_remove = [
                        "GRUB_TIMEOUT_STYLE='hidden'",
                        "GRUB_HIDDEN_TIMEOUT='0'",
                        "GRUB_HIDDEN_TIMEOUT_QUIET='true'"
                    ]

                    # Read the current contents of the file
                    with open(grub_file_path, "r") as file:
                        current_contents = file.readlines()

                    # Filter out the lines to remove
                    updated_contents = [
                        line for line in current_contents if line.strip() not in lines_to_remove
                    ]

                    # Write the updated contents back to the file
                    with open(grub_file_path, "w") as file:
                        file.writelines(updated_contents)

                    # Calculate SHA256 checksum after changes
                    sha256_after = calculate_sha256(grub_file_path)

                    # Compare checksums
                    if sha256_before != sha256_after:
                        subprocess.run(
                            ["/usr/sbin/grub2-mkconfig", "-o", "/boot/grub2/grub.cfg"],
                            capture_output=True,
                            text=True,
                        )
            else:
                # Fixup plymouth so it's more steamos-like
                plymouth_scripts_name = "plymouth-plugin-script"
                check_plymouth_scripts = subprocess.run(
                    ["rpm", "-q", plymouth_scripts_name], capture_output=True, text=True
                )
                plymouth_scripts_notinstalled = check_plymouth_scripts.returncode != 0
                if plymouth_scripts_notinstalled:
                    PackageUpdater(["plymouth-plugin-script"], "install", None)

                # Run the 'plymouth-set-default-theme' command and capture its output
                check_theme = subprocess.run(
                    ["plymouth-set-default-theme"],
                    capture_output=True,
                    text=True
                )
                if not 'steamos' in check_theme.stdout:
                    # Fixup grub so it's more steamos-like
                    subprocess.run(
                        ["plymouth-set-default-theme", "steamos"],
                        capture_output=True,
                        text=True,
                    )

                    subprocess.run(["dracut", "-f"], check=True)

                    # Path to the grub configuration file
                    grub_file_path = "/etc/default/grub"  # Use the test file path

                    # Function to calculate SHA256 checksum
                    def calculate_sha256(file_path):
                        result = subprocess.run(
                            ["sha256sum", file_path], capture_output=True, text=True
                        )
                        return result.stdout.split()[0]  # Extract the checksum from the output

                    # Calculate SHA256 checksum before changes
                    sha256_before = calculate_sha256(grub_file_path)

                    # Fixup grub so it's more steamos-like
                    subprocess.run(
                        ["sed", "-i", "s/GRUB_TIMEOUT='5'/GRUB_TIMEOUT='0'/g", "/etc/default/grub"],
                        capture_output=True,
                        text=True,
                    )

                    # Lines to add to the file
                    lines_to_add = [
                        "GRUB_TIMEOUT_STYLE='hidden'",
                        "GRUB_HIDDEN_TIMEOUT='0'",
                        "GRUB_HIDDEN_TIMEOUT_QUIET='true'"
                    ]

                    # Read the current contents of the file
                    with open(grub_file_path, "r") as file:
                        current_contents = file.readlines()

                    # Function to check if a line exists and append it if not
                    def add_line_if_missing(line):
                        if line + "\n" not in current_contents:  # Ensure newline is considered
                            with open(grub_file_path, "a") as file:  # Open file in append mode
                                file.write(line + "\n")  # Append the line with a newline

                    # Check and add each line
                    for line in lines_to_add:
                        add_line_if_missing(line)

                    # Calculate SHA256 checksum after changes
                    sha256_after = calculate_sha256(grub_file_path)

                    # Compare checksums
                    if sha256_before != sha256_after:
                        subprocess.run(
                            ["/usr/sbin/grub2-mkconfig", "-o", "/boot/grub2/grub.cfg"],
                            capture_output=True,
                            text=True,
                        )
        if gamescope_session_common_installed:
            if not gamescope_htpc_installed:
                # Return to normal grub + plymouth first.
                plymouth_scripts_name = "plymouth-plugin-script"
                check_plymouth_scripts = subprocess.run(
                    ["rpm", "-q", plymouth_scripts_name], capture_output=True, text=True
                )
                plymouth_scripts_notinstalled = check_plymouth_scripts.returncode != 0
                if plymouth_scripts_notinstalled:
                    PackageUpdater(["plymouth-plugin-script"], "install", None)

                # Run the 'plymouth-set-default-theme' command and capture its output
                check_theme = subprocess.run(
                    ["plymouth-set-default-theme"],
                    capture_output=True,
                    text=True
                )
                if 'steamos' in check_theme.stdout:
                    # Fixup grub so it's more steamos-like
                    subprocess.run(
                        ["plymouth-set-default-theme", "bgrt"],
                        capture_output=True,
                        text=True,
                    )

                    subprocess.run(["dracut", "-f"], check=True)

                    # Path to the grub configuration file
                    grub_file_path = "/etc/default/grub"  # Use the test file path

                    # Function to calculate SHA256 checksum
                    def calculate_sha256(file_path):
                        result = subprocess.run(
                            ["sha256sum", file_path], capture_output=True, text=True
                        )
                        return result.stdout.split()[0]  # Extract the checksum from the output

                    # Calculate SHA256 checksum before changes
                    sha256_before = calculate_sha256(grub_file_path)

                    # Fixup grub so it's more steamos-like
                    subprocess.run(
                        ["sed", "-i", "s/GRUB_TIMEOUT='0'/GRUB_TIMEOUT='5'/g", "/etc/default/grub"],
                        capture_output=True,
                        text=True,
                    )

                    # Lines to remove from the file
                    lines_to_remove = [
                        "GRUB_TIMEOUT_STYLE='hidden'",
                        "GRUB_HIDDEN_TIMEOUT='0'",
                        "GRUB_HIDDEN_TIMEOUT_QUIET='true'"
                    ]

                    # Read the current contents of the file
                    with open(grub_file_path, "r") as file:
                        current_contents = file.readlines()

                    # Filter out the lines to remove
                    updated_contents = [
                        line for line in current_contents if line.strip() not in lines_to_remove
                    ]

                    # Write the updated contents back to the file
                    with open(grub_file_path, "w") as file:
                        file.writelines(updated_contents)

                    # Calculate SHA256 checksum after changes
                    sha256_after = calculate_sha256(grub_file_path)

                    # Compare checksums
                    if sha256_before != sha256_after:
                        subprocess.run(
                            ["/usr/sbin/grub2-mkconfig", "-o", "/boot/grub2/grub.cfg"],
                            capture_output=True,
                            text=True,
                        )

        check_gamescope_hh = subprocess.run(
            ["rpm", "-q", "gamescope-handheld-common"], capture_output=True, text=True
        )
        gamescope_hh_installed = check_gamescope_hh.returncode == 0
        if gamescope_hh_installed:
            ppfeaturemask_check = subprocess.run(
                ["grep", "ppfeaturemask", "/proc/cmdline"], capture_output=True, text=True
            )
            ppfeaturemask_notinstalled = ppfeaturemask_check.returncode != 0
            subprocess.run(
                ['grubby', '--update-kernel=ALL', '--args="amdgpu.ppfeaturemask=0xffffffff"'],
                capture_output=True,
                text=True,
            )
            perform_reboot_request = 1

        # If it has an SD card reader, install gamescope-handheld-common:
        try:
            # Execute the lsblk command to list block devices
            result = subprocess.run(['lsblk', '-o', 'NAME,TYPE'], capture_output=True, text=True)

            # Check if the command was successful
            if result.returncode != 0:
                self.logger.info("Failed to execute lsblk command")


            # Parse the output
            output = result.stdout
            lines = output.splitlines()
            sdreader_detected = 0
            # Look for devices of type 'disk' that match the typical SD card reader pattern
            for line in lines:
                if 'mmcblk' in line and 'disk' in line:
                    sdreader_detected = 1

            if sdreader_detected == 1:
                check_gamescope_hh = subprocess.run(
                    ["rpm", "-q", "gamescope-handheld-common"], capture_output=True, text=True
                )
                gamescope_hh_notinstalled = check_gamescope_hh.returncode != 0
                if gamescope_hh_notinstalled:
                    updatelist.append("gamescope-handheld-common")
        except Exception as e:
            pass

        if len(remove_names) > 0:
            PackageUpdater(remove_names, "remove", None)

        if len(updatelist) > 0:
            PackageUpdater(updatelist, "install", None)

        # Also check if device is steamdeck, if so install jupiter packages
        check_galileo = subprocess.run(
            "dmesg | grep 'Galileo'", capture_output=True, text=True, shell=True
        )
        galileo_detected = check_galileo.returncode == 0

        check_jupiter = subprocess.run(
            "dmesg | grep 'Jupiter'", capture_output=True, text=True, shell=True
        )
        jupiter_detected = check_jupiter.returncode == 0

        if (galileo_detected or jupiter_detected):
            steamdeck_install = []

            jupiter_hw = "jupiter-hw-support"
            check_jupiter_hw = subprocess.run(
                ["rpm", "-q", jupiter_hw], capture_output=True, text=True
            )
            jupiter_hw_installed = check_jupiter_hw.returncode != 0
            if jupiter_hw_installed:
                steamdeck_install.append(jupiter_hw)

            jupiter_fan = "jupiter-fan-control"
            check_jupiter_fan = subprocess.run(
                ["rpm", "-q", jupiter_fan], capture_output=True, text=True
            )
            jupiter_fan_installed = check_jupiter_fan.returncode != 0
            if jupiter_fan_installed:
                steamdeck_install.append(jupiter_fan)

            steamdeck_dsp = "steamdeck-dsp"
            check_steamdeck_dsp = subprocess.run(
                ["rpm", "-q", steamdeck_dsp], capture_output=True, text=True
            )
            steamdeck_dsp_installed = check_steamdeck_dsp.returncode != 0
            if steamdeck_dsp_installed:
                steamdeck_install.append(steamdeck_dsp)

            steamdeck_firmware = "steamdeck-firmware"
            check_steamdeck_firmware = subprocess.run(
                ["rpm", "-q", steamdeck_firmware], capture_output=True, text=True
            )
            steamdeck_firmware_installed = check_steamdeck_firmware.returncode != 0
            if steamdeck_firmware_installed:
                steamdeck_install.append(steamdeck_firmware)

            if len(steamdeck_install) > 0:
                PackageUpdater(steamdeck_install, "install", None)

        # QUIRK 8: winehq-staging packaging changed in version 9.9. We need to completely remove older versions first.
        self.logger.info("QUIRK: winehq-staging packaging changed in version 9.9. We need to completely remove older versions before updating.")
        check_wine_staging_common = subprocess.run(
                ["rpm", "-q", "wine-staging-common"], capture_output=True, text=True
        )
        if check_wine_staging_common.returncode == 0:
            self.logger.info("Upstream wine packaging has changed, fixing conflicts")

            remove_names = []
            add_names = []

            remove_names.append("wine-staging-common")

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

            check_wine_staging = subprocess.run(
                ["rpm", "-q", "wine-staging"], capture_output=True, text=True
            )
            if check_wine_staging64.returncode == 0:
                remove_names.append("wine-staging")

            check_winetricks = subprocess.run(
                ["rpm", "-q", "winetricks"], capture_output=True, text=True
            )
            if check_wine_staging64.returncode == 0:
                remove_names.append("winetricks")

            add_names.append("winehq-staging")
            add_names.append("winetricks")
            add_names.append("wine-staging")

            if len(remove_names) > 0:
                PackageUpdater(remove_names, "remove", None)

            if len(add_names) > 0:
                PackageUpdater(add_names, "remove", None)

        # QUIRK 9: Obsolete package cleanup
        self.logger.info("QUIRK: Obsolete package cleanup.")
        obsolete = [
            "kf5-baloo-file",
            "layer-shell-qt5",
            "herqq",
            "hfsutils",
            "okular5-libs",
            "fedora-workstation-repositories",
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
        if len(obsolete_names) > 0:
            self.logger.info("Found obsolete packages, removing...")
            PackageUpdater(obsolete_names, "remove", None)

        # QUIRK 10: Problematic package cleanup
        self.logger.info("QUIRK: Problematic package cleanup.")
        problematic = [
            "unrar",
            "qt5-qtwebengine-freeworld",
            "qt6-qtwebengine-freeworld",
            "qgnomeplatform-qt6",
            "qgnomeplatform-qt5",
            "musescore",
            "okular5-libs",
            "fedora-workstation-repositories",
            "mesa-demos"
        ]
        problematic_names = []
        for package in problematic:
            problematic_check = subprocess.run(
                ["rpm", "-q", package], capture_output=True, text=True
            )
            if problematic_check.returncode == 0:
                problematic_names.append(package)
        if len(problematic_names) > 0:
            self.logger.info("Found problematic packages, removing...")
            PackageUpdater(problematic_names, "remove", None)

        # QUIRK 11: Cleanup incompatible package versions from Nobara 39 kde6:
        self.logger.info("QUIRK: Cleanup incompatible package versions from Nobara 39 kde6 and deprecated xpadneo")
        incompat = [
            "kf5-kxmlgui-5.116.0-2.fc39.x86_64",
            "kf5-kirigami2-5.116.0-2.fc39.x86_64",
            "kf5-kiconthemes-5.116.0-2.fc39.x86_64",
            "kf5-ki18n-5.116.0-2.fc39.x86_64",
            "xpadneo",
            "akmod-xpadneo",
            "kmod-xpadneo",
            "xpadneo-kmod-common"
        ]
        incompat_names = []
        for package in incompat:
            incompat_check = subprocess.run(
                ["rpm", "-q", package], capture_output=True, text=True
            )
            if incompat_check.returncode == 0:
                incompat_names.append(package.replace("-5.116.0-2.fc39.x86_64", ""))
        if len(incompat_names) > 0:
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

        reinstall.remove("xpadneo")
        reinstall.remove("akmod-xpadneo")
        reinstall.remove("kmod-xpadneo")
        reinstall.remove("xpadneo-kmod-common")

        if len(reinstall) > 0:
            PackageUpdater(reinstall, "install", None)

        # QUIRK 12: Clear plasmashell cache if a plasma-workspace update is available
        self.logger.info("QUIRK: Clear plasmashell cache if a plasma-workspace update is available.")
        # Function to run the rpm command and get the output
        def check_update():
            try:
                # Run the dnf check-update command and filter for plasma-workspace
                result = subprocess.run(['dnf', 'check-update'], capture_output=True, text=True, check=True)
                # Use Python's string filtering instead of piping in the shell
                if 'plasma-workspace' in result.stdout:
                    return True
                return False
            except subprocess.CalledProcessError as e:
                print(f"Failed to run dnf command: {e}")
                return False

        # Function to get the list of all user home directories
        def get_all_user_home_directories():
            home_directories = []
            for user in pwd.getpwall():
                if user.pw_uid >= 1000:  # Filter out system users
                    home_directories.append(user.pw_dir)
            return home_directories

        # Function to check and delete qmlcache folder if older than install date
        def delete_qmlcache(home_dir):
            qmlcache_dir = os.path.join(home_dir, ".cache", "plasmashell", "qmlcache")
            if os.path.exists(qmlcache_dir):
                try:
                    shutil.rmtree(qmlcache_dir)
                    print(f"Deleted '{qmlcache_dir}' directory successfully")
                except Exception as e:
                    print(f"Failed to delete '{qmlcache_dir}': {e}")
            else:
                print(f"Directory '{qmlcache_dir}' does not exist")

        # Main script execution
        if check_update:
            for home_dir in get_all_user_home_directories():
                delete_qmlcache(home_dir)

        # QUIRK 13: Media fixup
        media_fixup = 0
        if "gamescope" not in os.environ.get('XDG_CURRENT_DESKTOP', '').lower():
            self.logger.info("Media fixup.")
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
                        libavcodec_freeworld_check.returncode == 1
                        and media_fixup_check.returncode == 0
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
                        libavcodec_freeworld_check.returncode == 1
                        and media_fixup_check.returncode == 0
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

        # END QUIRKS LIST
        # Check if any packages contain "kernel" or "akmod"
        if "gamescope" in os.environ.get('XDG_CURRENT_DESKTOP', '').lower():
            gamescope_packages = [
                pkg for pkg in package_names if "gamescope" in pkg
            ]
            if gamescope_packages:
                perform_reboot_request = 1

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

