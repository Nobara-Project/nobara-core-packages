"""Disposable QEMU boot test for the shipped recovery initramfs (opt-in)."""
import os
from pathlib import Path
import shutil
import subprocess

PROJECT = Path(__file__).resolve().parents[1]


def install_root(root):
    for directory in ("bin", "sbin", "proc", "sys", "dev", "run", "tmp"):
        (root / directory).mkdir(exist_ok=True)
    shutil.copy2("/usr/bin/busybox", root / "bin/busybox")
    (root / "bin/sh").symlink_to("busybox")
    (root / "etc/os-release").write_text("ID=nobara\nVERSION_ID=44\n")
    init = root / "sbin/init"
    init.write_text('''#!/bin/busybox sh
exec >/dev/console 2>&1
read version </etc/version
if [ "$version" = 'old operating system' ] && [ -s /etc/nobara-updater-recovery.json ]; then
    echo NOBARA_VM_ROLLBACK_OK
else
    echo NOBARA_VM_ROLLBACK_FAILED
fi
/bin/busybox sync
echo o >/proc/sysrq-trigger
exec /bin/busybox sleep 600
''')
    init.chmod(0o755)


def boot(directory, disk, vg, config, luks_uuid=None, key=None):
    base = directory / "dracut"
    shutil.copytree("/usr/lib/dracut", base, symlinks=True)
    shutil.copy2("/usr/bin/dracut", base / "dracut")
    shutil.copytree(PROJECT / "data/dracut/91nobara-rollback", base / "modules.d/91nobara-rollback")
    confdir = directory / "empty-conf"
    confdir.mkdir()
    include = directory / "initrd-include"
    (include / "etc").mkdir(parents=True)
    shutil.copy2(config, include / "etc/nobara-rollback.conf")
    switch = include / "etc/systemd/system/initrd-switch-root.service.d/test-init.conf"
    switch.parent.mkdir(parents=True)
    switch.write_text("[Service]\nExecStart=\nExecStart=systemctl --no-block switch-root /sysroot /sbin/init\nStandardError=journal+console\n")
    if luks_uuid:
        shutil.copy2(key, include / "test-key")
        (include / "etc/crypttab").write_text("luks-" + luks_uuid + " UUID=" + luks_uuid + " /test-key luks,x-initrd.attach\n")
    kernel = os.uname().release
    initrd = directory / "vm-recovery.img"
    command = [str(base / "dracut"), "--local", "--force", "--conf", "/dev/null", "--confdir", str(confdir),
               "--no-hostonly", "--no-hostonly-cmdline", "--nolvmconf", "--nostrip", "--kver", kernel,
               "--modules", "nobara-rollback bash systemd systemd-initrd kernel-modules rootfs-block fs-lib base dm crypt lvm",
               "--drivers", "virtio_pci virtio_blk dm_snapshot dm_crypt ext4 xfs",
               "--include", str(include), "/", str(initrd)]
    with (directory / "dracut.log").open("w") as log:
        result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
    if result.returncode:
        raise RuntimeError((directory / "dracut.log").read_text())
    cmdline = ("console=ttyS0 rd.shell=0 rd.emergency=reboot panic=10 sysrq_always_enabled "
               "root=/dev/" + vg + "/root rd.lvm.lv=" + vg + "/root noresume nobara.rollback=" + "a" * 32)
    if luks_uuid:
        cmdline += " rd.luks.uuid=" + luks_uuid
    logfile = Path("/tmp/nobara-lvm-vm-" + ("encrypted" if luks_uuid else "plain") + ".log")
    command = ["qemu-system-x86_64", "-machine", "q35,accel=tcg", "-cpu", "max", "-smp", "2", "-m", "2048",
               "-display", "none", "-monitor", "none", "-serial", "stdio", "-no-reboot",
               "-kernel", "/usr/lib/modules/" + kernel + "/vmlinuz", "-initrd", str(initrd), "-append", cmdline,
               "-drive", "file=" + str(disk) + ",format=raw,if=virtio"]
    with logfile.open("w") as log:
        subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=240, check=True)
    text = logfile.read_text()
    if "NOBARA_VM_ROLLBACK_OK" not in text or "NOBARA_VM_ROLLBACK_FAILED" in text:
        raise RuntimeError("The recovery VM failed; see " + str(logfile) + "\n" + text[-8000:])
