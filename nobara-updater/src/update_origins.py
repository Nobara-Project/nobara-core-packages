"""Keep local RPMs intact and attribute only packages named in a failure."""
from __future__ import annotations

import logging
import re

from .update_state import UpdateError

LOG = logging.getLogger(__name__)
NOBARA_REPOS = frozenset({
    "nobara", "nobara-updates", "nobara-kernel-mainline", "nobara-kernel-lts",
    "nobara-pikaos-additional", "nobara-nvidia-production", "nobara-nvidia-new-feature",
})
LOCAL_REPOS = {"@commandline", "commandline"}
UNKNOWN_REPOS = {"", "<unknown>", "@System"}
# These legacy repository configuration RPMs are no longer shipped by Nobara
# and must not be held as local builds, even when installed from an RPM URL.
LOCAL_PROTECTION_EXEMPTIONS = frozenset({
    "rpmfusion-free-release", "rpmfusion-nonfree-release",
    "rpmfusion-free-release-tainted", "rpmfusion-nonfree-release-tainted",
    "rpmfusion-free-release-rawhide", "rpmfusion-nonfree-release-rawhide",
})


class PackageOriginError(UpdateError):
    def __init__(self, message, packages):
        self.package_conflicts = [{k: v for k, v in p.items() if k not in {"aliases", "blocked_replacements"}} for p in packages]
        super().__init__(message + "\n\n" + format_origins(packages))


def origin_kind(repo):
    if repo in LOCAL_REPOS:
        return "local"
    if repo in UNKNOWN_REPOS:
        return "unknown"
    return "nobara" if repo in NOBARA_REPOS else "third-party"


def format_origins(packages):
    lines = ["Packages involved in this failure:"]
    for package in packages:
        lines.append("- " + package["nevra"])
        if package["kind"] == "local":
            lines.append("  Manually installed RPM (@commandline), outside Nobara's repositories. The updater preserves this package.")
        elif package["kind"] == "unknown":
            lines.append("  No installation repository was recorded (for example, a direct rpm install). Nobara provenance is unverified; the updater preserves this package.")
        else:
            lines.append("  Installed from third-party repository '" + package["repo"] + "', not a Nobara-provided repository.")
        if package.get("nobara_repos"):
            lines.append("  Nobara provides this package in: " + ", ".join(package["nobara_repos"]) + ".")
    lines.append("Resolve the package conflict, rebuild the local RPM for the current dependencies, or explicitly choose the Nobara repository build, then run the updater again.")
    return "\n".join(lines)


def mentioned_packages(text, origins, *, replacements=False):
    """Match exact installed NEVRAs, not every local package or a name substring."""
    found = []
    for package in origins:
        aliases = list(package["aliases"])
        if replacements and package["kind"] in {"local", "unknown"}:
            aliases.extend(package.get("blocked_replacements", []))
        if any(re.search(r"(?<![A-Za-z0-9_.+~:-])" + re.escape(alias) + r"(?![A-Za-z0-9_.+~:-])", text) for alias in aliases):
            found.append(package)
    return found


def annotate_failure(error, origins, *, replacements=False):
    if isinstance(error, PackageOriginError):
        return error
    packages = mentioned_packages(str(error), origins, replacements=replacements)
    return PackageOriginError(str(error), packages) if packages else error


def protect_local_packages(base):
    # Keep libdnf imports off the recovery/report path: a failed update may
    # have damaged that library in the installation being diagnosed.
    import libdnf5.rpm as rpm

    installed = rpm.PackageQuery(base)
    installed.filter_installed()
    available = rpm.PackageQuery(base)
    available.filter_available()
    official = rpm.PackageQuery(available)
    official.filter_repo_id(list(NOBARA_REPOS))
    origins = []
    excludes = rpm.PackageSet(base)
    for package in installed:
        if package.get_name() in LOCAL_PROTECTION_EXEMPTIONS:
            continue
        repo = package.get_from_repo_id()
        kind = origin_kind(repo)
        if kind == "nobara":
            continue
        counterparts = rpm.PackageQuery(official)
        counterparts.filter_name([package.get_name()])
        counterparts.filter_arch([package.get_arch(), "noarch"])
        if kind == "third-party" and counterparts.empty():
            continue
        row = dict(name=package.get_name(), arch=package.get_arch(), nevra=package.get_full_nevra(),
                   aliases=sorted({package.get_full_nevra(), package.get_nevra()}), repo=repo, kind=kind,
                   nobara_repos=sorted({p.get_repo_id() for p in counterparts}))
        if kind in {"local", "unknown"}:
            candidates = rpm.PackageQuery(available)
            candidates.filter_name([package.get_name()])
            if package.get_arch() != "noarch":
                candidates.filter_arch([package.get_arch(), "noarch"])
            # A different package name can also replace a local RPM through
            # Obsoletes. Exclude those candidates before the solver runs.
            local = rpm.PackageSet(base)
            local.add(package)
            obsolete = rpm.PackageQuery(available)
            obsolete.filter_obsoletes(local)
            candidates.update(obsolete)
            row["blocked_replacements"] = sorted({alias for p in candidates for alias in (p.get_full_nevra(), p.get_nevra())})
            excludes.update(candidates)
            LOG.info("Keeping locally installed package %s (origin: %s).", row["nevra"], repo or "not recorded")
        origins.append(row)
    base.get_rpm_package_sack().add_user_excludes(excludes)
    return origins


def validate_local_packages(records, origins):
    """Explicit migrations/removals must not bypass the solver exclusions."""
    outgoing = {p["nevra"] for p in records if p["action"] in {"Remove", "Replaced", "Reinstall"}}
    changed = [p for p in origins if p["kind"] in {"local", "unknown"} and outgoing.intersection(p["aliases"])]
    if changed:
        raise PackageOriginError("This update would remove or replace a protected locally installed RPM. No packages were changed.", changed)
