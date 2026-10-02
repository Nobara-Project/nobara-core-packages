"""Migrate known retired repository endpoints without replacing user policy."""
from __future__ import annotations

import logging

LOG = logging.getLogger(__name__)
MEDIA_REPO = "nobara-pikaos-additional"
LEGACY_MEDIA_URL = "https://rpm.pika-os.com/nobara/media"


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
