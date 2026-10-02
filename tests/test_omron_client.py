"""Tests for our own OMRON connect client (homevitals/omron_client.py).

Every request goes to an httpx.MockTransport handler, so no socket is ever
opened.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from homevitals import omron_client as oc
from homevitals.config import OmronConfig
from homevitals.credentials import get_token, store_token

NA = "https://vlt-mobile-api.prd.us.ohiomron.com/prd"
EU1 = "https://vlt-mobile-api.prd.eu.ohiomron.eu/prd"
EU2 = "https://oi-api.ohiomron.eu/app"
PASSWORD = "Falcon-Dune-7731!"
FIXTURE_VALUES = [137, 89, 58, 164, 101, 77, 126, 82, 64, 119, 74, 69, 265, 155, 66, 133, 87, 71]


def _config(country="CA", server=None, user_number=None, email="adult-a@example.com"):
    return OmronConfig(email=email, password=PASSWORD, country=country, server=server, user_number=user_number)


class FakeOmron:
    """An in-memory OMRON server. responses maps (method, url-without-query) to a list of responses."""

    def __init__(self, responses: dict[tuple[str, str], list[httpx.Response]] | None = None, default=None):
        self.responses = {k: list(v) for k, v in (responses or {}).items()}
        self.default = default
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, f"{request.url.scheme}://{request.url.host}{request.url.path}")
        queue = self.responses.get(key)
        if queue:
            return queue.pop(0) if len(queue) > 1 else queue[0]
        if self.default is not None:
            return self.default(request)
        raise AssertionError(f"unexpected request {key}")

    def client(self, config=None) -> oc.OmronClient:
        return oc.OmronClient(config or _config(), transport=httpx.MockTransport(self))

    @property
    def hosts(self) -> list[str]:
        return [r.url.host for r in self.requests]


def ok(fixtures, name, status=200):
    return httpx.Response(status, json=fixtures.json(name))


def _logged_in_client(fixtures, sync_responses, config=None, base=NA):
    server = FakeOmron({("POST", f"{base}/login"): [ok(fixtures, "omron_v2_login_ok.json")],
                        ("GET", oc.data_path(base, "bp")): sync_responses})
    return server, server.client(config)


SINCE = datetime(2025, 9, 1, tzinfo=timezone.utc)

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_candidate_servers_canada_is_na_only():
    assert oc.candidate_servers("CA") == [NA]
    assert oc.candidate_servers(" ca ") == [NA]


def test_candidate_servers_uk_is_eu_primary_then_secondary():
    assert oc.candidate_servers("GB") == [EU1, EU2]


def test_candidate_servers_qatar_tries_eu_then_na():
    assert oc.candidate_servers("QA") == [EU1, EU2, NA]


def test_candidate_servers_override_region_and_url():
    assert oc.candidate_servers("QA", "na") == [NA]
    assert oc.candidate_servers("CA", "eu") == [EU1, EU2]
    assert oc.candidate_servers("QA", "https://example.invalid/prd/") == ["https://example.invalid/prd"]


def test_candidate_servers_v1_country_raises_without_override():
    with pytest.raises(ValueError, match="older servers"):
        oc.candidate_servers("JP")
    assert oc.candidate_servers("JP", "eu") == [EU1, EU2]


def test_candidate_servers_never_exceeds_three_hosts():
    for country in [*oc.OMRON_REGION_BY_COUNTRY, "QA", "AE", "ZZ"]:
        assert 1 <= len(oc.candidate_servers(country)) <= 3


def test_checksum_is_sha256_hex_of_body_and_of_empty_bytes_for_get():
    assert oc.checksum(b'{"a":1}') == hashlib.sha256(b'{"a":1}').hexdigest()
    assert oc.checksum(b"") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_data_path_adds_v2_only_for_app_hosts():
    assert oc.data_path(NA, "bp") == f"{NA}/sync/bp"
    assert oc.data_path(EU2, "bp") == f"{EU2}/v2/sync/bp"


def test_reading_id_is_stable_and_changes_with_any_value():
    a = oc.reading_id(1759395600000, 137, 89, 58)
    assert a == oc.reading_id(1759395600000, 137, 89, 58)
    assert a.startswith("bp-") and len(a) == 35
    assert a != oc.reading_id(1759395600000, 137, 89, None)
    assert a != oc.reading_id(1759395600001, 137, 89, 58)
    assert a != oc.reading_id(1759395600000, 138, 89, 58)


def test_parse_skips_non_integer_values_without_rounding():
    assert oc.as_exact_int("118.5") is None
    assert oc.as_exact_int(118.5) is None
    assert oc.as_exact_int(118.0) == 118
    assert oc.as_exact_int("118") == 118
    assert oc.as_exact_int(" -14400 ") == -14400
    assert oc.as_exact_int(True) is None
    assert oc.as_exact_int(None) is None
    row = {"measurementDate": 1759395600000, "timeZone": -14400, "systolic": "118.5", "diastolic": 76, "pulse": 61}
    assert oc.parse_v2_reading(row, user_number=None, fallback_tz=timezone.utc) is None
    row["systolic"] = True
    assert oc.parse_v2_reading(row, user_number=None, fallback_tz=timezone.utc) is None


def test_parse_pulse_missing_keeps_reading_with_none():
    row = {"measurementDate": 1759395600000, "timeZone": -14400, "systolic": 118, "diastolic": 76}
    reading = oc.parse_v2_reading(row, user_number=None, fallback_tz=timezone.utc)
    assert reading.pulse is None and reading.systolic == 118


def test_parse_flags_are_booleans(fixtures):
    row = fixtures.json("omron_v2_sync_bp_adult_a.json")["data"][5]
    reading = oc.parse_v2_reading(row, user_number=None, fallback_tz=timezone.utc)
    assert reading.irregular_heartbeat is True and reading.body_movement is True and reading.cuff_wrap_flag is False


def test_reading_repr_and_str_hide_numbers(fixtures):
    row = fixtures.json("omron_v2_sync_bp_adult_a.json")["data"][0]
    reading = oc.parse_v2_reading(row, user_number=None, fallback_tz=timezone.utc)
    for text in (repr(reading), str(reading), f"{reading}"):
        for value in ("137", "89", "58"):
            assert not re.search(rf"\b{value}\b", text.split("at=")[0])
        assert reading.reading_id in text


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


def test_login_posts_expected_body_headers_and_saves_keyed_token(fixtures):
    server = FakeOmron({("POST", f"{NA}/login"): [ok(fixtures, "omron_v2_login_ok.json")]})
    client = server.client()
    client.authenticate()
    (request,) = server.requests
    body = json.loads(request.content)
    assert set(body) == {"app", "country", "emailAddress", "password"}
    assert body == {"app": "OCM", "country": "CA", "emailAddress": "adult-a@example.com", "password": PASSWORD}
    assert request.headers["Checksum"] == hashlib.sha256(request.content).hexdigest()
    assert request.headers["Content-Type"] == "application/json"
    assert request.headers["Accept-Encoding"] == "identity"
    assert request.headers["User-Agent"] == oc.OMRON_USER_AGENT
    assert "authorization" not in request.headers
    token = get_token("omron:adult-a@example.com")
    assert token["access_token"] == "fake-omron-access-token"
    assert token["refresh_token"] == "fake-omron-refresh-token"
    assert token["base_url"] == NA


def test_login_failure_on_only_host_raises_login_error_naming_country_and_password(fixtures):
    server = FakeOmron({("POST", f"{NA}/login"): [ok(fixtures, "omron_v2_login_rejected.json", 401)]})
    with pytest.raises(oc.OmronLoginError) as exc:
        server.client().authenticate()
    message = str(exc.value)
    assert "country CA" in message and "password" in message and "--update-password" in message
    assert "vlt-mobile-api.prd.us.ohiomron.com" in message
    assert "SENTINEL" not in message
    from homevitals.sync import PermanentSyncError, _is_permanent
    assert isinstance(exc.value, PermanentSyncError) and _is_permanent(exc.value)


def test_login_eu_falls_back_to_secondary_host_after_primary_error(fixtures):
    server = FakeOmron({("POST", f"{EU1}/login"): [httpx.Response(500)],
                        ("POST", f"{EU2}/login"): [ok(fixtures, "omron_v2_login_ok.json")]})
    server.client(_config(country="GB")).authenticate()
    assert server.hosts == ["vlt-mobile-api.prd.eu.ohiomron.eu", "oi-api.ohiomron.eu"]
    assert get_token("omron:adult-a@example.com")["base_url"] == EU2


def test_login_qatar_tries_eu_then_na_and_remembers_accepting_host(fixtures):
    server = FakeOmron({("POST", f"{EU1}/login"): [httpx.Response(401)],
                        ("POST", f"{EU2}/login"): [httpx.Response(403)],
                        ("POST", f"{NA}/login"): [ok(fixtures, "omron_v2_login_ok.json")]})
    server.client(_config(country="QA")).authenticate()
    assert server.hosts == ["vlt-mobile-api.prd.eu.ohiomron.eu", "oi-api.ohiomron.eu",
                            "vlt-mobile-api.prd.us.ohiomron.com"]
    assert get_token("omron:adult-a@example.com")["base_url"] == NA


def test_login_stops_after_candidate_list():
    server = FakeOmron(default=lambda r: httpx.Response(401))
    with pytest.raises(oc.OmronLoginError):
        server.client(_config(country="QA")).authenticate()
    assert len(server.requests) == 3
    assert all(r.method == "POST" for r in server.requests)


def test_login_reply_without_access_token_counts_as_failure():
    server = FakeOmron({("POST", f"{NA}/login"): [httpx.Response(200, json={"message": "nope"})]})
    with pytest.raises(oc.OmronLoginError):
        server.client().authenticate()


def test_login_transport_error_moves_to_next_host(fixtures):
    def handler(request):
        if request.url.host == "vlt-mobile-api.prd.eu.ohiomron.eu":
            raise httpx.ConnectError("connection refused")
        return ok(fixtures, "omron_v2_login_ok.json")
    server = FakeOmron(default=handler)
    server.client(_config(country="GB")).authenticate()
    assert get_token("omron:adult-a@example.com")["base_url"] == EU2


def test_login_clears_stale_cached_token_on_total_failure():
    store_token("omron:adult-a@example.com", {"access_token": "old", "refresh_token": "old", "base_url": NA})
    server = FakeOmron(default=lambda r: httpx.Response(401))
    with pytest.raises(oc.OmronLoginError):
        server.client().authenticate(force_login=True)
    assert get_token("omron:adult-a@example.com") is None


def test_authenticate_uses_cached_token_and_its_base_url_without_network(fixtures):
    store_token("omron:adult-a@example.com", {"access_token": "cached", "refresh_token": "r", "base_url": EU2})
    server = FakeOmron({("GET", f"{EU2}/v2/sync/bp"): [ok(fixtures, "omron_v2_sync_bp_empty.json")]})
    client = server.client(_config(country="QA"))
    client.authenticate()
    assert server.requests == []
    client.fetch_readings(SINCE)
    assert server.requests[0].headers["authorization"] == "cached"


def test_authenticate_force_login_ignores_cached_token(fixtures):
    store_token("omron:adult-a@example.com", {"access_token": "cached", "refresh_token": "r", "base_url": NA})
    server = FakeOmron({("POST", f"{NA}/login"): [ok(fixtures, "omron_v2_login_ok.json")]})
    server.client().authenticate(force_login=True)
    assert [r.method for r in server.requests] == ["POST"]


def test_cached_token_missing_fields_is_ignored(fixtures):
    store_token("omron:adult-a@example.com", {"access_token": "cached"})
    server = FakeOmron({("POST", f"{NA}/login"): [ok(fixtures, "omron_v2_login_ok.json")]})
    server.client().authenticate()
    assert len(server.requests) == 1


# ---------------------------------------------------------------------------
# Fetching readings
# ---------------------------------------------------------------------------


def test_fetch_sends_window_in_ms_zero_page_key_and_raw_token(fixtures):
    server, client = _logged_in_client(fixtures, [ok(fixtures, "omron_v2_sync_bp_empty.json")])
    client.authenticate()
    client.fetch_readings(SINCE)
    get = server.requests[1]
    assert get.method == "GET"
    assert get.url.path == "/prd/sync/bp"
    assert get.url.params["lastSyncedTime"] == str(int(SINCE.timestamp() * 1000))
    assert get.url.params["nextpaginationKey"] == "0"
    assert get.url.params["phoneIdentifier"] == ""
    assert get.headers["authorization"] == "fake-omron-access-token"
    assert not get.headers["authorization"].startswith("Bearer")
    assert get.headers["Checksum"] == oc.checksum(b"")


def test_fetch_parses_fixture_skipping_deleted_and_manual_rows(fixtures):
    _, client = _logged_in_client(fixtures, [ok(fixtures, "omron_v2_sync_bp_adult_a.json")])
    client.authenticate()
    readings = client.fetch_readings(SINCE)
    assert [(r.systolic, r.diastolic, r.pulse) for r in readings] == [(133, 87, 71), (137, 89, 58), (119, 74, 69),
                                                                       (265, 155, 66)]


def test_fetch_filters_user_number_when_configured(fixtures):
    _, client = _logged_in_client(fixtures, [ok(fixtures, "omron_v2_sync_bp_adult_a.json")],
                                  config=_config(user_number=1))
    client.authenticate()
    assert [r.systolic for r in client.fetch_readings(SINCE)] == [133, 137, 265]


def test_fetch_keeps_all_slots_by_default(fixtures):
    _, client = _logged_in_client(fixtures, [ok(fixtures, "omron_v2_sync_bp_adult_a.json")])
    client.authenticate()
    assert {r.user_number for r in client.fetch_readings(SINCE)} == {1, 2}


def test_fetch_keeps_each_readings_own_offset(fixtures):
    _, client = _logged_in_client(fixtures, [ok(fixtures, "omron_v2_sync_bp_adult_a.json")])
    client.authenticate()
    by_value = {r.systolic: r for r in client.fetch_readings(SINCE)}
    assert by_value[137].timestamp.utcoffset() == timedelta(hours=-4)
    assert by_value[137].timestamp.isoformat() == "2025-10-02T05:00:00-04:00"
    assert by_value[133].timestamp.utcoffset() == timedelta(hours=3)
    assert by_value[133].timestamp.isoformat() == "2025-10-01T13:00:00+03:00"


@pytest.mark.parametrize("tz_value", [None, "abc", 90000, -86400])
def test_fetch_falls_back_to_machine_timezone_when_offset_missing_or_absurd(tz_value):
    fallback = timezone(timedelta(hours=5))
    row = {"measurementDate": 1759395600000, "systolic": 118, "diastolic": 76, "pulse": 61}
    if tz_value is not None:
        row["timeZone"] = tz_value
    reading = oc.parse_v2_reading(row, user_number=None, fallback_tz=fallback)
    assert reading.timestamp.utcoffset() == timedelta(hours=5)
    assert reading.timestamp == datetime(2025, 10, 2, 9, tzinfo=timezone.utc)


def test_fetch_drops_readings_before_since(fixtures):
    _, client = _logged_in_client(fixtures, [ok(fixtures, "omron_v2_sync_bp_adult_a.json")])
    client.authenticate()
    since = datetime(2025, 10, 2, 0, tzinfo=timezone.utc)
    assert 133 not in [r.systolic for r in client.fetch_readings(since)]


def test_fetch_sorts_oldest_first(fixtures):
    _, client = _logged_in_client(fixtures, [ok(fixtures, "omron_v2_sync_bp_adult_a.json")])
    client.authenticate()
    stamps = [r.timestamp for r in client.fetch_readings(SINCE)]
    assert stamps == sorted(stamps)


def test_fetch_refreshes_token_on_401_then_retries(fixtures):
    server = FakeOmron({
        ("POST", f"{NA}/login"): [ok(fixtures, "omron_v2_login_ok.json"), ok(fixtures, "omron_v2_login_refreshed.json")],
        ("GET", f"{NA}/sync/bp"): [httpx.Response(401), ok(fixtures, "omron_v2_sync_bp_empty.json")],
    })
    client = server.client()
    client.authenticate()
    client.fetch_readings(SINCE)
    methods = [r.method for r in server.requests]
    assert methods == ["POST", "GET", "POST", "GET"]
    refresh_body = json.loads(server.requests[2].content)
    assert refresh_body == {"app": "OCM", "emailAddress": "adult-a@example.com",
                            "refreshToken": "fake-omron-refresh-token"}
    assert server.requests[3].headers["authorization"] == "fake-omron-access-token-2"
    assert get_token("omron:adult-a@example.com")["access_token"] == "fake-omron-access-token-2"


def test_fetch_falls_back_to_password_login_when_refresh_fails(fixtures):
    server = FakeOmron({
        ("POST", f"{NA}/login"): [ok(fixtures, "omron_v2_login_ok.json"), httpx.Response(401),
                                  ok(fixtures, "omron_v2_login_refreshed.json")],
        ("GET", f"{NA}/sync/bp"): [httpx.Response(401), ok(fixtures, "omron_v2_sync_bp_empty.json")],
    })
    client = server.client()
    client.authenticate()
    client.fetch_readings(SINCE)
    bodies = [json.loads(r.content) for r in server.requests if r.method == "POST"]
    assert "refreshToken" in bodies[1]
    assert bodies[2]["password"] == PASSWORD


def test_fetch_gives_up_after_one_reauth(fixtures):
    server = FakeOmron({
        ("POST", f"{NA}/login"): [ok(fixtures, "omron_v2_login_ok.json")],
        ("GET", f"{NA}/sync/bp"): [httpx.Response(403)],
    })
    client = server.client()
    client.authenticate()
    with pytest.raises(oc.OmronApiError, match="even after logging in again"):
        client.fetch_readings(SINCE)
    assert [r.method for r in server.requests].count("GET") == 2


def test_fetch_pages_while_next_key_changes_and_dedupes_repeated_rows(fixtures):
    server, client = _logged_in_client(fixtures, [ok(fixtures, "omron_v2_sync_bp_page1.json"),
                                                  ok(fixtures, "omron_v2_sync_bp_page2.json")])
    client.authenticate()
    readings = client.fetch_readings(SINCE)
    assert [r.systolic for r in readings] == [121, 124]
    gets = [r for r in server.requests if r.method == "GET"]
    assert [g.url.params["nextpaginationKey"] for g in gets] == ["0", "7"]


def test_fetch_stops_at_max_pages(fixtures):
    counter = {"n": 0}

    def handler(request):
        if request.method == "POST":
            return ok(fixtures, "omron_v2_login_ok.json")
        counter["n"] += 1
        return httpx.Response(200, json={"data": [], "nextpaginationKey": counter["n"] + 100})
    server = FakeOmron(default=handler)
    client = server.client()
    client.authenticate()
    client.fetch_readings(SINCE)
    assert counter["n"] == oc.OMRON_MAX_PAGES


def test_fetch_rejects_naive_since(fixtures):
    _, client = _logged_in_client(fixtures, [ok(fixtures, "omron_v2_sync_bp_empty.json")])
    with pytest.raises(ValueError, match="timezone-aware"):
        client.fetch_readings(datetime(2025, 9, 1))


def test_fetch_4xx_is_permanent_error(fixtures):
    from homevitals.sync import PermanentSyncError, _is_permanent
    _, client = _logged_in_client(fixtures, [httpx.Response(400)])
    client.authenticate()
    with pytest.raises(oc.OmronApiError) as exc:
        client.fetch_readings(SINCE)
    assert isinstance(exc.value, PermanentSyncError) and _is_permanent(exc.value)
    assert "HTTP 400" in str(exc.value)


def test_fetch_5xx_is_retryable_server_error(fixtures):
    from homevitals.sync import _is_permanent
    _, client = _logged_in_client(fixtures, [httpx.Response(503)])
    client.authenticate()
    with pytest.raises(oc.OmronServerError) as exc:
        client.fetch_readings(SINCE)
    assert not _is_permanent(exc.value)


def test_fetch_unreadable_body_is_api_error(fixtures):
    _, client = _logged_in_client(fixtures, [httpx.Response(200, content=b"<html>SENTINEL-NEVER-LOG-HTML")])
    client.authenticate()
    with pytest.raises(oc.OmronApiError) as exc:
        client.fetch_readings(SINCE)
    assert "SENTINEL" not in str(exc.value)


def test_fetch_never_logs_numbers_sentinels_or_bodies(fixtures, caplog):
    caplog.set_level(logging.DEBUG)
    _, client = _logged_in_client(fixtures, [ok(fixtures, "omron_v2_sync_bp_adult_a.json")])
    client.authenticate()
    readings = client.fetch_readings(SINCE)
    messages = "\n".join(r.getMessage() for r in caplog.records)
    assert "SENTINEL" not in messages
    assert PASSWORD not in messages
    assert "fake-omron-access-token" not in messages and "fake-omron-refresh-token" not in messages
    for value in FIXTURE_VALUES:
        assert not re.search(rf"\b{value}\b", messages), value
    for reading in readings:
        assert reading.reading_id not in messages


def test_check_connection_fetches_one_day_window(fixtures):
    server, client = _logged_in_client(fixtures, [ok(fixtures, "omron_v2_sync_bp_empty.json")])
    client.authenticate()
    before = datetime.now(timezone.utc) - timedelta(days=1)
    client.check_connection()
    since_ms = int(server.requests[-1].url.params["lastSyncedTime"])
    assert abs(since_ms / 1000 - before.timestamp()) < 60


def test_close_closes_http_client(fixtures):
    _, client = _logged_in_client(fixtures, [ok(fixtures, "omron_v2_sync_bp_empty.json")])
    client.close()
    assert client._client.is_closed


def test_user_number_filter_works_for_monitors_with_more_than_two_users():
    # Fake rows from a four-user monitor: only the chosen user's readings get through.
    rows = [{"measurementDate": 1759395600000 + i * 60000, "timeZone": -14400, "systolic": 110 + i,
             "diastolic": 70, "pulse": 60, "userNumberInDevice": user} for i, user in enumerate((1, 2, 3, 4, 3))]
    for wanted in (1, 2, 3, 4):
        kept = [oc.parse_v2_reading(r, user_number=wanted, fallback_tz=timezone.utc) for r in rows]
        assert {k.user_number for k in kept if k is not None} == {wanted}
    kept = [oc.parse_v2_reading(r, user_number=3, fallback_tz=timezone.utc) for r in rows]
    assert [k.systolic for k in kept if k is not None] == [112, 114]
    # No setting: every reading in the account (one-user monitors, or one person per account).
    assert all(oc.parse_v2_reading(r, user_number=None, fallback_tz=timezone.utc) for r in rows)
