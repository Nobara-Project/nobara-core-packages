"""Conservative classification of a complete, dependency-resolved transaction."""
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
import re
from textwrap import wrap


CORE_PACKAGES = (
    "kernel", "kernel-*", "*-firmware", "microcode_ctl", "linux-firmware*", "dracut*", "grub*", "shim*", "plymouth*",
    "systemd*", "udev*", "dbus*", "glibc*", "libgcc*", "libstdc++*", "libxcrypt*", "openssl*",
    "rpm", "rpm-*", "libdnf*", "dnf", "dnf5", "dnf-data", "dnf-plugins-*", "dnf5-*",
    "python*-rpm", "python*-dnf", "python*-dnf-*", "python*-libdnf*", "python*-hawkey",
    "python*-librepo", "python*-libcomps", "python-unversioned-command",
    "nobara-updater", "nobara-release*", "nobara-repos", "fedora-release*",
    "bash", "coreutils*", "util-linux*", "filesystem", "setup", "sudo*", "pam*", "polkit*", "shadow-utils*",
    "selinux-policy*", "libselinux*", "apparmor*", "authselect*", "cryptsetup*", "lvm2*", "mdadm*",
    "btrfs-progs*", "e2fsprogs*", "xfsprogs*", "NetworkManager*", "networkmanager*",
    "mesa*", "libglvnd*", "libwayland*", "nvidia*", "libnvidia*", "dkms*", "akmod*", "kmod*",
    "xorg-x11-server*", "kwin*", "mutter*", "gnome-shell*", "gnome-session*", "gnome-settings-daemon*",
    "plasma-workspace*", "plasma-desktop*", "plasma-login*", "sddm*", "gdm*", "gamescope*",
    "pipewire*", "wireplumber*", "pulseaudio*", "xdg-desktop-portal*",
)
# Match interpreters and their runtime libraries, not every application/module
# whose RPM name starts with python. Package-manager bindings remain core above.
PYTHON_RUNTIME = re.compile(r"python(?:[23](?:\.[0-9]+)?)?(?:-libs|-freethreading)?")
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
# Application/session units are not system services. Exempt only these unit
# directories, not the user manager's configuration, generators or binaries.
USER_UNIT_PATHS = ("/usr/lib/systemd/user", "/etc/systemd/user")
# Auxiliary packages may share a core package's name without changing runtime
# code. Require both an auxiliary name AND a complete non-runtime file inventory;
# a -devel/-doc suffix must never hide boot files, services or shared libraries.
NON_RUNTIME_SUFFIXES = ("-doc", "-docs", "-devel", "-headers", "-static", "-debuginfo", "-debugsource",
                        "-wallpapers", "-backgrounds", "-licenses", "-whence")
NON_RUNTIME_PATHS = (
    "/usr/share/doc", "/usr/share/gtk-doc", "/usr/share/man", "/usr/share/info", "/usr/share/licenses",
    "/usr/include", "/usr/lib/debug", "/usr/src/debug", "/usr/share/wallpapers", "/usr/share/backgrounds",
    "/usr/lib/pkgconfig", "/usr/lib64/pkgconfig", "/usr/share/pkgconfig", "/usr/lib/cmake", "/usr/lib64/cmake",
)
LIBRARY_PATHS = {"/usr/lib", "/usr/lib64", "/lib", "/lib64"}


def non_runtime_payload(name: str, files: list[str] | None) -> bool:
    if not name.endswith(NON_RUNTIME_SUFFIXES) or not files:
        return False
    for filename in files:
        path = PurePosixPath(filename)
        if not path.is_absolute() or ".." in path.parts:
            return False
        if any(path.is_relative_to(prefix) for prefix in NON_RUNTIME_PATHS):
            continue
        if str(path.parent) in LIBRARY_PATHS and path.suffix == ".a":
            continue
        return False
    return True


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
        files = package.get("files")
        # Removals/downgrades already pass the solver, RPM checks and removal
        # authorization. Classify their actual contents just like upgrades.
        core = any(fnmatchcase(name, pattern) for pattern in CORE_PACKAGES) or PYTHON_RUNTIME.fullmatch(name)
        if core and not non_runtime_payload(name, files):
            reasons.add(f"Core component: {name}")
        if not files:
            reasons.add(f"No file inventory: {name}")
            continue
        for filename in files:
            path = PurePosixPath(filename)
            if not path.is_absolute() or ".." in path.parts:
                reasons.add(f"Unrecognized file inventory: {name}")
                break
            if any(path.is_relative_to(prefix) for prefix in USER_UNIT_PATHS):
                # This file alone is eligible for live installation. Keep
                # checking all other files and the package-level restrictions.
                continue
            if any(str(path) == prefix or str(path).startswith(prefix + "/") for prefix in CORE_PATHS):
                reasons.add(f"System files: {name}")
                break
            # Libraries shared across applications/sessions require offline
            # installation. Private libraries in an application's directory
            # can be replaced with that application.
            if str(path.parent) in LIBRARY_PATHS and (".so" in path.name or path.name.startswith("ld-")):
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
