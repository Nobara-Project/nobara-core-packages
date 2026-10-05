"""Best-effort Plymouth output for the offline worker, never desktop updates."""
from __future__ import annotations

import re
import shutil
import subprocess


class OfflineProgress:
    def __init__(self):
        self.enabled = False
        self.percent = -1
        self.last_message = None

    def send(self, *arguments: str) -> bool:
        if not self.enabled:
            return False
        try:
            result = subprocess.run(["plymouth", *arguments], timeout=2,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.enabled = result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            self.enabled = False
        return self.enabled

    def start(self, *, release_upgrade: bool = False, percent: int = 0) -> None:
        self.enabled = bool(shutil.which("plymouth"))
        self.percent = -1
        self.last_message = None
        # Boot progress is an estimate and can finish while RPM scripts are
        # still running. Update mode uses explicit transaction progress and
        # gives BGRT/Spinner a persistent title and power-off warning. Those
        # themes intentionally suppress display-message in update mode.
        self.send("change-mode", "--system-upgrade" if release_upgrade else "--updates")
        self.advance(percent)

    def advance(self, percent: int) -> None:
        percent = max(0, min(100, percent))
        if self.enabled and percent > self.percent:
            self.send("system-update", "--progress=" + str(percent))
            self.percent = percent

    def recovery(self) -> None:
        # Recovery runs in a separate service/interpreter. Leave the update
        # theme (which suppresses messages) so its failure notice is visible.
        self.enabled = bool(shutil.which("plymouth"))
        self.last_message = None
        self.send("change-mode", "--reboot")

    def message(self, text: str, *, percent: int | None = None) -> None:
        if percent is not None:
            self.advance(percent)
        if not self.enabled or text == self.last_message:
            return
        if self.last_message:
            self.send("hide-message", "--text=" + self.last_message)
        self.send("display-message", "--text=" + text)
        self.last_message = text

    def transaction_line(self, line: str) -> None:
        if not self.enabled:
            return
        # DNF5's C-locale, non-terminal output reports completed transaction
        # items as [current/total] ... | 100% | ... . Never treat a scriptlet's
        # own percentage or an incomplete item as overall completion. If this
        # presentation changes, stage messages/progress still work.
        line = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", line)
        match = re.match(r"^\s*\[\s*(\d{1,9})/\s*(\d{1,9})\]\s+[^|]+\|\s*100%\s*\|", line)
        if match:
            completed, total = map(int, match.groups())
            if 0 < completed <= total:
                # Stage weights, not a time estimate. Reserve the end for
                # module builds, boot files, dependency checks and boot setup.
                self.advance(5 + 80 * completed // total)
