import json
import logging
import multiprocessing
import os
import pwd
import sys
import subprocess
from pathlib import Path
from typing import Any

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SCRIPT_FILE = __file__

def run_as_user(
    uid: int, gid: int, func_name: str, option: str = "", *args: Any
) -> list[str] | None:
    # Determine the real directory of the main script
    script_dir = Path(SCRIPT_FILE).resolve().parent
    # Construct the path to run_as_user_target.py
    target_script = script_dir / "run_as_user_target.py"
    # Create a manager for shared queues
    manager = multiprocessing.Manager()
    log_queue = manager.Queue()
    update_queue = manager.Queue()

    # Serialize the queue data
    log_queue_data: list[Any] = []
    update_queue_data: list[Any] = []

    command = [
        sys.executable,
        target_script,
        str(uid),
        str(gid),
        func_name,
        json.dumps(log_queue_data),
        json.dumps(update_queue_data),
        str(option),
        *args,
    ]

    result = subprocess.run(command, capture_output=True, text=True)
    # Enable these for debugging run_as_user_target.py
    #print(result.stdout)
    #print(result.stderr)
    if result.returncode == 0 and result.stdout is not None:
        try:
            output = json.loads(result.stdout.strip())
            # Deserialize the queue data
            for item in output["log_queue"]:
                log_queue.put(item)
            for item in output["update_queue"]:
                update_queue.put(item)
            # Process logs
            while not log_queue.empty():
                log_message = log_queue.get()
                logger.info(log_message)
            return output["result"]
        except json.JSONDecodeError:
            logger.error("Failed to decode JSON output from subprocess")
            return None
    else:
        return None

