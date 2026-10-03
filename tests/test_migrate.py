"""Moving over from the earlier names (homevitals/migrate.py). Fake folders and an in-memory keyring only."""
from __future__ import annotations

import json
from pathlib import Path

import keyring
import pytest

from homevitals import credentials, migrate
from homevitals.cli import shared


def _old_folder() -> Path:
    old = shared.OLD_DATA_DIR
    old.mkdir(parents=True)
    (old / "config.yaml").write_text("users:\n  - name: Chris\n", encoding="utf-8")
    (old / "state.db").write_bytes(b"sqlite history")
    (old / "sync.log").write_text("old log\n", encoding="utf-8")
    (old / "health-sync-agent.vbs").write_text("old launcher", encoding="utf-8")
    (old / "sync.lock").write_text("held by an old run", encoding="utf-8")
    (old / "vault.lock").write_text("", encoding="utf-8")
    (old / "config.yaml.123.tmp").write_text("half written", encoding="utf-8")
    return old


def test_old_folder_is_copied_once_and_left_as_a_backup():
    old = _old_folder()
    moved = migrate.move_from_old_names()
    new = shared.DATA_DIR
    assert any("copied from" in line for line in moved)
    for name in ("config.yaml", "state.db", "sync.log", "health-sync-agent.vbs"):
        assert (new / name).read_bytes() == (old / name).read_bytes()
    # A stale lock or a half-written file never comes along.
    assert not (new / "sync.lock").exists() and not (new / "config.yaml.123.tmp").exists()
    assert not (new / "vault.lock").exists()
    # The old folder is untouched (a backup, and the original eufy-sync may still use it).
    assert (old / "sync.lock").exists() and (old / "config.yaml").exists()
    # Second start: nothing to do, and the new settings are not overwritten.
    (new / "config.yaml").write_text("users:\n  - name: Changed\n", encoding="utf-8")
    assert migrate.move_from_old_names() == []
    assert "Changed" in (new / "config.yaml").read_text(encoding="utf-8")


def test_nothing_is_copied_when_the_new_folder_already_exists():
    _old_folder()
    shared.DATA_DIR.mkdir(parents=True)
    assert migrate.move_from_old_names() == []
    assert not (shared.DATA_DIR / "config.yaml").exists()


def test_fresh_install_without_an_old_folder_does_nothing():
    assert migrate.move_from_old_names() == []
    assert not shared.DATA_DIR.exists()


@pytest.fixture
def keychain(monkeypatch):
    monkeypatch.setattr(credentials, "_keyring_available", lambda: True)


def test_saved_passwords_are_copied_from_the_old_keychain_entry(keychain):
    vault = {"passwords": {"Chris:garmin": "pw-g", "Chris:eufy": "pw-e"}, "tokens": {"garmin:a@example.com": {"t": 1}}}
    keyring.set_password(migrate.OLD_SERVICE_NAME, "vault", json.dumps(vault))
    moved = migrate.move_from_old_names()
    assert any("saved passwords copied" in line for line in moved)
    assert credentials.get_password("Chris:garmin") == "pw-g"
    assert credentials.get_password("Chris:eufy") == "pw-e"
    assert credentials.get_token("garmin:a@example.com") == {"t": 1}
    # The old entry stays as it was.
    assert json.loads(keyring.get_password(migrate.OLD_SERVICE_NAME, "vault")) == vault


def test_a_large_split_vault_is_copied_whole(keychain):
    big = {"passwords": {f"P{i}:garmin": "x" * 60 for i in range(60)}, "tokens": {}}
    payload = json.dumps(big)
    chunks = [payload[i:i + credentials.CHUNK_LIMIT] for i in range(0, len(payload), credentials.CHUNK_LIMIT)]
    assert len(chunks) > 1
    for i, chunk in enumerate(chunks, start=1):
        keyring.set_password(migrate.OLD_SERVICE_NAME, f"vault:{i}", chunk)
    keyring.set_password(migrate.OLD_SERVICE_NAME, "vault", json.dumps({"__chunks__": len(chunks)}))
    migrate.move_from_old_names()
    assert credentials.get_password("P59:garmin") == "x" * 60


def _write_tagged_vault(service: str, vault: dict, tag: str = "3.0a1b2c3d") -> list[str]:
    """The layout eufy-sync 1.14 and later write: tagged chunks and a checksummed header."""
    import hashlib
    payload = json.dumps(vault)
    chunks = [payload[i:i + credentials.CHUNK_LIMIT] for i in range(0, len(payload), credentials.CHUNK_LIMIT)]
    for i, chunk in enumerate(chunks, start=1):
        keyring.set_password(service, f"vault:{tag}:{i}", chunk)
    header = {"__vault__": {"gen": 3, "tag": tag, "chunks": len(chunks),
                            "sha256": hashlib.sha256(payload.encode()).hexdigest()}}
    keyring.set_password(service, "vault", json.dumps(header))
    return chunks


def test_a_vault_from_eufy_sync_1_14_or_later_is_copied_whole(keychain):
    big = {"passwords": {f"P{i}:garmin": "y" * 60 for i in range(60)}, "tokens": {"garmin:a@example.com": {"t": 2}}}
    assert len(_write_tagged_vault(migrate.OLD_SERVICE_NAME, big)) > 1
    moved = migrate.move_from_old_names()
    assert any("saved passwords copied" in line for line in moved)
    assert credentials.get_password("P59:garmin") == "y" * 60
    assert credentials.get_token("garmin:a@example.com") == {"t": 2}
    # The old entry is left exactly as it was, so the original eufy-sync keeps working.
    assert "__vault__" in json.loads(keyring.get_password(migrate.OLD_SERVICE_NAME, "vault"))
    assert keyring.get_password(migrate.OLD_SERVICE_NAME, "vault:3.0a1b2c3d:1") is not None


def test_a_damaged_old_vault_copies_nothing_and_never_stops_the_program(keychain):
    big = {"passwords": {f"P{i}:garmin": "z" * 60 for i in range(60)}, "tokens": {}}
    _write_tagged_vault(migrate.OLD_SERVICE_NAME, big)
    keyring.set_password(migrate.OLD_SERVICE_NAME, "vault:3.0a1b2c3d:1", "tampered")
    assert migrate.move_from_old_names() == []
    assert credentials.get_password("P0:garmin") is None


def test_existing_homevitals_passwords_are_never_overwritten(keychain):
    credentials.store_password("Chris:garmin", "new-pw")
    keyring.set_password(migrate.OLD_SERVICE_NAME, "vault",
                         json.dumps({"passwords": {"Chris:garmin": "old-pw"}, "tokens": {}}))
    assert migrate.move_from_old_names() == []
    assert credentials.get_password("Chris:garmin") == "new-pw"


def test_a_failed_copy_never_stops_the_program(monkeypatch):
    _old_folder()
    monkeypatch.setattr(migrate.shutil, "copytree", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")))
    monkeypatch.setattr(credentials, "copy_vault_from_service",
                        lambda old: (_ for _ in ()).throw(RuntimeError("keychain locked")))
    assert migrate.move_from_old_names() == []


def test_window_moves_things_before_it_creates_its_log_file():
    import inspect

    from homevitals import gui
    source = inspect.getsource(gui.main)
    assert source.index("move_from_old_names()") < source.index("configure_gui_logging()")


def test_command_line_moves_things_before_anything_else(monkeypatch):
    from homevitals.cli.app import main
    _old_folder()
    monkeypatch.setattr("sys.argv", ["homevitals", "--status"])
    with pytest.raises(SystemExit):
        main()
    assert (shared.DATA_DIR / "config.yaml").exists()


@pytest.mark.parametrize("flag", ["--version", "-V", "--help", "-h"])
def test_version_and_help_never_touch_the_data(monkeypatch, flag):
    from homevitals.cli.app import main
    _old_folder()
    monkeypatch.setattr("sys.argv", ["homevitals", flag])
    with pytest.raises(SystemExit):
        main()
    assert not shared.DATA_DIR.exists()


def test_tests_never_see_the_real_old_folder(tmp_path):
    # conftest points both folders into this test's own temporary folder.
    assert tmp_path in shared.OLD_DATA_DIR.parents and shared.OLD_DATA_DIR.name == ".garmin-sync"
    assert tmp_path in shared.DATA_DIR.parents and shared.DATA_DIR.name == ".homevitals"
