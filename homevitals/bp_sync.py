"""The Omron -> Garmin blood pressure step of a sync run.

Runs after the scale step, only for a person with an omron section, and fails
on its own: a problem here never stops the scale sync, and the other way
round (cli/app.py gives each step its own try/except).

Health data rules: values go to Garmin exactly as OMRON reported them; a
reading Garmin would refuse is skipped and counted, never rounded or
clamped. Logs and results carry counts only, never a reading's values.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TypeVar

from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from homevitals import state as state_module
from homevitals.config import UserConfig
from homevitals.garmin_client import GarminClient
from homevitals.network import is_transient_network_error
from homevitals.omron_client import BloodPressureReading, OmronClient, OmronError
from homevitals.state import SyncState
from homevitals.sync import MAX_RETRIES, RETRY_BASE_DELAY, PermanentSyncError, _is_permanent

logger = logging.getLogger(__name__)

T = TypeVar("T")

BP_TARGET = state_module.BP_TARGET
FIRST_SYNC_BACKFILL_DAYS = 30
REFETCH_OVERLAP_DAYS = 7
GARMIN_SAME_TIME_TOLERANCE_SECONDS = 2
# Mirrors garminconnect 0.3.17 set_blood_pressure's own range checks.
GARMIN_BP_LIMITS = {"systolic": (70, 260), "diastolic": (40, 150), "pulse": (20, 250)}
NOTES_DEVICE = "Omron M7"
SKIPPED_OUT_OF_RANGE_RESPONSE = '{"skipped": "outside_garmin_range"}'
# Garmin refused this one reading (a 4xx that is not about the login or the rate). Sending
# the same values again gets the same answer, so it is skipped like an out-of-range one
# rather than holding every newer reading back on each run.
SKIPPED_REJECTED_RESPONSE = '{"skipped": "rejected_by_garmin"}'
# 4xx answers that are about the session, the network or the rate, not the reading.
_NOT_ABOUT_THE_READING = (401, 403, 408, 429)


@dataclass
class BpSyncResult:
    """Counts for one person's run. Holds no readings, so its repr is safe to log."""

    fetched: int = 0
    uploaded: int = 0                # in a dry run: would have uploaded
    skipped_synced: int = 0
    skipped_in_garmin: int = 0
    skipped_out_of_range: int = 0
    skipped_rejected: int = 0        # Garmin refused the reading itself (HTTP 4xx)
    error: str | None = None         # plain text (safe_error_text) when an upload failed


def _in_range(value, limits: tuple[int, int]) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and limits[0] <= value <= limits[1]


def garmin_accepts(reading: BloodPressureReading) -> bool:
    return (_in_range(reading.systolic, GARMIN_BP_LIMITS["systolic"])
            and _in_range(reading.diastolic, GARMIN_BP_LIMITS["diastolic"])
            and (reading.pulse is None or _in_range(reading.pulse, GARMIN_BP_LIMITS["pulse"])))


def notes_for(reading: BloodPressureReading) -> str:
    notes = NOTES_DEVICE
    if reading.irregular_heartbeat:
        notes += "; irregular heartbeat detected"
    if reading.body_movement:
        notes += "; body movement detected"
    return notes


def window_start(state: SyncState, user_name: str, backfill_days: int | None, now: datetime) -> datetime:
    """How far back to ask OMRON for readings.

    The first time, 30 days. After that, a week before the newest reading
    already synced: a reading taken on Monday but only moved to the phone on
    Thursday must not hide behind Tuesday's. The state database removes the overlap.
    """
    if backfill_days:
        return now - timedelta(days=backfill_days)
    latest = state.get_latest_sync_timestamp(user_name, BP_TARGET)
    if latest is None:
        logger.info("No prior blood pressure syncs for %s, backfilling %d days", user_name, FIRST_SYNC_BACKFILL_DAYS)
        return now - timedelta(days=FIRST_SYNC_BACKFILL_DAYS)
    return datetime.fromtimestamp(latest, timezone.utc) - timedelta(days=REFETCH_OVERLAP_DAYS)


def safe_error_text(exc: BaseException) -> str:
    """A failure as text that never carries a reading's values or a server's reply."""
    if isinstance(exc, (PermanentSyncError, OmronError)):
        return str(exc)            # our own messages: hosts, statuses and commands only
    if isinstance(exc, GarminConnectTooManyRequestsError):
        return "Garmin is asking us to slow down (HTTP 429). Wait an hour and sync again."
    if isinstance(exc, GarminConnectAuthenticationError):
        return "Garmin session expired. Run: homevitals --reauth garmin"
    if isinstance(exc, GarminConnectConnectionError):
        # Garmin appends the server's detail after " - "; it can echo the values we sent.
        return f"Garmin upload failed ({str(exc).split(' - ', 1)[0]})"
    if is_transient_network_error(str(exc)):
        return str(exc)
    if isinstance(exc, ValueError):
        text = str(exc)
        # Our own setup errors keep their wording; anything else came from the Garmin library's range check.
        if "OMRON connect accounts created in" in text or "timezone-aware" in text:
            return text
        return "Garmin rejected a value (outside its accepted range)."
    return f"Unexpected error ({type(exc).__name__}). Details are in the log file."


def rejected_status(exc: BaseException) -> int | None:
    """The HTTP status when Garmin refused this one reading, else None."""
    if not isinstance(exc, GarminConnectConnectionError) or isinstance(exc, GarminConnectTooManyRequestsError):
        return None
    from homevitals.garmin_client import _status_code
    status = _status_code(exc)
    if status is None or not 400 <= status < 500 or status in _NOT_ABOUT_THE_READING:
        return None
    return status


def _retry_quietly(fn: Callable[[], T], description: str) -> T:
    """sync._retry, but its log lines carry safe_error_text instead of the raw error."""
    for attempt in range(MAX_RETRIES):
        try:
            return fn()
        except Exception as e:
            if (_is_permanent(e) or isinstance(e, ValueError) or rejected_status(e) is not None
                    or attempt == MAX_RETRIES - 1):
                raise
            delay = RETRY_BASE_DELAY * (2 ** attempt)
            logger.warning("%s failed (attempt %d/%d): %s. Retrying in %ds...",
                           description, attempt + 1, MAX_RETRIES, safe_error_text(e), delay)
            time.sleep(delay)
    raise AssertionError("unreachable")


def _record(state: SyncState, user_name: str, reading: BloodPressureReading, response: str | None) -> None:
    state.record_sync(
        user_name, reading.reading_id, reading.timestamp.astimezone(timezone.utc).isoformat(), None,
        datetime.now(timezone.utc).isoformat(), target=BP_TARGET, response=response,
    )


def sync_blood_pressure(user: UserConfig, state: SyncState, *, headless: bool = False, dry_run: bool = False,
                        backfill_days: int | None = None, now: datetime | None = None) -> BpSyncResult | None:
    """Move this person's new OMRON connect readings to their own Garmin account.

    None when the person has no omron section. Login and fetch failures
    raise (the caller reports them); a failed upload stops the loop and is
    returned in result.error with the counts so far.
    """
    if user.omron is None:
        return None
    if user.garmin is None:
        raise PermanentSyncError(f"Blood pressure for {user.name} has nowhere to go: add a garmin section.")

    now = now or datetime.now(timezone.utc)
    since = window_start(state, user.name, backfill_days, now)
    result = BpSyncResult()
    omron = OmronClient(user.omron)
    garmin = GarminClient(user.garmin)
    try:
        omron.authenticate()
        readings = _retry_quietly(lambda: omron.fetch_readings(since), "OMRON connect fetch")
        result.fetched = len(readings)
        candidates = sorted((r for r in readings if not state.is_synced(user.name, r.reading_id, BP_TARGET)),
                            key=lambda r: r.timestamp)
        result.skipped_synced = result.fetched - len(candidates)
        if not candidates:
            logger.info("No new blood pressure readings for %s", user.name)
            return result

        garmin.authenticate(allow_interactive=not headless)
        # One day of padding each side: a reading's own local date can differ from this machine's.
        existing = [] if dry_run else garmin.blood_pressure_instants(since.astimezone().date() - timedelta(days=1),
                                                                      now.astimezone().date() + timedelta(days=1))
        for reading in candidates:
            if not garmin_accepts(reading):
                result.skipped_out_of_range += 1
                if not dry_run:
                    _record(state, user.name, reading, SKIPPED_OUT_OF_RANGE_RESPONSE)
                continue
            if any(abs((reading.timestamp - inst).total_seconds()) <= GARMIN_SAME_TIME_TOLERANCE_SECONDS
                   for inst in existing):
                result.skipped_in_garmin += 1
                if not dry_run:
                    _record(state, user.name, reading, state_module.SKIPPED_IN_GARMIN_RESPONSE)
                continue
            if dry_run:
                result.uploaded += 1
                continue
            try:
                _retry_quietly(lambda r=reading: garmin.upload_blood_pressure(r, notes_for(r)),
                               "Garmin blood pressure upload")
            except ValueError:
                # The library's own range check; belt and braces. Never adjusted.
                result.skipped_out_of_range += 1
                _record(state, user.name, reading, SKIPPED_OUT_OF_RANGE_RESPONSE)
                continue
            except Exception as e:
                status = rejected_status(e)
                if status is not None:
                    # About this reading only: skip it, never change it, and carry on with newer ones.
                    result.skipped_rejected += 1
                    _record(state, user.name, reading, SKIPPED_REJECTED_RESPONSE)
                    logger.warning("Garmin refused a blood pressure reading for %s (HTTP %d); skipped it, "
                                   "nothing was changed", user.name, status)
                    continue
                result.error = safe_error_text(e)
                logger.error("Blood pressure upload failed for %s: %s", user.name, result.error)
                break
            _record(state, user.name, reading, None)
            result.uploaded += 1
            time.sleep(1)

        logger.info("Blood pressure for %s: %d uploaded, %d already synced, %d already in Garmin, "
                    "%d outside Garmin's range, %d refused by Garmin", user.name, result.uploaded,
                    result.skipped_synced, result.skipped_in_garmin, result.skipped_out_of_range,
                    result.skipped_rejected)
        return result
    finally:
        for client in (omron, garmin):
            try:
                client.close()
            except Exception:
                logger.debug("Closing a client failed", exc_info=True)
