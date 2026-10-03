"""Tests for the Omron -> Garmin blood pressure step (homevitals/bp_sync.py).

OmronClient and GarminClient are mocks; the state database is real (tmp_path).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from homevitals import bp_sync
from homevitals.config import EufyConfig, GarminConfig, OmronConfig, UserConfig
from homevitals.omron_client import (
    OmronApiError,
    OmronLoginError,
    OmronServerError,
    parse_v2_reading,
)
from homevitals.state import BP_TARGET, SKIPPED_IN_GARMIN_RESPONSE, SyncState
from homevitals.sync import PermanentSyncError

NOW = datetime(2025, 10, 3, 12, tzinfo=timezone.utc)


def _user(name="Chris", omron=True, garmin_email="adult-a@example.com", omron_email="adult-a@example.com"):
    return UserConfig(
        name=name,
        eufy=EufyConfig(email="scale@example.com", password="pw", customer_id="adult-a-0001"),
        garmin=GarminConfig(email=garmin_email, password="pw"),
        omron=OmronConfig(email=omron_email, password="pw", country="CA") if omron else None,
    )


def _readings(fixtures, name="omron_v2_sync_bp_adult_a.json"):
    rows = fixtures.json(name)["data"]
    out = [parse_v2_reading(r, user_number=None, fallback_tz=timezone.utc) for r in rows]
    return sorted([r for r in out if r is not None], key=lambda r: r.timestamp)


def _by_systolic(readings):
    return {r.systolic: r for r in readings}


@pytest.fixture
def state(tmp_path):
    s = SyncState(tmp_path / "state.db")
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _no_sleep():
    with patch("homevitals.bp_sync.time.sleep") as sleep:
        yield sleep


def _run(user, state, readings, existing=None, garmin=None, **kwargs):
    omron = MagicMock(name="OmronClient")
    omron.fetch_readings.return_value = readings
    garmin = garmin or MagicMock(name="GarminClient")
    garmin.blood_pressure_instants.return_value = existing or []
    garmin.upload_blood_pressure.return_value = {}
    with patch("homevitals.bp_sync.OmronClient", return_value=omron) as omron_ctor, \
         patch("homevitals.bp_sync.GarminClient", return_value=garmin) as garmin_ctor:
        result = bp_sync.sync_blood_pressure(user, state, now=kwargs.pop("now", NOW), **kwargs)
    return result, omron, garmin, omron_ctor, garmin_ctor


def _uploaded(garmin):
    return [c.args[0].systolic for c in garmin.upload_blood_pressure.call_args_list]


# ---------------------------------------------------------------------------
# Window and wiring
# ---------------------------------------------------------------------------


def test_user_without_omron_returns_none_and_touches_nothing(state):
    result, _, _, omron_ctor, garmin_ctor = _run(_user(omron=False), state, [])
    assert result is None
    omron_ctor.assert_not_called()
    garmin_ctor.assert_not_called()


def test_first_sync_backfills_30_days(state, fixtures):
    _, omron, *_ = _run(_user(), state, [])
    since = omron.fetch_readings.call_args.args[0]
    assert since.tzinfo is not None
    assert abs((NOW - timedelta(days=30) - since).total_seconds()) < 60


def test_later_sync_refetches_from_last_synced_minus_overlap(state):
    state.record_sync("Chris", "bp-old", "2025-10-01T09:00:00+00:00", None, "x", target=BP_TARGET)
    _, omron, *_ = _run(_user(), state, [])
    since = omron.fetch_readings.call_args.args[0]
    assert since == datetime(2025, 9, 24, 9, tzinfo=timezone.utc)


def test_backfill_days_overrides_window(state):
    state.record_sync("Chris", "bp-old", "2025-10-01T09:00:00+00:00", None, "x", target=BP_TARGET)
    _, omron, *_ = _run(_user(), state, [], backfill_days=3)
    assert omron.fetch_readings.call_args.args[0] == NOW - timedelta(days=3)


def test_each_user_gets_clients_built_from_their_own_config(state):
    _, _, _, omron_ctor, garmin_ctor = _run(_user("Jane", garmin_email="adult-b@example.com",
                                                  omron_email="adult-b@example.com"), state, [])
    assert omron_ctor.call_args.args[0].email == "adult-b@example.com"


# ---------------------------------------------------------------------------
# Uploads and skips
# ---------------------------------------------------------------------------


def test_uploads_new_readings_and_records_state_under_garmin_bp(state, fixtures):
    readings = [_by_systolic(_readings(fixtures))[137]]
    result, _, garmin, *_ = _run(_user(), state, readings)
    assert result.uploaded == 1 and result.fetched == 1 and result.error is None
    row = state._conn.execute(
        "SELECT target, weight_kg, measurement_timestamp, response FROM sync_log WHERE eufy_measurement_id = ?",
        (readings[0].reading_id,)).fetchone()
    assert row == (BP_TARGET, None, "2025-10-02T09:00:00+00:00", None)
    garmin.authenticate.assert_called_once_with(allow_interactive=True)


def test_headless_garmin_login_never_prompts(state, fixtures):
    _, _, garmin, *_ = _run(_user(), state, [_by_systolic(_readings(fixtures))[137]], headless=True)
    garmin.authenticate.assert_called_once_with(allow_interactive=False)


def test_already_synced_readings_are_skipped_without_garmin_calls(state, fixtures):
    readings = _readings(fixtures)
    for r in readings:
        state.record_sync("Chris", r.reading_id, "2025-10-02T09:00:00+00:00", None, "x", target=BP_TARGET)
    result, _, garmin, _, garmin_ctor = _run(_user(), state, readings)
    assert result.skipped_synced == len(readings) and result.uploaded == 0
    garmin.authenticate.assert_not_called()
    garmin.upload_blood_pressure.assert_not_called()


def test_reading_already_in_garmin_at_same_instant_is_skipped_and_recorded(state, fixtures):
    by = _by_systolic(_readings(fixtures))
    existing = [datetime(2025, 10, 2, 9, tzinfo=timezone.utc)]
    result, _, garmin, *_ = _run(_user(), state, [by[137], by[133]], existing=existing)
    assert result.skipped_in_garmin == 1
    assert _uploaded(garmin) == [133]
    row = state._conn.execute("SELECT response FROM sync_log WHERE eufy_measurement_id = ?",
                              (by[137].reading_id,)).fetchone()
    assert row == (SKIPPED_IN_GARMIN_RESPONSE,)


def test_same_time_tolerance_is_two_seconds(state, fixtures):
    reading = _by_systolic(_readings(fixtures))[137]
    near = [reading.timestamp + timedelta(seconds=1)]
    result, *_ = _run(_user(), state, [reading], existing=near)
    assert result.skipped_in_garmin == 1
    state._conn.execute("DELETE FROM sync_log")
    far = [reading.timestamp + timedelta(seconds=3)]
    result, _, garmin, *_ = _run(_user(), state, [reading], existing=far)
    assert result.uploaded == 1


def test_out_of_range_reading_is_skipped_counted_recorded_and_never_adjusted(state, fixtures):
    reading = _by_systolic(_readings(fixtures))[265]
    result, _, garmin, *_ = _run(_user(), state, [reading])
    assert result.skipped_out_of_range == 1 and result.uploaded == 0
    garmin.upload_blood_pressure.assert_not_called()
    row = state._conn.execute("SELECT response FROM sync_log WHERE eufy_measurement_id = ?",
                              (reading.reading_id,)).fetchone()
    assert row == (bp_sync.SKIPPED_OUT_OF_RANGE_RESPONSE,)


def test_library_value_error_counts_as_out_of_range(state, fixtures):
    reading = _by_systolic(_readings(fixtures))[137]
    garmin = MagicMock()
    garmin.upload_blood_pressure.side_effect = ValueError("systolic must be an int in [70, 260]")
    result, *_ = _run(_user(), state, [reading], garmin=garmin)
    assert result.skipped_out_of_range == 1 and result.error is None
    assert state.is_synced("Chris", reading.reading_id, BP_TARGET)


def test_notes_name_the_device_and_flags(fixtures):
    by = _by_systolic(_readings(fixtures))
    assert bp_sync.notes_for(by[133]) == "Omron M7; irregular heartbeat detected; body movement detected"
    assert bp_sync.notes_for(by[137]) == "Omron M7"


def test_timestamp_offset_survives_end_to_end(state, fixtures):
    reading = _by_systolic(_readings(fixtures))[133]
    _, _, garmin, *_ = _run(_user(), state, [reading])
    sent = garmin.upload_blood_pressure.call_args.args[0]
    assert sent.timestamp.isoformat() == "2025-10-01T13:00:00+03:00"
    assert garmin.upload_blood_pressure.call_args.args[1].startswith("Omron M7")


def test_no_candidate_readings_skips_garmin_entirely(state):
    result, _, garmin, _, garmin_ctor = _run(_user(), state, [])
    assert result.fetched == 0
    garmin.authenticate.assert_not_called()
    garmin.blood_pressure_instants.assert_not_called()


def test_garmin_range_read_is_called_once_per_run(state, fixtures):
    from datetime import date
    readings = [r for r in _readings(fixtures) if r.systolic != 265]
    _, _, garmin, *_ = _run(_user(), state, readings)
    garmin.blood_pressure_instants.assert_called_once()
    start, end = garmin.blood_pressure_instants.call_args.args
    assert start <= (NOW - timedelta(days=30)).astimezone().date() - timedelta(days=1)
    assert end >= NOW.astimezone().date() + timedelta(days=1)
    assert isinstance(start, date)


def test_upload_error_stops_loop_keeps_counts_and_sets_error(state, fixtures):
    readings = [r for r in _readings(fixtures) if r.systolic != 265]
    garmin = MagicMock()
    garmin.upload_blood_pressure.side_effect = [{}, PermanentSyncError("Garmin rejected the email or password. "
                                                                       "Run: homevitals --update-password"), {}]
    result, *_ = _run(_user(), state, readings, garmin=garmin)
    assert result.uploaded == 1
    assert "--update-password" in result.error
    assert garmin.upload_blood_pressure.call_count == 2


def test_upload_transient_error_is_retried_quietly(state, fixtures, caplog):
    caplog.set_level(logging.DEBUG)
    reading = _by_systolic(_readings(fixtures))[137]
    garmin = MagicMock()
    garmin.upload_blood_pressure.side_effect = [GarminConnectConnectionError("API Error 503 - body 137/89"), {}]
    result, *_ = _run(_user(), state, [reading], garmin=garmin)
    assert result.uploaded == 1
    messages = "\n".join(r.getMessage() for r in caplog.records)
    assert "137/89" not in messages and "API Error 503" in messages


def test_garmin_auth_failure_raises_whole_step(state, fixtures):
    garmin = MagicMock()
    garmin.authenticate.side_effect = PermanentSyncError("Garmin wants an MFA code. Run: homevitals --reauth garmin")
    with pytest.raises(PermanentSyncError):
        _run(_user(), state, [_by_systolic(_readings(fixtures))[137]], garmin=garmin)


def test_omron_login_error_propagates_as_permanent(state):
    omron = MagicMock()
    omron.authenticate.side_effect = OmronLoginError("OMRON connect rejected the login ... --update-password")
    with patch("homevitals.bp_sync.OmronClient", return_value=omron), \
         patch("homevitals.bp_sync.GarminClient"), pytest.raises(OmronLoginError):
        bp_sync.sync_blood_pressure(_user(), state, now=NOW)
    omron.fetch_readings.assert_not_called()


def test_omron_server_error_is_retried_then_raised(state):
    omron = MagicMock()
    omron.fetch_readings.side_effect = OmronServerError("OMRON connect server error HTTP 503 at host", 503)
    with patch("homevitals.bp_sync.OmronClient", return_value=omron), \
         patch("homevitals.bp_sync.GarminClient"), pytest.raises(OmronServerError):
        bp_sync.sync_blood_pressure(_user(), state, now=NOW)
    assert omron.fetch_readings.call_count == 3


def test_dry_run_counts_without_uploading_or_recording_and_skips_garmin_read(state, fixtures):
    readings = _readings(fixtures)
    result, _, garmin, *_ = _run(_user(), state, readings, dry_run=True)
    assert result.uploaded == 3 and result.skipped_out_of_range == 1
    garmin.upload_blood_pressure.assert_not_called()
    garmin.blood_pressure_instants.assert_not_called()
    assert state._conn.execute("SELECT COUNT(*) FROM sync_log").fetchone() == (0,)


def test_sleeps_one_second_between_uploads(state, fixtures, _no_sleep):
    readings = [r for r in _readings(fixtures) if r.systolic != 265]
    _run(_user(), state, readings)
    assert [c.args for c in _no_sleep.call_args_list] == [(1,)] * len(readings)


def test_clients_closed_on_success_and_on_failure(state, fixtures):
    _, omron, garmin, *_ = _run(_user(), state, [_by_systolic(_readings(fixtures))[137]])
    omron.close.assert_called_once()
    garmin.close.assert_called_once()
    garmin = MagicMock()
    garmin.authenticate.side_effect = PermanentSyncError("x --reauth")
    omron = MagicMock()
    omron.fetch_readings.return_value = [_by_systolic(_readings(fixtures))[133]]
    with patch("homevitals.bp_sync.OmronClient", return_value=omron), \
         patch("homevitals.bp_sync.GarminClient", return_value=garmin), pytest.raises(PermanentSyncError):
        bp_sync.sync_blood_pressure(_user(), state, now=NOW)
    omron.close.assert_called_once()
    garmin.close.assert_called_once()


def test_readings_processed_oldest_first(state, fixtures):
    readings = [r for r in _readings(fixtures) if r.systolic != 265]
    _, _, garmin, *_ = _run(_user(), state, list(reversed(readings)))
    stamps = [c.args[0].timestamp for c in garmin.upload_blood_pressure.call_args_list]
    assert stamps == sorted(stamps)


@pytest.mark.parametrize("exc,expected", [
    (PermanentSyncError("Garmin wants an MFA code. Run: homevitals --reauth garmin"),
     "Garmin wants an MFA code. Run: homevitals --reauth garmin"),
    (OmronLoginError("OMRON connect rejected the login for a@x (country CA)."),
     "OMRON connect rejected the login for a@x (country CA)."),
    (OmronApiError("OMRON connect returned HTTP 400 from h while fetching readings."),
     "OMRON connect returned HTTP 400 from h while fetching readings."),
    (OmronServerError("OMRON connect server error HTTP 503 at h", 503), "OMRON connect server error HTTP 503 at h"),
    (GarminConnectTooManyRequestsError("429 body 137"),
     "Garmin is asking us to slow down (HTTP 429). Wait an hour and sync again."),
    (GarminConnectAuthenticationError("401 body 137"), "Garmin session expired. Run: homevitals --reauth garmin"),
    (OSError("[Errno 11001] getaddrinfo failed"), "[Errno 11001] getaddrinfo failed"),
    (GarminConnectConnectionError("API Error 400 - {'systolic': 137}"), "Garmin upload failed (API Error 400)"),
    (ValueError("systolic must be an int in [70, 260]: 265"), "Garmin rejected a value (outside its accepted range)."),
    (ValueError("OMRON connect accounts created in JP use OMRON's older servers, which this build does not support."),
     "OMRON connect accounts created in JP use OMRON's older servers, which this build does not support."),
    (KeyError("137"), "Unexpected error (KeyError). Details are in the log file."),
])
def test_safe_error_text_table(exc, expected):
    assert bp_sync.safe_error_text(exc) == expected


def test_result_and_logs_contain_counts_only(state, fixtures, caplog):
    caplog.set_level(logging.DEBUG)
    readings = _readings(fixtures)
    result, *_ = _run(_user(), state, readings)
    text = repr(result) + "\n" + "\n".join(r.getMessage() for r in caplog.records)
    for value in (137, 89, 58, 119, 74, 69, 265, 155, 66, 133, 87, 71):
        assert not re.search(rf"\b{value}\b", text), value
    for reading in readings:
        assert reading.reading_id not in text
    json.dumps(result.__dict__)   # plain counts and text only


def test_garmin_accepts_mirrors_library_limits(fixtures):
    by = _by_systolic(_readings(fixtures))
    assert bp_sync.garmin_accepts(by[137])
    assert not bp_sync.garmin_accepts(by[265])


def test_a_reading_garmin_refuses_is_skipped_and_newer_ones_still_upload(state, fixtures, caplog):
    caplog.set_level(logging.DEBUG)
    readings = [r for r in _readings(fixtures) if r.systolic != 265]
    assert len(readings) >= 3
    first = readings[0]
    garmin = MagicMock()
    garmin.upload_blood_pressure.side_effect = (
        [GarminConnectConnectionError(f"API Error 400 - body {first.systolic}/{first.diastolic}")]
        + [{}] * (len(readings) - 1)
    )
    result, *_ = _run(_user(), state, readings, garmin=garmin)
    assert result.skipped_rejected == 1
    assert result.uploaded == len(readings) - 1
    assert result.error is None
    # Refused once, not retried: the same values get the same answer.
    assert garmin.upload_blood_pressure.call_count == len(readings)
    assert state.is_synced("Chris", first.reading_id, BP_TARGET)
    messages = "\n".join(r.getMessage() for r in caplog.records)
    assert "HTTP 400" in messages
    assert f"{first.systolic}/{first.diastolic}" not in messages

    # The next run does not send it again.
    result, _, garmin2, *_ = _run(_user(), state, readings)
    garmin2.upload_blood_pressure.assert_not_called()


@pytest.mark.parametrize("status", [401, 403, 408, 429, 500, 503])
def test_session_rate_and_server_errors_still_stop_the_loop(state, fixtures, status):
    readings = [r for r in _readings(fixtures) if r.systolic != 265]
    garmin = MagicMock()
    garmin.upload_blood_pressure.side_effect = GarminConnectConnectionError(f"API Error {status} - ")
    result, *_ = _run(_user(), state, readings, garmin=garmin)
    assert result.skipped_rejected == 0
    assert result.uploaded == 0
    assert result.error
    assert not state.is_synced("Chris", readings[0].reading_id, BP_TARGET)


def test_status_line_reports_refused_readings_without_values():
    from homevitals.cli.status import _bp_detail_lines

    result = bp_sync.BpSyncResult(fetched=3, uploaded=2, skipped_rejected=1)
    lines = _bp_detail_lines("Chris", result)
    assert lines == ["Chris: blood pressure - Garmin refused 1 reading, so it was skipped (nothing was changed). "
                     "Details are in the log."]
