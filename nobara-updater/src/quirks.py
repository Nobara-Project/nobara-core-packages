"""Compatibility interface for read-only fixup discovery.

Package mutations now belong to update_migrations.py and the offline worker.
Opening the updater or checking for fixups must never install/remove RPMs.
"""
import logging

from nobara_updater.update_state import read_state


class QuirkFixup:
    def __init__(self, logger=None):
        self.logger = logger or logging.getLogger(__name__)

    def system_quirk_fixup(self):
        self.logger.info("Package migrations will be resolved with the complete offline update.")
        state = read_state()
        return (0, int(state.get("status") in {"ready", "scheduled"}), 0, 0)
