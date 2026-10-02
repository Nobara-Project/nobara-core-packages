"""Read real DNF repo configuration without contacting remote repositories."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import test_offline_integration as fixtures
import libdnf5.base as base_api
import libdnf5.repo as repo_api

from nobara_updater import update_backend as backend, update_plan as planner
from nobara_updater.update_repositories import migrated_media_urls, persist_media_migration


class RepositoryMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="nobara-repo-migration-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.root = self.directory / "root"
        self.repos = self.directory / "repos"
        self.job = self.directory / "job"
        for path in (self.root, self.repos, self.job):
            path.mkdir()
        self.path = self.repos / "media.repo"

    def definition(self, url="https://rpm.pika-os.com/nobara/media", *, enabled=True, extra="", repo_id="nobara-pikaos-additional"):
        self.path.write_text(f"[{repo_id}]\nname=Media\nbaseurl={url}\nenabled={int(enabled)}\n"
                             "priority=25\ngpgcheck=1\nexclude=custom-hold*\n" + extra)

    def base(self):
        base = base_api.Base()
        config = base.get_config()
        for name, value in {"installroot": str(self.root), "config_file_path": "/dev/null",
                            "reposdir": [str(self.repos)], "logdir": str(self.job),
                            "plugins": False, "use_host_config": False}.items():
            getattr(config, "get_" + name + "_option")().set(value)
        base.get_vars().set("releasever", "44")
        return base

    def prepare_base(self):
        base = self.base()
        with patch.object(base_api, "Base", return_value=base), \
             patch.object(repo_api.RepoSackWeakPtr, "load_repos"), \
             patch.object(planner.subprocess, "run", return_value=Mock(stdout="")):
            return planner.configure_base(self.job, "43")

    def repo(self):
        base = self.base()
        base.setup()
        base.get_repo_sack().create_repos_from_system_configuration()
        # Keep Base alive while its SWIG-backed repo objects are in use.
        return base, next(iter(repo_api.RepoQuery(base)))

    def test_preparation_uses_new_endpoint_before_loading_without_writing_configuration(self):
        self.definition()
        before = self.path.read_bytes()
        base = self.prepare_base()
        self.addCleanup(base.unlock_system_repo)
        repo = next(iter(repo_api.RepoQuery(base)))
        config = repo.get_config()
        self.assertTrue(base.nobara_migrate_media)
        arch = base.get_vars().get_value("basearch")
        self.assertEqual(config.get_baseurl_option().get_value(), (f"https://rpm.pika-os.com/nobara/media/{arch}/",))
        self.assertEqual(config.get_priority_option().get_value(), 25)
        self.assertEqual(config.get_excludepkgs_option().get_value(), ("custom-hold*",))
        self.assertTrue(config.get_pkg_gpgcheck_option().get_value())
        self.assertEqual(self.path.read_bytes(), before)

    def test_disabled_repo_stays_disabled_and_does_not_trigger_migration(self):
        self.definition(enabled=False)
        base = self.prepare_base()
        self.addCleanup(base.unlock_system_repo)
        self.assertFalse(base.nobara_migrate_media)
        self.assertFalse(base.nobara_codecs)
        self.assertFalse(next(iter(repo_api.RepoQuery(base))).get_config().get_enabled_option().get_value())

    def test_custom_and_current_urls_are_preserved(self):
        for url in ("https://mirror.example/media", "https://rpm.pika-os.com/nobara/media/x86_64/",
                    "https://rpm.pika-os.com/nobara/media?custom=1"):
            with self.subTest(url=url):
                self.definition(url)
                base, repo = self.repo()
                self.assertIsNone(migrated_media_urls(repo, "x86_64"))

    def test_user_selected_mirror_service_is_preserved(self):
        for setting in ("mirrorlist", "metalink"):
            with self.subTest(setting=setting):
                self.definition(extra=setting + "=https://mirror.example/list\n")
                base, repo = self.repo()
                self.assertIsNone(migrated_media_urls(repo, "x86_64"))

    def test_unrelated_repo_and_other_baseurls_are_preserved(self):
        self.definition(repo_id="custom-media")
        base, repo = self.repo()
        self.assertIsNone(migrated_media_urls(repo, "x86_64"))
        self.definition("https://rpm.pika-os.com/nobara/media/ https://mirror.example/media")
        base, repo = self.repo()
        self.assertEqual(migrated_media_urls(repo, "aarch64"),
                         ("https://rpm.pika-os.com/nobara/media/aarch64/", "https://mirror.example/media"))

    def test_finalization_persists_only_the_retired_url_without_loading_metadata(self):
        self.definition()
        base = self.base()
        run = Mock()
        with patch.object(base_api, "Base", return_value=base), \
             patch.object(repo_api.RepoSackWeakPtr, "load_repos") as load:
            persist_media_migration(run)
        load.assert_not_called()
        run.assert_called_once_with(["dnf5", "config-manager", "setopt",
                                    "nobara-pikaos-additional.baseurl=https://rpm.pika-os.com/nobara/media/$basearch/"])

    def test_finalization_does_not_overwrite_newly_installed_or_custom_configuration(self):
        for url in ("https://rpm.pika-os.com/nobara/media/x86_64/", "https://mirror.example/media"):
            with self.subTest(url=url):
                self.definition(url)
                base = self.base()
                run = Mock()
                with patch.object(base_api, "Base", return_value=base):
                    persist_media_migration(run)
                run.assert_not_called()

    def test_offline_hook_executes_the_repository_migration(self):
        with patch("nobara_updater.update_repositories.persist_media_migration") as migrate:
            backend.apply_hooks(["migrate-media-repository"])
        migrate.assert_called_once_with(backend.run)

    @unittest.skipUnless(os.geteuid() == 0 and shutil.which("dnf5"), "Run inside unshare --user --map-root-user")
    def test_native_config_manager_persists_migration_in_target_root(self):
        self.definition()
        (self.root / "etc/dnf/repos.override.d").mkdir(parents=True)
        base = self.base()

        def target_run(command):
            subprocess.run([command[0], "--installroot=" + str(self.root), "--releasever=44",
                            "--config=/dev/null", "--setopt=reposdir=" + str(self.repos),
                            "--setopt=logdir=" + str(self.job), "--no-plugins", *command[1:]],
                           check=True, capture_output=True, text=True)

        with patch.object(base_api, "Base", return_value=base):
            persist_media_migration(target_run)
        fresh, repo = self.repo()
        self.assertEqual(repo.get_config().get_baseurl_option().get_value(),
                         ("https://rpm.pika-os.com/nobara/media/" + fresh.get_vars().get_value("basearch") + "/",))
        self.assertEqual(repo.get_config().get_priority_option().get_value(), 25)
        self.assertTrue(repo.get_config().get_pkg_gpgcheck_option().get_value())
        self.assertEqual(repo.get_config().get_excludepkgs_option().get_value(), ("custom-hold*",))


if __name__ == "__main__":
    unittest.main()
