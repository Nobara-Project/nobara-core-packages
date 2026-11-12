"""Read-only migration planning; package replacements share one DNF goal.

The old quirks routine must not be used to prepare offline updates: it removes
packages before replacements have been downloaded and mutates boot files.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import logging
from pathlib import Path

from .update_codecs import CODEC_REPLACEMENTS, CODEC_MULTILIB, CODEC_NATIVE

LOG = logging.getLogger(__name__)
BGRT_FILES = {
    "plymouth-theme-spinner": "/usr/share/plymouth/themes/bgrt/bgrt.plymouth",
    "plymouth-plugin-two-step": "/usr/lib64/plymouth/two-step.so",
}
# Standard installs that still use Fedora's default splash move once to the
# Nobara theme. nobara-logos >= 44.0.0-2 ships it and requires the script and
# label plugins. HTPC and handheld installs keep the SteamOS splash.
# gamescope-session-common alone is not one of those: every standard install
# has it, for the Steam session offered on the login screen.
HTPC_PACKAGES = frozenset({"gamescope-htpc-common", "gamescope-session-common"})
OLD_DEFAULT_THEME = "bgrt"
NOBARA_THEME = "nobara"
NOBARA_THEME_PACKAGE = "nobara-logos"
NOBARA_THEME_VERSION = ("0", "44.0.0", "2")
NOBARA_THEME_FILES = ("/usr/share/plymouth/themes/nobara/nobara.plymouth", "/usr/lib64/plymouth/script.so")
# Written into the updater's state directory only after the switch and the
# boot-image rebuild succeeded, so a later return to BGRT is respected.
NOBARA_THEME_MARKER = "/var/lib/nobara-updater/plymouth-nobara-theme.json"
PLYMOUTH_CONFIG = "/etc/plymouth/plymouthd.conf"
PLYMOUTH_DEFAULTS = "/usr/share/plymouth/plymouthd.defaults"
PLYMOUTH_THEMES = "/usr/share/plymouth/themes"
# SDDM moves to Plasma Login once. Written after a successful update, so a
# later return to SDDM is respected even without plasma-login-manager.
PLASMA_LOGIN_MARKER = "/var/lib/nobara-updater/plasma-login-migration.json"
# rsyslog is removed once: the journal already keeps every message, and the
# plain-text copies in /var/log grew without a size limit. Written after a
# successful update, so reinstalling rsyslog afterwards is respected.
RSYSLOG_MARKER = "/var/lib/nobara-updater/rsyslog-removal.json"
# The files Fedora's /etc/rsyslog.conf writes, with their logrotate copies
# (messages-20261108, or messages.1.gz with other logrotate settings).
# boot.log is left alone: Plymouth writes it too.
RSYSLOG_LOGS = ("messages", "secure", "maillog", "cron", "spooler")


@dataclass
class MigrationPlan:
    install: set[str] = field(default_factory=set)
    remove: set[str] = field(default_factory=set)
    hooks: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"install": sorted(self.install), "remove": sorted(self.remove), "hooks": self.hooks}


RETIRED = {
    "qt5-qtwebengine-freeworld", "qt6-qtwebengine-freeworld", "qgnomeplatform-qt6", "qgnomeplatform-qt5",
    "okular5-libs", "fedora-workstation-repositories", "deckyloader", "obs-studio-libs.i686",
    "obs-studio-plugin-vkcapture.i686", "obs-studio-plugin-source-record.i686",
    "plasma-workspace-geolocation", "plasma-workspace-geolocation-libs", "python3-torch-rocm-gfx9",
    "python3-torchaudio-rocm-gfx9", "kdelibs-webkit", "kate4-part", "kde-style-breeze",
    "libpostproc-free.x86_64", "libpostproc-free.i686",
}
NVIDIA = {
    "dkms-nvidia", "nvidia-driver", "libnvidia-ml", "libnvidia-ml.i686", "libnvidia-fbc",
    "nvidia-driver-cuda", "nvidia-driver-cuda-libs", "nvidia-driver-cuda-libs.i686",
    "nvidia-driver-libs", "nvidia-driver-libs.i686", "nvidia-kmod-common", "nvidia-libXNVCtrl",
    "nvidia-modprobe", "nvidia-persistenced", "nvidia-settings", "nvidia-xconfig",
    "libva-nvidia-driver", "nvidia-gpu-firmware", "libnvidia-cfg",
}
# Required by the replacement stack (nvidia-driver-libs, nvidia-driver-cuda-libs)
# but not available on every branch, so keep them without forcing an install.
NVIDIA_DEPENDENCIES = {"libnvidia-gpucomp"}
OLD_ROCM = {
    "comgr", "hip-devel", "hip-runtime-amd", "hipcc", "hsa-rocr", "hsa-rocr-devel",
    "hsakmt-roct-devel", "openmp-extras-runtime", "rocm-core", "rocm-device-libs",
    "rocm-hip-runtime", "rocm-language-runtime", "rocm-llvm", "rocm-opencl",
    "rocm-opencl-icd-loader", "rocm-opencl-runtime", "rocm-smi-lib", "rocminfo",
    "rocprofiler-register",
}


def plan_migrations(installed: list[dict], *, product: str = "", nvidia_closed: bool = False,
                    login_migrated: bool = False, rsyslog_removed: bool = False) -> MigrationPlan:
    names = {pkg["name"] for pkg in installed}
    specs = {f'{pkg["name"]}.{pkg["arch"]}' for pkg in installed} | names
    plan = MigrationPlan(remove=RETIRED & specs)
    plan.install.update({"dnf-app-center", "inputplumber", "falcond"} - names)
    if "falcond" not in names:
        plan.hooks.append("enable-falcond")
    if "maliit-keyboard" in names:
        plan.remove.add("maliit-keyboard")
        plan.install.add("plasma-keyboard")
    # SDDM next to Plasma Login, or after the migration, is a deliberate choice.
    login_migration = "sddm" in names and "plasma-login-manager" not in names and not login_migrated
    if login_migration:
        plan.remove.update(name for name in names if name == "sddm" or name.startswith("sddm-"))
        plan.install.add("plasma-login-manager")
        plan.hooks.append("plasma-login")
    if login_migration or ("plasma-login-manager" in names and "sddm" not in names):
        # These settings require SDDM and otherwise prevent its replacement,
        # or pull it back in after a partially completed login migration.
        plan.remove.update(names & {"kde-settings-sddm"})
    # rsyslog reinstalled after the removal is a deliberate choice.
    if "rsyslog" in names and not rsyslog_removed:
        plan.remove.update(name for name in names if name == "rsyslog" or name.startswith("rsyslog-"))
        plan.hooks.append("rsyslog-logs")
    if {"tigervnc-license", "tigervnc-server-minimal"} <= names:
        plan.install.update({"tigervnc-x11-server", "tigervnc-selinux"} - names)
    for old, new in (("rubberband.i686", "rubberband-libs"), ("tesseract.i686", "tesseract-libs")):
        if old in specs:
            plan.remove.add(old)
            plan.install.update({f"{new}.x86_64", f"{new}.i686"})
    if "ROG Ally" in product and "rogally-firmware" in names:
        plan.remove.add("rogally-firmware")
    if "Jupiter" in product or "Galileo" in product:
        plan.install.update({"jupiter-hw-support", "jupiter-fan-control", "steamdeck-dsp", "steamdeck-firmware"} - names)
    if "akmod-nvidia" in names or any("nvidia" in p["name"] and p.get("epoch") == "4" for p in installed):
        # Same-name packages are synchronized/downgraded rather than removed
        # and reinstalled in separate transactions.
        replacement_names = {spec.rsplit(".", 1)[0] if spec.endswith((".i686", ".x86_64")) else spec for spec in NVIDIA}
        replacement_names |= NVIDIA_DEPENDENCIES
        plan.remove.update(name for name in names if "nvidia" in name and name not in replacement_names)
        plan.install.update(NVIDIA)
        plan.hooks.append("nvidia-closed" if nvidia_closed else "nvidia")
    if not any(name.startswith("mesa-vulkan-drivers") for name in names):
        plan.install.update({"mesa-vulkan-drivers.x86_64", "mesa-vulkan-drivers.i686"})
    # Preserve the selected codec family; replace conflicting providers in
    # the same goal. Do not infer that a missing i686 package is installed.
    freeworld = "mesa-libgallium-freeworld" in names or "mesa-va-drivers-freeworld" in names
    if freeworld:
        for family in ("mesa-libgallium", "mesa-va-drivers"):
            for arch in ("x86_64", "i686"):
                if f"{family}.{arch}" in specs:
                    plan.remove.add(f"{family}.{arch}")
                    plan.install.add(f"{family}-freeworld.{arch}")
    if any("fsync" in p.get("version", "") or "fsync" in p.get("release", "") for p in installed if p["name"] == "kernel"):
        plan.install.add("kernel")
    # ROCm packages with surviving names are handled by distro-sync. Only
    # retire packages from the old repository that have no replacement name;
    # the planner checks availability before adding those removals.
    if any(p.get("repo") == "nobara-rocm-official" for p in installed):
        plan.install.add("rocm-meta")
        plan.hooks.append("rocm-transition")
    return plan


def machine_product() -> str:
    values = []
    for name in ("product_name", "board_name"):
        try:
            values.append((Path("/sys/class/dmi/id") / name).read_text().strip())
        except OSError:
            pass
    return " ".join(values)


def read_plymouth_theme(filename: Path) -> str:
    """Read Theme= like plymouth-set-default-theme does; empty when unset."""
    try:
        lines = filename.read_text().splitlines()
    except (OSError, UnicodeDecodeError):
        return ""
    for line in lines:
        key, separator, value = line.strip().partition("=")
        if separator and not key.startswith("#") and key.strip() == "Theme":
            return value.strip()
    return ""


def is_htpc(names: set[str]) -> bool:
    """HTPC and handheld installs: the only ones with both packages."""
    return HTPC_PACKAGES <= names


def configured_plymouth_theme(root: Path = Path("/")) -> str:
    """The administrator's choice, else the distribution default; empty if neither.

    Like plymouth-set-default-theme and plymouthd, a selected theme that is
    not installed does not count: a Theme=steamos line left behind by a
    removed gamescope-htpc-common shows the distribution default.
    """
    for filename in (PLYMOUTH_CONFIG, PLYMOUTH_DEFAULTS):
        theme = read_plymouth_theme(root / filename.lstrip("/"))
        if theme and (root / PLYMOUTH_THEMES.lstrip("/") / theme / f"{theme}.plymouth").is_file():
            return theme
    return ""


def nobara_theme_installed(root: Path = Path("/")) -> bool:
    return all((root / filename.lstrip("/")).is_file() for filename in NOBARA_THEME_FILES)


def plasma_login_migrated(root: Path = Path("/")) -> bool:
    return (root / PLASMA_LOGIN_MARKER.lstrip("/")).exists()


def rsyslog_removed(root: Path = Path("/")) -> bool:
    return (root / RSYSLOG_MARKER.lstrip("/")).exists()


def keep_rsyslog(plan: MigrationPlan, dependents: set[str]) -> None:
    """Leave rsyslog installed while another package requires it.

    Removing it would remove that package too, which the removal check
    rejects: the whole update would fail instead of skipping this cleanup.
    """
    if "rsyslog-logs" not in plan.hooks or not dependents:
        return
    LOG.info("Keeping rsyslog: %s requires it.", ", ".join(sorted(dependents)))
    plan.hooks.remove("rsyslog-logs")
    plan.remove.difference_update({name for name in plan.remove if name == "rsyslog" or name.startswith("rsyslog-")})


def nobara_theme_migrated(root: Path = Path("/")) -> bool:
    return (root / NOBARA_THEME_MARKER.lstrip("/")).exists()


def evr_at_least(package: dict, wanted: tuple[str, str, str]) -> bool:
    import rpm
    found = (str(package.get("epoch") or "0"), package["version"], package["release"])
    return rpm.labelCompare(found, wanted) >= 0


def settle_nobara_theme(plan: MigrationPlan, inbound: list[dict], *, root: Path = Path("/")) -> None:
    """Keep plymouth-nobara only if the theme is installed or arrives now.

    It is planned before resolution, when only the installed nobara-logos is
    known. A repository without the new theme would otherwise force an offline
    fixup and initramfs rebuild on every update.
    """
    if "plymouth-nobara" not in plan.hooks or nobara_theme_installed(root):
        return
    if any(package["name"] == NOBARA_THEME_PACKAGE and evr_at_least(package, NOBARA_THEME_VERSION)
           for package in inbound):
        return
    LOG.info("Keeping the current boot splash: the Nobara Plymouth theme is not available yet.")
    index = plan.hooks.index("plymouth-nobara")
    if any(not (root / filename.lstrip("/")).is_file() for filename in BGRT_FILES.values()):
        # The BGRT repair still needs its boot-image rebuild.
        plan.hooks[index] = "plymouth-rebuild"
    else:
        del plan.hooks[index]


def add_plymouth_migration(plan: MigrationPlan, installed: list[dict], *,
                          current_theme: str | None = None, root: Path = Path("/")) -> set[str]:
    """Restore the BGRT fallback before selecting themes or rebuilding initramfs.

    Return installed packages that need their missing payload restored. A
    same-version install alone is a no-op, so the planner must either upgrade
    these packages or explicitly reinstall them within the saved transaction.

    current_theme None means "not queried": standard installs then read the
    Plymouth configuration below root. An empty theme means none is set.
    """
    names = {package["name"] for package in installed}
    if "plymouth" not in names and not names & HTPC_PACKAGES:
        return set()
    repairs = set()
    missing = False
    for package, filename in BGRT_FILES.items():
        if not (root / filename.lstrip("/")).is_file():
            LOG.warning("Plymouth file %s is missing; preparing repair using %s.", filename, package)
            missing = True
            if package in names:
                repairs.add(package)
            else:
                plan.install.add(package)
    hook = None
    if is_htpc(names):
        if current_theme != "steamos":
            if "plymouth-plugin-script" not in names:
                plan.install.add("plymouth-plugin-script")
            hook = "plymouth-steamos"
    elif not nobara_theme_migrated(root):
        theme = configured_plymouth_theme(root) if current_theme is None else current_theme
        # Only the old default (chosen or by default) moves, and only when the
        # theme is installed or its package can bring it in this transaction;
        # settle_nobara_theme() drops the hook after resolution otherwise.
        # Selecting it also rebuilds boot images, which covers a BGRT repair.
        if theme in {"", OLD_DEFAULT_THEME} and (nobara_theme_installed(root) or NOBARA_THEME_PACKAGE in names):
            hook = "plymouth-nobara"
    if missing and hook is None:
        # Preserve a custom/current theme while restoring the default fallback.
        hook = "plymouth-rebuild"
    if hook and hook not in plan.hooks:
        plan.hooks.append(hook)
    return repairs


def add_codec_migration(plan: MigrationPlan, installed: list[dict]) -> None:
    specs = {f'{pkg["name"]}.{pkg["arch"]}' for pkg in installed}
    names = {pkg["name"] for pkg in installed}
    for old, new in CODEC_REPLACEMENTS.items():
        for arch in ("x86_64", "i686"):
            if f"{old}.{arch}" in specs:
                plan.remove.add(f"{old}.{arch}")
                plan.install.discard(f"{old}.{arch}")
                plan.install.add(f"{new}.{arch}")
    for name in CODEC_MULTILIB:
        plan.install.update({f"{name}.x86_64", f"{name}.i686"})
    if not any(name.startswith("mesa-vulkan-drivers") for name in names):
        plan.install.difference_update({"mesa-vulkan-drivers.x86_64", "mesa-vulkan-drivers.i686"})
        plan.install.update({"mesa-vulkan-drivers-freeworld.x86_64", "mesa-vulkan-drivers-freeworld.i686"})
    plan.install.update(name if name == "pipewire-codec-aptx" else name + ".x86_64" for name in CODEC_NATIVE)
    plan.hooks.append("enable-codecs")
