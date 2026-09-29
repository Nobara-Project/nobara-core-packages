#!/bin/bash
check() { require_binaries lvm findmnt || return 1; return 255; }
depends() { echo 'lvm crypt systemd-cryptsetup rootfs-block bash dracut-systemd'; }
installkernel() { hostonly='' instmods dm-snapshot; }
install() {
    inst_multiple lvm findmnt sleep sync mkdir mv xargs mount
    inst_simple "$moddir/rollback.sh" /sbin/nobara-lvm-rollback
    inst_simple "$moddir/nobara-lvm-rollback.service" "$systemdsystemunitdir/nobara-lvm-rollback.service"
    mkdir -p "$initdir$systemdsystemunitdir/initrd-root-fs.target.requires"
    ln -s ../nobara-lvm-rollback.service "$initdir$systemdsystemunitdir/initrd-root-fs.target.requires/nobara-lvm-rollback.service"
    for unit in sysroot.mount systemd-fsck-root.service; do
        mkdir -p "$initdir$systemdsystemunitdir/$unit.d"
        printf '[Unit]\nRequires=nobara-lvm-rollback.service\nAfter=nobara-lvm-rollback.service\n' \
            >"$initdir$systemdsystemunitdir/$unit.d/nobara-rollback.conf"
    done
    inst_simple "$moddir/mark-recovery.sh" /sbin/nobara-mark-restored
    inst_simple "$moddir/nobara-mark-restored.service" "$systemdsystemunitdir/nobara-mark-restored.service"
    mkdir -p "$initdir$systemdsystemunitdir/initrd-switch-root.target.requires"
    ln -s ../nobara-mark-restored.service "$initdir$systemdsystemunitdir/initrd-switch-root.target.requires/nobara-mark-restored.service"
    mkdir -p "$initdir$systemdsystemunitdir/initrd-switch-root.service.d"
    printf '[Unit]\nRequires=nobara-mark-restored.service\nAfter=nobara-mark-restored.service\n' \
        >"$initdir$systemdsystemunitdir/initrd-switch-root.service.d/nobara-rollback.conf"
}
