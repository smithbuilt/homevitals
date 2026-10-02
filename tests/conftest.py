"""Shared test fixtures."""
from __future__ import annotations

import ipaddress
import json
import socket
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

FIXTURES_DIR = Path(__file__).parent / "fixtures"


class RealNetworkAttempt(RuntimeError):
    """A test tried to reach a real server. Mock the client instead."""


def _is_loopback(host) -> bool:
    if host in (None, "", "localhost"):
        return True
    if isinstance(host, bytes):
        host = host.decode(errors="replace")
    try:
        return ipaddress.ip_address(str(host).split("%", 1)[0]).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    """Any attempt to reach a non-loopback host fails loudly.

    Two layers: Python sockets (httpx, requests, urllib) and curl_cffi, which
    garminconnect logs in through and which goes through libcurl, not Python
    sockets. Loopback stays allowed (socketpair, local test servers).
    """
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def _check(address):
        if isinstance(address, tuple) and address and not _is_loopback(address[0]):
            raise RealNetworkAttempt(f"test tried to connect to {address[0]!r}")

    def connect(self, address):
        _check(address)
        return real_connect(self, address)

    def connect_ex(self, address):
        _check(address)
        return real_connect_ex(self, address)

    def getaddrinfo(host, *args, **kwargs):
        if not _is_loopback(host):
            raise RealNetworkAttempt(f"test tried to look up {host!r}")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)

    try:
        import curl_cffi
        import curl_cffi.requests as cffi_requests
    except ImportError:
        return

    def blocked(*args, **kwargs):
        raise RealNetworkAttempt("test tried to make a real curl_cffi request")

    async def blocked_async(*args, **kwargs):
        raise RealNetworkAttempt("test tried to make a real curl_cffi request")

    monkeypatch.setattr(cffi_requests.Session, "request", blocked)
    if hasattr(cffi_requests, "AsyncSession"):
        monkeypatch.setattr(cffi_requests.AsyncSession, "request", blocked_async)
    for name in ("request", "get", "post", "put", "delete", "head", "patch", "options"):
        if hasattr(cffi_requests, name):
            monkeypatch.setattr(cffi_requests, name, blocked)
    # Lowest layer, in case something drives libcurl directly.
    monkeypatch.setattr(curl_cffi.Curl, "perform", blocked)


@pytest.fixture(autouse=True)
def _reset_mfa_override(monkeypatch):
    """No test may leak the GUI's Garmin code hook into the next one."""
    monkeypatch.setattr("homevitals.garmin_auth.MFA_PROMPT_OVERRIDE", None, raising=False)


class _FixtureLoader:
    def path(self, name: str) -> Path:
        return FIXTURES_DIR / name

    def text(self, name: str) -> str:
        return self.path(name).read_text(encoding="utf-8")

    def json(self, name: str):
        return json.loads(self.text(name))

    def yaml(self, name: str):
        return yaml.safe_load(self.text(name))


@pytest.fixture
def fixtures() -> _FixtureLoader:
    """Loader for tests/fixtures: .path(name), .text(name), .json(name), .yaml(name)."""
    return _FixtureLoader()


@pytest.fixture(autouse=True)
def _hermetic_machine(tmp_path, monkeypatch):
    """Keep every test off the real machine.

    Redirects CRED_FILE and the shared data-dir paths into tmp_path and
    replaces the keyring read/write/delete functions with an in-memory store,
    so no test can touch ~/.homevitals or the real keychain even if it
    forgets to redirect them itself (one already did, and wrote fake
    passwords into the real credentials file). Tests that install their own
    fakes (fake_keyring, patch("keyring.get_password"), ...) layer over this
    and keep working.
    """
    data_dir = tmp_path / ".homevitals"
    # Code that builds paths from Path.home() at call time (GarminAuth's
    # session.json, EufyClient's eufy_token.json) lands in tmp_path too.
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setattr("homevitals.credentials.CRED_FILE", data_dir / "credentials.json")
    monkeypatch.setattr("homevitals.cli.shared.DATA_DIR", data_dir)
    # The folder of earlier versions (migrate.py reads it): never the real ~/.homevitals.
    monkeypatch.setattr("homevitals.cli.shared.OLD_DATA_DIR", tmp_path / ".garmin-sync")
    monkeypatch.setattr("homevitals.cli.shared.DEFAULT_CONFIG", data_dir / "config.yaml")
    monkeypatch.setattr("homevitals.cli.shared.DEFAULT_DB", data_dir / "state.db")
    monkeypatch.setattr("homevitals.cli.shared.LOG_FILE", data_dir / "sync.log")
    monkeypatch.setattr(
        "homevitals.platform_support.macos.LAUNCH_AGENT_PATH",
        tmp_path / "LaunchAgents" / "com.sturimcode.eufy-garmin-sync.plist",
    )
    # setup.py captures shared.DATA_DIR at import time into this module-level
    # constant, so patching shared.DATA_DIR alone leaves it pointed at the
    # real ~/.homevitals.
    monkeypatch.setattr(
        "homevitals.cli.setup.UPGRADE_NOTICE_FILE", data_dir / ".strava_notice_shown"
    )

    store: dict[tuple[str, str], str] = {}

    def set_password(service, account, password):
        store[(service, account)] = password

    def get_password(service, account):
        return store.get((service, account))

    def delete_password(service, account):
        import keyring
        try:
            del store[(service, account)]
        except KeyError:
            raise keyring.errors.PasswordDeleteError("not found") from None

    monkeypatch.setattr("keyring.set_password", set_password)
    monkeypatch.setattr("keyring.get_password", get_password)
    monkeypatch.setattr("keyring.delete_password", delete_password)


@pytest.fixture(autouse=True)
def _mute_notifications(monkeypatch):
    """Stub the notifier for every test.

    notify shells out to osascript on macOS, so an unmocked call from any code
    path fires a real notification on the machine running the suite. Tests that
    assert on notifications patch homevitals.platform_support.notify themselves;
    that patch layers over this stub and restores it on exit.
    """
    monkeypatch.setattr("homevitals.platform_support.notify", MagicMock())
