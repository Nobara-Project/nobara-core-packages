"""Retire obsolete boot tarballs without deleting pending work or recovery."""
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / 'src'
if 'nobara_updater' not in sys.modules:
    package = types.ModuleType('nobara_updater')
    package.__path__ = [str(SOURCE)]
    sys.modules['nobara_updater'] = package
from nobara_updater import update_backend as backend, update_recovery as recovery, update_state as state
from nobara_updater.update_btrfs import JOURNAL

CURRENT, OLD = 'a' * 32, 'b' * 32


class JobCleanupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='nobara-job-cleanup-')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.root = self.directory / 'state'
        self.boot = self.directory / 'boot'
        self.entries = self.boot / 'loader/entries'
        self.entries.mkdir(parents=True)
        (self.entries / 'normal.conf').write_text('title Normal\n')
        self.environment = dict(saved_entry='normal')
        self.saved = dict(status='complete', job=CURRENT, recovery=dict(created=True), recovery_cleanup_complete=True)
        self.jobs = {identifier: self.make_job(identifier) for identifier in (CURRENT, OLD)}
        state.atomic_json(self.root / 'state.json', self.saved)
        real_is_mount = Path.is_mount
        mapped_paths = {'/etc/system-update', '/run/nobara-updater-offline', '/etc/nobara-updater-recovery.json'}
        for p in (patch.object(recovery, 'BOOT', self.boot), patch.object(state, 'TRIGGER', self.directory / 'system-update'),
                  patch.object(recovery, 'Path', side_effect=lambda value: self.directory / value.lstrip('/') if value in mapped_paths else Path(value)),
                  patch.object(Path, 'is_mount', lambda p: p == self.boot or real_is_mount(p)),
                  patch.object(recovery, 'grub_environment', side_effect=lambda: self.environment),
                  patch.object(recovery.os, 'sync')):
            p.start()
            self.addCleanup(p.stop)

    def make_job(self, identifier):
        job = self.root / 'jobs' / identifier
        job.mkdir(parents=True)
        for name in ('boot.tar', 'efi.tar', 'plan.json', 'transaction.json'):
            (job / name).write_text(name)
        (job / 'diagnostics').mkdir()
        (job / 'diagnostics/failure.log').write_text('error report')
        (job / 'packages').mkdir()
        (job / 'packages/example.rpm').write_text('download')
        return job

    def prune(self):
        return recovery.prune_boot_archives(self.saved, self.root)

    def test_legacy_archives_removed_but_metadata_and_reports_kept(self):
        self.assertEqual(self.prune(), (2 * (len('boot.tar') + len('efi.tar')), 0))
        for job in self.jobs.values():
            self.assertFalse((job / 'boot.tar').exists())
            self.assertFalse((job / 'efi.tar').exists())
            self.assertTrue((job / 'plan.json').is_file())
            self.assertTrue((job / 'diagnostics/failure.log').is_file())
        self.assertEqual(self.prune(), (0, 0))

    def test_boot_directories_and_recovery_or_restored_entries_preserve_archives(self):
        for reference in (self.boot / 'nobara-updater' / OLD,
                          self.entries / f'nobara-recovery-{OLD}.conf',
                          self.entries / f'nobara-restored-{OLD}.conf'):
            with self.subTest(reference=reference):
                reference.parent.mkdir(parents=True, exist_ok=True)
                reference.symlink_to(self.directory / 'missing')
                _, kept = self.prune()
                self.assertEqual(kept, 2)
                self.assertTrue((self.jobs[OLD] / 'boot.tar').is_file())
                reference.unlink()

    def test_current_job_kept_until_recovery_cleanup_finishes(self):
        self.saved['recovery_cleanup_complete'] = False
        _, kept = self.prune()
        self.assertEqual(kept, 2)
        self.assertTrue((self.jobs[CURRENT] / 'boot.tar').is_file())
        self.assertFalse((self.jobs[OLD] / 'boot.tar').exists())

    def test_pending_failed_or_unconfirmed_states_keep_everything(self):
        for status in state.ACTIVE | {'failed', 'interrupted', 'idle', 'installer-complete'}:
            with self.subTest(status=status):
                self.saved['status'] = status
                with self.assertRaises(state.UpdateError):
                    self.prune()
                self.assertTrue((self.jobs[OLD] / 'boot.tar').is_file())

    def test_triggers_recovery_markers_and_layout_journal_block_cleanup(self):
        blockers = [self.directory / 'system-update', self.directory / 'etc/system-update',
                    self.directory / 'run/nobara-updater-offline', self.directory / 'etc/nobara-updater-recovery.json',
                    self.root / JOURNAL, self.boot / 'nobara-updater/recovery-armed']
        for blocker in blockers:
            with self.subTest(blocker=blocker):
                blocker.parent.mkdir(parents=True, exist_ok=True)
                blocker.symlink_to(self.directory / 'missing')
                with self.assertRaises(state.UpdateError):
                    self.prune()
                self.assertTrue((self.jobs[OLD] / 'efi.tar').is_file())
                blocker.unlink()

    def test_grub_trial_and_recovery_selection_block_cleanup(self):
        for value in ({'nobara_fallback': 'fallback'}, {'nobara_trial': 'trial'}, {'next_entry': 'next'},
                      {'saved_entry': f'nobara-recovery-{OLD}'}, {'saved_entry': 'missing'}):
            with self.subTest(value=value):
                self.environment = dict(saved_entry='normal') | value
                with self.assertRaises(state.UpdateError):
                    self.prune()
                self.assertTrue((self.jobs[OLD] / 'boot.tar').is_file())

    def test_unmounted_boot_never_looks_like_retired_recovery(self):
        with patch.object(Path, 'is_mount', return_value=False), self.assertRaises(state.UpdateError):
            self.prune()
        self.assertTrue((self.jobs[OLD] / 'boot.tar').is_file())

    def test_unknown_jobs_and_symlinks_are_not_deleted(self):
        foreign = self.make_job('not-an-update')
        linked = self.root / 'jobs' / ('c' * 32)
        linked.symlink_to(foreign, target_is_directory=True)
        target = self.directory / 'keep.tar'
        target.write_text('keep')
        (self.jobs[OLD] / 'efi.tar').unlink()
        (self.jobs[OLD] / 'efi.tar').symlink_to(target)
        self.prune()
        self.assertTrue((foreign / 'boot.tar').is_file())
        self.assertTrue(linked.is_symlink())
        self.assertTrue((self.jobs[OLD] / 'efi.tar').is_symlink())
        self.assertEqual(target.read_text(), 'keep')

    def test_mounted_jobs_and_payload_directories_are_not_deleted(self):
        with patch.object(Path, 'is_mount', lambda p: p in {self.boot, self.jobs[OLD], self.jobs[CURRENT] / 'packages'}):
            backend.clean_update_cache(self.root)
        self.assertTrue((self.jobs[OLD] / 'boot.tar').is_file())
        self.assertTrue((self.jobs[OLD] / 'packages/example.rpm').is_file())
        self.assertTrue((self.jobs[CURRENT] / 'packages/example.rpm').is_file())

    def test_symlinked_jobs_root_blocks_archive_and_payload_cleanup(self):
        jobs = self.root / 'jobs'
        destination = self.directory / 'external-jobs'
        jobs.rename(destination)
        jobs.symlink_to(destination, target_is_directory=True)
        with self.assertRaises(state.UpdateError):
            backend.clean_update_cache(self.root)
        self.assertTrue((destination / OLD / 'boot.tar').is_file())
        self.assertTrue((destination / OLD / 'packages/example.rpm').is_file())

    def test_unreadable_boot_selection_preserves_archives_and_downloads(self):
        with patch.object(recovery, 'grub_environment', side_effect=OSError('cannot read GRUB environment')), \
             self.assertRaises(OSError):
            backend.clean_update_cache(self.root)
        self.assertTrue((self.jobs[OLD] / 'boot.tar').is_file())
        self.assertTrue((self.jobs[OLD] / 'packages/example.rpm').is_file())

    def test_explicit_cleanup_preserves_state_and_reports_without_changing_packages(self):
        before = (self.root / 'state.json').read_bytes()
        with patch.object(backend, 'run') as command, patch.object(backend, 'prune_kernel_packages') as kernels:
            backend.clean_update_cache(self.root)
        command.assert_not_called()
        kernels.assert_not_called()
        self.assertEqual((self.root / 'state.json').read_bytes(), before)
        for job in self.jobs.values():
            self.assertFalse((job / 'packages').exists())
            self.assertFalse((job / 'boot.tar').exists())
            self.assertTrue((job / 'diagnostics/failure.log').is_file())

    def test_explicit_cleanup_refuses_scheduled_job_and_busy_lock(self):
        state.write_state(self.root, self.saved, 'scheduled')
        with self.assertRaises(state.UpdateError):
            backend.clean_update_cache(self.root)
        state.write_state(self.root, self.saved, 'complete')
        with state.update_lock(self.root), self.assertRaisesRegex(state.UpdateError, 'running'):
            backend.clean_update_cache(self.root)
        self.assertTrue((self.jobs[CURRENT] / 'packages/example.rpm').is_file())
        self.assertTrue((self.jobs[CURRENT] / 'boot.tar').is_file())

    def test_already_confirmed_jobs_catch_up_without_repeating_snapshot_cleanup(self):
        with patch.object(backend, 'maintain_btrfs_root', return_value=True), \
             patch.object(backend, 'cleanup_kernel_retention'), patch.object(backend, 'prune_recovery') as snapshots:
            backend.cleanup_confirmed_update(self.root, self.saved)
        snapshots.assert_not_called()
        self.assertFalse((self.jobs[OLD] / 'boot.tar').exists())

    def test_cleanup_failure_does_not_invalidate_confirmed_boot(self):
        before = (self.root / 'state.json').read_bytes()
        with patch.object(recovery, 'prune_boot_archives', side_effect=OSError('read-only')), \
             self.assertLogs(backend.LOG, level='WARNING'):
            backend.cleanup_boot_archives(self.root, self.saved)
        self.assertEqual((self.root / 'state.json').read_bytes(), before)
        self.assertTrue((self.jobs[CURRENT] / 'boot.tar').is_file())

    def test_worker_cleanup_failure_is_nonzero_without_rewriting_update_state(self):
        import contextlib
        import importlib.util
        import io
        spec = importlib.util.spec_from_file_location('cleanup_worker_test', SOURCE / 'update_worker.py')
        worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(worker)
        for fails in (False, True):
            with self.subTest(fails=fails), patch.object(worker, 'STATE_DIR', self.root), \
                 patch.object(worker.sys, 'argv', ['worker', 'clean-cache']), \
                 patch.object(worker.os, 'geteuid', return_value=0), patch.object(worker.os, 'umask'), \
                 patch.object(worker.logging, 'basicConfig'), patch.object(worker, 'RotatingFileHandler'), \
                 patch.object(worker.backend, 'clean_update_cache', side_effect=state.UpdateError('pending update') if fails else None) as clean, \
                 patch.object(worker, 'write_state') as write, patch.object(worker, 'record_failure') as report, \
                 contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(worker.main(), 1 if fails else 0)
            clean.assert_called_once_with()
            write.assert_not_called()
            report.assert_not_called()


if __name__ == '__main__':
    unittest.main()
