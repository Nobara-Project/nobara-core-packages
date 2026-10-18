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
MODULES = Path("/usr/lib/modules")
GRUB_INSTALL = Path("/usr/lib/kernel/install.d/20-grub.install")
GRUB_INSTALL_OVERRIDE = Path("/etc/kernel/install.d/20-grub.install")
INBOUND = {"Install", "Upgrade", "Downgrade", "Reinstall"}


def installed_boot_kernels(root: Path = Path("/")) -> set[str]:
    """Ignore leftover module directories which have no kernel image to boot."""
    modules = root / "usr/lib/modules"
    if not modules.is_dir():
        return set()
    return {p.name for p in modules.iterdir() if p.is_dir()
            and ((p / "vmlinuz").is_file() or (root / "boot" / ("vmlinuz-" + p.name)).is_file())}


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


def kernel_install_diagnostics(kernel: str, image: Path) -> dict:
    """Log layout/plugin selection without publishing the plugin environment."""
    try:
        result = subprocess.run(["kernel-install", "--json=short", "inspect", kernel, str(image)],
                                check=True, capture_output=True, text=True, timeout=15)
        details = json.loads(result.stdout)
        if not isinstance(details, dict):
            raise ValueError("Invalid kernel-install inspection")
        LOG.info("kernel-install configuration for %s: %s", kernel, json.dumps({
            key: details[key] for key in ("Layout", "BootRoot", "EntryDirectory", "KernelImageType", "Plugins")
            if key in details}))
        return details
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        LOG.warning("Could not inspect kernel-install configuration for %s.", kernel)
        return {}


def ensure_grub_kernel(kernel: str, run) -> str:
    """Repair missing split images/entries on our already-supported GRUB layout.

    kernel-install's 'bls' layout is systemd's token/version/linux layout, not
    Fedora GRUB's vmlinuz-version layout. A stale token directory can select it
    automatically and make 20-grub.install silently skip installation. Retry
    the normal plugin chain in /boot unless it selects a different layout,
    then use the distribution's GRUB hook if necessary. Never infer UKI from
    a missing /boot/vmlinuz file or change persistent kernel-install settings.
    """
    image = BOOT / ("vmlinuz-" + kernel)

    def selected_entry():
        entry = kernel_entry(kernel)
        path = BOOT / "loader/entries" / (entry + ".conf")
        if path.is_symlink() or image.is_symlink() or image.stat().st_size == 0:
            raise UpdateError(f"Kernel boot files are empty or symlinked for {kernel}.")
        initrds = re.findall(r"(?m)^initrd\s+(.+)$", path.read_text())
        if not any(Path(word).name == "initramfs-" + kernel + ".img" for line in initrds for word in line.split()):
            raise UpdateError(f"The GRUB entry for {kernel} is missing its split initramfs reference.")
        return entry

    try:
        return selected_entry()
    except UpdateError as error:
        LOG.warning("Repairing GRUB boot files for %s: %s", kernel, error)
    check_selection_support()
    machine = MACHINE_ID.read_text().strip()
    if not re.fullmatch(r"[a-fA-F0-9]{32}", machine):
        raise UpdateError("Cannot repair GRUB boot files without a valid machine ID.")
    canonical = BOOT / "loader/entries" / (machine + "-" + kernel + ".conf")
    if canonical.is_symlink() or image.is_symlink():
        raise UpdateError("Cannot repair symlinked kernel boot files.")
    source = MODULES / kernel / "vmlinuz"
    if not source.is_file():
        source = image
    if not source.is_file() or source.stat().st_size == 0:
        raise UpdateError(f"The packaged kernel image for {kernel} is missing or empty.")
    details = kernel_install_diagnostics(kernel, source)
    if details.get("KernelImageType") == "uki":
        raise UpdateError(f"The packaged image for {kernel} is a UKI; automatic selection requires a split GRUB kernel.")
    if details.get("Layout") not in {"bls", "uki"}:
        run(["kernel-install", "--boot-path=" + str(BOOT), "add", kernel, str(source)])
        try:
            return selected_entry()
        except UpdateError:
            pass

    # Do not bypass an administrator's disabled/replaced GRUB plugin. Its
    # normal invocation during RPM installation already had the opportunity
    # to handle the boot. Don't create another unused generic BLS/UKI tree.
    if (GRUB_INSTALL_OVERRIDE.exists() or GRUB_INSTALL_OVERRIDE.is_symlink()
            or not GRUB_INSTALL.is_file() or not os.access(GRUB_INSTALL, os.X_OK)):
        raise UpdateError(f"kernel-install did not create GRUB boot files for {kernel}. "
                          "Restore the distribution's 20-grub.install hook; it is missing, disabled, or overridden.")
    # TuneD may have already extended the entry made by 90-loaderentry.install.
    # Preserve its late-bound settings when the GRUB hook recreates that entry.
    old = canonical.read_text() if canonical.is_file() else ""
    if not old:
        running = BOOT / "loader/entries" / (machine + "-" + os.uname().release + ".conf")
        if running.is_file() and not running.is_symlink():
            old = running.read_text()
    tuned = {key: variable for key, variable in (("options", "$tuned_params"), ("initrd", "$tuned_initrd"))
             if any(variable in line.split()[1:] for line in old.splitlines() if line.startswith(key + " "))}
    LOG.warning("Recreating the split kernel and BLS entry with the distribution GRUB hook for %s.", kernel)
    with tempfile.TemporaryDirectory(prefix="nobara-grub-install-") as directory:
        # Fedora's hook skips existing entry directories regardless of layout.
        # Supply an absent directory in a private temporary parent; do not
        # delete or repurpose any existing systemd-boot/kernel-install tree.
        run(["env", "KERNEL_INSTALL_MACHINE_ID=" + machine, "KERNEL_INSTALL_LAYOUT=other",
             "KERNEL_INSTALL_BOOT_ROOT=" + str(BOOT), "KERNEL_INSTALL_ENTRY_TOKEN=" + machine,
             str(GRUB_INSTALL), "add", kernel, str(Path(directory) / "entry"), str(source)])
    try:
        entry = selected_entry()
    except UpdateError as error:
        raise UpdateError(f"Could not restore GRUB boot files for {kernel}: {error}") from error
    if tuned and entry == canonical.stem:
        lines = canonical.read_text().splitlines()
        for index, line in enumerate(lines):
            fields = line.split()
            if fields and fields[0] in tuned and tuned[fields[0]] not in fields[1:]:
                lines[index] += " " + tuned[fields[0]]
        atomic_text(canonical, "\n".join(lines) + "\n")
    return entry


def validate_grub_initramfs(kernel: str, run) -> None:
    """Require the selected entry to use the rebuilt initramfs, plus valid extras."""
    from .update_recovery import expand_bls, grub_variables
    entry = BOOT / "loader/entries" / (kernel_entry(kernel) + ".conf")
    image = BOOT / ("initramfs-" + kernel + ".img")
    fields = re.findall(r"(?m)^initrd\s+(.+)$", entry.read_text())
    variables = grub_variables() if any("$" in field for field in fields) else {}
    names = [name for field in fields for name in expand_bls("initrd", field, variables)]
    aliases = {"/" + image.name, "/boot/" + image.name}
    if not aliases.intersection(names):
        aliases.update((output(["grub2-mkrelpath", str(image)]), output(["grub2-mkrelpath", "-r", str(image)])))
    if not aliases.intersection(names):
        raise UpdateError(f"The GRUB entry for {kernel} does not reference its rebuilt initramfs.")
    for name in names:
        if name in aliases:
            payload = image
        else:
            payload = BOOT / name.lstrip("/")
            if not payload.is_file():
                # Without a separate /boot, GRUB paths include /boot and
                # possibly the Btrfs subvolume; resolve them relative to BOOT.
                for command in (["grub2-mkrelpath", str(BOOT)], ["grub2-mkrelpath", "-r", str(BOOT)]):
                    prefix = output(command).rstrip("/") + "/"
                    if name.startswith(prefix):
                        payload = BOOT / name[len(prefix):]
                        break
        if not payload.resolve().is_relative_to(BOOT.resolve()) or not payload.is_file() or payload.stat().st_size == 0:
            raise UpdateError(f"The GRUB initramfs image is missing, empty, or outside /boot: {name}")
        # The main image was verified immediately before its atomic rename.
        # Only auxiliary images (e.g. TuneD/microcode) still need inspection.
        if payload != image:
            run(["lsinitrd", str(payload)])


def validate_running_entry(kernel: str, entry: str) -> None:
    """Read-only checks before manually confirming an interrupted trial boot."""
    from .update_recovery import boot_payloads, expand_bls, grub_variables
    if os.uname().release != kernel or kernel_entry(kernel) != entry:
        raise UpdateError("Boot the updated kernel's normal entry before confirming this update.")
    modules = MODULES / kernel
    if not (modules / "kernel").is_dir() or not (modules / "modules.dep").is_file():
        raise UpdateError("The updated kernel's complete module tree is missing.")
    path = BOOT / "loader/entries" / (entry + ".conf")
    if path.is_symlink():
        raise UpdateError("The updated boot entry is a symlink.")
    options = re.findall(r"(?m)^options\s+(.+)$", path.read_text())
    if len(options) != 1:
        raise UpdateError("The updated boot entry has ambiguous root options.")
    tokens = shlex.split(" ".join(expand_bls("options", options[0], grub_variables())))
    current = json.loads(output(["findmnt", "--json", "--mountpoint", "/",
                                 "--output", "FSTYPE,UUID,FSROOT,SOURCE"]))["filesystems"][0]
    roots = [token[5:] for token in tokens if token.startswith("root=")]
    matches = len(roots) == 1 and bool(current.get("uuid")) and roots[0] == "UUID=" + current["uuid"]
    if len(roots) == 1 and roots[0].startswith("/dev/") and current.get("source", "").startswith("/dev/"):
        matches = os.path.realpath(roots[0]) == os.path.realpath(current["source"].split("[", 1)[0])
    if current.get("fstype") == "btrfs":
        flags = [flag for token in tokens if token.startswith("rootflags=") for flag in token[10:].split(",")]
        matches = (matches and [flag[7:].strip("/") for flag in flags if flag.startswith("subvol=")]
                   == [current["fsroot"].strip("/")] and not any(flag.startswith("subvolid=") for flag in flags))
    if not matches or any(token.startswith("nobara.rollback=") for token in tokens):
        raise UpdateError("The updated boot entry does not select the running original root.")
    payloads = boot_payloads(path)
    for index, payload in enumerate(payloads):
        if not payload.is_file() or payload.stat().st_size == 0:
            raise UpdateError(f"Updated boot image is missing or empty: {payload}")
        if index:
            subprocess.run(["lsinitrd", str(payload)], check=True, stdout=subprocess.DEVNULL)


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
        archive = BOOT / "nobara-updater" / match[1]
        # The recovery entry holds TuneD's values resolved at snapshot time; the
        # normal entry gets the original options back, with $tuned_params.
        record = archive / "bls-source.json"
        source_options = json.loads(record.read_text()).get("options") if record.is_file() else None
        if source_options is not None and (not isinstance(source_options, str) or "\n" in source_options):
            raise UpdateError("The recovery archive's boot entry record is invalid.")
        rewritten = []
        for line in lines:
            fields = line.split(None, 1)
            if len(fields) == 2 and fields[0] == "options" and source_options:
                line = "options " + source_options
            elif len(fields) == 2 and fields[0] in {"linux", "initrd"}:
                paths = []
                for name in fields[1].split():
                    source = BOOT / name.lstrip("/")
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


def reconcile_recovered_kernels(job: str, selected: str) -> None:
    """Hide this root's unbootable normal entries before retiring recovery.

    /boot is shared across snapshots. Keep saved images and other installations
    intact, but don't offer a kernel installed only in the abandoned root.
    """
    if not re.fullmatch(r"[a-f0-9]{32}", job) or not re.fullmatch(r"[A-Za-z0-9_.+~-]+", selected):
        raise UpdateError("Invalid recovered boot selection.")
    machine = MACHINE_ID.read_text().strip()
    if not re.fullmatch(r"[a-fA-F0-9]{32}", machine):
        raise UpdateError("Cannot identify the recovered installation's boot entries.")
    kernel = os.uname().release
    if not re.fullmatch(r"[A-Za-z0-9_.+~-]+", kernel):
        raise UpdateError("Invalid running kernel version.")

    def has_modules(version):
        # A leftover directory containing only external DKMS/akmods modules is
        # not the complete in-tree module set of the restored kernel.
        directory = MODULES / version
        return directory.is_dir() and (directory / "kernel").is_dir() and (directory / "modules.dep").is_file()

    if not has_modules(kernel):
        raise UpdateError(f"The recovered root has no complete module tree for running kernel {kernel}. "
                          "Boot its saved previous-system kernel before confirming recovery.")
    entries = BOOT / "loader/entries"
    selected_path = entries / (selected + ".conf")
    if (selected_path.is_symlink() or re.findall(r"(?m)^version\s+(.+)$", selected_path.read_text()) != [kernel]):
        raise UpdateError("The recovered boot selection does not match the running kernel.")
    current = json.loads(output(["findmnt", "--json", "--mountpoint", "/",
                                 "--output", "FSTYPE,UUID,FSROOT,SOURCE"]))["filesystems"][0]
    if not current.get("uuid"):
        raise UpdateError("Cannot identify the recovered filesystem for boot-entry cleanup.")
    environment = dict(line.split("=", 1) for line in output(
        ["grub2-editenv", str(BOOT / "grub2/grubenv"), "list"]).splitlines() if "=" in line)
    retired = []
    for path in sorted(entries.glob(machine + "-*.conf")):
        if path == selected_path or "0-rescue" in path.name:
            continue
        if path.is_symlink():
            raise UpdateError("A normal kernel boot entry is a symlink; cannot safely reconcile recovery.")
        text = path.read_text()
        versions = re.findall(r"(?m)^version\s+(.+)$", text)
        options = re.findall(r"(?m)^options\s+(.+)$", text)
        if len(versions) != 1 or not re.fullmatch(r"[A-Za-z0-9_.+~-]+", versions[0]) or len(options) != 1:
            continue
        version = versions[0]
        if has_modules(version):
            continue
        expanded = options[0].replace("$kernelopts", environment.get("kernelopts", ""))
        expanded = re.sub(r"(?<!\S)\$tuned_params(?!\S)", "", expanded)
        if "$" in expanded:
            raise UpdateError(f"Cannot identify the root selected by {path.name}.")
        tokens = shlex.split(expanded)
        roots = [token[5:] for token in tokens if token.startswith("root=")]
        if len(roots) != 1:
            continue
        same_root = roots[0] == "UUID=" + current["uuid"]
        if roots[0].startswith("/dev/") and current.get("source", "").startswith("/dev/"):
            same_root = os.path.realpath(roots[0]) == os.path.realpath(current["source"].split("[", 1)[0])
        if not same_root:
            continue
        if current["fstype"] == "btrfs":
            flags = [flag for token in tokens if token.startswith("rootflags=") for flag in token[10:].split(",")]
            paths = [flag[7:].strip("/") for flag in flags if flag.startswith("subvol=")]
            if paths != [current["fsroot"].strip("/")] or any(flag.startswith("subvolid=") for flag in flags):
                continue  # Another snapshot may still contain that kernel.
        retired.append((path, text))
    archive = BOOT / "nobara-updater" / job / "disabled-entries"
    if any(path.is_symlink() for path in (entries.parent, entries, archive.parent.parent, archive.parent, archive)):
        raise UpdateError("Recovery boot storage contains a symlink.")
    for path, text in retired:
        saved = archive / path.name
        if saved.is_symlink() or (saved.exists() and saved.read_text() != text):
            raise UpdateError("A disabled boot entry already exists with different contents; preserve it for support.")
    for path, text in retired:
        # A durable copy precedes removal from BLS. Keep images for diagnosis
        # and other roots; never delete RPM-owned kernel files during rollback.
        write_boot_file(archive / path.name, text=text)
        path.unlink()
        LOG.warning("Disabled boot entry %s: kernel %s has no module tree in the recovered root.",
                    path.name, re.findall(r"(?m)^version\s+(.+)$", text)[0])
    os.sync()
