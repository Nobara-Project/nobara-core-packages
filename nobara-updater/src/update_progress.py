"""Package progress events and best-effort Plymouth output for offline updates."""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess

EVENT_PREFIX = "NOBARA_UPDATE_PROGRESS "


def emit_progress(event: str, **fields) -> None:
    """Versioned service-to-client events; human CLI output filters these."""
    logging.getLogger(__name__).info("%s%s", EVENT_PREFIX,
                                     json.dumps(dict(version=1, event=event, **fields), separators=(",", ":")))


class PackageProgress:
    def __init__(self, transaction: str, records: list[dict], *, stage: str = "main"):
        self.transaction = transaction
        self.records = [dict(p, id=p["action"] + ":" + p["nevra"]) for p in records
                        if p["action"] not in {"Replaced", "Reason Change"}]
        self.stage = stage

    def plan(self):
        emit_progress("plan", transaction=self.transaction, stage=self.stage, packages=self.records)

    def package(self, record, phase, fraction=0):
        if record:
            emit_progress("package", transaction=self.transaction, id=record["id"],
                          phase=phase, fraction=max(0, min(1, fraction)))

    def match(self, description, *, action=None):
        description = description.removesuffix(".rpm")
        matches = []
        for record in self.records:
            if action and record["action"] != action:
                continue
            variants = {record["nevra"], record["nevra"].replace("-0:", "-"),
                        record["name"] + "." + record["arch"]}
            if description in variants or ((description.endswith("...") or description.endswith("…"))
                    and any(v.startswith(description.rstrip(".…")) for v in variants)):
                matches.append(record)
        # Never guess which architecture/version an ambiguous truncated line describes.
        return matches[0] if len(matches) == 1 else None

    def transaction_line(self, line):
        line = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", line)
        match = re.match(r"^\s*\[\s*(\d{1,9})/\s*(\d{1,9})\]\s+(\w+)\s+(.+?)\s+(?:\|\s*)?(\d{1,3})%\s*\|", line)
        if not match:
            return
        completed, total, verb, description, percent = match.groups()
        if not 0 < int(completed) <= int(total) or int(percent) > 100:
            return
        action = {"Installing": "Install", "Upgrading": "Upgrade", "Downgrading": "Downgrade",
                  "Reinstalling": "Reinstall", "Erasing": "Remove", "Removing": "Remove"}.get(verb)
        if action:
            self.package(self.match(description, action=action), "applying", int(percent) / 100)


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
        match = re.match(r"^\s*\[\s*(\d{1,9})/\s*(\d{1,9})\]\s+.+?\s+(?:\|\s*)?100%\s*\|", line)
        if match:
            completed, total = map(int, match.groups())
            if 0 < completed <= total:
                # Stage weights, not a time estimate. Reserve the end for
                # module builds, boot files, dependency checks and boot setup.
                self.advance(5 + 80 * completed // total)
