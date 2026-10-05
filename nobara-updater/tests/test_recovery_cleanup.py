"""Recovery retirement must never delete active roots or referenced boot files."""
import json
from pathlib import Path
import shutil
import subprocess
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
from nobara_updater import update_recovery as recovery
from nobara_updater.update_state import UpdateError

UUID = '12345678-1234-1234-1234-123456789abc'
CURRENT, LATEST, OLD = 'a' * 32, 'b' * 32, 'c' * 32


class RecoveryCleanupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.top = self.directory / 'top'
        self.boot = self.directory / 'boot'
        self.entries = self.boot / 'loader/entries'
        self.entries.mkdir(parents=True)
        self.root = '/.nobara-updater/' + CURRENT + '/root'
        self.environment = {'saved_entry': 'normal'}
        self.mounts = []
        self.commands = []
        self.layout = dict(created=True, uuid=UUID)
        self.parents = {job: self.owned(job) for job in (CURRENT, LATEST, OLD)}
        for name in ('kernel', 'initramfs'):
            (self.boot / name).write_text(name)
        self.normal = self.entries / 'normal.conf'
        self.normal.write_text('title Nobara Linux\nlinux /kernel\ninitrd /initramfs\noptions root=UUID=' + UUID + ' rootflags=subvol=' + self.root + '\n')
        for patcher in (patch.object(recovery, 'BOOT', self.boot), patch.object(recovery, 'BTRFS_TOP', self.top),
                        patch.object(recovery, 'output', side_effect=self.output),
                        patch.object(recovery, 'grub_environment', side_effect=lambda: self.environment),
                        patch.object(recovery.subprocess, 'run', side_effect=self.run_command), patch.object(recovery.os, 'sync')):
            patcher.start()
            self.addCleanup(patcher.stop)

    def owned(self, job):
        parent = self.top / '.nobara-updater' / job
        parent.mkdir(parents=True)
        (parent / 'owner.json').write_text(json.dumps(dict(owner='nobara-updater', job=job)))
        for name in ('root', 'saved'):
            (parent / name).mkdir()
            (parent / name / 'system').write_text(job + name)
        archive = self.boot / 'nobara-updater' / job
        archive.mkdir(parents=True)
        (archive / 'kernel').write_text('saved kernel')
        (self.entries / ('nobara-recovery-' + job + '.conf')).write_text('title previous system\nlinux /nobara-updater/' + job + '/kernel\n')
        return parent

    def output(self, command):
        if '--mountpoint' in command:
            return json.dumps({'filesystems': [dict(fstype='btrfs', uuid=UUID, fsroot=self.root)]})
        if '--list' in command:
            return json.dumps({'filesystems': self.mounts})
        raise AssertionError(command)

    def run_command(self, command, **kwargs):
        self.commands.append(command)
        if command[:3] == ['btrfs', 'subvolume', 'delete']:
            # Menu removal must happen first, even if deleting a snapshot fails.
            self.assertFalse(list(self.entries.glob('nobara-recovery-*.conf')))
            target = Path(command[-1])
            self.assertTrue(target.is_relative_to(self.top))
            shutil.rmtree(target)
        elif command[0] not in {'mount', 'umount'}:
            raise AssertionError(command)

    def cleanup(self):
        recovery.prune_recovery(self.layout, LATEST)

    def test_latest_and_older_backups_are_removed_but_active_system_survives(self):
        self.cleanup()
        self.assertEqual((self.parents[CURRENT] / 'root/system').read_text(), CURRENT + 'root')
        self.assertTrue((self.parents[CURRENT] / 'owner.json').is_file())
        self.assertFalse((self.parents[CURRENT] / 'saved').exists())
        for job in (LATEST, OLD):
            self.assertFalse(self.parents[job].exists())
        self.assertFalse(list(self.entries.glob('nobara-recovery-*.conf')))
        self.assertFalse(list((self.boot / 'nobara-updater').iterdir()))
        self.assertTrue(self.normal.is_file())
        self.cleanup()  # Idempotent after the latest job's directory is gone.
        self.assertTrue((self.parents[CURRENT] / 'root/system').is_file())

    def test_auxiliary_initrd_referenced_by_normal_entry_is_preserved(self):
        archive = self.boot / 'nobara-updater' / CURRENT
        (archive / 'microcode').write_text('microcode')
        self.normal.write_text(self.normal.read_text().replace('initrd /initramfs', 'initrd /initramfs /nobara-updater/' + CURRENT + '/microcode'))
        self.cleanup()
        self.assertEqual((archive / 'microcode').read_text(), 'microcode')
        self.assertFalse(list(self.entries.glob('nobara-recovery-*.conf')))

    def test_tuned_variables_in_normal_entries_do_not_block_cleanup(self):
        # TuneD's kernel-install hook re-adds these to every entry after a kernel update.
        text = self.normal.read_text().replace('initrd /initramfs', 'initrd /initramfs $tuned_initrd')
        self.normal.write_text(text.rstrip('\n') + ' $tuned_params\n')
        self.cleanup()
        self.assertFalse(list(self.entries.glob('nobara-recovery-*.conf')))

    def test_unknown_variable_in_boot_images_blocks_all_deletion(self):
        self.normal.write_text(self.normal.read_text().replace('initrd /initramfs', 'initrd /initramfs $early_initrd'))
        with self.assertRaises(UpdateError):
            self.cleanup()
        self.assertEqual(len(list(self.entries.glob('nobara-recovery-*.conf'))), 3)
        self.assertFalse([c for c in self.commands if c[:3] == ['btrfs', 'subvolume', 'delete']])

    def test_armed_or_recovery_default_blocks_all_deletion(self):
        for values in ({'nobara_fallback': 'fallback'}, {'nobara_trial': 'normal'},
                       {'next_entry': 'other'}, {'saved_entry': 'nobara-recovery-' + CURRENT}):
            with self.subTest(values=values):
                self.environment = dict(saved_entry='normal', **{k: v for k, v in values.items() if k != 'saved_entry'})
                self.environment.update(values)
                with self.assertRaises(UpdateError):
                    self.cleanup()
                self.assertTrue((self.parents[LATEST] / 'saved').exists())
                self.assertEqual(len(list(self.entries.glob('nobara-recovery-*.conf'))), 3)

    def test_missing_or_stale_normal_entry_preserves_fallback_data(self):
        original = self.normal.read_text()
        for replacement in ('title broken\n', original.replace(self.root, '/@'), original.replace('/kernel', '/missing')):
            self.normal.write_text(replacement)
            with self.assertRaises(UpdateError):
                self.cleanup()
            self.assertTrue((self.parents[LATEST] / 'root').exists())
            self.assertEqual(len(list(self.entries.glob('nobara-recovery-*.conf'))), 3)

    def test_mounted_backup_is_preserved_and_retired_on_retry_after_unmount(self):
        self.mounts = [dict(uuid=UUID, fsroot='/.nobara-updater/' + LATEST + '/root')]
        with self.assertRaisesRegex(UpdateError, 'still mounted'):
            self.cleanup()
        self.assertTrue((self.parents[LATEST] / 'root/system').is_file())
        self.mounts.clear()
        self.cleanup()
        self.assertFalse(self.parents[LATEST].exists())

    def test_unowned_and_symlinked_snapshot_directories_are_not_touched(self):
        foreign = self.owned('d' * 32)
        (foreign / 'owner.json').write_text('{}')
        linked = self.top / '.nobara-updater' / ('e' * 32)
        linked.symlink_to(foreign, target_is_directory=True)
        # Only check ordering for entries belonging to this updater.
        def delete(command, **kwargs):
            if command[:3] == ['btrfs', 'subvolume', 'delete']:
                shutil.rmtree(command[-1])
        with patch.object(recovery.subprocess, 'run', side_effect=delete):
            self.cleanup()
        self.assertTrue((foreign / 'saved/system').is_file())
        self.assertTrue(linked.is_symlink())
        self.assertTrue((self.entries / ('nobara-recovery-' + 'd' * 32 + '.conf')).is_file())

    def test_interrupted_deletion_keeps_ownership_for_retry(self):
        first = True
        def fail_once(command, **kwargs):
            nonlocal first
            if command[:3] == ['btrfs', 'subvolume', 'delete'] and first:
                first = False
                raise subprocess.CalledProcessError(1, command)
            return self.run_command(command, **kwargs)
        with patch.object(recovery.subprocess, 'run', side_effect=fail_once):
            with self.assertRaises(subprocess.CalledProcessError):
                self.cleanup()
        self.assertTrue((self.parents[LATEST] / 'owner.json').is_file())
        self.assertEqual(self.commands[-1][0], 'umount')
        self.cleanup()
        self.assertFalse(self.parents[LATEST].exists())


if __name__ == '__main__':
    unittest.main()
