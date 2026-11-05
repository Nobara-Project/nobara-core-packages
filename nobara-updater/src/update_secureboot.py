#!/usr/bin/python3
"""Secure Boot key setup for the Nobara Easy Anti-Cheat kernel, at desktop login.

With Secure Boot on, shim boots the Easy Anti-Cheat kernel (kernel-eac) and
GRUB only once the Nobara EAC CA is enrolled as a MOK; until then the system
keeps booting the normal kernel and the GRUB already on the ESP. This offers
the enrollment: the user chooses a one-time password, and nobara-eac-enroll
(from nobara-eac, run through pkexec) queues the keys. The user confirms them
in MokManager on the next boot, with that password: shim requires someone at
the computer to confirm, so this step cannot be automated.

Runs unprivileged, in the user's session. "--notify" (desktop autostart) only
shows a notification when the enrollment is needed and not declined.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import string
import subprocess
import threading

ENROLL = "/usr/bin/nobara-eac-enroll"
EAC_CA = "/usr/share/nobara/eac/nobara-eac-ca.der"
# MokManager reads the password with the firmware's keyboard handling, which
# can't be relied on beyond US-layout letters and digits. nobara-eac-enroll
# applies the same rule.
PASSWORD_CHARACTERS = frozenset(string.ascii_letters + string.digits)
PASSWORD_LENGTH = (4, 32)
# pkexec: the user dismissed the authentication dialog, or wasn't authorized
PKEXEC_DECLINED = {126, 127}


def state_directory() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "nobara-updater"


def declined_marker() -> Path:
    return state_directory() / "eac-enroll-declined"


def valid_password(password: str) -> bool:
    return PASSWORD_LENGTH[0] <= len(password) <= PASSWORD_LENGTH[1] and set(password) <= PASSWORD_CHARACTERS


def output(command: list[str]) -> str:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def eac_kernel_installed() -> bool:
    try:
        return subprocess.run(["rpm", "-q", "--quiet", "kernel-eac-core"], timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def enrollment_needed() -> bool:
    """Secure Boot is enforced, kernel-eac is installed and the Nobara EAC CA
    is neither enrolled nor waiting for confirmation on the next boot."""
    if not Path("/sys/firmware/efi").is_dir() or Path("/run/ostree-booted").exists():
        return False
    if not (Path(ENROLL).is_file() and Path(EAC_CA).is_file()):
        return False
    state = output(["mokutil", "--sb-state"])
    if "SecureBoot enabled" not in state or "validation is disabled" in state.lower():
        return False
    if not eac_kernel_installed():
        return False
    # Readable without root: mokutil reads MokListRT and MokNew from efivarfs
    test = output(["mokutil", "--test-key", EAC_CA])
    return not any(line.strip() in {f"{EAC_CA} is already enrolled",
                                    f"{EAC_CA} is already in the enrollment request"}
                   for line in test.splitlines())


def enroll(password: str) -> tuple[bool, str]:
    """Queue the keys with nobara-eac-enroll, as root through pkexec. The
    password goes through stdin, never on a command line."""
    if not valid_password(password):
        return False, "The password must be 4 to 32 letters (A–Z, a–z) and digits."
    try:
        result = subprocess.run(["pkexec", ENROLL, "--password-stdin"], input=password + "\n",
                                capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as error:
        return False, f"Could not run nobara-eac-enroll: {error}"
    if result.returncode in PKEXEC_DECLINED:
        return False, "Authentication was cancelled. The keys were not enrolled."
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        return False, "The keys could not be enrolled." + (f"\n{detail}" if detail else "")
    return True, ""


def decline() -> None:
    marker = declined_marker()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("Delete this file to be asked about the Nobara Secure Boot keys again.\n")


def notify() -> bool:
    """Whether the user asked to set it up. Without notifications, show the dialog."""
    try:
        result = subprocess.run([
            "notify-send", "--app-name=Nobara System Update", "--icon=security-high",
            "--urgency=normal", "--expire-time=0",
            "--action=setup=Set up", "--action=never=Don't ask again",
            "The Easy Anti-Cheat kernel needs a Secure Boot key",
            "Enroll the Nobara EAC key so your computer can boot the Easy Anti-Cheat kernel "
            "with Secure Boot on. It takes one restart."], capture_output=True, text=True)
    except OSError:
        return True
    if result.returncode:
        return True
    choice = result.stdout.strip()
    if choice == "never":
        decline()
    return choice == "setup"


INSTRUCTIONS = """The keys are enrolled on your next restart. You don't need to restart now: if a system update is waiting, the same restart installs it.

On that restart, a blue MokManager screen appears right after the firmware logo. It waits only about 10 seconds, so watch for it:

1. Press any key.
2. Choose "Enroll MOK", then "Continue".
3. Choose "Yes", then type the password you just chose. The screen doesn't show what you type.
4. Choose "Reboot".

If you miss it, the computer starts as usual and you can set up the keys again.

After MokManager restarts the computer, it starts the normal kernel once more and sets up the Easy Anti-Cheat kernel: from the restart after that, the Easy Anti-Cheat kernel is the default boot entry."""


def show_dialog() -> None:
    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import GLib, Gtk

    app = Gtk.Application(application_id="org.nobaraproject.EacEnroll")

    def activate(application):
        window = application.get_active_window()
        if window:
            window.present()
            return
        window = Gtk.ApplicationWindow(application=application, title="Secure Boot Keys for Easy Anti-Cheat")
        window.set_default_size(560, -1)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_top=24, margin_bottom=24, margin_start=24, margin_end=24)
        window.set_child(box)

        def label(text, *, css=None):
            item = Gtk.Label(label=text, wrap=True, xalign=0, max_width_chars=60)
            if css:
                item.add_css_class(css)
            box.append(item)
            return item

        label("Enroll the Nobara Secure Boot keys", css="title-2")
        label("Secure Boot is on, so your computer boots the Easy Anti-Cheat kernel only once the "
              "Nobara EAC key is enrolled. Until then it keeps starting the normal kernel.\n\n"
              "Choose a one-time password. On your next restart, a blue MokManager screen asks "
              "for it to confirm the keys.")
        label("Use 4 to 32 letters (A–Z, a–z) and digits: MokManager may not read other "
              "characters correctly, and it uses a US keyboard layout.", css="dim-label")
        first = Gtk.PasswordEntry(show_peek_icon=True, placeholder_text="Password")
        second = Gtk.PasswordEntry(show_peek_icon=True, placeholder_text="Password again")
        box.append(first)
        box.append(second)
        message = label("")
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.END)
        box.append(buttons)
        never = Gtk.Button(label="Don't ask again")
        later = Gtk.Button(label="Not now")
        ok = Gtk.Button(label="Enroll", sensitive=False)
        ok.add_css_class("suggested-action")
        for item in (never, later, ok):
            buttons.append(item)

        def changed(*_):
            password, again = first.get_text(), second.get_text()
            if password and not valid_password(password):
                message.set_text("The password must be 4 to 32 letters (A–Z, a–z) and digits.")
            elif again and password != again:
                message.set_text("The passwords don't match.")
            else:
                message.set_text("")
            ok.set_sensitive(valid_password(password) and password == again)

        def done(success, text):
            if success:
                for item in (first, second, never, later):
                    item.set_visible(False)
                message.set_text(INSTRUCTIONS)
                ok.set_label("Close")
                ok.disconnect_by_func(start)
                ok.connect("clicked", lambda _: window.close())
                ok.set_sensitive(True)
            else:
                for item in (first, second, never, later):
                    item.set_sensitive(True)
                changed()
                # After changed(), which clears the message for valid passwords
                message.set_text(text)
            return False

        def start(_):
            password = first.get_text()
            for item in (first, second, never, later, ok):
                item.set_sensitive(False)
            message.set_text("Enrolling the keys…")
            threading.Thread(target=lambda: GLib.idle_add(done, *enroll(password)), daemon=True).start()

        def decline_clicked(_):
            try:
                decline()
            except OSError:
                pass
            window.close()

        first.connect("changed", changed)
        second.connect("changed", changed)
        second.connect("activate", lambda _: ok.get_sensitive() and ok.emit("clicked"))
        ok.connect("clicked", start)
        later.connect("clicked", lambda _: window.close())
        never.connect("clicked", decline_clicked)
        window.present()

    app.connect("activate", activate)
    app.run([])


def main():
    parser = argparse.ArgumentParser(description="Enroll the Nobara Secure Boot keys for the Easy Anti-Cheat kernel.")
    parser.add_argument("--notify", action="store_true",
                        help="At desktop login: notify only when the enrollment is needed and was not declined.")
    args = parser.parse_args()
    if args.notify:
        if declined_marker().exists() or not enrollment_needed():
            return
        if not notify():
            return
    elif not enrollment_needed():
        print("Nothing to do: Secure Boot is off, the Easy Anti-Cheat kernel isn't installed, "
              "or the Nobara EAC key is already enrolled or waiting for confirmation on the next restart.")
        return
    show_dialog()


if __name__ == "__main__":
    main()
