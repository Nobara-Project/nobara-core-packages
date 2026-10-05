"""Opt-in dracut/QEMU validation of renamed Btrfs roots, without host reboot."""
import os
from pathlib import Path
import shutil
import subprocess


def install_root(root):
    for directory in ("bin", "sbin", "proc", "sys", "dev", "run", "tmp"):
        (root / directory).mkdir(exist_ok=True)
    shutil.copy2("/usr/bin/busybox", root / "bin/busybox")
    (root / "bin/sh").symlink_to("busybox")
    (root / "etc/os-release").write_text("ID=nobara\nVERSION_ID=44\n")
    init = root / "sbin/init"
    init.write_text('''#!/bin/busybox sh
exec >/dev/console 2>&1
read contents </system
if [ "$contents" = 'custom labwc repaired in recovery' ]; then
    echo NOBARA_VM_BTRFS_ROOT_OK
else
    echo NOBARA_VM_BTRFS_ROOT_FAILED
fi
/bin/busybox sync
echo o >/proc/sysrq-trigger
exec /bin/busybox sleep 600
''')
    init.chmod(0o755)


def boot(directory, disk, uuid, root_id, old_path):
    include = directory / "initrd-include"
    config = include / "etc/cmdline.d"
    config.mkdir(parents=True)
    # Model a pre-recovery hostonly initramfs containing the old root path.
    # The explicit BLS arguments must override this after a rename.
    (config / "95root-dev.conf").write_text(
        f"root=UUID={uuid} rootfstype=btrfs rootflags=subvol={old_path}\n")
    switch = include / "etc/systemd/system/initrd-switch-root.service.d/test-init.conf"
    switch.parent.mkdir(parents=True)
    switch.write_text("[Service]\nExecStart=\nExecStart=systemctl --no-block switch-root /sysroot /sbin/init\nStandardError=journal+console\n")
    confdir = directory / "empty-conf"
    confdir.mkdir()
    kernel = os.uname().release
    initrd = directory / "vm-btrfs.img"
    command = ["dracut", "--force", "--conf", "/dev/null", "--confdir", str(confdir),
               "--no-hostonly", "--no-hostonly-cmdline", "--nostrip", "--kver", kernel,
               "--modules", "bash systemd systemd-initrd kernel-modules rootfs-block fs-lib base btrfs",
               "--drivers", "virtio_pci virtio_blk btrfs",
               "--include", str(include), "/", str(initrd)]
    with (directory / "dracut.log").open("w") as log:
        result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
    if result.returncode:
        raise RuntimeError((directory / "dracut.log").read_text())
    for selector, name in ((f"subvolid={root_id}", "id"), ("subvol=@", "path")):
        logfile = Path("/tmp/nobara-btrfs-root-vm-" + name + ".log")
        cmdline = ("console=ttyS0 rd.shell=0 rd.emergency=reboot panic=10 sysrq_always_enabled "
                   f"root=UUID={uuid} rootfstype=btrfs rootflags={selector} ro noresume")
        command = ["qemu-system-x86_64", "-machine", "q35,accel=tcg", "-cpu", "max", "-smp", "2", "-m", "2048",
                   "-display", "none", "-monitor", "none", "-serial", "stdio", "-no-reboot",
                   "-kernel", "/usr/lib/modules/" + kernel + "/vmlinuz", "-initrd", str(initrd), "-append", cmdline,
                   "-drive", "file=" + str(disk) + ",format=raw,if=virtio"]
        with logfile.open("w") as log:
            subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=240, check=True)
        text = logfile.read_text()
        if "NOBARA_VM_BTRFS_ROOT_OK" not in text or "NOBARA_VM_BTRFS_ROOT_FAILED" in text:
            raise RuntimeError("The renamed-root VM failed; see " + str(logfile) + "\n" + text[-7000:])
