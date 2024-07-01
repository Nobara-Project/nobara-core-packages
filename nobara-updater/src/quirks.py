#!/usr/bin/python3
import logging
import subprocess
import threading
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

        hhd_name = "hhd"
        check_hhd = subprocess.run(
            ["rpm", "-q", hhd_name], capture_output=True, text=True
        )

        # QUIRK 7: Install HHD for Legion Go and ROG Ally, cleanup old packages
        check_legion = subprocess.run(
            "dmesg | grep 'Legion Go'", capture_output=True, text=True, shell=True
        )
        check_ally = subprocess.run(
            "dmesg | grep 'ROG Ally'", capture_output=True, text=True, shell=True
        )

        legion_detected = check_legion.returncode == 0
        ally_detected = check_ally.returncode == 0
        hhd_installed = check_hhd.returncode != 0

        if (legion_detected or ally_detected) and hhd_installed:
            self.logger.info(
                "Found Legion Go or ROG Ally, performing old package cleanup and installing hhd (Handheld Daemon)"
            )
            remove_names = []

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

            if remove_names:
                PackageUpdater(remove_names, "remove", None)

            PackageUpdater([hhd_name], "install", None)

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

        # QUIRK 11: Media fixup
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

