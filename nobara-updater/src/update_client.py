"""CLI client; systemd owns preparation and installation lifetimes."""
import logging
import os
import subprocess
import time
import uuid

from .update_state import STATE_DIR, UpdateError, atomic_json, package_selection, read_state, status_message
from .update_progress import EVENT_PREFIX, PackageProgress


def report_prepared(state):
    """Reconstruct the resolved rows if a frontend attached after the plan log."""
    if not state.get("job"):
        return
    reporter = PackageProgress(state["job"], state.get("packages", []))
    reporter.plan()
    for record in reporter.records:
        reporter.package(record, "downloaded", 1)


def prepare_update(logger: logging.Logger, *, codecs: bool = False, packages=None, progress=False) -> bool:
    packages = package_selection(packages)
    state = read_state()
    if state.get("status") in {"ready", "scheduled"}:
        if not worker_action("refresh-pending", logger):
            return False
        state = read_state()
    if state.get("status") in {"ready", "scheduled", "awaiting-boot"}:
        if packages != state.get("selection"):
            raise UpdateError("An update with a different package selection is already prepared. Finish it or run sudo nobara-sync cancel-update before selecting another update.")
        if codecs and not state.get("codecs") and "enable-codecs" not in state.get("migrations", {}).get("hooks", []):
            logger.error("Finish or cancel the prepared update before preparing a codec installation.")
            return False
        if progress:
            report_prepared(state)
        return True
    if packages:
        request = uuid.uuid4().hex
        path = STATE_DIR / "requests" / (request + ".json")
        atomic_json(path, {"packages": packages})
        # The instance identifies immutable root-owned input, preserved across
        # the worker's early self-upgrade and interpreter replacement.
        success = run_service("nobara-updater-prepare@" + request + ".service", logger,
                              {"ready", "scheduled", "unchanged"}, progress=progress)
        # Do not unlink on KeyboardInterrupt/disconnection: systemd still
        # owns preparation and may need the input after its early re-exec.
        if success:
            path.unlink(missing_ok=True)
            if progress:
                report_prepared(read_state())
        return success
    service = "nobara-updater-prepare-codecs.service" if codecs else "nobara-updater-prepare.service"
    success = run_service(service, logger, {"ready", "scheduled", "unchanged"}, **({"progress": True} if progress else {}))
    if success and progress:
        report_prepared(read_state())
    return success


def install_live_update(logger: logging.Logger, *, progress=False) -> bool:
    return run_service("nobara-updater-live.service", logger, {"live-complete"}, **({"progress": True} if progress else {}))


def run_service(service: str, logger: logging.Logger, success_states: set[str], *, progress=False) -> bool:
    log_path = STATE_DIR / "update.log"
    stream = log_path.open(errors="replace") if log_path.exists() else None
    if stream:
        stream.seek(0, 2)
    # Wait for the systemd job, not a racy snapshot of ActiveState. Losing
    # this client does not terminate the independently owned system service.
    try:
        process = subprocess.Popen(["systemctl", "start", service])
        while True:
            finished = process.poll() is not None
            # Retain the open inode across rotations so the tail of the old
            # log (possibly a final package event) is drained before the new one.
            while True:
                if stream is None:
                    try:
                        stream = log_path.open(errors="replace")
                    except FileNotFoundError:
                        break
                if os.fstat(stream.fileno()).st_size < stream.tell():
                    stream.seek(0)
                while True:
                    position = stream.tell()
                    line = stream.readline()
                    if not line:
                        break
                    if not line.endswith("\n"):
                        stream.seek(position)
                        break
                    if progress or EVENT_PREFIX not in line:
                        logger.info("%s", line.rstrip())
                try:
                    rotated = os.fstat(stream.fileno()).st_ino != log_path.stat().st_ino
                except FileNotFoundError:
                    break
                if not rotated:
                    break
                stream.close()
                stream = None
            if finished:
                break
            time.sleep(0.5)
    finally:
        if stream:
            stream.close()
    state = read_state()
    success = process.returncode == 0 and state.get("status") in success_states
    if not success:
        logger.error("%s failed: %s", service, status_message(state))
    return success


def worker_action(action: str, logger: logging.Logger) -> bool:
    result = subprocess.run(["/usr/libexec/nobara-update-worker", action], capture_output=True, text=True)
    if result.stdout:
        logger.info("%s", result.stdout.strip())
    if result.stderr:
        logger.error("%s", result.stderr.strip())
    return result.returncode == 0
