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
OLD_ROCM = {
    "comgr", "hip-devel", "hip-runtime-amd", "hipcc", "hsa-rocr", "hsa-rocr-devel",
    "hsakmt-roct-devel", "openmp-extras-runtime", "rocm-core", "rocm-device-libs",
    "rocm-hip-runtime", "rocm-language-runtime", "rocm-llvm", "rocm-opencl",
    "rocm-opencl-icd-loader", "rocm-opencl-runtime", "rocm-smi-lib", "rocminfo",
    "rocprofiler-register",
}


def plan_migrations(installed: list[dict], *, product: str = "", nvidia_closed: bool = False) -> MigrationPlan:
    names = {pkg["name"] for pkg in installed}
    specs = {f'{pkg["name"]}.{pkg["arch"]}' for pkg in installed} | names
    plan = MigrationPlan(remove=RETIRED & specs)
    plan.install.update({"dnf-app-center", "inputplumber", "falcond"} - names)
    if "falcond" not in names:
        plan.hooks.append("enable-falcond")
    if "maliit-keyboard" in names:
        plan.remove.add("maliit-keyboard")
        plan.install.add("plasma-keyboard")
    if "sddm" in names:
        plan.remove.update(name for name in names if name == "sddm" or name.startswith("sddm-"))
        plan.install.add("plasma-login-manager")
        plan.hooks.append("plasma-login")
    if names & {"sddm", "plasma-login-manager"}:
        # These settings require SDDM and otherwise prevent its replacement,
        # or pull it back in after a partially completed login migration.
        plan.remove.update(names & {"kde-settings-sddm"})
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


def add_plymouth_migration(plan: MigrationPlan, installed: list[dict], *,
                          current_theme: str | None = None, root: Path = Path("/")) -> set[str]:
    """Restore the BGRT fallback before selecting themes or rebuilding initramfs.

    Return installed packages that need their missing payload restored. A
    same-version install alone is a no-op, so the planner must either upgrade
    these packages or explicitly reinstall them within the saved transaction.
    """
    names = {package["name"] for package in installed}
    gaming = names & {"gamescope-htpc-common", "gamescope-session-common"}
    if "plymouth" not in names and not gaming:
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
    if gaming:
        wanted = "steamos" if len(gaming) == 2 else "bgrt"
        if current_theme != wanted or (wanted == "bgrt" and missing):
            # BGRT belongs to the spinner package and uses two-step, not script.
            provider = "plymouth-plugin-script" if wanted == "steamos" else "plymouth-theme-spinner"
            if provider not in names:
                plan.install.add(provider)
            hook = "plymouth-" + wanted
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
