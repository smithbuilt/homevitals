"""Household safety rules: the promises HomeVitals makes, checked on every change.

These run in the Stop hook every time. Never skip them or mark them xfail.
"""
from __future__ import annotations

import socket

import pytest

from tests.conftest import RealNetworkAttempt

# ---------------------------------------------------------------------------
# Tests never contact real servers
# ---------------------------------------------------------------------------


def test_network_blocker_rejects_remote_tcp_connect():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(RealNetworkAttempt):
            s.connect(("192.0.2.1", 443))
    finally:
        s.close()


def test_network_blocker_rejects_remote_dns():
    with pytest.raises(RealNetworkAttempt):
        socket.getaddrinfo("example.com", 443)


def test_network_blocker_allows_loopback():
    socket.getaddrinfo("127.0.0.1", 0)


def test_network_blocker_rejects_httpx():
    import httpx

    with pytest.raises(RealNetworkAttempt):
        httpx.get("https://api.eufylife.com/v1/device/data", timeout=1)


def test_network_blocker_rejects_curl_cffi():
    cffi_requests = pytest.importorskip("curl_cffi.requests")
    with pytest.raises(RealNetworkAttempt):
        cffi_requests.Session().request("GET", "https://example.com")
    with pytest.raises(RealNetworkAttempt):
        cffi_requests.get("https://example.com")


# ---------------------------------------------------------------------------
# Two-person configs without customer_id are rejected (WP1)
# ---------------------------------------------------------------------------


def test_two_users_without_customer_id_are_rejected(fixtures):
    from homevitals.config import load_config

    with pytest.raises(ValueError, match=r"Jane.*customer_id"):
        load_config(fixtures.path("config_household_missing_customer_id.yaml"))


def test_two_users_sharing_a_garmin_account_are_rejected(tmp_path):
    import yaml

    from homevitals.config import load_config

    raw = yaml.safe_load(_household_yaml())
    raw["users"][1]["garmin"]["email"] = raw["users"][0]["garmin"]["email"].upper()
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="both use the Garmin account"):
        load_config(path)


def _household_yaml() -> str:
    from pathlib import Path
    return (Path(__file__).parent / "fixtures" / "config_household.yaml").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Legacy tokens are never guessed onto someone with two people (WP2)
# ---------------------------------------------------------------------------


def test_legacy_token_migration_never_guesses_with_two_users():
    from types import SimpleNamespace

    from homevitals.credentials import get_token, migrate_legacy_tokens, store_token

    store_token("garmin", {"di_token": "legacy"})
    store_token("eufy", {"access_token": "legacy"})
    users = [
        SimpleNamespace(name=n, eufy=SimpleNamespace(email="scale@example.com"), garmin=SimpleNamespace(email=e))
        for n, e in (("Chris", "adult-a@example.com"), ("Jane", "adult-b@example.com"))
    ]
    assert migrate_legacy_tokens(users) == []
    assert get_token("garmin") == {"di_token": "legacy"}
    assert get_token("eufy") == {"access_token": "legacy"}
    for keyed in ("garmin:adult-a@example.com", "garmin:adult-b@example.com", "eufy:scale@example.com"):
        assert get_token(keyed) is None


# ---------------------------------------------------------------------------
# Each Garmin account keeps its own login token (WP3)
# ---------------------------------------------------------------------------


def test_garmin_tokens_are_separated_per_email(tmp_path):
    import json
    from unittest.mock import MagicMock

    from homevitals.garmin_auth import GarminAuth

    blob = {"di_token": "a-token", "di_refresh_token": "r", "di_client_id": "c"}
    session = tmp_path / "session.json"
    a = GarminAuth("adult-a@example.com", "pw", session_path=session)
    b = GarminAuth("adult-b@example.com", "pw", session_path=session)
    garmin = MagicMock()
    garmin.client.dumps.return_value = json.dumps(blob)
    a._save_token(garmin)
    assert a._load_token() == blob
    assert b._load_token() is None
    # An old session.json not tied to any email is never restored for anyone.
    session.write_text(json.dumps(blob))
    assert b._load_token() is None


# ---------------------------------------------------------------------------
# Each person's weigh-ins only reach their own Garmin; kids never sync (WP5)
# ---------------------------------------------------------------------------

KID_WEIGHTS = {30.4, 30.6, 24.0, 24.2, 30.8}
ADULT_A_WEIGHTS = {80.2, 80.0, 79.9}
ADULT_B_WEIGHTS = {62.1, 61.9}


def _household_user_configs():
    import yaml

    from homevitals.config import EufyConfig, GarminConfig, UserConfig

    raw = yaml.safe_load(_household_yaml())
    return [
        UserConfig(
            name=u["name"],
            eufy=EufyConfig(email=u["eufy"]["email"], password="pw", customer_id=u["eufy"]["customer_id"]),
            garmin=GarminConfig(email=u["garmin"]["email"], password="pw"),
        )
        for u in raw["users"]
    ]


def _run_household_sync(fixtures, tmp_path):
    """Run the real sync_user for both adults over the four-profile fixture.

    Eufy's HTTP calls and the Garmin client are faked; the profile filtering,
    state checks and per-user wiring are the real code. Returns
    ({garmin email: [uploaded weights]}, state).
    """
    from unittest.mock import MagicMock, patch

    from homevitals.eufy_client import EufyClient
    from homevitals.state import SyncState
    from homevitals.sync import sync_user

    records = fixtures.json("eufy_device_data_household.json")["data"]
    raw = fixtures.json("eufy_raw_wifi_household.json")["list"]
    uploads: dict[str, list[float]] = {}

    def garmin_factory(cfg):
        client = MagicMock(name=f"GarminClient({cfg.email})")
        client.has_weight_on_date.return_value = False
        client.upload_body_composition.side_effect = (
            lambda body, email=cfg.email: uploads.setdefault(email, []).append(round(body.weight, 1))
        )
        return client

    state = SyncState(tmp_path / "state.db")
    with patch.object(EufyClient, "authenticate", lambda self: None), \
         patch.object(EufyClient, "_get_records", lambda self, after: [dict(r) for r in records]), \
         patch.object(EufyClient, "_list_device_ids", lambda self: ["scale-0001"]), \
         patch.object(EufyClient, "_get_raw_records", lambda self, device, after: [dict(r) for r in raw]), \
         patch("homevitals.garmin_client.GarminClient", side_effect=garmin_factory),          patch("homevitals.sync.time.sleep"):
        for user in _household_user_configs():
            sync_user(user, state, backfill_days=60, headless=True)
    return uploads, state


def test_each_person_only_receives_their_own_weigh_ins(fixtures, tmp_path):
    uploads, state = _run_household_sync(fixtures, tmp_path)
    try:
        assert set(uploads["adult-a@example.com"]) == ADULT_A_WEIGHTS
        assert set(uploads["adult-b@example.com"]) == ADULT_B_WEIGHTS
        assert set(uploads) == {"adult-a@example.com", "adult-b@example.com"}
    finally:
        state.close()


def test_unlinked_kid_profiles_are_never_uploaded(fixtures, tmp_path):
    uploads, state = _run_household_sync(fixtures, tmp_path)
    try:
        everything = {w for weights in uploads.values() for w in weights}
        assert not everything & KID_WEIGHTS
        for record in fixtures.json("eufy_device_data_household.json")["data"]:
            if record["customer_id"].startswith("kid-"):
                measurement_id = f"{record['customer_id']}_{record['create_time']}"
                for user in ("Chris", "Jane"):
                    assert not state.is_synced(user, measurement_id, "garmin")
    finally:
        state.close()


FAKE_PASSWORDS = ("Zebra-Quilt-9471!", "Mango-Kite-2208!")


def test_cli_run_output_never_contains_password(fixtures, tmp_path, capsys, caplog):
    import logging
    import shutil
    from unittest.mock import patch

    from homevitals.cli.app import main
    from homevitals.credentials import store_password

    caplog.set_level(logging.DEBUG)
    config_path = tmp_path / "config.yaml"
    shutil.copy(fixtures.path("config_household.yaml"), config_path)
    for name, pw in zip(("Chris", "Jane"), FAKE_PASSWORDS, strict=True):
        store_password(f"{name}:eufy", pw)
        store_password(f"{name}:garmin", pw)

    argv = ["homevitals", "--config", str(config_path), "--db", str(tmp_path / "state.db"), "--headless"]
    with patch("sys.argv", argv), \
         patch("homevitals.sync.sync_user", side_effect=RuntimeError("boom")), \
         pytest.raises(SystemExit):
        main()

    captured = capsys.readouterr()
    for pw in FAKE_PASSWORDS:
        assert pw not in captured.out
        assert pw not in captured.err
        assert pw not in caplog.text


def test_cli_run_output_never_contains_password_on_a_login_failure(fixtures, tmp_path, capsys, caplog):
    """Same as above, with the error type a real failed login raises."""
    import logging
    import shutil
    from unittest.mock import patch

    from homevitals.cli.app import main
    from homevitals.credentials import store_password
    from homevitals.sync import PermanentSyncError

    caplog.set_level(logging.DEBUG)
    config_path = tmp_path / "config.yaml"
    shutil.copy(fixtures.path("config_household.yaml"), config_path)
    for name, pw in zip(("Chris", "Jane"), FAKE_PASSWORDS, strict=True):
        store_password(f"{name}:eufy", pw)
        store_password(f"{name}:garmin", pw)

    argv = ["homevitals", "--config", str(config_path), "--db", str(tmp_path / "state.db"), "--headless"]
    error = PermanentSyncError("Garmin rejected the email or password. Run: homevitals --update-password")
    with patch("sys.argv", argv), patch("homevitals.sync.sync_user", side_effect=error), pytest.raises(SystemExit):
        main()

    captured = capsys.readouterr()
    for pw in FAKE_PASSWORDS:
        assert pw not in captured.out + captured.err + caplog.text


# ---------------------------------------------------------------------------
# The upstream self-update is disabled in the household build (WP11)
# ---------------------------------------------------------------------------


def test_upstream_self_update_is_disabled(tmp_path, capsys):
    from unittest.mock import patch

    from homevitals import __version__
    from homevitals.cli import updater

    assert __version__
    assert updater._self_update_disabled() is True
    with patch("homevitals.cli.shared.DATA_DIR", tmp_path), \
         patch("homevitals.cli.updater.urllib.request.urlopen") as urlopen, \
         patch("homevitals.cli.updater.subprocess.run") as run, \
         patch("homevitals.cli.updater.subprocess.Popen") as popen:
        updater._check_for_updates()
        updater._self_update()
    urlopen.assert_not_called()
    run.assert_not_called()
    popen.assert_not_called()
    assert "doesn't update itself" in capsys.readouterr().out


def test_doctor_never_checks_pypi_in_the_household_build():
    from unittest.mock import patch

    from homevitals.cli import doctor

    rows = []
    with patch("homevitals.cli.updater._latest_pypi_version", side_effect=AssertionError("PyPI contacted")):
        doctor._check_version(lambda *args: rows.append(args))
    assert rows[0][0] == "PASS"
    assert "uv tool upgrade homevitals" in rows[0][2]


# ---------------------------------------------------------------------------
# No password ever appears in config.yaml, logs or the window's text (WP11)
# ---------------------------------------------------------------------------

CLI_LINES = [
    "Syncs completed: Garmin 2.",
    "No new measurements for 2 profiles.",
    "Chris: synced 2 weigh-ins to Garmin.",
    "Jane: nothing new. Garmin failed - Garmin wants an MFA code. Run: homevitals --reauth garmin",
    "Sync failed for: Chris/garmin, Jane. Run with --verbose for details.",
    "homevitals could not start: No garmin password found for user 'Jane'. Run: homevitals --update-password",
    "Another homevitals run is in progress; skipping.",
    "2026-10-01 10:00:00,123 ERROR homevitals.sync: Authentication failed for Jane/garmin: rejected",
]


def test_person_editor_never_writes_passwords_to_config_or_logs(tmp_path, caplog):
    import logging

    from homevitals import gui_logic
    from homevitals.credentials import use_file_store

    caplog.set_level(logging.DEBUG)
    use_file_store()
    config_path = tmp_path / "config.yaml"
    for i, (name, pw) in enumerate(zip(("Chris", "Jane"), FAKE_PASSWORDS, strict=True)):
        gui_logic.connect_garmin(config_path, name, f"{name.lower()}@example.com", pw)
        gui_logic.connect_scale(config_path, name, "scale@example.com", pw, f"adult-{i}-000{i}")

    config_text = config_path.read_text(encoding="utf-8")
    people = gui_logic.read_people(config_path)
    gui_texts = [gui_logic.translate_line(line, people) or "" for line in CLI_LINES]
    gui_texts += [" ".join(gui_logic.person_row(p)) for p in people]
    gui_texts += [gui_logic.account_status(p, service) for p in people for service in ("garmin", "eufy", "omron")]
    gui_texts += [gui_logic.translate_line(f"Chris: failed - login error for {pw}", people) or ""
                  for pw in FAKE_PASSWORDS]
    for pw in FAKE_PASSWORDS:
        leaky = RuntimeError(f"Traceback ... password={pw}")
        gui_texts.append(gui_logic.classify_garmin_error(leaky, "Chris").text)
        gui_texts.append(gui_logic.classify_eufy_error(leaky, "Chris").text)
        gui_texts.append(gui_logic.translate_error(f"keychain could not be read: {pw}"))

    for pw in FAKE_PASSWORDS:
        assert pw not in config_text
        assert pw not in repr(people)
        assert pw not in caplog.text
        for text in gui_texts:
            assert pw not in text


def test_fix_problems_text_never_contains_tracebacks_or_raw_errors():
    from homevitals import gui_logic

    exc = RuntimeError("Traceback (most recent call last): secret-detail")
    for result in (gui_logic.classify_garmin_error(exc, "Chris"), gui_logic.classify_eufy_error(exc, "Chris")):
        assert "Traceback" not in result.text
        assert "secret-detail" not in result.text


# ---------------------------------------------------------------------------
# Part 4: two people never share one OMRON connect account (WP14)
# ---------------------------------------------------------------------------


def test_two_users_sharing_an_omron_account_are_rejected(fixtures, tmp_path):
    import yaml

    from homevitals.config import load_config

    raw = yaml.safe_load(fixtures.text("config_household_omron.yaml"))
    raw["users"][1]["omron"]["email"] = raw["users"][0]["omron"]["email"].upper()
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="both use the OMRON connect account"):
        load_config(path)


# ---------------------------------------------------------------------------
# Part 4: the OMRON connect client (WP15)
# ---------------------------------------------------------------------------

OMRON_PASSWORD = "Falcon-Dune-7731!"
BP_FIXTURE_VALUES = (137, 89, 58, 164, 101, 77, 126, 82, 64, 119, 74, 69, 265, 155, 66, 133, 87, 71,
                     142, 93, 67, 128, 80, 62)


def _omron(country, handler):
    import httpx

    from homevitals.config import OmronConfig
    from homevitals.omron_client import OmronClient

    config = OmronConfig(email="adult-a@example.com", password=OMRON_PASSWORD, country=country)
    return OmronClient(config, transport=httpx.MockTransport(handler))


def test_omron_client_only_talks_to_allowed_hosts():
    import httpx

    from homevitals.omron_client import OmronLoginError

    for country, expected in (
        ("CA", ["vlt-mobile-api.prd.us.ohiomron.com"]),
        ("QA", ["vlt-mobile-api.prd.eu.ohiomron.eu", "oi-api.ohiomron.eu", "vlt-mobile-api.prd.us.ohiomron.com"]),
    ):
        seen = []

        def handler(request, seen=seen):
            seen.append((request.method, request.url.host))
            return httpx.Response(401)

        client = _omron(country, handler)
        with pytest.raises(OmronLoginError):
            client.authenticate()
        client.close()
        assert seen == [("POST", host) for host in expected]


def test_omron_client_never_logs_response_body_or_password(fixtures, caplog):
    import logging
    import re
    from datetime import datetime, timezone

    import httpx

    caplog.set_level(logging.DEBUG)

    def handler(request):
        if request.method == "POST" and request.url.host == "vlt-mobile-api.prd.eu.ohiomron.eu":
            return httpx.Response(401, json=fixtures.json("omron_v2_login_rejected.json"))
        if request.method == "POST":
            return httpx.Response(200, json=fixtures.json("omron_v2_login_ok.json"))
        return httpx.Response(200, json=fixtures.json("omron_v2_sync_bp_adult_a.json"))

    client = _omron("GB", handler)
    client.authenticate()
    readings = client.fetch_readings(datetime(2025, 9, 1, tzinfo=timezone.utc))
    client.close()
    assert readings
    messages = "\n".join(r.getMessage() for r in caplog.records)
    assert OMRON_PASSWORD not in messages
    assert "SENTINEL" not in messages
    assert "fake-omron-access-token" not in messages and "fake-omron-refresh-token" not in messages
    for value in BP_FIXTURE_VALUES:
        assert not re.search(rf"\b{value}\b", messages), value


def test_bp_reading_repr_hides_numbers():
    import re
    from datetime import datetime, timezone

    from homevitals.omron_client import BloodPressureReading

    reading = BloodPressureReading("bp-fake", datetime(2025, 10, 2, 9, tzinfo=timezone.utc), 137, 89, 58,
                                   False, False, False, 1)
    for text in (repr(reading), str(reading), f"{reading!r}", "%s" % (reading,)):
        for value in ("137", "89", "58"):
            assert not re.search(rf"\b{value}\b", text)


# ---------------------------------------------------------------------------
# Part 4: health data reaches Garmin exactly as measured (WP16)
# ---------------------------------------------------------------------------


def test_garmin_bp_upload_sends_values_unchanged():
    from datetime import datetime
    from unittest.mock import MagicMock

    from homevitals.config import GarminConfig
    from homevitals.garmin_client import GarminClient
    from homevitals.omron_client import BloodPressureReading

    reading = BloodPressureReading("bp-fake", datetime.fromisoformat("2025-10-01T13:00:00+03:00"), 137, 89, 58,
                                   False, False, False, 1)
    client = GarminClient(GarminConfig(email="adult-a@example.com", password="pw"))
    client._garmin = MagicMock()
    client.upload_blood_pressure(reading, "Omron M7")
    kwargs = client._garmin.set_blood_pressure.call_args.kwargs
    assert (kwargs["systolic"], kwargs["diastolic"], kwargs["pulse"]) == (137, 89, 58)
    assert all(type(kwargs[k]) is int for k in ("systolic", "diastolic", "pulse"))
    assert kwargs["timestamp"] == "2025-10-01T13:00:00+03:00"


# ---------------------------------------------------------------------------
# Part 4: blood pressure readings only reach their owner's Garmin, unchanged,
# and their numbers never reach a log (WP17)
# ---------------------------------------------------------------------------


def _bp_users(fixtures):
    import yaml

    from homevitals.config import EufyConfig, GarminConfig, OmronConfig, UserConfig

    raw = yaml.safe_load(fixtures.text("config_household_omron.yaml"))
    return [
        UserConfig(
            name=u["name"],
            eufy=EufyConfig(email=u["eufy"]["email"], password="pw", customer_id=u["eufy"]["customer_id"]),
            garmin=GarminConfig(email=u["garmin"]["email"], password="pw"),
            omron=OmronConfig(email=u["omron"]["email"], password=OMRON_PASSWORD, country=u["omron"]["country"],
                              server=u["omron"].get("server")),
        )
        for u in raw["users"]
    ]


def _fake_omron_server(fixtures):
    """A MockTransport handler serving each OMRON account its own fixture, keyed by the login email."""
    import json

    import httpx

    files = {"a": "omron_v2_sync_bp_adult_a.json", "b": "omron_v2_sync_bp_adult_b.json"}

    def handler(request):
        if request.method == "POST":
            who = json.loads(request.content)["emailAddress"].split("@")[0][-1]   # adult-a -> "a"
            return httpx.Response(200, json={"accessToken": f"fake-omron-access-token-{who}",
                                             "refreshToken": "fake-omron-refresh-token"})
        who = request.headers["authorization"].rsplit("-", 1)[1]
        return httpx.Response(200, json=fixtures.json(files[who]))

    return handler


def _run_bp_household(fixtures, tmp_path, garmin_side_effect=None):
    from datetime import datetime, timezone
    from unittest.mock import MagicMock, patch

    import httpx

    from homevitals import bp_sync
    from homevitals.omron_client import OmronClient
    from homevitals.state import SyncState

    handler = _fake_omron_server(fixtures)
    uploads: dict[str, list[tuple]] = {}

    def garmin_factory(cfg):
        client = MagicMock(name=f"GarminClient({cfg.email})")
        client.blood_pressure_instants.return_value = []

        def upload(reading, notes, email=cfg.email):
            if garmin_side_effect:
                garmin_side_effect(reading)
            uploads.setdefault(email, []).append((reading.systolic, reading.diastolic, reading.pulse))
            return {}

        client.upload_blood_pressure.side_effect = upload
        return client

    state = SyncState(tmp_path / "state.db")
    results = {}
    with patch("homevitals.bp_sync.OmronClient", side_effect=lambda cfg: OmronClient(cfg, transport=httpx.MockTransport(handler))), \
         patch("homevitals.bp_sync.GarminClient", side_effect=garmin_factory), \
         patch("homevitals.bp_sync.time.sleep"):
        for user in _bp_users(fixtures):
            results[user.name] = bp_sync.sync_blood_pressure(
                user, state, headless=True, now=datetime(2025, 10, 3, 12, tzinfo=timezone.utc))
    return uploads, results, state


def test_each_person_only_receives_their_own_bp_readings(fixtures, tmp_path):
    uploads, results, state = _run_bp_household(fixtures, tmp_path)
    try:
        assert sorted(uploads["adult-a@example.com"]) == [(119, 74, 69), (133, 87, 71), (137, 89, 58)]
        assert sorted(uploads["adult-b@example.com"]) == [(128, 80, 62), (142, 93, 67)]
        assert set(uploads) == {"adult-a@example.com", "adult-b@example.com"}
        rows = state._conn.execute("SELECT user_name, COUNT(*) FROM sync_log WHERE target = 'garmin_bp' "
                                   "AND response IS NULL GROUP BY user_name").fetchall()
        assert dict(rows) == {"Chris": 3, "Jane": 2}
    finally:
        state.close()


def test_out_of_range_bp_readings_are_skipped_never_adjusted(fixtures, tmp_path):
    from homevitals.bp_sync import SKIPPED_OUT_OF_RANGE_RESPONSE

    uploads, results, state = _run_bp_household(fixtures, tmp_path)
    try:
        everything = [v for values in uploads.values() for triple in values for v in triple]
        for value in (265, 155, 260, 150):
            assert value not in everything
        assert results["Chris"].skipped_out_of_range == 1
        rows = state._conn.execute("SELECT response FROM sync_log WHERE response = ?",
                                   (SKIPPED_OUT_OF_RANGE_RESPONSE,)).fetchall()
        assert len(rows) == 1
    finally:
        state.close()


def test_bp_numbers_never_appear_in_log_output(fixtures, tmp_path, caplog):
    import logging
    import re

    caplog.set_level(logging.DEBUG)
    _, _, state = _run_bp_household(fixtures, tmp_path)
    state.close()
    first = "\n".join(r.getMessage() for r in caplog.records)

    # The library's own range check raising must not leak the values either.
    caplog.clear()

    def refuse(reading):
        raise ValueError(f"systolic must be an int in [70, 260]: {reading.systolic}")

    _, _, state = _run_bp_household(fixtures, tmp_path / "second", garmin_side_effect=refuse)
    state.close()
    second = "\n".join(r.getMessage() for r in caplog.records)

    for messages in (first, second):
        assert "SENTINEL" not in messages
        assert OMRON_PASSWORD not in messages
        for value in BP_FIXTURE_VALUES:
            assert not re.search(rf"\b{value}\b", messages), value
        for seq in range(1001, 1007):
            assert str(seq) not in messages
        assert "bp-" not in messages


def test_cli_run_output_never_contains_bp_numbers(fixtures, tmp_path, capsys, caplog):
    import logging
    import re
    import shutil
    from unittest.mock import MagicMock, patch

    import httpx

    from homevitals.cli.app import main
    from homevitals.credentials import store_password
    from homevitals.omron_client import OmronClient

    caplog.set_level(logging.DEBUG)
    config_path = tmp_path / "config.yaml"
    shutil.copy(fixtures.path("config_household_omron.yaml"), config_path)
    for name in ("Chris", "Jane"):
        for service in ("eufy", "garmin"):
            store_password(f"{name}:{service}", "pw")
        store_password(f"{name}:omron", OMRON_PASSWORD)
    handler = _fake_omron_server(fixtures)
    garmin = MagicMock()
    garmin.blood_pressure_instants.return_value = []
    garmin.upload_blood_pressure.return_value = {}
    argv = ["homevitals", "--config", str(config_path), "--db", str(tmp_path / "state.db"), "--headless",
            "--backfill-days", "4000"]
    with patch("sys.argv", argv), \
         patch("homevitals.sync.sync_user", return_value=({"garmin": 0}, {})), \
         patch("homevitals.bp_sync.OmronClient",
               side_effect=lambda cfg: OmronClient(cfg, transport=httpx.MockTransport(handler))), \
         patch("homevitals.bp_sync.GarminClient", return_value=garmin), \
         patch("homevitals.bp_sync.time.sleep"), \
         pytest.raises(SystemExit):
        main()
    captured = capsys.readouterr()
    assert "Chris: synced 3 blood pressure readings to Garmin." in captured.out
    everything = captured.out + captured.err + "\n".join(r.getMessage() for r in caplog.records)
    assert "SENTINEL" not in everything and OMRON_PASSWORD not in everything
    for value in BP_FIXTURE_VALUES:
        assert not re.search(rf"\b{value}\b", everything), value


def test_person_without_omron_never_constructs_an_omron_client(fixtures, tmp_path):
    import shutil
    from unittest.mock import patch

    from homevitals.cli.app import main
    from homevitals.credentials import store_password

    config_path = tmp_path / "config.yaml"
    shutil.copy(fixtures.path("config_household.yaml"), config_path)
    for name in ("Chris", "Jane"):
        store_password(f"{name}:eufy", "pw")
        store_password(f"{name}:garmin", "pw")
    argv = ["homevitals", "--config", str(config_path), "--db", str(tmp_path / "state.db"), "--headless"]
    with patch("sys.argv", argv), \
         patch("homevitals.sync.sync_user", return_value=({"garmin": 1}, {})) as sync_user, \
         patch("homevitals.bp_sync.OmronClient") as omron, \
         pytest.raises(SystemExit):
        main()
    omron.assert_not_called()
    assert [c.args[0].name for c in sync_user.call_args_list] == ["Chris", "Jane"]


# ---------------------------------------------------------------------------
# Part 4: the OMRON connect password never reaches config, logs or the window (WP19)
# ---------------------------------------------------------------------------

BP_GUI_LINES = [
    "Chris: synced 2 weigh-ins and 1 blood pressure reading to Garmin.",
    "Chris: blood pressure - no new readings. Open the OMRON connect app on the phone so it picks up readings "
    "from the monitor, then sync again.",
    "Jane: blood pressure failed - OMRON connect rejected the login for adult-b@example.com (country QA).",
    "Jane: blood pressure failed - Garmin upload failed (API Error 400)",
    "Syncs completed: Garmin 1. Blood pressure: 2 readings.",
    "Syncs completed: blood pressure 2.",
    "Sync failed for: Chris/garmin_bp. Run with --verbose for details.",
]


def test_omron_password_never_appears_in_config_logs_or_gui_text(tmp_path, caplog):
    import logging

    from homevitals import gui_logic
    from homevitals.credentials import use_file_store
    from homevitals.omron_client import OmronLoginError

    caplog.set_level(logging.DEBUG)
    use_file_store()
    config_path = tmp_path / "config.yaml"
    gui_logic.connect_garmin(config_path, "Chris", "adult-a@example.com", "pw")
    gui_logic.connect_scale(config_path, "Chris", "scale@example.com", "pw", "adult-a-0001")
    gui_logic.connect_omron(config_path, "Chris", "adult-a@example.com", OMRON_PASSWORD, "CA")
    people = gui_logic.read_people(config_path)
    texts = [gui_logic.translate_line(line, people) or "" for line in BP_GUI_LINES]
    texts.append(gui_logic.translate_line(f"Chris: blood pressure failed - {OMRON_PASSWORD}", people) or "")
    texts.append(gui_logic.classify_omron_error(OmronLoginError(f"rejected {OMRON_PASSWORD}"), "Chris").text)
    texts.append(gui_logic.classify_omron_error(RuntimeError(OMRON_PASSWORD), "Chris").text)
    assert OMRON_PASSWORD not in config_path.read_text(encoding="utf-8")
    assert OMRON_PASSWORD not in repr(people)
    assert OMRON_PASSWORD not in caplog.text
    for text in texts:
        assert OMRON_PASSWORD not in text


# ---------------------------------------------------------------------------
# Part 5: a partly set up household keeps every safety rule (WP22)
# ---------------------------------------------------------------------------


def test_partial_household_still_rejects_an_unlinked_scale(fixtures, tmp_path):
    import yaml

    from homevitals.config import load_config

    raw = yaml.safe_load(fixtures.text("config_household_partial.yaml"))
    del raw["users"][2]["eufy"]["customer_id"]          # Sam: a scale with no profile chosen
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match=r"Sam.*customer_id") as exc:
        load_config(path)
    assert "Jane" not in str(exc.value)                  # Jane has no scale: the rule doesn't apply to her


def test_profile_claimed_by_a_garmin_less_person_is_still_a_duplicate(fixtures, tmp_path):
    from datetime import datetime, timezone

    import yaml

    from homevitals import gui_logic
    from homevitals.config import load_config
    from homevitals.eufy_client import EufyProfile

    raw = yaml.safe_load(fixtures.text("config_household_partial.yaml"))
    raw["users"][0]["eufy"]["customer_id"] = "adult-b-0002"     # Chris takes Sam's profile
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="linked to the same Eufy profile"):
        load_config(path)

    people = gui_logic.read_people(fixtures.path("config_household_partial.yaml"))
    profile = EufyProfile("adult-b-0002", datetime(2026, 9, 30, tzinfo=timezone.utc), 61.9)
    (choice,) = gui_logic.profile_choices([profile], people)
    assert choice.linked_to == "Sam"


# ---------------------------------------------------------------------------
# Part 5: the Person window saves one account at a time (WP25, WP27)
# ---------------------------------------------------------------------------


def test_connect_scale_refuses_a_profile_linked_to_someone_else(fixtures, tmp_path):
    import shutil

    from homevitals import gui_logic
    from homevitals.credentials import get_password

    path = tmp_path / "config.yaml"
    shutil.copy(fixtures.path("config_household.yaml"), path)
    before = path.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="same Eufy profile"):
        gui_logic.connect_scale(path, "Jane", "scale@example.com", FAKE_PASSWORDS[1], "adult-a-0001")
    assert path.read_text(encoding="utf-8") == before
    assert get_password("Jane:eufy") is None


def test_connect_garmin_refuses_an_account_used_by_someone_else(fixtures, tmp_path):
    import shutil

    from homevitals import gui_logic
    from homevitals.credentials import get_password

    path = tmp_path / "config.yaml"
    shutil.copy(fixtures.path("config_household.yaml"), path)
    before = path.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="Garmin account"):
        gui_logic.connect_garmin(path, "Sam", "ADULT-A@Example.com", FAKE_PASSWORDS[0])
    assert path.read_text(encoding="utf-8") == before
    assert get_password("Sam:garmin") is None


def test_removing_the_last_person_deletes_legacy_tokens(fixtures, tmp_path, monkeypatch):
    import shutil

    from homevitals import gui_logic
    from homevitals.credentials import get_token, store_token

    monkeypatch.setattr("homevitals.gui_logic.platform_support.uninstall_agent", lambda: None)
    path = tmp_path / "config.yaml"
    shutil.copy(fixtures.path("config_household.yaml"), path)
    store_token("garmin", {"legacy": "garmin"})
    store_token("eufy", {"legacy": "eufy"})
    gui_logic.remove_person(path, "Jane")
    # Someone is still set up: the old one-person tokens stay (migration may still need them).
    assert get_token("garmin") == {"legacy": "garmin"} and get_token("eufy") == {"legacy": "eufy"}
    result = gui_logic.remove_person(path, "Chris")
    assert result.last_person
    # Nobody left: the next person added can never inherit a previous owner's session.
    assert get_token("garmin") is None and get_token("eufy") is None


def _run_partial_household(fixtures, tmp_path):
    """main() on the partial fixture: Chris has everything, Jane only Garmin, Sam only the scale.

    Eufy's HTTP calls and the Garmin client are faked; the per-person loop,
    readiness, profile filtering and state are the real code. Blood pressure
    is stubbed at the step (its own routing has its own tests above).
    """
    import shutil
    from unittest.mock import MagicMock, patch

    from homevitals import bp_sync
    from homevitals import sync as sync_module
    from homevitals.cli.app import main
    from homevitals.credentials import store_password
    from homevitals.eufy_client import EufyClient

    config_path = tmp_path / "config.yaml"
    shutil.copy(fixtures.path("config_household_partial.yaml"), config_path)
    for key in ("Chris:eufy", "Chris:garmin", "Chris:omron", "Jane:garmin", "Sam:eufy"):
        store_password(key, "pw")
    records = fixtures.json("eufy_device_data_household.json")["data"]
    garmin_built: list[str] = []
    uploads: dict[str, list[float]] = {}
    omron_built: list[str] = []
    bp_ran: list[str] = []

    def garmin_factory(cfg):
        garmin_built.append(cfg.email)
        client = MagicMock(name=f"GarminClient({cfg.email})")
        client.has_weight_on_date.return_value = False
        client.upload_body_composition.side_effect = (
            lambda body, email=cfg.email: uploads.setdefault(email, []).append(round(body.weight, 1))
        )
        return client

    def bp_step(user, state, **kwargs):
        bp_ran.append(user.name)
        return bp_sync.BpSyncResult()

    real_sync_user = sync_module.sync_user
    # A long first look-back, so the fixture's weigh-ins are in range however old they get.
    argv = ["homevitals", "--config", str(config_path), "--db", str(tmp_path / "state.db"), "--headless",
            "--backfill-days", "3650"]
    with patch("sys.argv", argv), \
         patch.object(EufyClient, "authenticate", lambda self: None), \
         patch.object(EufyClient, "_get_records", lambda self, after: [dict(r) for r in records]), \
         patch.object(EufyClient, "_list_device_ids", lambda self: []), \
         patch("homevitals.garmin_client.GarminClient", side_effect=garmin_factory), \
         patch("homevitals.bp_sync.OmronClient", side_effect=lambda cfg: omron_built.append(cfg.email)), \
         patch("homevitals.bp_sync.sync_blood_pressure", side_effect=bp_step), \
         patch("homevitals.sync.sync_user", wraps=real_sync_user) as sync_user, \
         patch("homevitals.sync.time.sleep"), \
         pytest.raises(SystemExit) as exit_info:
        main()
    return {
        "exit": exit_info.value.code, "garmin_built": garmin_built, "uploads": uploads,
        "omron_built": omron_built, "bp_ran": bp_ran,
        "sync_user_ran": [c.args[0].name for c in sync_user.call_args_list],
        "db": tmp_path / "state.db",
    }


def test_garmin_only_person_never_receives_anyones_data(fixtures, tmp_path):
    import sqlite3

    run = _run_partial_household(fixtures, tmp_path)
    # Jane (Garmin only) and Sam (scale only) run no step at all.
    assert run["sync_user_ran"] == ["Chris"]
    assert run["bp_ran"] == ["Chris"]
    assert "adult-b@example.com" not in run["garmin_built"]
    assert run["omron_built"] == []
    # Every weight sent went to Chris's own Garmin and is one of his.
    assert set(run["uploads"]) == {"adult-a@example.com"}
    assert run["uploads"]["adult-a@example.com"]
    assert set(run["uploads"]["adult-a@example.com"]) <= ADULT_A_WEIGHTS
    with sqlite3.connect(run["db"]) as conn:
        users = {row[0] for row in conn.execute("SELECT DISTINCT user_name FROM sync_log")}
    assert users == {"Chris"}


def test_not_ready_people_are_not_failures(fixtures, tmp_path, capsys):
    from homevitals import platform_support

    run = _run_partial_household(fixtures, tmp_path)
    out = capsys.readouterr().out
    assert run["exit"] in (0, None)
    assert "Jane: not fully set up yet (connect" in out
    assert "Sam: not fully set up yet (connect Garmin" in out
    assert "failed" not in out.lower()
    for call in platform_support.notify.call_args_list:
        assert "failed" not in str(call.args[0]).lower()


# ---------------------------------------------------------------------------
# Weights never reach the log file (owner request, 2026-10-02: people sharing a
# scale may keep their weight to themselves)
# ---------------------------------------------------------------------------


def test_weights_never_appear_in_log_output(fixtures, tmp_path, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    uploads, state = _run_household_sync(fixtures, tmp_path)
    try:
        assert uploads, "the run must have synced something for this test to mean anything"
        for weight in ADULT_A_WEIGHTS | ADULT_B_WEIGHTS | KID_WEIGHTS:
            for text in (f"{weight:.1f}", f"{weight:.2f}", f"{weight * 2.20462:.1f}"):
                assert text not in caplog.text
    finally:
        state.close()
