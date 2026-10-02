"""Moving over from the names this program had before (eufy-sync, then Health Sync for Garmin).

Those versions kept everything in ~/.garmin-sync (shared with the original one-person
eufy-sync) and the saved passwords under the keychain service "eufy-garmin-sync".
HomeVitals has ~/.homevitals and its own "homevitals" entry. On the first start the
old folder is copied and the password vault copied, then both old copies are left
alone as a backup (and so the original eufy-sync keeps working if it is installed).

Runs first thing in both the window and the command line, before anything reads the
settings or creates the new folder. Never raises: a failure is logged and the
program starts as a fresh install would.
"""
from __future__ import annotations

import logging
import shutil

logger = logging.getLogger(__name__)

OLD_SERVICE_NAME = "eufy-garmin-sync"
# Not worth carrying over: a lock held by a run of the old version, and half-written temp files.
_SKIP_NAMES = {"sync.lock", "__pycache__"}


def _skip(_folder: str, names: list[str]) -> set[str]:
    return {n for n in names if n in _SKIP_NAMES or n.endswith(".tmp")}


def move_from_old_names() -> list[str]:
    """Copy the old settings folder and password vault over, once. Returns what was copied."""
    from homevitals import credentials
    from homevitals.cli import shared

    moved: list[str] = []
    old, new = shared.OLD_DATA_DIR, shared.DATA_DIR
    try:
        if old.is_dir() and not new.exists():
            shutil.copytree(old, new, ignore=_skip)
            moved.append(f"settings, sync history and log copied from {old} to {new}")
    except FileExistsError:
        pass        # another start (the window and a scheduled run together) got there first
    except Exception:
        logger.exception("Could not copy the settings from %s", old)
    try:
        if credentials.copy_vault_from_service(OLD_SERVICE_NAME):
            moved.append("saved passwords copied to the HomeVitals entry in the system keychain")
    except Exception:
        logger.exception("Could not copy the saved passwords from the old keychain entry")
    for line in moved:
        logger.info("Moved to the HomeVitals name: %s", line)
    return moved
