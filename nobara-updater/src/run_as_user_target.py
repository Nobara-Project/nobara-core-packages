import json
import logging
import multiprocessing
import os
import pwd
import sys
from pathlib import Path
from typing import Any

import nobara_updater.shared_functions as shared_functions  # type: ignore[import]

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def run_as_user_target(
    uid: int,
    gid: int,
    func_name: str,
    log_queue_data: list[Any],
    update_queue_data: list[Any],
    option: str = "",
    *args: Any,
) -> None:
    os.setgid(gid)
    os.setuid(uid)

    # Get the user's home directory and other details
    pw_record = pwd.getpwuid(uid)
    user_home = Path(pw_record.pw_dir)

    # Update environment variables
    os.environ["HOME"] = str(user_home)
    os.environ["USER"] = pw_record.pw_name
    os.environ["LOGNAME"] = pw_record.pw_name
    os.environ["SHELL"] = pw_record.pw_shell
    os.environ["XDG_CACHE_HOME"] = str(user_home / ".cache")
    os.environ["XDG_CONFIG_HOME"] = str(user_home / ".config")
    os.environ["XDG_DATA_HOME"] = str(user_home / ".local" / "share")
    os.environ["XDG_RUNTIME_DIR"] = f"/run/user/{uid}"

    runtime_dir = Path(os.environ["XDG_RUNTIME_DIR"])
    if not runtime_dir.exists():
        runtime_dir.mkdir(parents=True)
        os.chown(runtime_dir, uid, gid)

    # Change working directory to the user's home directory
    os.chdir(user_home)

    # Create the queues from the passed data
    manager = multiprocessing.Manager()
    log_queue = manager.Queue()
    update_queue = manager.Queue()

    for item in log_queue_data:
        log_queue.put(item)

    for item in update_queue_data:
        update_queue.put(item)

    # Import the function dynamically from shared_functions
    func = getattr(shared_functions, func_name)
    result = func(uid, gid, log_queue, update_queue, option, *args)

    # Collect the queue data to return
    log_queue_data = []
    while not log_queue.empty():
        log_queue_data.append(log_queue.get())

    update_queue_data = []
    while not update_queue.empty():
        update_queue_data.append(update_queue.get())

    sys.stdout.write(
        json.dumps(
            {
                "result": result,
                "log_queue": log_queue_data,
                "update_queue": update_queue_data,
            }
        )
        + "\n"
    )


if __name__ == "__main__":
    uid = int(sys.argv[1])
    gid = int(sys.argv[2])
    func_name = sys.argv[3]
    log_queue_data = json.loads(sys.argv[4])
    update_queue_data = json.loads(sys.argv[5])
    option = str(sys.argv[6])
    args = sys.argv[7:]
    run_as_user_target(uid, gid, func_name, log_queue_data, update_queue_data, option, *args)
