"""Bounded DNF5 cache recovery; fixtures never use host repos or packages."""
import importlib.util
import logging
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import test_repositories as fixtures
import libdnf5.base as base_api
import libdnf5.repo as repo_api
from nobara_updater import update_repositories as repositories
from test_frontend_contract import ROOT, functions_from_file

FAILURE = ('Failed to download metadata (metalink: "https://example.invalid/metalink") '
           'for repository "terra": Usable URL not found')


class MetadataRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.bases = [Mock(), Mock()]
        self.bases[0].get_repo_sack().load_repos.side_effect = RuntimeError(FAILURE)
        self.factory = Mock(side_effect=self.bases)
        self.repo = Mock()
        self.repo.get_id.return_value = 'terra'
        self.repo.get_cachedir.return_value = '/fixture/cache/terra-0123456789abcdef'
        self.repo.get_config().get_enabled_option().get_value.return_value = True
        for patcher in (patch.object(repo_api, 'RepoQuery', return_value=[self.repo]),
                        patch.object(repo_api, 'RepoCache'), patch.object(repositories.os, 'geteuid', return_value=0)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.cache = repo_api.RepoCache.return_value
        self.cache.remove_metadata().get_errors.return_value = 0
        self.cache.remove_solv_files().get_errors.return_value = 0
        self.cache.reset_mock()

    def test_exact_failure_cleans_only_metadata_and_retries_a_fresh_base(self):
        with self.assertLogs(repositories.LOG, level='WARNING'):
            self.assertIs(repositories.load_repositories(self.factory), self.bases[1])
        self.assertEqual(self.factory.call_count, 2)
        repo_api.RepoCache.assert_called_once_with(self.bases[0], self.repo.get_cachedir())
        self.cache.remove_metadata.assert_called_once()
        self.cache.remove_solv_files.assert_called_once()
        self.cache.remove_packages.assert_not_called()
        self.cache.remove_all.assert_not_called()
        self.bases[0].unlock_system_repo.assert_called_once()
        self.bases[1].unlock_system_repo.assert_not_called()

    def test_persistent_failure_is_propagated_after_one_retry(self):
        self.bases[1].get_repo_sack().load_repos.side_effect = RuntimeError(FAILURE)
        with self.assertRaisesRegex(RuntimeError, 'Usable URL not found'):
            repositories.load_repositories(self.factory)
        self.assertEqual(self.factory.call_count, 2)
        self.cache.remove_metadata.assert_called_once()
        self.bases[1].unlock_system_repo.assert_called_once()

    def test_unrelated_errors_never_clear_cache_or_retry(self):
        for message in ('signature verification failed', 'Another package manager is running',
                        'Failed to download metadata for repository "terra": Bad GPG signature',
                        'Usable URL not found', FAILURE.replace('"terra"', '"unknown-repo"')):
            with self.subTest(message=message):
                self.factory.reset_mock(side_effect=True)
                self.factory.side_effect = self.bases
                self.bases[0].get_repo_sack().load_repos.side_effect = RuntimeError(message)
                with self.assertRaises(RuntimeError):
                    repositories.load_repositories(self.factory)
                self.assertEqual(self.factory.call_count, 1)
        repo_api.RepoCache.assert_not_called()

    def test_disabled_repository_is_never_cleaned(self):
        self.repo.get_config().get_enabled_option().get_value.return_value = False
        with self.assertRaises(RuntimeError):
            repositories.load_repositories(self.factory)
        repo_api.RepoCache.assert_not_called()

    def test_cleanup_failure_keeps_original_repository_error_without_retry(self):
        self.cache.remove_metadata().get_errors.return_value = 1
        with self.assertRaisesRegex(RuntimeError, 'Usable URL not found'):
            repositories.load_repositories(self.factory)
        self.assertEqual(self.factory.call_count, 1)
        self.bases[0].unlock_system_repo.assert_called_once()

    def test_recovery_applies_to_other_repo_names_without_glob_matching(self):
        repo_id = 'copr:example:desktop'
        self.repo.get_id.return_value = repo_id
        self.bases[0].get_repo_sack().load_repos.side_effect = RuntimeError(FAILURE.replace('"terra"', '"' + repo_id + '"'))
        self.assertIs(repositories.load_repositories(self.factory), self.bases[1])
        self.cache.remove_metadata.assert_called_once()

    def test_multiple_bad_repos_are_bounded_to_three_cleanups(self):
        bases, repos = [], []
        for index in range(4):
            name = f'repo-{index}'
            base, repo = Mock(), Mock()
            base.get_repo_sack().load_repos.side_effect = RuntimeError(FAILURE.replace('"terra"', '"' + name + '"'))
            repo.get_id.return_value = name
            bases.append(base)
            repos.append(repo)
        repo_api.RepoQuery.return_value = repos
        factory = Mock(side_effect=bases)
        with self.assertRaisesRegex(RuntimeError, 'repo-3'):
            repositories.load_repositories(factory)
        self.assertEqual(factory.call_count, 4)
        self.assertEqual(self.cache.remove_metadata.call_count, 3)


@unittest.skipUnless(shutil.which('createrepo_c'), 'Requires native repository tools')
class NativeMetadataRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='nobara-metadata-recovery-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'repo'
        self.source.mkdir()
        subprocess.run(['createrepo_c', str(self.source)], check=True, capture_output=True)
        self.bases = []

    def create_base(self):
        base = base_api.Base()
        config = base.get_config()
        for name, value in {'config_file_path': '/dev/null', 'installroot': str(self.root / 'system'),
                            'cachedir': str(self.root / 'cache'), 'system_cachedir': str(self.root / 'root-cache'),
                            'logdir': str(self.root / 'log'), 'plugins': False, 'use_host_config': False,
                            'reposdir': [], 'pkg_gpgcheck': True}.items():
            getattr(config, 'get_' + name + '_option')().set(value)
        base.get_vars().set('releasever', '44')
        base.setup()
        self.assertTrue(base.lock_system_repo(), 'Previous failed Base still holds the RPM lock')
        self.addCleanup(base.unlock_system_repo)
        repo = base.get_repo_sack().create_repo('terra')
        repo.get_config().get_baseurl_option().set([self.source.as_uri()])
        repo.get_config().get_priority_option().set(13)
        repo.get_config().get_excludepkgs_option().set(['held-package*'])
        repo.get_config().get_skip_if_unavailable_option().set(False)
        self.bases.append(base)
        if len(self.bases) == 1:
            self.cachedir = Path(repo.get_cachedir())
            self.other = self.cachedir.parent / 'other-0123456789abcdef'
            self.root_copy = self.root / 'root-cache' / self.cachedir.name
            for directory in (self.cachedir, self.other, self.root_copy):
                for name in ('repodata/stale.xml', 'solv/stale.solv', 'metalink.xml', 'mirrorlist', 'packages/keep.rpm'):
                    path = directory / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text('old cached data')
        return base

    def test_native_cleanup_preserves_rpms_and_other_repos_then_reloads_from_source(self):
        native_load = repo_api.RepoSackWeakPtr.load_repos
        def load(sack):
            if len(self.bases) == 1:
                raise RuntimeError(FAILURE)
            # Real native cleaner must have removed the poisoned metalink and
            # repodata, and a fresh Base must bypass the bad shared root copy.
            for name in ('repodata/stale.xml', 'solv/stale.solv', 'metalink.xml', 'mirrorlist'):
                self.assertFalse((self.cachedir / name).exists(), name)
                self.assertTrue((self.other / name).exists(), name)
                self.assertTrue((self.root_copy / name).exists(), name)
            self.assertTrue((self.cachedir / 'packages/keep.rpm').exists())
            return native_load(sack)
        with patch.object(repo_api.RepoSackWeakPtr, 'load_repos', new=load), \
             patch.object(repositories.os, 'geteuid', return_value=1000):
            base = repositories.load_repositories(self.create_base)
        self.assertEqual(len(self.bases), 2)
        repo = next(repo for repo in repo_api.RepoQuery(base) if repo.get_id() == 'terra')
        self.assertTrue((Path(repo.get_cachedir()) / 'repodata/repomd.xml').is_file())
        self.assertTrue(repo.get_config().get_pkg_gpgcheck_option().get_value())
        self.assertEqual(repo.get_config().get_priority_option().get_value(), 13)
        self.assertEqual(repo.get_config().get_excludepkgs_option().get_value(), ('held-package*',))
        self.assertFalse(repo.get_config().get_skip_if_unavailable_option().get_value())


@unittest.skipUnless((ROOT / 'appcenter/repositories.py').exists(), 'App Center source unavailable')
class MetadataFrontendTests(unittest.TestCase):
    def test_appcenter_uses_shared_recovery(self):
        spec = importlib.util.spec_from_file_location('appcenter_repository_test', ROOT / 'appcenter/repositories.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        factory = Mock()
        with patch.object(repositories, 'load_repositories') as recover:
            self.assertIs(module.load_repositories(factory), recover.return_value)
        recover.assert_called_once_with(factory)

    def test_appcenter_queries_and_privileged_operations_use_the_loader(self):
        import libdnf5
        for filename, function, owner in (('dnf_backend.py', '_create_base', 'DnfBackend'),
                                          ('privileged_helper.py', '_build_backend', None)):
            with self.subTest(filename=filename):
                ns = functions_from_file(filename, {function}, owner)
                loader = Mock(side_effect=lambda factory: factory())
                ns['load_repositories'] = loader
                fake = Mock()
                with patch.object(libdnf5.base, 'Base', return_value=fake):
                    if owner:
                        ns[function](Mock(libdnf5=libdnf5))
                    else:
                        ns[function]()
                loader.assert_called_once()
                fake.get_repo_sack().load_repos.assert_not_called()


if __name__ == '__main__':
    unittest.main()
