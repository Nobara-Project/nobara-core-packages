"""Recover repository metadata and migrate retired endpoints without changing policy."""
from __future__ import annotations

import logging
import os
import re

LOG = logging.getLogger(__name__)
MEDIA_REPO = "nobara-pikaos-additional"
LEGACY_MEDIA_URL = "https://rpm.pika-os.com/nobara/media"


def load_repositories(create_base):
    """Load configured repositories, refreshing failed mirror metadata once.

    The factory must return a fresh, configured Base with unloaded repos.
    Never retry a partly loaded sack or a transaction that has begun RPM writes.
    """
    import libdnf5.repo as repo_api

    refreshed = set()
    while True:
        base = create_base()
        try:
            if refreshed and os.geteuid() != 0:
                # Otherwise libdnf can copy the same broken root cache straight
                # back into the user's freshly cleaned cache. No sudo needed.
                config = base.get_config()
                config.get_system_cachedir_option().set(config.get_cachedir_option().get_value())
            base.get_repo_sack().load_repos()
            return base
        except Exception as error:
            try:
                message = str(error)
                match = re.search(r'''for repository ["']([^"'\n]+)["']''', message)
                if ("Failed to download metadata" not in message or "Usable URL not found" not in message
                        or not match or match[1] in refreshed or len(refreshed) >= 3):
                    raise
                repo = next((repo for repo in repo_api.RepoQuery(base)
                             if repo.get_id() == match[1] and repo.get_config().get_enabled_option().get_value()), None)
                if repo is None:
                    raise
                LOG.warning("Repository %s could not find a usable metadata URL. Clearing its cached metadata "
                            "and mirror list, then retrying once with a fresh DNF5 instance.", repo.get_id())
                cache = repo_api.RepoCache(base, repo.get_cachedir())
                # Same cache types as `dnf5 clean metadata`, scoped to this repo.
                # Preserve downloaded RPMs, other repositories, and configuration.
                metadata = cache.remove_metadata()
                solv = cache.remove_solv_files()
                if metadata.get_errors() or solv.get_errors():
                    LOG.error("Could not clear repository %s's metadata cache; keeping the original error.", repo.get_id())
                    raise error
                refreshed.add(repo.get_id())
            finally:
                # Preparation holds the RPM lock; release it before constructing
                # another Base. Calling this on an unlocked query Base is safe.
                base.unlock_system_repo()


def migrated_media_urls(repo, arch: str):
    if repo.get_id() != MEDIA_REPO:
        return None
    config = repo.get_config()
    # A user-selected mirror service takes precedence over baseurl. Do not
    # rewrite it or leave misleading changes to its unused fallback URLs.
    for option in (config.get_mirrorlist_option(), config.get_metalink_option()):
        if not option.empty() and option.get_value():
            return None
    original = config.get_baseurl_option().get_value()
    migrated = tuple(f"{LEGACY_MEDIA_URL}/{arch}/" if url.rstrip("/") == LEGACY_MEDIA_URL else url
                     for url in original)
    return migrated if migrated != tuple(original) else None


def persist_media_migration(run):
    # Read configuration again after RPM replay. nobara-repos may already
    # have fixed the file, or the administrator may have selected a custom
    # mirror. Only the exact retired endpoint still warrants an override.
    import libdnf5.base as base_api
    import libdnf5.repo as repo_api

    base = base_api.Base()
    base.load_config()
    base.get_config().get_plugins_option().set(False)
    base.setup()
    base.get_repo_sack().create_repos_from_system_configuration()
    for repo in repo_api.RepoQuery(base):
        urls = migrated_media_urls(repo, "$basearch")
        if urls is not None:
            LOG.info("Saving the current Nobara media repository endpoint.")
            run(["dnf5", "config-manager", "setopt", MEDIA_REPO + ".baseurl=" + ",".join(urls)])
