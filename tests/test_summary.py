from __future__ import annotations

import time
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

from homevitals.cli.status import _print_summary, _show_status
from homevitals.reporting import SyncReport


def _mock_state(last_sync_ts: int | None = None):
    state = MagicMock()
    state.get_latest_sync_timestamp.return_value = last_sync_ts
    return state


def _mock_user(name: str = "default", has_garmin: bool = True, has_strava: bool = False, has_zwift: bool = False):
    user = MagicMock()
    user.name = name
    if has_garmin:
        user.garmin.email = "test@example.com"
        user.garmin.password = "pw"
    else:
        user.garmin = None
    if has_strava:
        user.strava.client_id = "12345"
        user.strava.client_secret = "secret"
    else:
        user.strava = None
    if has_zwift:
        user.zwift.email = "z@example.com"
        user.zwift.password = "pw"
    else:
        user.zwift = None
    return user


def _patch_token_status(return_value):
    """Patch GarminAuth.token_status at the class level."""
    return patch(
        "homevitals.garmin_auth.GarminAuth.token_status",
        return_value=return_value,
    )


def _patch_eufy_token_status(return_value=None):
    """Patch EufyClient.token_status to avoid hitting real tokens."""
    if return_value is None:
        return_value = {"state": "valid", "days_remaining": 25}
    return patch(
        "homevitals.eufy_client.EufyClient.token_status",
        return_value=return_value,
    )


def test_summary_no_new_measurements(capsys):
    last_sync = int(time.time()) - 3600 * 5  # 5 hours ago
    state = _mock_state(last_sync)
    user = _mock_user()

    with _patch_eufy_token_status(), _patch_token_status({"state": "valid", "days_remaining": 200}):
        _print_summary({}, [], state, [user])

    output = capsys.readouterr().out.strip()
    assert "No new measurements" in output
    assert "last sync: 5h ago" in output
    assert "Garmin connected" in output


def test_summary_no_new_measurements_days_ago(capsys):
    last_sync = int(time.time()) - 86400 * 3  # 3 days ago
    state = _mock_state(last_sync)
    user = _mock_user()

    with _patch_eufy_token_status(), _patch_token_status({"state": "valid", "days_remaining": 100}):
        _print_summary({}, [], state, [user])

    output = capsys.readouterr().out.strip()
    assert "last sync: 3d ago" in output


def test_summary_synced_garmin_only(capsys):
    state = _mock_state()
    user = _mock_user()

    _print_summary({"garmin": 3}, [], state, [user])

    output = capsys.readouterr().out.strip()
    assert "Syncs completed: Garmin 3." == output


def test_summary_synced_strava_only(capsys):
    state = _mock_state()
    user = _mock_user(has_garmin=False, has_strava=True)

    _print_summary({"strava": 2}, [], state, [user])

    output = capsys.readouterr().out.strip()
    assert "Syncs completed: Strava 2." == output


def test_summary_synced_zwift_only(capsys):
    state = _mock_state()
    user = _mock_user(has_garmin=False, has_zwift=True)

    _print_summary({"zwift": 1}, [], state, [user])

    assert capsys.readouterr().out.strip() == "Syncs completed: Zwift 1."


def test_summary_reports_zwift_token_health(capsys):
    state = _mock_state()
    user = _mock_user(has_garmin=False, has_zwift=True)

    with _patch_eufy_token_status(), patch(
        "homevitals.cli.status._zwift_token_status",
        return_value={"state": "valid"},
    ):
        _print_summary({}, [], state, [user])

    assert "Zwift connected" in capsys.readouterr().out


def test_summary_treats_zwift_refresh_pending_as_connected(capsys):
    state = _mock_state()
    user = _mock_user(has_garmin=False, has_zwift=True)

    with _patch_eufy_token_status(), patch(
        "homevitals.cli.status._zwift_token_status",
        return_value={"state": "refresh_needed"},
    ):
        _print_summary({}, [], state, [user])

    output = capsys.readouterr().out
    assert "Zwift connected" in output
    assert "Zwift not connected" not in output


def test_status_explains_zwift_refresh_is_automatic(capsys):
    state = _mock_state()
    user = _mock_user(has_garmin=False, has_zwift=True)

    with _patch_eufy_token_status(), patch(
        "homevitals.cli.status._zwift_token_status",
        return_value={"state": "refresh_needed"},
    ):
        _show_status(state, [user])

    output = capsys.readouterr().out
    assert "Zwift auth: connected (refreshes on next sync)" in output
    assert "--reauth zwift" not in output


def test_summary_synced_both_targets(capsys):
    state = _mock_state()
    user = _mock_user(has_garmin=True, has_strava=True)

    _print_summary({"garmin": 3, "strava": 3}, [], state, [user])

    output = capsys.readouterr().out.strip()
    assert output == "Syncs completed: Garmin 3, Strava 3."


def test_summary_synced_singular(capsys):
    state = _mock_state()
    user = _mock_user()

    _print_summary({"garmin": 1}, [], state, [user])

    output = capsys.readouterr().out.strip()
    assert "Syncs completed: Garmin 1." == output


def test_summary_includes_current_run_garmin_skip(capsys):
    state = _mock_state()
    user = _mock_user(has_garmin=True, has_strava=True, has_zwift=True)
    report = SyncReport(garmin_existing_dates={
        ("default", datetime(2026, 5, 10, tzinfo=timezone.utc).date()),
    })

    _print_summary(
        {"garmin": 0, "strava": 1, "zwift": 1}, [], state, [user], report,
    )

    assert capsys.readouterr().out.strip() == (
        "Syncs completed: Garmin 0, Strava 1, Zwift 1. "
        "Garmin already has a weigh-in dated 2026-05-10."
    )


def test_summary_failure(capsys):
    state = _mock_state()
    user = _mock_user()

    _print_summary({}, [("default", "connection timeout")], state, [user])

    output = capsys.readouterr().out.strip()
    assert "Sync failed for: default" in output
    assert "--verbose" in output


def test_summary_partial_failure_keeps_completed_counts(capsys):
    _print_summary(
        {"garmin": 0, "strava": 1, "zwift": 1},
        [("default/garmin", "login failed")],
        _mock_state(),
        [_mock_user(has_strava=True, has_zwift=True)],
    )

    assert capsys.readouterr().out.strip().splitlines() == [
        "Syncs completed: Garmin 0, Strava 1, Zwift 1.",
        "Sync failed for: default/garmin. Run with --verbose for details.",
    ]


def test_summary_zero_upload_garmin_skip_uses_current_run_evidence(capsys):
    report = SyncReport(garmin_existing_dates={("default", date(2026, 5, 10))})

    _print_summary({"garmin": 0}, [], _mock_state(), [_mock_user()], report)

    assert capsys.readouterr().out.strip() == (
        "Garmin already has a weigh-in dated 2026-05-10."
    )


def test_summary_multi_user_noop_avoids_first_user_health_claims(capsys):
    report = SyncReport(multiple_users=True)

    _print_summary({}, [], _mock_state(), [_mock_user("one"), _mock_user("two")], report)

    assert capsys.readouterr().out.strip() == "No new measurements for 2 profiles."


def test_summary_refresh_needed(capsys):
    last_sync = int(time.time()) - 3600
    state = _mock_state(last_sync)
    user = _mock_user()

    with _patch_eufy_token_status(), _patch_token_status({"state": "refresh_needed", "days_remaining": 300}):
        _print_summary({}, [], state, [user])

    output = capsys.readouterr().out.strip()
    assert "Garmin not connected" in output


def test_summary_expired_token(capsys):
    last_sync = int(time.time()) - 3600
    state = _mock_state(last_sync)
    user = _mock_user()

    with _patch_eufy_token_status(), _patch_token_status({"state": "expired", "days_remaining": 0}):
        _print_summary({}, [], state, [user])

    output = capsys.readouterr().out.strip()
    assert "Garmin not connected" in output


def test_summary_no_session(capsys):
    state = _mock_state(None)  # never synced
    user = _mock_user()

    with _patch_eufy_token_status(), _patch_token_status({"state": "no_session", "days_remaining": None}):
        _print_summary({}, [], state, [user])

    output = capsys.readouterr().out.strip()
    assert "No new measurements" in output
    assert "Eufy token valid" in output


def test_summary_no_op_includes_open_app_hint(capsys):
    state = _mock_state(int(time.time()) - 3600)
    user = _mock_user()

    with _patch_eufy_token_status(), _patch_token_status({"state": "valid", "days_remaining": 50}):
        _print_summary({}, [], state, [user])

    output = capsys.readouterr().out
    assert "open the Eufy app" in output


def test_summary_synced_omits_open_app_hint(capsys):
    state = _mock_state()
    user = _mock_user()

    _print_summary({"garmin": 2}, [], state, [user])

    output = capsys.readouterr().out
    assert "open the Eufy app" not in output


# ---------------------------------------------------------------------------
# Household: one line per person (WP5)
# ---------------------------------------------------------------------------

def test_user_summary_line_plural():
    from homevitals.cli.status import _user_summary_line
    assert _user_summary_line("Chris", {"garmin": 2}, {}) == "Chris: synced 2 weigh-ins to Garmin."


def test_user_summary_line_singular():
    from homevitals.cli.status import _user_summary_line
    assert _user_summary_line("Chris", {"garmin": 1}, {}) == "Chris: synced 1 weigh-in to Garmin."


def test_user_summary_line_nothing_new():
    from homevitals.cli.status import _user_summary_line
    assert _user_summary_line("Jane", {"garmin": 0}, {}) == "Jane: nothing new."
    assert _user_summary_line("Jane", {}, {}) == "Jane: nothing new."


def test_user_summary_line_with_target_error_is_ascii():
    from homevitals.cli.status import _user_summary_line
    line = _user_summary_line("Jane", {"garmin": 0}, {"garmin": "Garmin wants an MFA code → Run: homevitals --reauth garmin"})
    assert line.startswith("Jane: nothing new. Garmin failed - Garmin wants an MFA code")
    assert line.isascii()


def test_user_failure_line_truncates():
    from homevitals.cli.status import _user_failure_line
    line = _user_failure_line("Chris", "x" * 500)
    assert line.startswith("Chris: failed - ")
    assert len(line) == len("Chris: failed - ") + 160


def test_history_shows_every_person_even_when_the_first_has_none(capsys):
    from homevitals.cli.status import _show_history
    state = MagicMock()
    state.get_history.side_effect = lambda name, limit: [] if name == "Chris" else [
        {"timestamp": "2026-09-30T07:00:00+00:00", "weight_kg": 61.9, "targets": ["garmin"]}]
    _show_history(state, [_mock_user("Chris"), _mock_user("Jane")])
    out = capsys.readouterr().out
    assert "Chris" in out and "Jane" in out
    assert "2026-09-30" in out


# ---------------------------------------------------------------------------
# Part 4: blood pressure in the per-person lines and the summary (WP17)
# ---------------------------------------------------------------------------

def _bp_result(**kwargs):
    from homevitals.bp_sync import BpSyncResult
    return BpSyncResult(**kwargs)


def test_user_summary_line_with_weigh_ins_and_bp():
    from homevitals.cli.status import _user_summary_line
    assert _user_summary_line("Chris", {"garmin": 2}, {}, bp_uploaded=1) == (
        "Chris: synced 2 weigh-ins and 1 blood pressure reading to Garmin.")


def test_user_summary_line_bp_only_singular_and_plural():
    from homevitals.cli.status import _user_summary_line
    assert _user_summary_line("Chris", {"garmin": 0}, {}, bp_uploaded=1) == (
        "Chris: synced 1 blood pressure reading to Garmin.")
    assert _user_summary_line("Chris", {}, {}, bp_uploaded=3) == "Chris: synced 3 blood pressure readings to Garmin."


def test_user_summary_line_bp_zero_is_unchanged():
    from homevitals.cli.status import _user_summary_line
    assert _user_summary_line("Chris", {"garmin": 2}, {}, bp_uploaded=0) == "Chris: synced 2 weigh-ins to Garmin."
    assert _user_summary_line("Chris", {"garmin": 0}, {}, bp_uploaded=0) == "Chris: nothing new."


def test_bp_detail_lines_error():
    from homevitals.cli.status import _bp_detail_lines
    assert _bp_detail_lines("Chris", _bp_result(error="x" * 300)) == [f"Chris: blood pressure failed - {'x' * 160}"]


def test_bp_detail_lines_out_of_range():
    from homevitals.cli.status import _bp_detail_lines
    assert _bp_detail_lines("Chris", _bp_result(skipped_out_of_range=1)) == [
        "Chris: blood pressure - skipped 1 reading with values outside the range Garmin accepts "
        "(nothing was changed or rounded)."]
    assert "2 readings" in _bp_detail_lines("Chris", _bp_result(skipped_out_of_range=2))[0]


def test_bp_detail_lines_already_in_garmin():
    from homevitals.cli.status import _bp_detail_lines
    assert _bp_detail_lines("Chris", _bp_result(skipped_in_garmin=2)) == [
        "Chris: blood pressure - Garmin already had 2 readings, so they were skipped."]


def test_bp_detail_lines_hint_only_when_truly_nothing():
    from homevitals.cli.status import _bp_detail_lines
    assert _bp_detail_lines("Chris", _bp_result()) == [
        "Chris: blood pressure - no new readings. Open the OMRON connect app on the phone so it picks up readings "
        "from the monitor, then sync again."]
    assert _bp_detail_lines("Chris", _bp_result(skipped_synced=4)) == _bp_detail_lines("Chris", _bp_result())


def test_bp_detail_lines_empty_when_uploaded():
    from homevitals.cli.status import _bp_detail_lines
    assert _bp_detail_lines("Chris", _bp_result(uploaded=1)) == []


def test_bp_detail_lines_none_result():
    from homevitals.cli.status import _bp_detail_lines
    assert _bp_detail_lines("Chris", None) == []


def test_bp_detail_lines_are_ascii():
    from homevitals.cli.status import _bp_detail_lines
    lines = _bp_detail_lines("Chris", _bp_result(error="weird → text", skipped_out_of_range=1, skipped_in_garmin=1))
    assert len(lines) == 3 and all(line.isascii() for line in lines)


def test_blood_pressure_note_singular_plural_and_empty():
    from homevitals.reporting import SyncReport, blood_pressure_note
    assert blood_pressure_note(None) == ""
    assert blood_pressure_note(SyncReport()) == ""
    assert blood_pressure_note(SyncReport(bp_uploaded=1)) == " Blood pressure: 1 reading."
    assert blood_pressure_note(SyncReport(bp_uploaded=3)) == " Blood pressure: 3 readings."


def test_print_summary_bp_only_line(capsys):
    from homevitals.reporting import SyncReport
    _print_summary({"garmin": 0}, [], _mock_state(), [_mock_user()], report=SyncReport(bp_uploaded=2))
    assert capsys.readouterr().out.strip() == "Syncs completed: blood pressure 2."


def test_print_summary_weigh_ins_and_bp(capsys):
    from homevitals.reporting import SyncReport
    _print_summary({"garmin": 1}, [], _mock_state(), [_mock_user()], report=SyncReport(bp_uploaded=1))
    assert capsys.readouterr().out.strip() == "Syncs completed: Garmin 1. Blood pressure: 1 reading."


# ---------------------------------------------------------------------------
# Part 5: partly set up people (WP23)
# ---------------------------------------------------------------------------

def test_user_not_ready_line_variants():
    from homevitals.cli.status import _user_not_ready_line
    assert _user_not_ready_line("Jane", ["Garmin"], False) == "Jane: not fully set up yet (connect Garmin)."
    assert _user_not_ready_line("Jane", ["Garmin", "the scale or the blood pressure monitor"], False) == (
        "Jane: not fully set up yet (connect Garmin and the scale or the blood pressure monitor).")
    line = _user_not_ready_line("Jane", ["Garmin", "the scale or the blood pressure monitor"], True)
    assert line == "Jane: not set up yet (connect Garmin and the scale or the blood pressure monitor)."
    assert line.isascii()


def test_print_summary_prints_nothing_when_nobody_was_ready(capsys):
    _print_summary({}, [], _mock_state(), [])
    assert capsys.readouterr().out == ""


def test_print_summary_multi_user_singular_profile(capsys):
    _print_summary({}, [], _mock_state(), [_mock_user("Chris")], report=SyncReport(multiple_users=True))
    assert capsys.readouterr().out.strip() == "No new measurements for 1 profile."


def test_print_summary_single_bp_only_user_skips_eufy_parts(capsys):
    user = _mock_user("Jane")
    user.eufy = None
    with patch("homevitals.eufy_client.EufyClient") as eufy, _patch_token_status({"state": "valid"}):
        _print_summary({}, [], _mock_state(), [user])
    eufy.assert_not_called()
    out = capsys.readouterr().out
    assert "Eufy" not in out and "open the Eufy app" not in out
