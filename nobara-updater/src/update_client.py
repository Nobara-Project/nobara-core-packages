"""CLI client; systemd owns preparation and installation lifetimes."""
import logging
import subprocess
import time

from .update_state import STATE_DIR, read_state, status_message


def prepare_update(logger: logging.Logger, *, codecs: bool = False) -> bool:
    state = read_state()
    if state.get("status") in {"ready", "scheduled"}:
        if not worker_action("refresh-pending", logger):
            return False
        state = read_state()
    if state.get("status") in {"ready", "scheduled", "awaiting-boot"}:
        if codecs and not state.get("codecs") and "enable-codecs" not in state.get("migrations", {}).get("hooks", []):
            logger.error("Finish or cancel the prepared update before preparing a codec installation.")
            return False
        return True
    service = "nobara-updater-prepare-codecs.service" if codecs else "nobara-updater-prepare.service"
    return run_service(service, logger, {"ready", "scheduled", "unchanged"})


def install_live_update(logger: logging.Logger) -> bool:
    return run_service("nobara-updater-live.service", logger, {"live-complete"})


def run_service(service: str, logger: logging.Logger, success_states: set[str]) -> bool:
    log_path = STATE_DIR / "update.log"
    position = log_path.stat().st_size if log_path.exists() else 0
    # Wait for the systemd job, not a racy snapshot of ActiveState. Losing
    # this client does not terminate the independently owned system service.
    process = subprocess.Popen(["systemctl", "start", service])
    while True:
        if log_path.exists():
            with log_path.open(errors="replace") as stream:
                if log_path.stat().st_size < position:
                    position = 0
                stream.seek(position)
                for line in stream.readlines():
                    logger.info("%s", line.rstrip())
                position = stream.tell()
        if process.poll() is not None:
            break
        time.sleep(0.5)
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
