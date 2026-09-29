"""Origin diagnostics must identify evidence without blaming unrelated packages."""
import sys
from pathlib import Path
import types
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'src'
if 'nobara_updater' not in sys.modules:
    package = types.ModuleType('nobara_updater')
    package.__path__ = [str(SOURCE)]
    sys.modules['nobara_updater'] = package
from nobara_updater import update_origins as origins
from nobara_updater.update_state import UpdateError


def local(name='labwc', kind='local', repo='@commandline'):
    return dict(name=name, arch='x86_64', nevra=name + '-0:5-1.x86_64',
                aliases=[name + '-0:5-1.x86_64', name + '-5-1.x86_64'],
                repo=repo, kind=kind, nobara_repos=['nobara'])


class PackageOriginTests(unittest.TestCase):
    def test_repo_origin_not_install_reason_decides_whether_a_package_is_local(self):
        for repo in origins.NOBARA_REPOS:
            self.assertEqual(origins.origin_kind(repo), 'nobara')
        self.assertEqual(origins.origin_kind('@commandline'), 'local')
        self.assertEqual(origins.origin_kind('<unknown>'), 'unknown')
        self.assertEqual(origins.origin_kind('copr:someone:labwc'), 'third-party')

    def test_exact_nevra_identifies_only_package_involved_in_failure(self):
        records = [local(), local('unrelated')]
        error = origins.annotate_failure(UpdateError('package labwc-5-1.x86_64 conflicts with a system dependency'), records)
        self.assertEqual([p['name'] for p in error.package_conflicts], ['labwc'])
        self.assertIn('Manually installed RPM', str(error))
        self.assertNotIn('unrelated', str(error))
        self.assertNotIn('aliases', error.package_conflicts[0])

    def test_substring_or_different_version_is_not_evidence_against_installed_package(self):
        for text in ('labwc-tools-5-1.x86_64 conflict', 'labwc-9-1.x86_64 conflict', 'not-labwc-5-1.x86_64 conflict', 'No space left on device'):
            error = UpdateError(text)
            self.assertIs(origins.annotate_failure(error, [local()]), error)

    def test_excluded_candidate_can_identify_its_local_hold_in_solver_failure(self):
        package = dict(local(), blocked_replacements=['labwc-6-1.x86_64'])
        error = UpdateError('labwc-6-1.x86_64 is excluded by filtering')
        self.assertIs(origins.annotate_failure(error, [package]), error)
        annotated = origins.annotate_failure(error, [package], replacements=True)
        self.assertEqual(annotated.package_conflicts[0]['name'], 'labwc')
        self.assertIs(origins.annotate_failure(annotated, [package]), annotated)

    def test_unknown_origin_is_not_falsely_claimed_to_be_third_party(self):
        text = str(origins.PackageOriginError('Conflict', [local(kind='unknown', repo='<unknown>')]))
        self.assertIn('provenance is unverified', text)
        self.assertNotIn('Installed from third-party', text)

    def test_third_party_conflict_lists_original_and_nobara_repositories(self):
        error = origins.annotate_failure(UpdateError('labwc-0:5-1.x86_64 requires missing-abi'),
                                        [local(kind='third-party', repo='copr:someone:labwc')])
        self.assertIn("third-party repository 'copr:someone:labwc'", str(error))
        self.assertIn('Nobara provides this package in: nobara', str(error))


if __name__ == '__main__':
    unittest.main()
