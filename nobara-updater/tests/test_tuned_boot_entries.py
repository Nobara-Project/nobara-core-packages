"""BLS entries carrying TuneD's $tuned_params/$tuned_initrd (issue #142).

TuneD's kernel-install hook appends these variables to every entry. GRUB
fills them in at boot from TuneD's grub.cfg block or grubenv, and an unset
variable expands to nothing.
"""
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "src"
if "nobara_updater" not in sys.modules:
    package = types.ModuleType("nobara_updater")
    package.__path__ = [str(SOURCE)]
    sys.modules["nobara_updater"] = package
from nobara_updater import update_recovery as recovery
from nobara_updater.update_state import UpdateError

KERNEL = "7.2.6-201.nobara.fc44.x86_64"
UUID = "12345678-1234-1234-1234-123456789abc"
JOB = "a" * 32
# The entry from the issue, as rewritten by TuneD's 92-tuned.install hook.
ENTRY = f"""title Nobara Linux ({KERNEL}) 44 (KDE Plasma Desktop Edition)
version {KERNEL}
linux /vmlinuz-{KERNEL}
initrd /initramfs-{KERNEL}.img $tuned_initrd
options root=UUID={UUID} ro rootflags=subvol=@ rd.driver.blacklist=nouveau quiet splash $tuned_params
grub_users $grub_users
grub_arg --unrestricted
grub_class nobara
"""
HEADER = "### BEGIN /etc/grub.d/00_header ###\nload_env\n### END /etc/grub.d/00_header ###\n"
BLSCFG = "### BEGIN /etc/grub.d/10_linux ###\ninsmod blscfg\nblscfg\n### END /etc/grub.d/10_linux ###\n"


def tuned_block(params="", initrd=""):
    return ("### BEGIN /etc/grub.d/00_tuned ###\n"
            f'set tuned_params="{params}"\nexport tuned_params\n'
            f'set tuned_initrd="{initrd}"\nexport tuned_initrd\n'
            "### END /etc/grub.d/00_tuned ###\n")


class TunedBootEntryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.boot = Path(temporary.name) / "boot"
        (self.boot / "grub2").mkdir(parents=True)
        (self.boot / "loader/entries").mkdir(parents=True)
        self.entry = self.boot / "loader/entries" / ("machine-" + KERNEL + ".conf")
        self.entry.write_text(ENTRY)
        for name in ("vmlinuz-" + KERNEL, "initramfs-" + KERNEL + ".img"):
            (self.boot / name).write_text(name)
        self.grub_cfg(tuned_block())
        self.environment = {"saved_entry": self.entry.stem}
        for patcher in (patch.object(recovery, "BOOT", self.boot),
                        patch.object(recovery, "grub_environment", side_effect=lambda: dict(self.environment))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def grub_cfg(self, tuned=""):
        (self.boot / "grub2/grub.cfg").write_text(HEADER + tuned + BLSCFG)

    def payload_names(self):
        return [path.name for path in recovery.boot_payloads(self.entry)]

    def test_reporters_entry_with_empty_tuned_values_is_supported(self):
        self.assertEqual(self.payload_names(), ["vmlinuz-" + KERNEL, "initramfs-" + KERNEL + ".img"])

    def test_variables_tuned_never_set_expand_to_nothing(self):
        # Before the next grub2-mkconfig, grub.cfg has no 00_tuned block yet.
        self.grub_cfg()
        self.assertEqual(self.payload_names(), ["vmlinuz-" + KERNEL, "initramfs-" + KERNEL + ".img"])

    def test_grub_cfg_block_overrides_grubenv(self):
        # 00_header loads grubenv first; the later 00_tuned block wins.
        self.environment.update(tuned_params="isolcpus=1", tuned_initrd="/stale.img")
        self.grub_cfg(tuned_block(params="nohz=on"))
        variables = recovery.grub_variables()
        self.assertEqual((variables["tuned_params"], variables["tuned_initrd"]), ("nohz=on", ""))

    def test_grubenv_is_used_without_a_grub_cfg_block(self):
        self.environment.update(tuned_params="skew_tick=1 tsc=reliable")
        self.grub_cfg()
        self.assertEqual(recovery.grub_variables()["tuned_params"], "skew_tick=1 tsc=reliable")

    def test_tuned_initrd_overlay_is_a_payload_in_order(self):
        (self.boot / "tuned-initrd.img").write_text("overlay")
        self.grub_cfg(tuned_block(initrd="/tuned-initrd.img"))
        self.assertEqual(self.payload_names(), ["vmlinuz-" + KERNEL, "initramfs-" + KERNEL + ".img", "tuned-initrd.img"])

    def test_missing_or_outside_overlay_is_refused(self):
        for value in ("/tuned-initrd.img", "/../etc/passwd"):
            with self.subTest(value=value):
                self.grub_cfg(tuned_block(initrd=value))
                with self.assertRaisesRegex(UpdateError, "unavailable"):
                    recovery.boot_payloads(self.entry)

    def test_other_variables_are_still_refused(self):
        for old, new in (("splash $tuned_params", "splash $foo"), ("$tuned_initrd", "$tuned_params"),
                         ("splash $tuned_params", "splash ${tuned_params}"), ("/vmlinuz-", "$tuned_initrd/vmlinuz-")):
            with self.subTest(new=new):
                self.entry.write_text(ENTRY.replace(old, new))
                with self.assertRaises(UpdateError):
                    recovery.boot_payloads(self.entry)

    def test_unverifiable_grub_cfg_assignments_are_refused(self):
        for config in (tuned_block(params='a" b'),
                       tuned_block(params="$foo"),
                       tuned_block() + 'set tuned_params="late"\n',
                       'if true; then\nset tuned_params=""\nfi\n',
                       tuned_block() + "load_env\n"):
            with self.subTest(config=config):
                self.grub_cfg(config)
                with self.assertRaises(UpdateError):
                    recovery.grub_variables()

    def test_unverifiable_grubenv_value_is_refused(self):
        self.environment["tuned_params"] = "a;b"
        self.grub_cfg()
        with self.assertRaises(UpdateError):
            recovery.grub_variables()

    def test_recovery_entry_is_self_contained(self):
        (self.boot / "tuned-initrd.img").write_text("overlay")
        self.grub_cfg(tuned_block(params="skew_tick=1 nohz=on", initrd="/tuned-initrd.img"))
        subvolume = ".nobara-updater/" + JOB + "/root"
        text = recovery.recovery_entry(self.entry, JOB, subvolume)
        fields = dict(line.split(" ", 1) for line in text.splitlines())
        archive = self.boot / "nobara-updater" / JOB
        self.assertEqual(fields["linux"], f"/nobara-updater/{JOB}/linux-0-vmlinuz-{KERNEL}")
        self.assertEqual(fields["initrd"].split(), [f"/nobara-updater/{JOB}/initrd-0-initramfs-{KERNEL}.img",
                                                    f"/nobara-updater/{JOB}/initrd-1-tuned-initrd.img"])
        self.assertEqual((archive / "initrd-1-tuned-initrd.img").read_text(), "overlay")
        self.assertEqual(fields["options"].split()[-3:], ["skew_tick=1", "nohz=on", "rootflags=subvol=" + subvolume])
        self.assertNotIn("$", fields["linux"] + fields["initrd"] + fields["options"])
        self.assertEqual(fields["grub_users"], "$grub_users")
        self.assertEqual(fields["title"], "Nobara — previous system (" + JOB[:8] + ")")


if __name__ == "__main__":
    unittest.main()
