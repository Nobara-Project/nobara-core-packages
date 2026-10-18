"""Exercise real kernel-install and GRUB hooks in a disposable mount namespace.

Only /tmp fixtures are writable. Device discovery and initrd generation use
fixtures; the native layout detection, GRUB hook and generic BLS hook are real.
This does not boot a kernel or modify host packages, boot files or settings.
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "src"
KERNEL = "7.2.11-200.nobara.fc44.x86_64"
OLD = os.uname().release
MACHINE = "a" * 32
GRUB_HOOK = Path("/usr/lib/kernel/install.d/20-grub.install")


@unittest.skipUnless(shutil.which("bwrap") and shutil.which("kernel-install") and GRUB_HOOK.is_file(),
                     "requires bubblewrap and Fedora's kernel-install/GRUB hooks")
class NativeBootRepairTests(unittest.TestCase):
    def fixture(self, layout, boot_root="/boot"):
        temporary = tempfile.TemporaryDirectory(prefix="nobara-kernel-install-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for name in ("boot/loader/entries", "boot/grub2", "boot/efi/loader/entries", "etc/kernel", "etc/default",
                     "modules/" + KERNEL, "var/tmp", "tmp", "bin"):
            (root / name).mkdir(parents=True, exist_ok=True)
        (root / "etc/machine-id").write_text(MACHINE + "\n")
        (root / "etc/passwd").write_text("root:x:0:0:root:/root:/bin/sh\n")
        (root / "etc/group").write_text("root:x:0:\n")
        (root / "etc/os-release").write_text('ID=nobara\nNAME=Nobara\nVERSION=44\nPRETTY_NAME="Nobara Linux 44"\n')
        (root / "etc/default/grub").write_text("GRUB_ENABLE_BLSCFG=true\n")
        (root / "etc/kernel/cmdline").write_text("root=UUID=fixture rootflags=subvol=@ ro\n")
        (root / "etc/kernel/install.conf").write_text("layout=" + layout + "\nBOOT_ROOT=" + boot_root + "\n")
        (root / "boot/grub2/grub.cfg").write_text("blscfg\n")
        (root / "modules" / KERNEL / "vmlinuz").write_bytes(b"fixture packaged kernel\n")
        (root / "boot/loader/entries" / (MACHINE + "-" + OLD + ".conf")).write_text(
            f"title Nobara\nversion {OLD}\nlinux /vmlinuz-{OLD}\ninitrd /initramfs-{OLD}.img $tuned_initrd\n"
            "options root=UUID=fixture rootflags=subvol=@ $tuned_params\n")
        # Exercise auto-detection triggered by an old entry-token directory.
        if layout == "auto":
            (root / boot_root.lstrip("/") / MACHINE).mkdir()
        programs = {
            "grub2-probe": 'case "$*" in *--target=device*) echo /dev/fixture;; *) echo ext4;; esac\n',
            "grub2-mkrelpath": 'for arg in "$@"; do last="$arg"; done\nprintf "/%s\\n" "${last##*/}"\n',
            "grub2-get-kernel-settings": 'echo GRUB_UPDATE_DEFAULT_KERNEL=false\n',
            "50-dracut.install": 'printf "fixture initrd\\n" > "$KERNEL_INSTALL_STAGING_AREA/initrd"\n',
            "99-tuned.install": 'entry="$KERNEL_INSTALL_BOOT_ROOT/loader/entries/$KERNEL_INSTALL_ENTRY_TOKEN-$2.conf"\n'
                                '[ -f "$entry" ] || exit 0\n'
                                "sed -i '/^initrd / s/$/ $tuned_initrd/; /^options / s/$/ $tuned_params/' \"$entry\"\n",
        }
        for name, body in programs.items():
            path = root / "bin" / name
            path.write_text("#!/bin/sh\nset -eu\n" + body)
            path.chmod(0o755)
        return root

    def execute(self, root, script):
        runner = root / "runner.py"
        runner.write_text(
            "import json, os, subprocess, sys, types\nfrom pathlib import Path\n"
            "package = types.ModuleType('nobara_updater')\n"
            "package.__path__ = [" + repr(str(SOURCE)) + "]\n"
            "sys.modules['nobara_updater'] = package\n"
            "from nobara_updater import update_boot as boot\n"
            "from nobara_updater.update_state import UpdateError\n"
            "kernel = " + repr(KERNEL) + "\nmachine = " + repr(MACHINE) + "\n"
            "def run(args):\n    subprocess.run(args, check=True)\n" + script)
        command = ["bwrap", "--unshare-all", "--die-with-parent", "--uid", "0", "--gid", "0", "--ro-bind", "/", "/"]
        for name, dest in (("boot", "/boot"), ("etc", "/etc"), ("modules", "/usr/lib/modules"),
                           ("tmp", "/tmp"), ("var", "/var")):
            command += ["--bind", str(root / name), dest]
        command += ["--tmpfs", "/run", "--dev", "/dev", "--proc", "/proc",
                    "--ro-bind", str(root / "bin"), "/tmp/fixturebin", "--ro-bind", str(runner), "/tmp/runner.py",
                    "--setenv", "PATH", "/tmp/fixturebin:/usr/bin:/usr/sbin", "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
                    "--setenv", "KERNEL_INSTALL_PLUGINS", str(GRUB_HOOK) + " /tmp/fixturebin/50-dracut.install "
                    "/usr/lib/kernel/install.d/90-loaderentry.install /tmp/fixturebin/99-tuned.install",
                    "/usr/bin/python3", "-B", "/tmp/runner.py"]
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
        if result.returncode and "bwrap:" in result.stderr and ("not permitted" in result.stderr or "No permissions" in result.stderr):
            self.skipTest("User/mount namespaces unavailable: " + result.stderr.strip())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_normal_kernel_install_restores_flat_image_and_entry(self):
        root = self.fixture("other")
        self.execute(root, """
entry = boot.ensure_grub_kernel(kernel, run)
assert entry == machine + '-' + kernel
assert Path('/boot/vmlinuz-' + kernel).read_bytes() == Path('/usr/lib/modules/' + kernel + '/vmlinuz').read_bytes()
assert 'initrd /initramfs-' + kernel + '.img' in Path('/boot/loader/entries/' + entry + '.conf').read_text()
""")

    def test_generic_auto_layout_and_esp_destination_are_repaired(self):
        for destination in ("/boot", "/boot/efi"):
            with self.subTest(destination=destination):
                root = self.fixture("auto", destination)
                self.execute(root, """
settings = Path('/etc/kernel/install.conf').read_bytes()
image = '/usr/lib/modules/' + kernel + '/vmlinuz'
details = json.loads(subprocess.check_output(['kernel-install', '--json=short', 'inspect', kernel, image]))
assert details['Layout'] == 'bls', details
run(['kernel-install', 'add', kernel, image])
# Reproduce the report: the RPM's hook succeeds, but GRUB has no flat image.
assert not Path('/boot/vmlinuz-' + kernel).exists()
generic = Path(details['EntryDirectory']) / 'linux'
assert generic.is_file()
generic_payload = generic.read_bytes()
entry = boot.ensure_grub_kernel(kernel, run)
assert entry == machine + '-' + kernel
assert Path('/boot/vmlinuz-' + kernel).read_bytes() == generic_payload
text = Path('/boot/loader/entries/' + entry + '.conf').read_text()
assert 'linux /vmlinuz-' + kernel in text, text
assert 'initrd /initramfs-' + kernel + '.img' in text, text
assert '$tuned_initrd' in text, text
assert '$tuned_params' in text, text
assert generic.read_bytes() == generic_payload
assert Path('/etc/kernel/install.conf').read_bytes() == settings
""")


if __name__ == "__main__":
    unittest.main()
