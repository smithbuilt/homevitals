"""OMRON connect client: blood pressure readings from OMRON's v2 cloud servers.

Written for this project from documented protocol facts only (server
addresses, field names, units and header names; see
docs/omron-protocol-notes.md). No code from the reference projects is
used.

Privacy: blood pressure readings are health data. Nothing here logs a
reading's values, a response body, a request body or a token. Log lines
carry counts, hosts, emails and status codes only.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, TypeVar
from urllib.parse import urlsplit

import httpx

from homevitals.config import OMRON_V1_COUNTRIES, OmronConfig
from homevitals.sync import PermanentSyncError

logger = logging.getLogger(__name__)

T = TypeVar("T")

OMRON_APP = "OCM"
# Shaped like the OMRON connect iOS app (version 8.2.1). The iOS and Alamofire
# version numbers are placeholders; adjust this first if logins fail with a
# correct password and country (spec 24.3).
OMRON_USER_AGENT = "OMRON connect/8.2.1 (com.omronhealthcare.omronconnect; build:24; iOS 17.5.1) Alamofire/5.9.1"
OMRON_SERVERS: dict[str, tuple[str, ...]] = {
    "na": ("https://vlt-mobile-api.prd.us.ohiomron.com/prd",),
    "eu": ("https://vlt-mobile-api.prd.eu.ohiomron.eu/prd", "https://oi-api.ohiomron.eu/app"),
}
OMRON_REGION_BY_COUNTRY: dict[str, str] = {
    **dict.fromkeys(("AW", "BM", "BZ", "CA", "PR", "SR", "TT", "US"), "na"),
    **dict.fromkeys(("AT", "BE", "BG", "CH", "CY", "CZ", "DE", "DK", "EE", "ES", "FI", "FR", "GB", "GR", "HR", "HU",
                     "IE", "IT", "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO", "SE", "SI", "SK"), "eu"),
}
# Every other country (Qatar and the Gulf among them) tries Europe, then North America.
OMRON_FALLBACK_REGIONS = ("eu", "na")
OMRON_MAX_PAGES = 10
OMRON_TIMEOUT_SECONDS = 30.0
# Every v2 request carries a SHA-256 of its body. Kept switchable in case the
# real servers turn out not to want it on GET requests (spec 24.2).
SEND_CHECKSUM_ON_GET = True

__all__ = [
    "OMRON_V1_COUNTRIES", "BloodPressureReading", "OmronApiError", "OmronClient", "OmronError", "OmronLoginError",
    "OmronServerError", "candidate_servers",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class OmronError(RuntimeError):
    """Base for everything this module raises on purpose."""


class OmronLoginError(PermanentSyncError, OmronError):
    """Every candidate host rejected the password login."""


class OmronApiError(PermanentSyncError, OmronError):
    """A request OMRON refused for good, or a reply this client can't read."""


class OmronServerError(OmronError):
    """A 5xx from OMRON. Worth retrying."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class _OmronAuthRejected(OmronError):
    """Internal: 401/403 on a call that carried a token."""

    def __init__(self, status: int, host: str):
        super().__init__(f"HTTP {status} from {host}")
        self.status = status
        self.host = host


# ---------------------------------------------------------------------------
# Readings
# ---------------------------------------------------------------------------


@dataclass(frozen=True, repr=False)
class BloodPressureReading:
    reading_id: str
    timestamp: datetime           # tz-aware, in the offset where the reading was taken
    systolic: int                 # mmHg, exactly as OMRON reported
    diastolic: int
    pulse: int | None             # beats/min, None when OMRON sent none
    irregular_heartbeat: bool
    body_movement: bool
    cuff_wrap_flag: bool          # meaning unconfirmed; never sent to Garmin
    user_number: int | None

    # The values are left out on purpose, so a logged reading can never leak them.
    def __repr__(self) -> str:
        return f"BloodPressureReading(id={self.reading_id!r}, at={self.timestamp.isoformat()})"

    __str__ = __repr__


def candidate_servers(country: str, server: str | None = None) -> list[str]:
    """The hosts to try for a login, in order (never more than three)."""
    if server:
        key = server.strip().lower()
        if key in OMRON_SERVERS:
            return list(OMRON_SERVERS[key])
        return [server.strip().rstrip("/")]
    code = (country or "").strip().upper()
    region = OMRON_REGION_BY_COUNTRY.get(code)
    if region:
        return list(OMRON_SERVERS[region])
    if code in OMRON_V1_COUNTRIES:
        raise ValueError(
            f"OMRON connect accounts created in {code} use OMRON's older servers, which this build does not support."
        )
    hosts: list[str] = []
    for region in OMRON_FALLBACK_REGIONS:
        hosts.extend(OMRON_SERVERS[region])
    return hosts


def checksum(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def data_path(base: str, kind: str) -> str:
    return f"{base}/v2/sync/{kind}" if "/app" in base else f"{base}/sync/{kind}"


def host_of(url: str) -> str:
    return urlsplit(url).netloc


def reading_id(measured_ms: int, systolic: int, diastolic: int, pulse: int | None) -> str:
    """A stable id for one reading: OMRON sends none we can rely on."""
    key = f"{measured_ms}|{systolic}|{diastolic}|{pulse if pulse is not None else ''}".encode("ascii")
    return "bp-" + hashlib.sha256(key).hexdigest()[:32]


def as_exact_int(value: Any) -> int | None:
    """The value as an int only when it is exactly a whole number. Never rounds."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        text = value.strip()
        if text[:1] in "+-":
            sign, digits = text[0], text[1:]
        else:
            sign, digits = "", text
        if digits.isdigit():
            return int(sign + digits)
    return None


def _flag(row: dict, key: str) -> bool:
    return bool(as_exact_int(row.get(key)) or 0)


def parse_v2_reading(row: Any, *, user_number: int | None, fallback_tz: tzinfo) -> BloodPressureReading | None:
    """One row of the sync/bp "data" list as a reading, or None to skip it."""
    if not isinstance(row, dict):
        return None
    if as_exact_int(row.get("deleteFlag")) not in (None, 0):
        return None          # deleted in the app
    if as_exact_int(row.get("isManualEntry")) not in (None, 0):
        return None          # typed into the app, not measured
    if user_number is not None and as_exact_int(row.get("userNumberInDevice")) != user_number:
        return None
    measured_ms = as_exact_int(row.get("measurementDate"))
    systolic = as_exact_int(row.get("systolic"))
    diastolic = as_exact_int(row.get("diastolic"))
    if measured_ms is None or systolic is None or diastolic is None:
        return None
    pulse = as_exact_int(row.get("pulse"))
    offset = as_exact_int(row.get("timeZone"))
    tz = timezone(timedelta(seconds=offset)) if offset is not None and abs(offset) < 86400 else fallback_tz
    return BloodPressureReading(
        reading_id=reading_id(measured_ms, systolic, diastolic, pulse),
        timestamp=datetime.fromtimestamp(measured_ms / 1000, tz=tz),
        systolic=systolic,
        diastolic=diastolic,
        pulse=pulse,
        irregular_heartbeat=_flag(row, "irregularHB"),
        body_movement=_flag(row, "movementDetect"),
        cuff_wrap_flag=_flag(row, "cuffWrapDetect"),
        user_number=as_exact_int(row.get("userNumberInDevice")),
    )


def parse_v2_sync_page(body: Any) -> tuple[list[dict], int | None]:
    """The rows of one sync/bp page and the next page key (the key's name is unconfirmed)."""
    if not isinstance(body, dict):
        return [], None
    rows = body.get("data") if isinstance(body.get("data"), list) else []
    return rows, as_exact_int(body.get("nextpaginationKey", body.get("nextPaginationKey")))


def _reason(exc: BaseException) -> str:
    status = getattr(exc, "status", None)
    return f"HTTP {status}" if status else type(exc).__name__


class _StatusError(OmronError):
    """Internal: a non-auth 4xx during login, carrying the status for the log line."""

    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.status = status


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class OmronClient:
    def __init__(self, config: OmronConfig, transport: httpx.BaseTransport | None = None) -> None:
        self.config = config
        self._client = httpx.Client(
            headers={"User-Agent": OMRON_USER_AGENT, "Accept": "application/json", "Accept-Encoding": "identity"},
            timeout=OMRON_TIMEOUT_SECONDS,
            transport=transport,
        )
        self._base: str | None = None
        self._access: str | None = None
        self._refresh: str | None = None

    # -- public --------------------------------------------------------------

    def authenticate(self, force_login: bool = False) -> None:
        if not force_login and self._load_cached_token():
            return
        self._password_login()

    def check_connection(self) -> None:
        """Prove the login works by reading one day of readings (results discarded)."""
        self.fetch_readings(since=datetime.now(timezone.utc) - timedelta(days=1))

    def fetch_readings(self, since: datetime) -> list[BloodPressureReading]:
        if since.tzinfo is None:
            raise ValueError("since must be timezone-aware")
        if self._base is None:
            self.authenticate()
        since_ms = int(since.timestamp() * 1000)
        rows: list[dict] = []
        key = 0
        for _ in range(OMRON_MAX_PAGES):
            body = self._call_with_reauth(lambda key=key: self._get_page("bp", since_ms, key))
            page_rows, nxt = parse_v2_sync_page(body)
            rows.extend(page_rows)
            if nxt in (None, 0, key):
                break
            key = nxt

        fallback = datetime.now().astimezone().tzinfo or timezone.utc
        readings: list[BloodPressureReading] = []
        seen: set[str] = set()
        for row in rows:
            reading = parse_v2_reading(row, user_number=self.config.user_number, fallback_tz=fallback)
            if reading is None or reading.timestamp < since or reading.reading_id in seen:
                continue
            seen.add(reading.reading_id)
            readings.append(reading)
        readings.sort(key=lambda r: r.timestamp)
        logger.info(
            "Fetched %d blood pressure reading(s) from OMRON connect for %s (%d row(s) skipped: deleted, manual, "
            "other user, repeated or outside the window)", len(readings), self.config.email, len(rows) - len(readings),
        )
        return readings

    def close(self) -> None:
        self._client.close()

    # -- tokens --------------------------------------------------------------

    def _token_name(self) -> str:
        from homevitals.credentials import account_token_name
        return account_token_name("omron", self.config.email)

    def _load_cached_token(self) -> bool:
        from homevitals.credentials import get_token
        blob = get_token(self._token_name())
        if not isinstance(blob, dict):
            return False
        access, refresh, base = blob.get("access_token"), blob.get("refresh_token"), blob.get("base_url")
        if not all(isinstance(v, str) and v for v in (access, refresh, base)):
            return False
        self._base, self._access, self._refresh = base, access, refresh
        logger.info("Using cached OMRON connect session for %s at %s", self.config.email, host_of(base))
        return True

    def _save_token(self) -> None:
        from homevitals.credentials import store_token
        store_token(self._token_name(), {"access_token": self._access, "refresh_token": self._refresh,
                                         "base_url": self._base, "saved_at": time.time()})

    # -- login ---------------------------------------------------------------

    def _password_login(self) -> None:
        hosts = candidate_servers(self.config.country, self.config.server)
        tried = []
        for base in hosts:
            tried.append(host_of(base))
            try:
                access, refresh = self._post_login(base, {
                    "app": OMRON_APP, "country": self.config.country,
                    "emailAddress": self.config.email, "password": self.config.password,
                })
            except (httpx.TransportError, OmronError, ValueError) as e:
                logger.warning("OMRON connect login at %s failed (%s)", host_of(base), _reason(e))
                continue
            self._base, self._access, self._refresh = base, access, refresh
            self._save_token()
            logger.info("Logged in to OMRON connect at %s for %s", host_of(base), self.config.email)
            return
        # The saved session is left alone: a failed login (a mistyped password in a check,
        # every server unreachable for a moment) says nothing about whether it still works.
        raise OmronLoginError(
            f"OMRON connect rejected the login for {self.config.email} (country {self.config.country}). Check the "
            f"email, the password, and the country the account was created in; tried {', '.join(tried)}. "
            "Run: homevitals --update-password"
        )

    def _refresh_token(self) -> bool:
        if not self._refresh or not self._base:
            return False
        try:
            access, refresh = self._post_login(self._base, {
                "app": OMRON_APP, "emailAddress": self.config.email, "refreshToken": self._refresh,
            })
        except Exception as e:
            logger.info("OMRON connect token refresh failed (%s); logging in again", _reason(e))
            return False
        self._access, self._refresh = access, refresh
        self._save_token()
        return True

    def _post_login(self, base: str, body: dict) -> tuple[str, str]:
        data = self._request("POST", f"{base}/login", json_body=body, authed=False)
        access = data.get("accessToken") if isinstance(data, dict) else None
        refresh = data.get("refreshToken") if isinstance(data, dict) else None
        if not (isinstance(access, str) and access and isinstance(refresh, str) and refresh):
            raise OmronError("no access token in the reply")
        return access, refresh

    # -- requests ------------------------------------------------------------

    def _call_with_reauth(self, call: Callable[[], T]) -> T:
        try:
            return call()
        except _OmronAuthRejected:
            pass
        if not self._refresh_token():
            self._password_login()
        try:
            return call()
        except _OmronAuthRejected as e:
            raise OmronApiError(
                f"OMRON connect refused the request (HTTP {e.status}) at {e.host} even after logging in again. "
                "Run: homevitals --update-password"
            ) from None

    def _get_page(self, kind: str, since_ms: int, key: int) -> Any:
        return self._request("GET", data_path(self._base, kind),
                             params={"nextpaginationKey": key, "lastSyncedTime": since_ms, "phoneIdentifier": ""})

    def _request(self, method: str, url: str, *, params: dict | None = None, json_body: dict | None = None,
                 authed: bool = True) -> Any:
        content = json.dumps(json_body, separators=(",", ":")).encode("utf-8") if json_body is not None else b""
        headers: dict[str, str] = {}
        if json_body is not None or SEND_CHECKSUM_ON_GET:
            headers["Checksum"] = checksum(content)
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        if authed and self._access:
            headers["authorization"] = self._access          # no "Bearer" prefix on v2
        resp = self._client.request(method, url, params=params, headers=headers,
                                    content=content if json_body is not None else None)
        status, host = resp.status_code, host_of(url)
        if status in (401, 403) and authed:
            raise _OmronAuthRejected(status, host)
        if 400 <= status < 500:
            if not authed:
                raise _StatusError(status)
            raise OmronApiError(f"OMRON connect returned HTTP {status} from {host} while fetching readings.")
        if status >= 500:
            raise OmronServerError(f"OMRON connect server error HTTP {status} at {host}", status)
        try:
            return resp.json()
        except (ValueError, httpx.DecodingError) as e:
            raise OmronApiError(f"OMRON connect sent a reply this build could not read ({type(e).__name__}).") from None
