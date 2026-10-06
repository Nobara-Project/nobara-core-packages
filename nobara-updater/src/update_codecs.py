"""Codec families managed by Nobara, and transaction-level removal protection."""
from __future__ import annotations

import json
import sys
import tempfile

from .update_state import UpdateError

CODEC_REPLACEMENTS = {
    "ffmpeg": "ffmpeg-free", "ffmpeg-libs": "libavcodec-free",
    "libavdevice": "libavdevice-free", "noopenh264": "openh264",
    "x264": "x264-libs", "x265": "x265-libs",
    "mesa-libgallium": "mesa-libgallium-freeworld",
    "mesa-va-drivers": "mesa-va-drivers-freeworld",
    "mesa-vulkan-drivers": "mesa-vulkan-drivers-freeworld",
    "mesa-vulkan-drivers-git": "mesa-vulkan-drivers-git-freeworld",
}
CODEC_MULTILIB = {
    "mesa-libgallium-freeworld", "libavcodec-free", "libavutil-free", "libswresample-free",
    "libavformat-free", "libswscale-free", "libavfilter-free", "libavdevice-free",
    "gstreamer1-plugins-bad-free-extras", "openh264", "x264-libs", "x265-libs",
    "libavcodec-freeworld", "libheif-freeworld", "libheif",
}
CODEC_NATIVE = {"ffmpeg-free", "mozilla-openh264", "pipewire-codec-aptx"}
# The wizard can select freeworld Vulkan drivers, but users also switch these
# providers through Driver Manager. Keep that choice outside codec protection.
VULKAN_DRIVER_PACKAGES = frozenset({
    "mesa-vulkan-drivers", "mesa-vulkan-drivers-freeworld",
    "mesa-vulkan-drivers-git", "mesa-vulkan-drivers-git-freeworld",
})
CODEC_PACKAGES = (frozenset(CODEC_REPLACEMENTS) | frozenset(CODEC_REPLACEMENTS.values())
                  | CODEC_MULTILIB | CODEC_NATIVE) - VULKAN_DRIVER_PACKAGES


def installed_codec_dependencies(base) -> set[tuple[str, str]]:
    """Include installed providers of strong dependencies, transitively.

    Use libsolv's dependency matching, including versioned, file and rich RPM
    dependencies. Keep architecture identity so a remaining 64-bit package
    does not authorize removing its protected 32-bit counterpart.
    """
    import libdnf5.rpm as rpm

    installed = rpm.PackageQuery(base)
    installed.filter_installed()
    # Do not re-protect a switchable Vulkan driver through a codec's dependency
    # on its virtual provides, or traverse dependencies belonging only to that
    # driver. Shared dependencies reached from actual codecs stay protected.
    drivers = rpm.PackageQuery(installed)
    drivers.filter_name(sorted(VULKAN_DRIVER_PACKAGES))
    installed.difference(drivers)
    pending = rpm.PackageQuery(installed)
    pending.filter_name(sorted(CODEC_PACKAGES))
    seen = rpm.PackageSet(base)
    while not pending.empty():
        seen.update(pending)
        providers = rpm.PackageQuery(base)
        providers.clear()
        for package in pending:
            dependencies = rpm.PackageQuery(installed)
            dependencies.filter_provides(package.get_requires())
            providers.update(dependencies)
        providers.difference(seen)
        pending = providers
    return {(p.get_name(), p.get_arch()) for p in seen}


def validate_codec_changes(packages: list[dict], protected: set[tuple[str, str]]) -> None:
    # Same-name, same-architecture upgrades remain normal package maintenance.
    # Swaps, removals, downgrades and reinstalls go through the Nobara worker.
    upgrades = {(p["name"], p["arch"]) for p in packages if p["action"] == "U"}
    blocked = set()
    for package in packages:
        identity = package["name"], package["arch"]
        action = package["action"]
        if identity in protected and action in {"E", "O", "D", "R"} and identity not in upgrades:
            blocked.add(".".join(identity))
        if package["name"] in CODEC_PACKAGES and action in {"I", "D", "R"}:
            blocked.add(".".join(identity))
    if blocked:
        raise UpdateError("Nobara manages these media codec packages or their dependencies: "
                          + ", ".join(sorted(blocked))
                          + ". Use the Codec Wizard or nobara-sync install-codecs / nobara-sync cli. "
                            "Manual removal and codec provider changes are blocked; normal version upgrades are allowed.")


def request(domain, **args):
    print(json.dumps({"op": "get", "domain": domain, "args": args}), flush=True)
    response = json.loads(sys.stdin.readline())
    if response.get("status") != "OK":
        raise UpdateError("Cannot check codec protection: " + response.get("message", "invalid DNF response"))
    return response["return"]


def guard_main() -> int:
    """DNF5 actions-plugin JSON protocol; run before any RPM changes."""
    try:
        import libdnf5.base as base_api

        packages = request("trans_packages", output=["name", "arch", "action"])["trans_packages"]
        if not packages:
            return 0
        root = request("conf", key="installroot")["keys_val"][0]["value"]
        with tempfile.TemporaryDirectory(prefix="nobara-codec-guard-") as temporary:
            base = base_api.Base()
            config = base.get_config()
            for name, value in {"installroot": root, "config_file_path": "/dev/null", "plugins": False,
                                "cachedir": temporary, "system_cachedir": temporary, "logdir": temporary,
                                "skip_system_repo_lock": True}.items():
                getattr(config, f"get_{name}_option")().set(value)
            base.setup()
            # No repository definitions or network access: only the current RPMDB.
            base.get_repo_sack().load_repos()
            validate_codec_changes(packages, installed_codec_dependencies(base))
        return 0
    except Exception as error:
        print(json.dumps({"op": "error", "args": {"message": str(error)}}), flush=True)
        return 1
