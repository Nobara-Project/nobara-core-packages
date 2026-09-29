"""Conservative classification of a complete, dependency-resolved transaction."""
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from textwrap import wrap


CORE_PACKAGES = (
    "kernel*", "*-firmware", "microcode_ctl", "linux-firmware*", "dracut*", "grub*", "shim*", "plymouth*",
    "systemd*", "udev*", "dbus*", "glibc*", "libgcc*", "libstdc++*", "libxcrypt*", "openssl*",
    "rpm*", "libdnf*", "dnf*", "python*", "nobara-updater", "nobara-release*", "nobara-repos", "fedora-release*",
    "bash*", "coreutils*", "util-linux*", "filesystem", "setup", "sudo*", "pam*", "polkit*", "shadow-utils*",
    "selinux-policy*", "libselinux*", "apparmor*", "authselect*", "cryptsetup*", "lvm2*", "mdadm*",
    "btrfs-progs*", "e2fsprogs*", "xfsprogs*", "NetworkManager*", "networkmanager*",
    "mesa*", "libglvnd*", "libwayland*", "nvidia*", "libnvidia*", "dkms*", "akmod*", "kmod*",
    "xorg-x11-server*", "kwin*", "mutter*", "gnome-shell*", "gnome-session*", "gnome-settings-daemon*",
    "plasma-workspace*", "plasma-desktop*", "plasma-login*", "sddm*", "gdm*", "gamescope*",
    "pipewire*", "wireplumber*", "pulseaudio*", "xdg-desktop-portal*",
)
CORE_PATHS = (
    "/boot", "/efi", "/usr/lib/modules", "/lib/modules", "/usr/lib/firmware", "/lib/firmware",
    "/usr/lib/systemd", "/etc/systemd", "/usr/lib/udev", "/etc/udev", "/etc/init.d", "/etc/rc.d",
    "/etc/pam.d", "/etc/security", "/etc/selinux", "/etc/apparmor.d", "/etc/modprobe.d", "/usr/lib/modprobe.d",
    "/etc/dracut.conf.d", "/usr/lib/dracut", "/etc/grub.d", "/etc/default/grub",
    "/usr/lib/rpm", "/usr/lib/sysusers.d", "/usr/lib/tmpfiles.d", "/etc/sysctl.d", "/usr/lib/sysctl.d",
    "/etc/sysusers.d", "/etc/tmpfiles.d", "/usr/lib/security", "/usr/lib64/security", "/usr/libexec/polkit-1",
    "/sbin", "/usr/sbin", "/usr/libexec/dnf5", "/usr/libexec/nobara-update-worker",
    "/usr/share/dbus-1/system-services", "/usr/share/polkit-1", "/etc/dbus-1", "/usr/lib/dnf5",
    "/etc/ld.so.conf.d", "/etc/ld.so.conf", "/etc/ld.so.preload", "/etc/fstab", "/etc/crypttab",
)


def execution_policy(packages: list[dict], source_release: str, target_release: str, hooks: list[str]) -> dict:
    """Classify all changes, including dependencies and outgoing files.

    Splitting an already resolved transaction would invalidate its dependency
    and RPM checks. Missing inventories never imply eligibility for live use.
    """
    reasons = set()
    if source_release != target_release:
        reasons.add(f"Nobara release upgrade ({source_release} → {target_release})")
    if hooks:
        reasons.add("System configuration fixups")
    for package in packages:
        if package["action"] == "Reason Change":
            # Changing user/dependency ownership does not replace any files.
            continue
        name = package["name"]
        if package["action"] in {"Remove", "Downgrade"}:
            reasons.add("Package removals or downgrades")
        if any(fnmatchcase(name, pattern) for pattern in CORE_PACKAGES):
            reasons.add(f"Core component: {name}")
        files = package.get("files")
        if not files:
            reasons.add(f"No file inventory: {name}")
            continue
        for filename in files:
            path = PurePosixPath(filename)
            if not path.is_absolute() or ".." in path.parts:
                reasons.add(f"Unrecognized file inventory: {name}")
                break
            if any(str(path) == prefix or str(path).startswith(prefix + "/") for prefix in CORE_PATHS):
                reasons.add(f"System files: {name}")
                break
            # Libraries shared across applications/sessions require offline
            # installation. Private libraries in an application's directory
            # can be replaced with that application.
            if str(path.parent) in {"/usr/lib", "/usr/lib64", "/lib", "/lib64"} and (".so" in path.name or path.name.startswith("ld-")):
                reasons.add(f"Shared system library: {name}")
                break
    return {"mode": "offline" if reasons else "live", "reasons": sorted(reasons)}


def format_restart_reasons(reasons: list[str], width: int = 88) -> str:
    """Keep full diagnostic reasons in the plan, but present each package once."""
    categories = (
        ("Core component", "Core components"),
        ("Shared system library", "Shared system libraries"),
        ("System files", "System services and configuration"),
        ("No file inventory", "Packages without file inventories"),
        ("Unrecognized file inventory", "Packages with unrecognized file inventories"),
    )
    grouped = {key: set() for key, _ in categories}
    other = set()
    for reason in reasons:
        category, separator, package = reason.partition(": ")
        if separator and category in grouped:
            grouped[category].add(package)
        else:
            other.add(reason)
    lines = ["Installation requires a restart:"]
    for reason in sorted(other):
        lines.extend(wrap(reason, width=width, initial_indent="- ", subsequent_indent="  "))
    shown = set()
    for category, label in categories:
        packages = sorted(grouped[category] - shown)
        if not packages:
            continue
        shown.update(packages)
        lines.append(f"- {label} ({len(packages)}):")
        lines.extend(wrap(", ".join(packages), width=width, initial_indent="    ", subsequent_indent="    ",
                          break_long_words=False, break_on_hyphens=False))
    return "\n".join(lines)
