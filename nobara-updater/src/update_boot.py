"""Select and verify the kernel installed by an update on Nobara GRUB/BLS."""
from __future__ import annotations

from functools import cmp_to_key
import json
import logging
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile

from .update_state import UpdateError

LOG = logging.getLogger(__name__)
BOOT = Path("/boot")
GRUB_DEFAULTS = Path("/etc/default/grub")
MACHINE_ID = Path("/etc/machine-id")
KERNEL_CMDLINE = Path("/etc/kernel/cmdline")
INBOUND = {"Install", "Upgrade", "Downgrade", "Reinstall"}


def boot_space_requirements(packages, hooks, *, boot=BOOT) -> dict[str, int]:
    """Budget new images and one temporary rebuild, excluding generic rescue images."""
    mib = 1024**2
    incoming = [p for p in packages if p["action"] in INBOUND]
    kernels = sum(p["name"] == "kernel-core" or
                  (p["name"].startswith("kernel-") and p["name"].endswith("-core")
                   and "modules" not in p["name"]) for p in incoming)
    rebuild = bool(kernels) or any(any(word in p["name"] for word in ("dkms", "akmod", "kmod", "dracut"))
                                  for p in incoming) or any(h.startswith("plymouth-") for h in hooks)

    def largest(directory, pattern, default):
        return max((p.stat().st_size for p in directory.glob(pattern)
                    if "0-rescue" not in p.name and p.is_file()), default=default)

    initramfs = largest(boot, "initramfs-*.img", 128 * mib)
    kernel = largest(boot, "vmlinuz-*", 32 * mib)
    budgets = {str(boot): 32 * mib + kernels * (kernel + initramfs) + int(rebuild) * initramfs}
    for directory in (boot / "EFI/Linux", boot / "efi/EFI/Linux"):
        if any(directory.glob("*.efi")):
            mount = boot / "efi" if directory == boot / "efi/EFI/Linux" else boot
            budgets[str(mount)] = max(budgets.get(str(mount), 0),
                                     32 * mib + (kernels + int(rebuild)) * largest(directory, "*.efi", 160 * mib))
    if (boot / "efi").is_mount() and any(p["name"].startswith(("shim", "grub2-efi", "systemd-boot")) for p in incoming):
        budgets[str(boot / "efi")] = budgets.get(str(boot / "efi"), 0) + 32 * mib
    return budgets


def changes_kernel(packages: list[dict]) -> bool:
    return any(p["name"] in {"kernel", "kernel-core"} and p["action"] in INBOUND for p in packages)


def output(command: list[str]) -> str:
    LOG.info("Running: %s", " ".join(command))
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def check_selection_support() -> None:
    """Fail during preparation, before changing RPMs, on unsupported layouts."""
    config = BOOT / "grub2/grub.cfg"
    if not config.is_file() or not re.search(r"(?m)^\s*blscfg(?:\s|$)", config.read_text()):
        raise UpdateError("Automatic kernel selection requires Nobara's GRUB/BLS boot configuration.")
    if not GRUB_DEFAULTS.is_file():
        raise UpdateError("The GRUB default configuration is missing.")
    if not any(re.search(r"(?m)^linux\s+", path.read_text()) for path in (BOOT / "loader/entries").glob("*.conf")):
        raise UpdateError("Automatic kernel selection requires split kernel/initramfs BLS entries; this boot layout is unsupported.")
    for program in ("grub2-set-default", "grub2-editenv", "grub2-mkconfig", "grub2-script-check", "grub2-mkrelpath"):
        if shutil.which(program) is None:
            raise UpdateError(f"Kernel selection requires {program}.")


def newest_updated_kernel(packages: list[dict]) -> str | None:
    """Use RPM ordering, and don't switch an LTS update to retained mainline."""
    specs = sorted({p["nevra"] for p in packages if p["name"] in {"kernel", "kernel-core"} and p["action"] in INBOUND})
    if not specs:
        return None
    from libdnf5.rpm import rpmvercmp
    records = output(["rpm", "-q", "--qf", "%{EPOCHNUM}|%{VERSION}|%{RELEASE}|%{ARCH}\\n", *specs])
    kernels = set()
    for line in records.splitlines():
        parts = tuple(line.split("|"))
        if len(parts) != 4 or not parts[0].isdigit() or not all(re.fullmatch(r"[A-Za-z0-9_.+~-]+", part) for part in parts[1:]):
            raise UpdateError("Cannot identify the installed kernel version from RPM.")
        kernels.add(parts)
    if not kernels or len({p[3] for p in kernels}) != 1:
        raise UpdateError("Cannot identify one target kernel architecture.")

    def compare(left, right):
        for a, b in zip(left[:3], right[:3]):
            difference = rpmvercmp(a, b)
            if difference:
                return difference
        return 0

    _, version, release, arch = max(kernels, key=cmp_to_key(compare))
    return f"{version}-{release}.{arch}"


def kernel_entry(kernel: str) -> str:
    """Never choose rescue/snapshot entries or guess between ambiguous entries."""
    image = BOOT / ("vmlinuz-" + kernel)
    if not image.is_file():
        raise UpdateError(f"Kernel image is missing for default selection: {image}")
    image_paths = {"/" + image.name, "/boot/" + image.name}
    expanded_paths = False
    entries = []
    for path in (BOOT / "loader/entries").glob("*.conf"):
        if path.name.startswith(("nobara-recovery-", "nobara-restored-")) or "0-rescue" in path.name:
            continue
        fields = {}
        for line in path.read_text().splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2:
                fields.setdefault(parts[0], []).append(parts[1].strip())
        linux = fields.get("linux", [])
        if fields.get("version") != [kernel] or len(linux) != 1 or Path(linux[0]).name != image.name:
            continue
        if linux[0] not in image_paths and not expanded_paths:
            # kernel-install uses GRUB's filesystem-relative paths on Btrfs
            # roots without a separate /boot, including snapshot layouts.
            image_paths.add(output(["grub2-mkrelpath", str(image)]))
            image_paths.add(output(["grub2-mkrelpath", "-r", str(image)]))
            expanded_paths = True
        if linux[0] not in image_paths:
            continue
        if not re.fullmatch(r"[A-Za-z0-9_.+~-]+", path.stem):
            raise UpdateError("Unsupported boot entry identifier.")
        entries.append(path.stem)
    machine = MACHINE_ID.read_text().strip()
    canonical = machine + "-" + kernel
    if re.fullmatch(r"[a-fA-F0-9]{32}", machine) and canonical in entries:
        return canonical
    if len(entries) != 1:
        raise UpdateError(f"Cannot identify one normal boot entry for kernel {kernel}.")
    return entries[0]


def atomic_text(path: Path, text: str) -> None:
    path = path.resolve(strict=True)
    fd, temporary = tempfile.mkstemp(prefix=".nobara-", dir=path.parent)
    os.close(fd)
    try:
        shutil.copy2(path, temporary)
        with open(temporary, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.sync()
    finally:
        Path(temporary).unlink(missing_ok=True)


def uses_saved_default(text: str) -> bool:
    return bool(re.search(r'(?m)^\s*set\s+default=["\']?\$(?:\{saved_entry\}|saved_entry)["\']?\s*$', text))


def ensure_saved_default() -> None:
    """Normalize a static GRUB_DEFAULT pin; regenerate config atomically."""
    config = (BOOT / "grub2/grub.cfg").resolve(strict=True)
    old = GRUB_DEFAULTS.read_text()
    # Append the final assignment so earlier literal pins cannot override it.
    assignments = re.findall(r"(?m)^\s*(?:export\s+)?GRUB_DEFAULT\s*=([^\n]*)", old)
    declared_saved = bool(assignments and assignments[-1].strip() in {"saved", "'saved'", '"saved"'})
    if declared_saved and uses_saved_default(config.read_text()):
        return
    replacement = re.sub(r"(?m)^\s*(?:export\s+)?GRUB_DEFAULT\s*=[^\n]*\n?", "", old)
    replacement = replacement.rstrip() + "\nGRUB_DEFAULT=saved\n"
    fd, name = tempfile.mkstemp(prefix=".nobara-grub-", suffix=".cfg", dir=config.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        atomic_text(GRUB_DEFAULTS, replacement)
        output(["grub2-mkconfig", "--no-grubenv-update", "-o", str(temporary)])
        output(["grub2-script-check", str(temporary)])
        generated = temporary.read_text()
        if not uses_saved_default(generated) or not re.search(r"(?m)^\s*blscfg(?:\s|$)", generated):
            raise UpdateError("The generated GRUB configuration does not honor the saved kernel selection.")
        shutil.copystat(config, temporary)
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, config)
        os.sync()
    except Exception:
        atomic_text(GRUB_DEFAULTS, old)
        raise
    finally:
        temporary.unlink(missing_ok=True)
        Path(str(temporary) + ".new").unlink(missing_ok=True)


def pin_kernel(kernel: str) -> dict:
    check_selection_support()
    entry = kernel_entry(kernel)
    ensure_saved_default()
    output(["grub2-set-default", "--boot-directory=" + str(BOOT), entry])
    # Both one-shot selection and its saved-entry restoration can otherwise
    # override the pin. tmp_saved_entry is used by kernel-install's hooks.
    env_path = str(BOOT / "grub2/grubenv")
    output(["grub2-editenv", env_path, "unset", "next_entry", "prev_saved_entry", "tmp_saved_entry"])
    environment = dict(line.split("=", 1) for line in output(["grub2-editenv", env_path, "list"]).splitlines() if "=" in line)
    if environment.get("saved_entry") != entry or any(environment.get(key) for key in ("next_entry", "prev_saved_entry", "tmp_saved_entry")):
        raise UpdateError("GRUB did not retain the new default kernel selection.")
    os.sync()
    LOG.info("Pinned kernel %s as the default boot entry (%s).", kernel, entry)
    return {"kernel": kernel, "entry": entry, "loader": "grub"}


def replace_root_subvolume(options: str, subvolume: str) -> str:
    """Preserve encryption and other options; discard obsolete subvolume IDs."""
    tokens = shlex.split(options)
    flags = []
    kept = []
    for token in tokens:
        if token.startswith("rootflags="):
            flags.extend(flag for flag in token[10:].split(",") if flag and not flag.startswith(("subvol=", "subvolid=")))
        else:
            kept.append(token)
    kept.append("rootflags=" + ",".join(flags + ["subvol=" + subvolume]))
    return shlex.join(kept)


def write_boot_file(path: Path, *, text: str | None = None, source: Path | None = None) -> None:
    """Atomic boot-file replacement, including newly created canonical entries."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if text is not None and path.is_file() and path.read_text() == text:
        return
    fd, temporary = tempfile.mkstemp(prefix=".nobara-", dir=path.parent)
    os.close(fd)
    try:
        if source is not None:
            shutil.copy2(source, temporary)
        else:
            Path(temporary).write_text(text)
            os.chmod(temporary, 0o644)
        with open(temporary, "rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_DIRECTORY | os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def synchronize_boot_root(*, recovery_entry: str | None = None) -> str | None:
    """Make a recovered Btrfs root the normal system, without moving subvolumes.

    Recovery entries and their archived images stay immutable. On an actual
    rollback, restore the booted kernel/initramfs to normal paths from those
    known-good copies before creating a canonical entry. Later updates simply
    retain this root in BLS entries and kernel-install's persistent command line.
    This function does not change GRUB's default or disarm recovery.
    """
    root = json.loads(output(["findmnt", "--json", "--mountpoint", "/", "--output", "FSTYPE,FSROOT,UUID"]))["filesystems"][0]
    match = re.fullmatch(r"/\.nobara-updater/([a-f0-9]{32})/root", root.get("fsroot", ""))
    if root.get("fstype") != "btrfs" or not match:
        return None
    uuid = root.get("uuid", "")
    if not re.fullmatch(r"[a-fA-F0-9-]{36}", uuid):
        raise UpdateError("Cannot identify the recovered root filesystem for normal boot.")
    machine = MACHINE_ID.read_text().strip()
    kernel = os.uname().release
    if not re.fullmatch(r"[a-fA-F0-9]{32}", machine) or not re.fullmatch(r"[A-Za-z0-9_.+~-]+", kernel):
        raise UpdateError("Cannot identify a normal boot entry for the recovered system.")
    subvolume = root["fsroot"].lstrip("/")
    canonical = BOOT / "loader/entries" / (machine + "-" + kernel + ".conf")
    environment = dict(line.split("=", 1) for line in output(["grub2-editenv", str(BOOT / "grub2/grubenv"), "list"]).splitlines() if "=" in line)

    def expand(options):
        options = options.replace("$kernelopts", environment.get("kernelopts", ""))
        # TuneD's kernel-install hook appends $tuned_params; GRUB fills it in
        # at boot. Keep it as the last word (shlex.join would quote it).
        options, tuned = re.subn(r"(?<!\S)\$tuned_params(?!\S)", "", options)
        if "$" in options or [token for token in shlex.split(options) if token.startswith("root=")] != ["root=UUID=" + uuid]:
            raise UpdateError("The boot entry does not identify the recovered root filesystem unambiguously.")
        return replace_root_subvolume(options, subvolume) + (" $tuned_params" if tuned else "")

    # Construct and check the complete change before writing anything.
    images = []
    replacements = {}
    if recovery_entry:
        if recovery_entry != "nobara-recovery-" + match[1]:
            raise UpdateError("The recovery boot entry does not match the running root.")
        recovery_path = BOOT / "loader/entries" / (recovery_entry + ".conf")
        lines = recovery_path.read_text().splitlines()
        if not re.search(r"(?m)^version\s+" + re.escape(kernel) + r"\s*$", "\n".join(lines)):
            raise UpdateError("The recovery boot entry does not match the running kernel.")
        linux = re.findall(r"(?m)^linux\s+(\S+)\s*$", "\n".join(lines))
        if len(linux) != 1 or not any(line.startswith("initrd ") for line in lines):
            raise UpdateError("The recovery boot entry has incomplete boot images.")
        rewritten = []
        for line in lines:
            fields = line.split(None, 1)
            if len(fields) == 2 and fields[0] in {"linux", "initrd"}:
                paths = []
                for name in fields[1].split():
                    source = BOOT / name.lstrip("/")
                    archive = BOOT / "nobara-updater" / match[1]
                    if not source.resolve().is_relative_to(archive.resolve()) or not source.is_file():
                        raise UpdateError("A saved recovery boot image is missing or outside its archive.")
                    if fields[0] == "linux":
                        target = BOOT / ("vmlinuz-" + kernel)
                    elif source.name.endswith("initramfs-" + kernel + ".img"):
                        target = BOOT / ("initramfs-" + kernel + ".img")
                    else:
                        # Preserve auxiliary initrds such as CPU microcode.
                        paths.append(name)
                        continue
                    images.append((source, target))
                    paths.append("/" + target.name)
                line = fields[0] + " " + " ".join(paths)
            elif fields and fields[0] == "title":
                line = f"title Nobara Linux ({kernel})"
            rewritten.append(line)
        replacements[canonical] = "\n".join(rewritten) + "\n"
    elif not canonical.is_file():
        raise UpdateError("The recovered system has no normal boot entry for its running kernel.")

    for path in (BOOT / "loader/entries").glob(machine + "-*.conf"):
        replacements.setdefault(path, path.read_text())
    for path, text in list(replacements.items()):
        # Only this installation's entries on this filesystem. Independent
        # recovery entries, other installations, and memtest are untouched.
        options = re.findall(r"(?m)^options\s+(.+)$", text)
        if len(options) != 1 or ("root=UUID=" + uuid not in shlex.split(options[0]) and "$kernelopts" not in options[0]):
            if path == canonical:
                raise UpdateError("The normal boot entry points to a different root filesystem.")
            del replacements[path]
            continue
        replacements[path] = re.sub(r"(?m)^options\s+.+$", lambda _: "options " + expand(options[0]), text)
    options = re.search(r"(?m)^options\s+(.+)$", replacements[canonical])[1]
    cmdline = expand(KERNEL_CMDLINE.read_text().strip()) if KERNEL_CMDLINE.exists() else options
    defaults = GRUB_DEFAULTS.read_text()
    defaults = re.sub(r"rootflags=[^\s'\"]+", lambda found: replace_root_subvolume(found[0], subvolume), defaults)
    kernelopts = environment.get("kernelopts")
    if kernelopts:
        kernelopts = expand(kernelopts)
    for source, target in images:
        write_boot_file(target, source=source)
    write_boot_file(KERNEL_CMDLINE, text=cmdline + "\n")
    write_boot_file(GRUB_DEFAULTS, text=defaults)
    for path, text in replacements.items():
        write_boot_file(path, text=text)
    if kernelopts and kernelopts != environment.get("kernelopts"):
        output(["grub2-editenv", str(BOOT / "grub2/grubenv"), "set", "kernelopts=" + kernelopts])
    os.sync()
    LOG.info("Normal kernel entries now boot the active system at %s.", root["fsroot"])
    return canonical.stem
