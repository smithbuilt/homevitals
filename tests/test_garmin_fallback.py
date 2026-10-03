"""The Cloudflare-block fallback, one relogin per run and upload classification in homevitals/garmin_client.py.

Taken from eufy-sync 1.15.0's tests/test_garmin_client.py. No real network: the
library's API session runs on a fake requests adapter and curl_cffi is faked, except
one test that posts a multipart upload to a local server.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
import requests
from garminconnect import Garmin

from homevitals import garmin_client
from homevitals.config import GarminConfig
from homevitals.garmin_client import GarminClient
from homevitals.transform import GarminBodyComposition


def _client_with_fake_garmin(fake_garmin):
    client = GarminClient(GarminConfig(email="g@example.com", password="pw"))
    client._garmin = fake_garmin
    return client


def test_a_successful_relogin_is_not_repeated_when_the_api_keeps_refusing():
    # Cloudflare 403s on the API while SSO logins succeed: the first call
    # relogs in, and every later call must fail with its own error instead
    # of running a full login each time. The upload, having outlived both the
    # relogin and the fingerprint retry, is classified as permanent so _retry
    # does not ask again seconds later.
    from garminconnect import GarminConnectConnectionError

    from homevitals.sync import PermanentSyncError

    blocked = GarminConnectConnectionError("API Error 403 - ")
    stale = MagicMock()
    stale.get_body_composition.side_effect = blocked
    fresh = MagicMock()
    fresh.get_body_composition.side_effect = blocked
    fresh.get_daily_weigh_ins.side_effect = blocked
    fresh.add_body_composition.side_effect = blocked
    client = _client_with_fake_garmin(stale)
    client._allow_interactive = False
    bc = GarminBodyComposition(timestamp="2026-06-10T08:00:00+00:00", weight=86.2)

    with patch.object(client._auth, "silent_reauth", return_value=fresh) as reauth:
        assert client.has_weight_on_date(datetime(2026, 6, 10, tzinfo=timezone.utc)) is False
        with pytest.raises(GarminConnectConnectionError, match="403"):
            client.check_connection()
        with pytest.raises(PermanentSyncError, match="403"):
            client.upload_body_composition(bc)

    reauth.assert_called_once()


UPLOADED_AT = datetime(2026, 7, 9, 7, 0, tzinfo=timezone.utc)


DATE_STR = UPLOADED_AT.astimezone().strftime("%Y-%m-%d")


def _millis(dt: datetime) -> int:
    """Garmin reports weigh-in timestamps as epoch milliseconds."""
    return int(dt.timestamp() * 1000)


def _weigh_ins(*entries):
    fake = MagicMock()
    fake.get_daily_weigh_ins.return_value = {"dateWeightList": list(entries)}
    return fake


def test_failed_relogin_in_duplicate_check_is_not_retried_by_the_upload():
    # The duplicate check fails open after a relogin that wants MFA. The
    # upload that follows must report that same failure, with its fix-it hint,
    # rather than try a second login (another MFA demand, more 429 risk).
    from garminconnect import GarminConnectConnectionError

    from homevitals.sync import PermanentSyncError
    dead = MagicMock()
    dead.get_body_composition.side_effect = GarminConnectConnectionError("API Error 403")
    dead.add_body_composition.side_effect = GarminConnectConnectionError("API Error 403")
    client = _client_with_fake_garmin(dead)
    client._allow_interactive = False
    failure = PermanentSyncError("Garmin wants an MFA code. Run: homevitals --reauth garmin")
    bc = GarminBodyComposition(timestamp="2026-06-10T08:00:00+00:00", weight=80.0)
    with patch.object(client._auth, "silent_reauth", side_effect=failure) as reauth:
        assert client.has_weight_on_date(datetime(2026, 6, 10, tzinfo=timezone.utc)) is False
        with pytest.raises(PermanentSyncError) as exc:
            client.upload_body_composition(bc)
    reauth.assert_called_once()
    assert exc.value is failure
    assert "--reauth garmin" in str(exc.value)


def test_failed_relogin_in_delete_is_not_retried_by_the_upload():
    from garminconnect import GarminConnectAuthenticationError

    from homevitals.sync import PermanentSyncError
    dead = MagicMock()
    dead.get_daily_weigh_ins.side_effect = GarminConnectAuthenticationError("dead")
    dead.add_body_composition.side_effect = GarminConnectAuthenticationError("dead")
    client = _client_with_fake_garmin(dead)
    client._allow_interactive = True
    failure = PermanentSyncError("Garmin login cancelled")
    bc = GarminBodyComposition(timestamp="2026-06-10T08:00:00+00:00", weight=80.0)
    with patch.object(client._auth, "force_reauth", side_effect=failure) as reauth:
        assert client.delete_weight_entry(UPLOADED_AT, 85.0) is False
        with pytest.raises(PermanentSyncError):
            client.upload_body_composition(bc)
    reauth.assert_called_once()


def test_old_session_still_serves_calls_after_a_failed_relogin():
    # A Cloudflare 403 reads like a dead session but may pass. After the
    # relogin fails, the old session is kept, and a later call that goes
    # through is not blocked by the remembered failure.
    from garminconnect import GarminConnectConnectionError

    from homevitals.sync import PermanentSyncError
    session = MagicMock()
    session.get_body_composition.side_effect = GarminConnectConnectionError("API Error 403")
    session.add_body_composition.return_value = {"ok": True}
    client = _client_with_fake_garmin(session)
    client._allow_interactive = False
    bc = GarminBodyComposition(timestamp="2026-06-10T08:00:00+00:00", weight=80.0)
    with patch.object(client._auth, "silent_reauth", side_effect=PermanentSyncError("mfa")):
        assert client.has_weight_on_date(datetime(2026, 6, 10, tzinfo=timezone.utc)) is False
        assert client.upload_body_composition(bc) == {"ok": True}
    assert client._garmin is session


TOKEN = "token-abc"


CF_PAGE = (
    403,
    {"Content-Type": "text/html; charset=UTF-8", "Server": "cloudflare", "CF-RAY": "8f1-EWR"},
    b"<!DOCTYPE html><html><head><title>Just a moment...</title></head>"
    b"<body><script src='/cdn-cgi/challenge-platform/h/b/orchestrate'></script></body></html>",
)


CF_MITIGATED = (403, {"Content-Type": "text/html", "cf-mitigated": "challenge"}, b"<html></html>")


# Issue #444's block: the same JSON a refused token gets, behind the same
# Cloudflare headers every API answer carries.
JSON_403 = (
    403,
    {"Content-Type": "application/json", "Server": "cloudflare", "CF-RAY": "8f1-EWR"},
    b'{"message":"HTTP 403 Forbidden","error":"ForbiddenException"}',
)


# A gateway error page in Cloudflare's livery. It says nothing about whether
# Garmin received the request.
CF_502 = (
    502,
    {"Content-Type": "text/html; charset=UTF-8", "Server": "cloudflare", "CF-RAY": "8f1-EWR"},
    b"<html><head><title>502 Bad gateway</title></head><body><div id='cf-error-details'>"
    b"<span>Cloudflare Ray ID: <strong>8f1</strong></span></div></body></html>",
)


def _ok(body: dict, status: int = 200):
    return (status, {"Content-Type": "application/json"}, json.dumps(body).encode())


class _FakeAdapter(requests.adapters.BaseAdapter):
    """Answers the library's requests session from a script of responses."""

    def __init__(self, responses):
        super().__init__()
        self.responses = list(responses)
        self.sent = []

    def send(self, request, **kwargs):
        self.sent.append(request)
        status, headers, body = self.responses.pop(0)
        resp = requests.Response()
        resp.status_code = status
        resp.headers.update(headers)
        resp._content = body
        resp.url = request.url
        resp.request = request
        return resp

    def close(self):
        pass


class _FakeCffiResponse:
    def __init__(self, status, headers, body):
        self.status_code = status
        self.headers = headers
        self.content = body
        self.text = body.decode()

    def json(self):
        return json.loads(self.content)


class _FakeCffi:
    """Stands in for curl_cffi sessions; records what each fallback sent."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def session(self):
        fake = self

        class _Session:
            def request(self, method, url, headers=None, **kwargs):
                fake.calls.append({"method": method, "url": url, "headers": headers, **kwargs})
                return _FakeCffiResponse(*fake.responses.pop(0))

            def close(self):
                pass

        return _Session()


def _real_garmin(responses):
    garmin = Garmin("g@example.com", "pw", retry_attempts=0)
    garmin.client.di_token = TOKEN
    adapter = _FakeAdapter(responses)
    garmin.client._api_session.mount("https://", adapter)
    return garmin, adapter


def _client_on(garmin, interactive=False):
    client = _client_with_fake_garmin(garmin)
    client._allow_interactive = interactive
    client._watch_responses(garmin)
    return client


BC = GarminBodyComposition(timestamp="2026-06-10T08:00:00+00:00", weight=86.2, percent_fat=18.5)


def test_cloudflare_page_on_upload_retries_through_curl_cffi_without_relogin():
    garmin, adapter = _real_garmin([CF_PAGE])
    original_session = garmin.client._api_session
    client = _client_on(garmin)
    cffi = _FakeCffi([_ok({"detailedImportResult": {"successes": [], "failures": []}}, status=202)])

    with patch.object(garmin_client, "_new_impersonating_session", cffi.session), \
            patch.object(client._auth, "silent_reauth") as reauth:
        result = client.upload_body_composition(BC)

    reauth.assert_not_called()   # the token was never the problem
    assert result == {"detailedImportResult": {"successes": [], "failures": []}}
    assert len(adapter.sent) == 1 and len(cffi.calls) == 1
    call = cffi.calls[0]
    # Same endpoint, same token, and the FIT goes as a curl_cffi multipart
    # body because curl_cffi refuses files=.
    assert call["method"] == "POST"
    assert call["url"] == "https://connectapi.garmin.com/upload-service/upload"
    assert call["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert "files" not in call and call["multipart"] is not None
    assert garmin.client._api_session is original_session   # swapped back


def test_json_403_on_a_read_tries_the_fingerprint_before_any_relogin():
    # Issue #444: the block is a JSON 403. The fingerprint retry is one
    # request; a relogin is a login, a 429 risk, and on that network its own
    # token check fails the same way.
    garmin, _ = _real_garmin([JSON_403])
    client = _client_on(garmin)
    cffi = _FakeCffi([_ok({"dateWeightList": [{"weight": 86000}]})])

    with patch.object(garmin_client, "_new_impersonating_session", cffi.session), \
            patch.object(client._auth, "silent_reauth") as reauth:
        assert client.has_weight_on_date(datetime(2026, 6, 10, tzinfo=timezone.utc)) is True

    reauth.assert_not_called()
    call = cffi.calls[0]
    assert call["method"] == "GET"
    assert call["url"] == "https://connectapi.garmin.com/weight-service/weight/dateRange"
    assert call["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert set(call["params"]) == {"startDate", "endDate"}


def test_once_the_fingerprint_works_later_calls_skip_plain_requests():
    # The block belongs to the network, so after one fingerprint success the
    # rest of the run goes straight through curl_cffi. A new client (the next
    # run) starts on plain requests again.
    held = {"samplePk": 1, "weight": 86200.0, "timestampGMT": _millis(BC_INSTANT)}
    garmin, adapter = _real_garmin([JSON_403])
    client = _client_on(garmin)
    cffi = _FakeCffi([
        _ok({"dateWeightList": []}),
        _ok({"dateWeightList": [held]}),
        _ok({}),
        _ok({"detailedImportResult": {}}, status=202),
    ])
    original_session = garmin.client._api_session

    with patch.object(garmin_client, "_new_impersonating_session", cffi.session), \
            patch.object(client._auth, "silent_reauth") as reauth:
        assert client.has_weight_on_date(datetime(2026, 6, 10, tzinfo=timezone.utc)) is False
        assert client.delete_weight_entry(BC_INSTANT, 86.2) is True
        client.upload_body_composition(BC)

    reauth.assert_not_called()
    assert len(adapter.sent) == 1   # only the first, refused, plain request
    assert [c["method"] for c in cffi.calls] == ["GET", "GET", "DELETE", "POST"]
    assert garmin.client._api_session is original_session

    next_run, next_adapter = _real_garmin([_ok({"dateWeightList": []})])
    fresh_client = _client_on(next_run)
    with patch.object(garmin_client, "_new_impersonating_session", cffi.session):
        fresh_client.has_weight_on_date(datetime(2026, 6, 10, tzinfo=timezone.utc))
    assert len(next_adapter.sent) == 1 and len(cffi.calls) == 4


def test_a_failed_fingerprint_retry_does_not_make_it_sticky():
    garmin, adapter = _real_garmin([JSON_403, _ok({"dateWeightList": []})])
    client = _client_on(garmin)
    client._reauth_attempted = True   # isolate the fallback from the relogin
    cffi = _FakeCffi([JSON_403])
    with patch.object(garmin_client, "_new_impersonating_session", cffi.session):
        assert client.has_weight_on_date(datetime(2026, 6, 10, tzinfo=timezone.utc)) is False
        assert client.has_weight_on_date(datetime(2026, 6, 10, tzinfo=timezone.utc)) is False
    assert len(adapter.sent) == 2 and len(cffi.calls) == 1


def test_a_cloudflare_block_that_survives_the_fingerprint_never_relogs_in():
    from homevitals.sync import PermanentSyncError

    garmin, _ = _real_garmin([CF_PAGE])
    client = _client_on(garmin)
    cffi = _FakeCffi([CF_MITIGATED])

    with patch.object(garmin_client, "_new_impersonating_session", cffi.session), \
            patch.object(client._auth, "silent_reauth") as reauth:
        with pytest.raises(PermanentSyncError, match="Cloudflare"):
            client.upload_body_composition(BC)

    reauth.assert_not_called()
    assert len(cffi.calls) == 1   # one fallback per call, not a loop


def test_a_json_403_that_survives_the_fingerprint_relogs_in_once():
    # Both transports refuse the token, so it may really be dead: the run's one
    # relogin happens, and the fresh session gets its own fingerprint retry.
    stale, _ = _real_garmin([JSON_403])
    fresh, fresh_adapter = _real_garmin([JSON_403])
    client = _client_on(stale)
    cffi = _FakeCffi([JSON_403, _ok({"dateWeightList": []})])

    with patch.object(garmin_client, "_new_impersonating_session", cffi.session), \
            patch.object(client._auth, "silent_reauth", return_value=fresh) as reauth:
        assert client.has_weight_on_date(datetime(2026, 6, 10, tzinfo=timezone.utc)) is False

    reauth.assert_called_once()
    assert len(fresh_adapter.sent) == 1 and len(cffi.calls) == 2
    assert client._garmin is fresh


def test_a_401_relogs_in_without_the_fingerprint_retry():
    from garminconnect import GarminConnectConnectionError

    dead = MagicMock()
    dead.get_body_composition.side_effect = GarminConnectConnectionError("API Error 401 - ")
    fresh = MagicMock()
    fresh.get_body_composition.return_value = {"dateWeightList": []}
    client = _client_with_fake_garmin(dead)
    client._allow_interactive = False
    with patch.object(client, "_call_impersonating") as fallback, \
            patch.object(client._auth, "silent_reauth", return_value=fresh):
        client.has_weight_on_date(datetime(2026, 6, 10, tzinfo=timezone.utc))
    fallback.assert_not_called()
    dead.get_body_composition.assert_called_once()


def test_no_fallback_when_the_library_has_no_api_session():
    # A future library without _api_session loses the fallback, not the sync:
    # a 403 goes straight to the relogin as before.
    from garminconnect import GarminConnectConnectionError

    stale = MagicMock()
    stale.client._api_session = None
    stale.get_body_composition.side_effect = GarminConnectConnectionError("API Error 403 - ")
    fresh = MagicMock()
    fresh.get_body_composition.return_value = {"dateWeightList": [{"weight": 1}]}
    client = _client_with_fake_garmin(stale)
    client._allow_interactive = False
    with patch.object(client._auth, "silent_reauth", return_value=fresh) as reauth:
        assert client.has_weight_on_date(datetime(2026, 6, 10, tzinfo=timezone.utc)) is True
    reauth.assert_called_once()
    stale.get_body_composition.assert_called_once()


CONFLICT_409 = (409, {"Content-Type": "application/json"}, json.dumps({
    "detailedImportResult": {"failures": [{"messages": [{"content": "Duplicate Activity."}]}]},
}).encode())


BC_INSTANT = datetime.fromisoformat(BC.timestamp)


def test_upload_409_counts_as_uploaded_once_the_lookup_finds_the_weigh_in():
    held = {"samplePk": 1, "weight": 86200.0, "timestampGMT": _millis(BC_INSTANT)}
    garmin, adapter = _real_garmin([CONFLICT_409, _ok({"dateWeightList": [held]})])
    client = _client_on(garmin)
    with patch.object(client._auth, "silent_reauth") as reauth:
        assert client.upload_body_composition(BC) == {"status": "duplicate"}
    reauth.assert_not_called()
    assert len(adapter.sent) == 2
    assert "/weight-service/weight/dayview/" in adapter.sent[1].url


@pytest.mark.parametrize("entries", [
    [],
    # Right weight, but a manual weigh-in hours away is not our upload.
    [{"samplePk": 1, "weight": 86200.0, "timestampGMT": _millis(BC_INSTANT + timedelta(hours=3))}],
    # Right time, wrong weight.
    [{"samplePk": 1, "weight": 90000.0, "timestampGMT": _millis(BC_INSTANT)}],
])
def test_upload_409_without_the_weigh_in_on_garmin_is_permanent(entries):
    from homevitals.sync import PermanentSyncError, _is_permanent

    garmin, _ = _real_garmin([CONFLICT_409, _ok({"dateWeightList": entries})])
    client = _client_on(garmin)
    with pytest.raises(PermanentSyncError, match="409") as exc:
        client.upload_body_composition(BC)
    assert _is_permanent(exc.value)


@pytest.mark.parametrize("entry", [
    {"samplePk": 1, "weight": 86200.0},
    {"samplePk": 1, "weight": 86200.0, "timestampGMT": None, "date": "2026-06-10"},
    {"samplePk": 1, "weight": 86200.0, "timestampGMT": "08:00"},
])
def test_upload_409_is_not_confirmed_by_an_untimed_entry(entry):
    """Same weight, but no parseable timestamp: it could be any weigh-in that
    day, so it does not prove ours is there. delete_weight_entry still trusts
    a lone untimed match; the 409 confirmation does not."""
    from homevitals.sync import PermanentSyncError

    garmin, _ = _real_garmin([CONFLICT_409, _ok({"dateWeightList": [entry]})])
    client = _client_on(garmin)
    with pytest.raises(PermanentSyncError, match="409"):
        client.upload_body_composition(BC)


@pytest.mark.parametrize("status", [500, 502, 503])
def test_upload_409_lookup_that_keeps_failing_waits_for_the_next_run(status):
    """Only the lookup is repeated, a bounded number of times, and the error
    that escapes is one sync's _retry does not retry in-run."""
    from homevitals.sync import RetryNextRunError, _is_permanent

    failing = (status, {"Content-Type": "application/json"}, b"{}")
    garmin, adapter = _real_garmin([CONFLICT_409] + [failing] * garmin_client._CONFIRM_LOOKUP_ATTEMPTS)
    client = _client_on(garmin)
    cffi = _FakeCffi([])
    with patch.object(garmin_client, "_new_impersonating_session", cffi.session), \
            patch.object(client._auth, "silent_reauth") as reauth, \
            patch.object(garmin_client.time, "sleep") as sleep:
        with pytest.raises(RetryNextRunError) as exc:
            client.upload_body_composition(BC)
    assert not _is_permanent(exc.value)
    reauth.assert_not_called()
    # One POST, then only the GET is repeated; nothing replayed through curl_cffi.
    assert [r.method for r in adapter.sent] == ["POST"] + ["GET"] * garmin_client._CONFIRM_LOOKUP_ATTEMPTS
    assert cffi.calls == []
    assert sleep.call_count == garmin_client._CONFIRM_LOOKUP_ATTEMPTS - 1


def test_upload_409_lookup_that_recovers_confirms_without_resending_the_upload():
    held = {"samplePk": 1, "weight": 86200.0, "timestampGMT": _millis(BC_INSTANT)}
    failing = (503, {"Content-Type": "application/json"}, b"{}")
    garmin, adapter = _real_garmin([CONFLICT_409, failing, _ok({"dateWeightList": [held]})])
    client = _client_on(garmin)
    with patch.object(garmin_client.time, "sleep"):
        assert client.upload_body_composition(BC) == {"status": "duplicate"}
    assert [r.method for r in adapter.sent] == ["POST", "GET", "GET"]


def test_upload_409_lookup_network_failure_waits_for_the_next_run():
    from garminconnect import GarminConnectConnectionError

    from homevitals.sync import RetryNextRunError, _is_permanent

    fake = MagicMock()
    fake.add_body_composition.side_effect = GarminConnectConnectionError("API Error 409 - Duplicate")
    fake.get_daily_weigh_ins.side_effect = GarminConnectConnectionError("Connection error: timed out")
    client = _client_with_fake_garmin(fake)
    with patch.object(garmin_client.time, "sleep"):
        with pytest.raises(RetryNextRunError, match="timed out") as exc:
            client.upload_body_composition(BC)
    assert not _is_permanent(exc.value)
    fake.add_body_composition.assert_called_once()
    assert fake.get_daily_weigh_ins.call_count == garmin_client._CONFIRM_LOOKUP_ATTEMPTS


def test_unconfirmed_409_posts_once_per_sync_run_and_queues_the_measurement(tmp_path):
    """Through sync_user: a lookup that keeps failing produces exactly one
    POST in the run, and the measurement waits in the retry queue."""
    from garminconnect import GarminConnectConnectionError

    from homevitals.config import EufyConfig, GarminConfig, UserConfig
    from homevitals.eufy_client import EufyMeasurement
    from homevitals.state import SyncState
    from homevitals.sync import sync_user

    taken = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=2)
    m = EufyMeasurement(
        measurement_id="m1", customer_id="cust", device_id="dev", timestamp=taken, weight_kg=86.2,
    )
    fake = MagicMock()
    fake.add_body_composition.side_effect = GarminConnectConnectionError("API Error 409 - Duplicate")
    fake.get_daily_weigh_ins.side_effect = GarminConnectConnectionError("Connection error: timed out")
    fake.get_body_composition.return_value = {"dateWeightList": []}
    client = _client_with_fake_garmin(fake)
    client.authenticate = MagicMock()
    eufy = MagicMock()
    eufy.fetch_measurements.return_value = [m]
    user = UserConfig(
        name="default",
        eufy=EufyConfig(email="e@example.com", password="pw"),
        garmin=GarminConfig(email="g@example.com", password="pw"),
    )
    state = SyncState(tmp_path / "s.db")

    with patch("homevitals.sync.EufyClient", return_value=eufy), \
            patch("homevitals.garmin_client.GarminClient", return_value=client), \
            patch("homevitals.sync.time.sleep"), \
            patch.object(garmin_client.time, "sleep"):
        counts, errors = sync_user(user, state, headless=True)

    fake.add_body_composition.assert_called_once()
    assert counts["garmin"] == 0 and "409" in errors["garmin"]
    assert state.waiting_upload_retries("default") == {"garmin": 1}
    state.close()


def test_upload_409_lookup_rate_limit_ends_garmin_for_the_run():
    from garminconnect import GarminConnectTooManyRequestsError

    garmin, adapter = _real_garmin([CONFLICT_409, (429, {"Content-Type": "application/json"}, b"{}")])
    client = _client_on(garmin)
    with pytest.raises(GarminConnectTooManyRequestsError):
        client.upload_body_composition(BC)
    assert [r.method for r in adapter.sent] == ["POST", "GET"]


def test_upload_409_lookup_gets_the_fingerprint_fallback_without_resending_the_upload():
    held = {"samplePk": 1, "weight": 86200.0, "timestampGMT": _millis(BC_INSTANT)}
    garmin, adapter = _real_garmin([CONFLICT_409, JSON_403])
    client = _client_on(garmin)
    cffi = _FakeCffi([_ok({"dateWeightList": [held]})])
    with patch.object(garmin_client, "_new_impersonating_session", cffi.session), \
            patch.object(client._auth, "silent_reauth") as reauth:
        assert client.upload_body_composition(BC) == {"status": "duplicate"}
    reauth.assert_not_called()
    assert [r.method for r in adapter.sent] == ["POST", "GET"]
    assert [c["method"] for c in cffi.calls] == ["GET"]
    assert client._impersonate_always is True


def test_upload_409_lookup_rides_the_sticky_fingerprint_path():
    held = {"samplePk": 1, "weight": 86200.0, "timestampGMT": _millis(BC_INSTANT)}
    garmin, adapter = _real_garmin([])
    client = _client_on(garmin)
    client._impersonate_always = True
    cffi = _FakeCffi([CONFLICT_409, _ok({"dateWeightList": [held]})])
    with patch.object(garmin_client, "_new_impersonating_session", cffi.session):
        assert client.upload_body_composition(BC) == {"status": "duplicate"}
    assert adapter.sent == []
    assert [c["method"] for c in cffi.calls] == ["POST", "GET"]


def test_upload_409_lookup_relogs_in_once_without_resending_the_upload():
    from garminconnect import GarminConnectConnectionError

    held = {"samplePk": 1, "weight": 86200.0, "timestampGMT": _millis(BC_INSTANT)}
    dead = MagicMock()
    dead.add_body_composition.side_effect = GarminConnectConnectionError("API Error 409 - Duplicate")
    dead.get_daily_weigh_ins.side_effect = GarminConnectConnectionError("API Error 401 - ")
    fresh = MagicMock()
    fresh.get_daily_weigh_ins.return_value = {"dateWeightList": [held]}
    client = _client_with_fake_garmin(dead)
    client._allow_interactive = False
    with patch.object(client._auth, "silent_reauth", return_value=fresh) as reauth:
        assert client.upload_body_composition(BC) == {"status": "duplicate"}
    reauth.assert_called_once()
    dead.add_body_composition.assert_called_once()
    fresh.add_body_composition.assert_not_called()


def test_upload_409_lookup_refused_after_the_relogin_is_permanent():
    from garminconnect import GarminConnectConnectionError

    from homevitals.sync import PermanentSyncError

    dead = MagicMock()
    dead.add_body_composition.side_effect = GarminConnectConnectionError("API Error 409 - Duplicate")
    dead.get_daily_weigh_ins.side_effect = GarminConnectConnectionError("API Error 401 - ")
    fresh = MagicMock()
    fresh.get_daily_weigh_ins.side_effect = GarminConnectConnectionError("API Error 401 - ")
    client = _client_with_fake_garmin(dead)
    client._allow_interactive = False
    with patch.object(client._auth, "silent_reauth", return_value=fresh):
        with pytest.raises(PermanentSyncError, match="--reauth garmin"):
            client.upload_body_composition(BC)
    fresh.add_body_composition.assert_not_called()


def test_upload_429_becomes_a_rate_limit_that_sync_does_not_retry():
    from garminconnect import GarminConnectTooManyRequestsError

    from homevitals.sync import _is_permanent

    garmin, adapter = _real_garmin([(429, {"Content-Type": "application/json", "Retry-After": "120"}, b"{}")])
    client = _client_on(garmin)
    with patch.object(client._auth, "silent_reauth") as reauth:
        with pytest.raises(GarminConnectTooManyRequestsError) as exc:
            client.upload_body_composition(BC)
    reauth.assert_not_called()
    assert len(adapter.sent) == 1
    assert _is_permanent(exc.value)


@pytest.mark.parametrize("status", [400, 404, 413, 422])
def test_upload_other_4xx_is_a_permanent_bad_request(status):
    from homevitals.sync import PermanentSyncError, _is_permanent

    garmin, adapter = _real_garmin([(status, {"Content-Type": "application/json"}, b'{"message":"bad file"}')])
    client = _client_on(garmin)
    with patch.object(client._auth, "silent_reauth") as reauth:
        with pytest.raises(PermanentSyncError, match=str(status)) as exc:
            client.upload_body_composition(BC)
    reauth.assert_not_called()
    assert len(adapter.sent) == 1
    assert _is_permanent(exc.value)


@pytest.mark.parametrize("status", [408, 500, 502, 503])
def test_upload_408_and_5xx_stay_transient_for_retry(status):
    from garminconnect import GarminConnectConnectionError

    from homevitals.sync import _is_permanent

    garmin, _ = _real_garmin([(status, {"Content-Type": "application/json"}, b"{}")])
    client = _client_on(garmin)
    with patch.object(client._auth, "silent_reauth") as reauth:
        with pytest.raises(GarminConnectConnectionError) as exc:
            client.upload_body_composition(BC)
    reauth.assert_not_called()
    assert not _is_permanent(exc.value)


@pytest.mark.parametrize("response", [
    CF_502,
    (504, CF_502[1], CF_502[2].replace(b"502", b"504")),
    (500, {"Content-Type": "text/html"}, b"<html>Just a moment...</html>"),
])
def test_upload_after_a_gateway_error_is_never_replayed_through_curl_cffi(response):
    """Garmin may have stored the upload behind a gateway error, so only the
    ordinary retry policy may send it again, not the fingerprint fallback."""
    from garminconnect import GarminConnectConnectionError

    from homevitals.sync import _is_permanent

    garmin, adapter = _real_garmin([response])
    client = _client_on(garmin)
    cffi = _FakeCffi([])
    with patch.object(garmin_client, "_new_impersonating_session", cffi.session), \
            patch.object(client._auth, "silent_reauth") as reauth:
        with pytest.raises(GarminConnectConnectionError) as exc:
            client.upload_body_composition(BC)
    assert len(adapter.sent) == 1 and cffi.calls == []
    reauth.assert_not_called()
    assert not _is_permanent(exc.value)
    assert client._impersonate_always is False


def test_upload_is_not_replayed_when_the_recorded_answer_contradicts_a_403_message():
    # The recorded response is what Garmin answered; a stray "403" in the
    # error text must not trigger a second POST after a 502.
    from garminconnect import GarminConnectConnectionError

    from homevitals.sync import PermanentSyncError

    fake = MagicMock()
    client = _client_with_fake_garmin(fake)
    client._reauth_attempted = True   # isolate the fallback from the relogin

    def gateway_failure(**kwargs):
        client._last_response.record(_FakeCffiResponse(*CF_502))
        raise GarminConnectConnectionError("API Error 403 - proxied")

    fake.add_body_composition.side_effect = gateway_failure
    with patch.object(client, "_call_impersonating") as fallback, \
            patch.object(client, "_can_impersonate", return_value=True), \
            patch.object(client._auth, "silent_reauth") as reauth:
        with pytest.raises(PermanentSyncError):
            client.upload_body_composition(BC)
    fallback.assert_not_called()
    reauth.assert_not_called()
    fake.add_body_composition.assert_called_once()


def test_upload_network_failure_stays_transient():
    from garminconnect import GarminConnectConnectionError

    fake = MagicMock()
    fake.add_body_composition.side_effect = GarminConnectConnectionError("Connection error: timed out")
    client = _client_with_fake_garmin(fake)
    with pytest.raises(GarminConnectConnectionError, match="timed out"):
        client.upload_body_composition(BC)


def test_upload_401_after_a_successful_relogin_is_permanent():
    from garminconnect import GarminConnectConnectionError

    from homevitals.sync import PermanentSyncError

    dead = MagicMock()
    dead.add_body_composition.side_effect = GarminConnectConnectionError("API Error 401 - ")
    fresh = MagicMock()
    fresh.add_body_composition.side_effect = GarminConnectConnectionError("API Error 401 - ")
    client = _client_with_fake_garmin(dead)
    client._allow_interactive = False
    with patch.object(client._auth, "silent_reauth", return_value=fresh) as reauth:
        with pytest.raises(PermanentSyncError, match="--reauth garmin"):
            client.upload_body_composition(BC)
    reauth.assert_called_once()
    fresh.add_body_composition.assert_called_once()


@pytest.mark.parametrize("message, expected", [
    ("API Error 403 - ", 403),
    ("API Error 409 - Duplicate Activity.", 409),
    ("API call client error (403): API Error 403", 403),
    ("Connection error: timed out", None),
    ("weight 4031 kg", None),
])
def test_status_code_reads_the_library_messages(message, expected):
    from garminconnect import GarminConnectConnectionError
    assert garmin_client._status_code(GarminConnectConnectionError(message)) == expected


@pytest.mark.parametrize("response, blocked", [
    (CF_PAGE, True),
    (CF_MITIGATED, True),
    (JSON_403, False),   # Cloudflare headers alone prove nothing
    ((403, {"Content-Type": "text/html"}, b"<html>Forbidden</html>"), False),
    ((200, {"Content-Type": "text/html"}, b"Just a moment"), False),
    (CF_502, False),   # branded gateway page: Garmin may have the request
    ((504, {"Content-Type": "text/html", "cf-mitigated": "challenge"}, b"<html></html>"), False),
    ((503, {"Content-Type": "text/html"}, b"<title>Just a moment...</title>"), True),
    ((429, {"Content-Type": "text/html"}, b"<div id='cf-chl-widget'></div>"), True),
    ((429, {"Content-Type": "text/html"}, b"<div id='cf-error-details'>Cloudflare Ray ID</div>"), False),
    ((403, {"Content-Type": "text/html"}, b"<title>Attention Required! | Cloudflare</title>"), True),
    ((403, {"Content-Type": "text/html"}, b"<div id='cf-error-details'>Cloudflare Ray ID: 8f1</div>"), False),
])
def test_cloudflare_block_detection(response, blocked):
    recorder = garmin_client._LastResponse()
    recorder.record(_FakeCffiResponse(*response))
    assert recorder.is_cloudflare_block() is blocked


def test_watch_responses_installs_its_hook_once():
    garmin, _ = _real_garmin([])
    client = _client_on(garmin)
    client._watch_responses(garmin)
    hooks = garmin.client._api_session.hooks["response"]
    assert hooks.count(client._last_response.hook) == 1


def test_curl_cffi_multipart_upload_reaches_a_local_server_intact(loopback_curl):
    """The maintainer's reason not to use curl_cffi for data calls is that it
    cannot do files=. It cannot, but CurlMime can: send a FIT-sized binary
    through the real fallback transport to a local server and parse it back."""
    import threading
    from email.parser import BytesParser
    from http.server import BaseHTTPRequestHandler, HTTPServer

    received = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received["content_type"] = self.headers["Content-Type"]
            received["auth"] = self.headers["Authorization"]
            received["body"] = self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true}')

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        payload = bytes(range(256)) * 8
        session = garmin_client._ImpersonatingSession(garmin_client._LastResponse())
        resp = session.request(
            "POST", f"http://127.0.0.1:{server.server_port}/upload-service/upload",
            headers={"Authorization": f"Bearer {TOKEN}"},
            files={"file": ("body_composition.fit", payload)},
            timeout=10,
        )
    finally:
        server.shutdown()

    assert resp.status_code == 200 and resp.json() == {"ok": True}
    assert received["auth"] == f"Bearer {TOKEN}"
    message = BytesParser().parsebytes(
        b"Content-Type: " + received["content_type"].encode() + b"\r\n\r\n" + received["body"]
    )
    (part,) = message.get_payload()
    assert part.get_param("name", header="content-disposition") == "file"
    assert part.get_filename() == "body_composition.fit"
    assert part.get_payload(decode=True) == payload


def test_upload_passes_a_failed_relogins_own_error_through_unclassified():
    # A login that failed with an HTTP status is not Garmin refusing the
    # upload, so it must not be dressed up as "refused after a fresh login".
    from garminconnect import GarminConnectConnectionError

    dead = MagicMock()
    dead.add_body_composition.side_effect = GarminConnectConnectionError("API Error 401 - ")
    login_failure = GarminConnectConnectionError("Mobile login failed: HTTP 403")
    client = _client_with_fake_garmin(dead)
    client._allow_interactive = False
    with patch.object(client._auth, "silent_reauth", side_effect=login_failure):
        with pytest.raises(GarminConnectConnectionError) as exc:
            client.upload_body_composition(BC)
    assert exc.value is login_failure


def test_the_loopback_fixture_still_refuses_real_servers(loopback_curl):
    from curl_cffi import requests as cffi_requests

    from tests.conftest import RealNetworkAttempt

    with pytest.raises(RealNetworkAttempt):
        cffi_requests.Session().request("GET", "https://connectapi.garmin.com/")
