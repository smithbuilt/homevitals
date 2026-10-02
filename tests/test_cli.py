from __future__ import annotations

import os
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from homevitals.cli.maintenance import (
    _install_launch_agent,
    _offer_launch_agent,
    _uninstall,
    _uninstall_launch_agent,
)
from homevitals.cli.shared import _write_config
from homevitals.platform_support.macos import LAUNCH_AGENT_LABEL, _generate_plist
from homevitals.prompt import PROMPT_TIMEOUT_SECONDS


@pytest.fixture(autouse=True)
def skip_scheduled_retry_wait(monkeypatch):
    wait = MagicMock()
    monkeypatch.setattr("homevitals.cli.app.sleep", wait)
    return wait


def test_scheduled_network_retry_recovers_after_wake(skip_scheduled_retry_wait):
    from homevitals.cli.app import _sync_with_network_retry

    with patch("homevitals.sync.sync_user", side_effect=[
        RuntimeError("network is unreachable"), ({"garmin": 1}, {}),
    ]) as sync:
        assert _sync_with_network_retry(None, None, headless=True) == ({"garmin": 1}, {})
    assert sync.call_count == 2
    skip_scheduled_retry_wait.assert_called_once_with(60)


def test_retry_preserves_partial_success_when_next_attempt_fails(skip_scheduled_retry_wait):
    from homevitals.cli.app import _sync_with_network_retry

    with patch("homevitals.sync.sync_user", side_effect=[
        ({"garmin": 1}, {"strava": "network is unreachable"}),
        RuntimeError("Garmin wants an MFA code. Run: homevitals --reauth garmin"),
    ]) as sync:
        counts, errors = _sync_with_network_retry(None, None, headless=True)
    assert counts == {"garmin": 1}
    assert "--reauth garmin" in errors["sync"]
    assert sync.call_count == 2
    skip_scheduled_retry_wait.assert_called_once_with(60)


@pytest.fixture
def pin_macos_impl(monkeypatch):
    """Pin the launch-agent dispatch to the macOS implementation.

    maintenance delegates through platform_support._impl(), which resolves the
    active module from platform.system(). On a Windows CI runner these
    macOS-specific tests would otherwise drive the real Windows schtasks path;
    pinning _active (the way test_doctor.py does) keeps them on the macOS module
    regardless of host OS, so the homevitals.platform_support.macos.* patches
    below actually take effect."""
    from homevitals import platform_support
    from homevitals.platform_support import macos
    monkeypatch.setattr(platform_support, "_active", macos)


def test_main_ctrl_c_exits_cleanly(capsys):
    from homevitals.cli import app
    with patch.object(app, "_main", side_effect=KeyboardInterrupt):
        with pytest.raises(SystemExit) as exc_info:
            app.main()
    assert exc_info.value.code == 130
    assert "Cancelled" in capsys.readouterr().out


def test_update_password_path_configures_logging(monkeypatch, tmp_path: Path):
    # --update-password reaches a Garmin login, so main() must configure
    # logging first or garminconnect's per-strategy 429 warnings leak through
    # logging's last-resort handler.
    import sys as _sys

    from homevitals.cli import app, shared
    calls = []
    monkeypatch.setattr(shared, "_configure_logging", lambda v: calls.append(v))
    missing_config = tmp_path / "missing.yaml"
    monkeypatch.setattr(_sys, "argv",
                        ["homevitals", "--update-password", "--config", str(missing_config)])
    with pytest.raises(SystemExit):
        app.main()
    assert calls == [False]


def test_write_config_creates_file_with_restricted_permissions(tmp_path: Path):
    config_path = tmp_path / "subdir" / "config.yaml"
    config = {"users": [{"name": "test"}]}

    _write_config(config_path, config)

    assert config_path.exists()
    # File should be 600 (owner read/write only). Windows reports 666/777
    # regardless of the mode passed, so skip the POSIX-mode check there.
    if os.name != "nt":
        mode = oct(config_path.stat().st_mode)[-3:]
        assert mode == "600"

    # Content should be valid YAML
    with open(config_path) as f:
        loaded = yaml.safe_load(f)
    assert loaded["users"][0]["name"] == "test"


def test_write_config_parent_directory_is_restricted(tmp_path: Path):
    config_path = tmp_path / "secure_dir" / "config.yaml"
    _write_config(config_path, {"test": True})

    # POSIX modes only; Windows does not honor them.
    if os.name != "nt":
        parent_mode = oct(config_path.parent.stat().st_mode)[-3:]
        assert parent_mode == "700"


def test_write_config_overwrites_existing(tmp_path: Path):
    config_path = tmp_path / "config.yaml"

    _write_config(config_path, {"version": 1})
    _write_config(config_path, {"version": 2})

    with open(config_path) as f:
        loaded = yaml.safe_load(f)
    assert loaded["version"] == 2


def test_interrupted_config_write_keeps_previous_file(tmp_path: Path):
    """config.yaml is replaced atomically: a write that dies partway through
    must leave the previous contents intact instead of truncating the file in
    place and stranding the user with an empty config."""
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, {"users": [{"name": "default"}]})
    before = config_path.read_bytes()

    with patch("homevitals.cli.shared.yaml.dump", side_effect=RuntimeError("disk full")), \
         pytest.raises(RuntimeError):
        _write_config(config_path, {"users": [{"name": "replacement"}]})

    assert config_path.read_bytes() == before
    # No partial temp file left behind (the temp name carries the writer pid).
    leftovers = list(config_path.parent.glob(config_path.name + ".*.tmp"))
    assert leftovers == []


# --- Launch Agent tests ---


def test_generate_plist_points_at_given_program():
    plist = _generate_plist("/home/user/.garmin-sync/eufy-sync-agent")
    assert "/home/user/.garmin-sync/eufy-sync-agent" in plist
    # --headless lives in the wrapper script, not the plist, so the registered
    # program's bytes stay stable across updates.
    assert "--headless" not in plist
    assert LAUNCH_AGENT_LABEL in plist
    assert "StartInterval" in plist
    assert "14400" in plist


def test_write_run_script_creates_executable_wrapper(tmp_path):
    from homevitals.platform_support.macos import _write_run_script
    with patch("homevitals.cli.shared.DATA_DIR", tmp_path):
        script = _write_run_script("/home/user/.local/bin/homevitals")
    assert script == tmp_path / "eufy-sync-agent"
    content = script.read_text()
    assert content.startswith("#!/bin/sh")
    assert 'exec "/home/user/.local/bin/homevitals" --headless' in content
    # POSIX modes only; Windows does not honor them.
    if os.name != "nt":
        assert oct(script.stat().st_mode)[-3:] == "755"


def test_write_run_script_rotates_the_log_before_exec(tmp_path):
    # launchd appends to StandardOutPath forever and never rotates, so the
    # wrapper does it: past 1 MB, sync.log moves onto sync.log.1.
    from homevitals.cli import shared
    from homevitals.platform_support.macos import _write_run_script
    with patch("homevitals.cli.shared.DATA_DIR", tmp_path):
        content = _write_run_script("/home/user/.local/bin/homevitals").read_text()

    assert f'log="{shared.LOG_FILE}"' in content
    # BSD stat; this script only ever runs on macOS.
    assert '[ "$(stat -f%z "$log" 2>/dev/null || echo 0)" -gt 1048576 ]' in content
    assert 'mv -f "$log" "$log.1"' in content
    # Rotation happens while nothing new has been written, and the exec is
    # outside the if, so a skipped roll still starts the sync.
    assert content.index("mv -f") < content.index("\nfi\n") < content.index("exec ")


def test_write_run_script_rotation_cannot_stop_the_sync(tmp_path):
    # Every step of the roll is best-effort: an unreadable size falls back to 0
    # (no rotation) and a failed move is swallowed, so neither ends the run
    # before the sync starts.
    from homevitals.platform_support.macos import _write_run_script
    with patch("homevitals.cli.shared.DATA_DIR", tmp_path):
        content = _write_run_script("/home/user/.local/bin/homevitals").read_text()

    assert "|| echo 0" in content
    assert 'mv -f "$log" "$log.1" 2>/dev/null || true' in content
    assert content.rstrip().endswith('exec "/home/user/.local/bin/homevitals" --headless')


def test_write_run_script_is_byte_stable_across_installs(tmp_path):
    # macOS re-announces background items when the registered file changes;
    # a second install with the same binary must not rewrite the script.
    import os

    from homevitals.platform_support.macos import _write_run_script
    with patch("homevitals.cli.shared.DATA_DIR", tmp_path):
        first = _write_run_script("/home/user/.local/bin/homevitals")
        mtime_before = os.stat(first).st_mtime_ns
        second = _write_run_script("/home/user/.local/bin/homevitals")
    assert first == second
    assert os.stat(second).st_mtime_ns == mtime_before
    assert first.read_text() == second.read_text()


def test_generate_plist_contains_log_path():
    # The plist embeds str(shared.LOG_FILE), which conftest points at a tmp
    # path; assert against that actual path rather than a hardcoded
    # forward-slash literal, which would not match Windows separators.
    from homevitals.cli import shared
    plist = _generate_plist("/any/path")
    assert str(shared.LOG_FILE) in plist


@patch("homevitals.platform_support.macos.subprocess.run")
@patch("homevitals.platform_support.macos.shutil.which", return_value="/home/user/.local/bin/homevitals")
@patch("homevitals.platform_support.macos.platform.system", return_value="Darwin")
@patch("homevitals.platform_support.macos.LAUNCH_AGENT_PATH")
def test_install_launch_agent_writes_plist_and_loads(mock_path, mock_system, mock_which, mock_run, tmp_path, pin_macos_impl):
    mock_path.parent.mkdir = MagicMock()
    mock_path.write_text = MagicMock()

    with patch("homevitals.cli.shared.DATA_DIR", tmp_path):
        _install_launch_agent()

    mock_which.assert_called_once_with("homevitals")
    mock_path.write_text.assert_called_once()
    plist_content = mock_path.write_text.call_args[0][0]
    # The agent registers the stable wrapper, never the pipx/uv binary, so
    # updates do not re-trigger the macOS background-activity announcement.
    assert str(tmp_path / "eufy-sync-agent") in plist_content
    assert "/home/user/.local/bin/homevitals" not in plist_content
    wrapper = tmp_path / "eufy-sync-agent"
    assert 'exec "/home/user/.local/bin/homevitals" --headless' in wrapper.read_text()

    # Should call launchctl unload then load
    assert mock_run.call_count == 2
    assert "unload" in mock_run.call_args_list[0][0][0]
    assert "load" in mock_run.call_args_list[1][0][0]


@patch("homevitals.platform_support.macos.subprocess.run")
@patch("homevitals.platform_support.macos.shutil.which", return_value="/home/user/.local/bin/homevitals")
@patch("homevitals.platform_support.macos.platform.system", return_value="Darwin")
@patch("homevitals.platform_support.macos.LAUNCH_AGENT_PATH")
def test_install_launch_agent_removes_legacy_wrapper(mock_path, mock_system, mock_which, mock_run, tmp_path, pin_macos_impl):
    """Re-installing must delete the pre-1.7.17 run-sync.sh wrapper so it does
    not linger as an orphan next to the new eufy-sync-agent script."""
    mock_path.parent.mkdir = MagicMock()
    mock_path.write_text = MagicMock()

    legacy = tmp_path / "run-sync.sh"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("#!/bin/sh\nexec old\n")

    with patch("homevitals.cli.shared.DATA_DIR", tmp_path):
        _install_launch_agent()

    assert not legacy.exists()
    assert (tmp_path / "eufy-sync-agent").exists()


@patch("homevitals.platform_support.macos.platform.system", return_value="Linux")
def test_install_launch_agent_skips_on_linux(mock_system, capsys, pin_macos_impl):
    _install_launch_agent()
    assert "only supported on macOS" in capsys.readouterr().out


@patch("homevitals.platform_support.macos.shutil.which", return_value=None)
@patch("homevitals.platform_support.macos.platform.system", return_value="Darwin")
def test_install_launch_agent_warns_if_binary_not_found(mock_system, mock_which, capsys, pin_macos_impl):
    _install_launch_agent()
    assert "could not find homevitals" in capsys.readouterr().out


@patch("homevitals.platform_support.macos.subprocess.run")
@patch("homevitals.platform_support.macos.LAUNCH_AGENT_PATH")
def test_uninstall_launch_agent_removes_plist(mock_path, mock_run, pin_macos_impl):
    mock_path.exists.return_value = True
    mock_path.unlink = MagicMock()

    _uninstall_launch_agent()

    assert mock_run.call_count == 1
    assert "unload" in mock_run.call_args[0][0]
    mock_path.unlink.assert_called_once()


@patch("homevitals.platform_support.macos.LAUNCH_AGENT_PATH")
def test_uninstall_launch_agent_noop_if_not_installed(mock_path, capsys, pin_macos_impl):
    mock_path.exists.return_value = False

    _uninstall_launch_agent()

    assert "No Launch Agent installed" in capsys.readouterr().out


@patch("homevitals.platform_support.macos._install_launch_agent")
@patch("builtins.input", return_value="y")
@patch("homevitals.platform_support.macos.sys.stdin")
@patch("homevitals.platform_support.macos.platform.system", return_value="Darwin")
def test_offer_launch_agent_installs_on_yes(mock_system, mock_stdin, mock_input, mock_install, pin_macos_impl):
    mock_stdin.isatty.return_value = True

    _offer_launch_agent()

    mock_install.assert_called_once()


@patch("homevitals.platform_support.macos._install_launch_agent")
@patch("builtins.input", return_value="n")
@patch("homevitals.platform_support.macos.sys.stdin")
@patch("homevitals.platform_support.macos.platform.system", return_value="Darwin")
def test_offer_launch_agent_skips_on_no(mock_system, mock_stdin, mock_input, mock_install):
    mock_stdin.isatty.return_value = True

    _offer_launch_agent()

    mock_install.assert_not_called()


@patch("homevitals.platform_support.macos._install_launch_agent")
@patch("homevitals.platform_support.macos.sys.stdin")
@patch("homevitals.platform_support.macos.platform.system", return_value="Darwin")
def test_offer_launch_agent_skips_non_interactive(mock_system, mock_stdin, mock_install):
    mock_stdin.isatty.return_value = False

    _offer_launch_agent()

    mock_install.assert_not_called()


# --- _uninstall keychain cleanup ---


@patch("homevitals.platform_support.macos.LAUNCH_AGENT_PATH")
@patch("homevitals.platform_support.macos.subprocess.run")
@patch("homevitals.credentials._keyring_available", return_value=True)
@patch("homevitals.credentials.delete_token")
@patch("homevitals.credentials.delete_password")
@patch("homevitals.cli.maintenance.sys.stdin")
@patch("builtins.input", side_effect=["y", "n"])
def test_uninstall_clears_keychain_for_configured_user_name(
    mock_input, mock_stdin, mock_delete_pw, mock_delete_tok,
    mock_keyring, mock_run, mock_launch_path, tmp_path,
):
    """_uninstall must read the user's name from config, not assume 'default'."""
    mock_stdin.isatty.return_value = True
    mock_launch_path.exists.return_value = False

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config_path = data_dir / "config.yaml"
    _write_config(config_path, {
        "users": [{
            "name": "elias",
            "eufy": {"email": "e@example.com"},
            "garmin": {"email": "g@example.com"},
        }],
    })

    _uninstall(data_dir)

    deleted_accounts = {call.args[0] for call in mock_delete_pw.call_args_list}
    assert "elias:eufy" in deleted_accounts, (
        f"_uninstall should clear keychain for the configured username, got {deleted_accounts}"
    )
    assert "elias:garmin" in deleted_accounts
    # The Strava client secret lives in the vault too, so --uninstall has to
    # name it as well or it survives the uninstall.
    assert "elias:strava" in deleted_accounts
    deleted_tokens = {call.args[0] for call in mock_delete_tok.call_args_list}
    assert "garmin:g@example.com" in deleted_tokens
    assert "eufy:e@example.com" in deleted_tokens


@patch("homevitals.platform_support.macos.LAUNCH_AGENT_PATH")
@patch("homevitals.platform_support.macos.subprocess.run")
@patch("homevitals.credentials._keyring_available", return_value=True)
@patch("homevitals.credentials.delete_password", side_effect=RuntimeError("keychain locked"))
@patch("homevitals.cli.maintenance.sys.stdin")
@patch("builtins.input", side_effect=["y", "n"])
def test_uninstall_survives_locked_keychain(
    mock_input, mock_stdin, mock_delete_pw,
    mock_keyring, mock_run, mock_launch_path, tmp_path, capsys,
):
    """A locked keychain makes the vault clear raise; _uninstall must catch
    that, note it, and still erase the data dir, not abort with a traceback
    and a half-removed install."""
    mock_stdin.isatty.return_value = True
    mock_launch_path.exists.return_value = False

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config_path = data_dir / "config.yaml"
    _write_config(config_path, {
        "users": [{"name": "default", "eufy": {"email": "e@example.com"}}],
    })

    _uninstall(data_dir)  # must not raise

    assert not data_dir.exists()
    out = capsys.readouterr().out
    assert "keychain" in out.lower()


@patch("homevitals.platform_support.macos.LAUNCH_AGENT_PATH")
@patch("homevitals.platform_support.macos.subprocess.run")
@patch("homevitals.credentials._keyring_available", return_value=True)
@patch("homevitals.credentials.delete_token")
@patch("homevitals.credentials.delete_password")
@patch("homevitals.cli.maintenance.sys.stdin")
@patch("builtins.input", side_effect=["y", "n"])
def test_uninstall_names_uv_for_uv_installs(
    mock_input, mock_stdin, mock_delete_pw, mock_delete_tok,
    mock_keyring, mock_run, mock_launch_path, tmp_path, capsys,
):
    """When homevitals is running from a `uv tool install` venv, the
    uninstall hint must tell the user to run `uv tool uninstall`, not the
    always-pipx line - pipx was never involved in a uv install."""
    from homevitals.cli.maintenance import _uninstall

    mock_stdin.isatty.return_value = True
    mock_launch_path.exists.return_value = False

    data_dir = tmp_path / "data"
    data_dir.mkdir()

    with patch("homevitals.install.sys.executable", "/Users/x/.local/share/uv/tools/homevitals/bin/python"), \
         patch("homevitals.install.shutil.which", return_value="/usr/local/bin/uv"):
        _uninstall(data_dir)

    out = capsys.readouterr().out
    assert "uv tool uninstall homevitals" in out
    assert "pipx uninstall" not in out


@patch("homevitals.platform_support.macos.LAUNCH_AGENT_PATH")
@patch("homevitals.platform_support.macos.subprocess.run")
@patch("homevitals.credentials._keyring_available", return_value=True)
@patch("homevitals.credentials.delete_token")
@patch("homevitals.credentials.delete_password")
@patch("homevitals.cli.maintenance.sys.stdin")
@patch("builtins.input", side_effect=["y", "n"])
def test_uninstall_names_pipx_when_not_uv(
    mock_input, mock_stdin, mock_delete_pw, mock_delete_tok,
    mock_keyring, mock_run, mock_launch_path, tmp_path, capsys,
):
    """A regular pipx install should keep naming pipx as before."""
    from homevitals.cli.maintenance import _uninstall

    mock_stdin.isatty.return_value = True
    mock_launch_path.exists.return_value = False

    data_dir = tmp_path / "data"
    data_dir.mkdir()

    with patch("homevitals.install.sys.executable", "/Users/x/.local/pipx/venvs/homevitals/bin/python"), \
         patch("homevitals.install.shutil.which", return_value="/usr/local/bin/pipx"):
        _uninstall(data_dir)

    out = capsys.readouterr().out
    assert "pipx uninstall homevitals" in out


@patch("homevitals.platform_support.macos.LAUNCH_AGENT_PATH")
@patch("homevitals.platform_support.macos.subprocess.run")
@patch("homevitals.credentials._keyring_available", return_value=True)
@patch("homevitals.credentials.delete_token")
@patch("homevitals.credentials.delete_password")
@patch("homevitals.cli.maintenance.sys.stdin")
@patch("builtins.input", side_effect=["y", "n"])
def test_uninstall_removes_custom_config_and_db_paths(
    mock_input, mock_stdin, mock_delete_pw, mock_delete_tok,
    mock_keyring, mock_run, mock_launch_path, tmp_path,
):
    """--uninstall must delete a custom --config/--db path, not just the
    files under the default ~/.homevitals directory. Before the fix,
    _uninstall(DATA_DIR) always looked at data_dir/config.yaml and
    data_dir/state.db, so custom paths elsewhere on disk were left behind."""
    from homevitals.cli.maintenance import _uninstall

    mock_stdin.isatty.return_value = True
    mock_launch_path.exists.return_value = False

    data_dir = tmp_path / "data"
    data_dir.mkdir()

    custom_config = tmp_path / "custom" / "myconfig.yaml"
    custom_db = tmp_path / "custom" / "mystate.db"
    custom_config.parent.mkdir(parents=True)
    custom_config.write_text("users:\n  - name: elias\n    eufy:\n      email: e@example.com\n")
    custom_db.write_text("not a real sqlite file, just needs to exist")

    _uninstall(data_dir, config_path=custom_config, db_path=custom_db)

    assert not custom_config.exists()
    assert not custom_db.exists()

    deleted_accounts = {call.args[0] for call in mock_delete_pw.call_args_list}
    assert "elias:eufy" in deleted_accounts, (
        "keychain cleanup should read the username from the custom config path"
    )


def test_prompt_profile_choice_returns_selected_customer_id():
    from datetime import datetime, timezone
    from unittest.mock import patch

    from homevitals.cli.profiles import _prompt_profile_choice
    from homevitals.eufy_client import EufyProfile
    profiles = [
        EufyProfile("cid-a", datetime(2026, 6, 1, tzinfo=timezone.utc), 80.0),
        EufyProfile("cid-b", datetime(2026, 6, 2, tzinfo=timezone.utc), 62.0),
    ]
    with patch("builtins.input", return_value="2"):
        assert _prompt_profile_choice(profiles) == "cid-b"


@patch("homevitals.credentials._keyring_available", return_value=False)
def test_select_profile_writes_chosen_customer_id(_keyring, tmp_path: Path):
    from datetime import datetime, timezone
    from unittest.mock import MagicMock, patch

    from homevitals.cli.profiles import _select_profile
    from homevitals.cli.shared import _write_config
    from homevitals.eufy_client import EufyProfile

    cfg_path = tmp_path / "config.yaml"
    _write_config(cfg_path, {
        "users": [{
            "name": "default",
            "eufy": {"email": "e@example.com", "password": "pw"},
            "garmin": {"email": "g@example.com", "password": "pw"},
        }],
    })

    fake = MagicMock()
    fake.list_profiles.return_value = [
        EufyProfile("cid-a", datetime(2026, 6, 2, tzinfo=timezone.utc), 80.0),
        EufyProfile("cid-b", datetime(2026, 6, 1, tzinfo=timezone.utc), 62.0),
    ]

    with patch("homevitals.eufy_client.EufyClient", return_value=fake), \
         patch("builtins.input", return_value="1"):
        _select_profile(cfg_path)

    written = yaml.safe_load(cfg_path.read_text())
    assert written["users"][0]["eufy"]["customer_id"] == "cid-a"


def test_prompt_profile_choice_retries_on_invalid_input():
    from datetime import datetime, timezone
    from unittest.mock import patch

    from homevitals.cli.profiles import _prompt_profile_choice
    from homevitals.eufy_client import EufyProfile
    profiles = [
        EufyProfile("cid-a", datetime(2026, 6, 1, tzinfo=timezone.utc), 80.0),
        EufyProfile("cid-b", datetime(2026, 6, 2, tzinfo=timezone.utc), 62.0),
    ]
    with patch("builtins.input", side_effect=["abc", "9", "1"]):
        assert _prompt_profile_choice(profiles) == "cid-a"


def test_configure_logging_quiets_garminconnect_when_not_verbose():
    import logging

    from homevitals.cli.shared import _configure_logging

    logging.getLogger("garminconnect").setLevel(logging.NOTSET)
    _configure_logging(verbose=False)
    assert logging.getLogger("garminconnect").level == logging.ERROR


def test_configure_logging_keeps_garminconnect_detail_when_verbose():
    import logging

    from homevitals.cli.shared import _configure_logging

    logging.getLogger("garminconnect").setLevel(logging.ERROR)
    _configure_logging(verbose=True)
    assert logging.getLogger("garminconnect").level == logging.DEBUG


def test_save_customer_id_writes_into_config(tmp_path: Path):
    from homevitals.cli.profiles import _save_customer_id
    from homevitals.cli.shared import _write_config

    cfg_path = tmp_path / "config.yaml"
    _write_config(cfg_path, {
        "users": [{
            "name": "default",
            "eufy": {"email": "e@example.com", "password": "pw"},
            "garmin": {"email": "g@example.com", "password": "pw"},
        }],
    })

    _save_customer_id(cfg_path, "cid-xyz", "default")

    written = yaml.safe_load(cfg_path.read_text())
    assert written["users"][0]["eufy"]["customer_id"] == "cid-xyz"
    # Existing fields are left intact.
    assert written["users"][0]["eufy"]["email"] == "e@example.com"
    assert written["users"][0]["name"] == "default"


def _ambiguous_profiles():
    from datetime import datetime, timezone

    from homevitals.eufy_client import EufyProfile
    return [
        EufyProfile("cid-human", datetime(2026, 6, 27, tzinfo=timezone.utc), 88.0),
        EufyProfile("cid-pet", datetime(2026, 4, 4, tzinfo=timezone.utc), 4.5),
    ]


def _write_synced_config(tmp_path: Path):
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, {
        "users": [{
            "name": "default",
            "eufy": {"email": "e@example.com", "password": "pw"},
            "garmin": {"email": "g@example.com", "password": "pw"},
        }],
    })
    return config_path


@patch("homevitals.credentials._keyring_available", return_value=True)
def test_migration_overwrites_stale_keychain_entry_with_yaml_value(_keyring, tmp_path):
    """A user who corrects a password in the YAML file must have that new
    value win, even if the keychain already has a (now-stale) entry for the
    same account. The old behavior only stored to the keychain when nothing
    was there yet, then deleted the YAML key regardless - silently keeping
    the stale keychain value."""
    from homevitals.cli.setup import _migrate_config_passwords

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, {
        "users": [{
            "name": "default",
            "eufy": {"email": "e@example.com", "password": "new-corrected-password"},
        }],
    })

    with patch("homevitals.credentials.get_password", return_value="stale-old-password"), \
         patch("homevitals.credentials.store_password") as mock_store:
        _migrate_config_passwords(config_path)

    mock_store.assert_called_once_with("default:eufy", "new-corrected-password")

    written = yaml.safe_load(config_path.read_text())
    assert "password" not in written["users"][0]["eufy"]


@patch("homevitals.credentials._keyring_available", return_value=True)
def test_migration_skips_env_var_reference_passwords(_keyring, tmp_path):
    """A password of the form ${VAR_NAME} is a deliberate env-var reference
    (resolved later by config.py's interpolation), not a literal secret. The
    migration must leave it untouched in the YAML and must not store the
    literal placeholder string into the keychain."""
    from homevitals.cli.setup import _migrate_config_passwords

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, {
        "users": [{
            "name": "default",
            "eufy": {"email": "e@example.com", "password": "${EUFY_PASSWORD}"},
        }],
    })

    with patch("homevitals.credentials.store_password") as mock_store:
        _migrate_config_passwords(config_path)

    mock_store.assert_not_called()

    written = yaml.safe_load(config_path.read_text())
    assert written["users"][0]["eufy"]["password"] == "${EUFY_PASSWORD}"


@patch("homevitals.credentials._keyring_available", return_value=False)
def test_first_run_setup_keeps_the_strava_secret_out_of_the_yaml(_keyring, tmp_path):
    """A fresh install must never write the client secret to config.yaml -
    otherwise new users land in exactly the state the migration exists to
    clean up."""
    from homevitals.cli.setup import _first_run_setup
    from homevitals.credentials import get_password

    config_path = tmp_path / "config.yaml"
    answers = ["e@example.com", "n", "y", "n", "12345", "sekrit"]

    with patch("builtins.input", side_effect=answers), \
         patch("getpass.getpass", return_value="eufy-pw"), \
         patch("homevitals.eufy_client.EufyClient", side_effect=RuntimeError("offline")), \
         patch("homevitals.strava_client.authorize_strava", return_value={}):
        _first_run_setup(config_path)

    assert get_password("default:strava") == "sekrit"
    assert get_password("default:eufy") == "eufy-pw"
    written = yaml.safe_load(config_path.read_text())
    assert written["users"][0]["strava"] == {"client_id": "12345"}


@patch("homevitals.credentials._keyring_available", return_value=False)
def test_first_run_setup_allows_zwift_only(_keyring, tmp_path, capsys):
    from homevitals.cli.setup import _first_run_setup
    from homevitals.credentials import get_password

    config_path = tmp_path / "config.yaml"
    client = MagicMock()
    answers = ["e@example.com", "n", "n", "y", "z@example.com"]

    with patch("sys.stdin.isatty", return_value=True), \
         patch("builtins.input", side_effect=answers), \
         patch("getpass.getpass", side_effect=["eufy-pw", "zwift-pw"]), \
         patch("homevitals.zwift_client.ZwiftClient", return_value=client), \
         patch("homevitals.eufy_client.EufyClient", side_effect=RuntimeError("offline")):
        _first_run_setup(config_path)

    user = yaml.safe_load(config_path.read_text())["users"][0]
    assert user["zwift"] == {"email": "z@example.com"}
    assert "garmin" not in user
    assert "strava" not in user
    assert get_password("default:eufy") == "eufy-pw"
    assert get_password("default:zwift") == "zwift-pw"
    client.authenticate.assert_called_once_with(force=True)
    client.check_connection.assert_called_once_with()
    assert "Running first sync to Zwift" in capsys.readouterr().out


@patch("homevitals.credentials._keyring_available", return_value=False)
def test_first_run_setup_allows_garmin_and_zwift(_keyring, tmp_path):
    from homevitals.cli.setup import _first_run_setup
    from homevitals.credentials import get_password

    config_path = tmp_path / "config.yaml"
    client = MagicMock()
    answers = ["e@example.com", "y", "n", "y", "g@example.com", "z@example.com"]

    with patch("sys.stdin.isatty", return_value=True), \
         patch("builtins.input", side_effect=answers), \
         patch("getpass.getpass", side_effect=["eufy-pw", "garmin-pw", "zwift-pw"]), \
         patch("homevitals.zwift_client.ZwiftClient", return_value=client), \
         patch("homevitals.eufy_client.EufyClient", side_effect=RuntimeError("offline")):
        _first_run_setup(config_path)

    user = yaml.safe_load(config_path.read_text())["users"][0]
    assert user["garmin"] == {"email": "g@example.com"}
    assert user["zwift"] == {"email": "z@example.com"}
    assert "strava" not in user
    assert get_password("default:garmin") == "garmin-pw"
    assert get_password("default:zwift") == "zwift-pw"


@patch("homevitals.credentials._keyring_available", return_value=False)
def test_first_run_setup_allows_all_three_targets(_keyring, tmp_path):
    from homevitals.cli.setup import _first_run_setup
    from homevitals.credentials import get_password

    config_path = tmp_path / "config.yaml"
    client = MagicMock()
    answers = [
        "e@example.com", "y", "y", "y", "g@example.com",
        "12345", "strava-secret", "z@example.com",
    ]

    with patch("sys.stdin.isatty", return_value=True), \
         patch("builtins.input", side_effect=answers), \
         patch("getpass.getpass", side_effect=["eufy-pw", "garmin-pw", "zwift-pw"]), \
         patch("homevitals.zwift_client.ZwiftClient", return_value=client), \
         patch("homevitals.eufy_client.EufyClient", side_effect=RuntimeError("offline")), \
         patch("homevitals.strava_client.authorize_strava", return_value={}):
        _first_run_setup(config_path)

    user = yaml.safe_load(config_path.read_text())["users"][0]
    assert user["garmin"] == {"email": "g@example.com"}
    assert user["strava"] == {"client_id": "12345"}
    assert user["zwift"] == {"email": "z@example.com"}
    assert get_password("default:strava") == "strava-secret"


@patch("homevitals.credentials._keyring_available", return_value=False)
def test_first_run_setup_rejects_no_targets_without_storing_credentials(_keyring, tmp_path):
    from homevitals.cli.setup import _first_run_setup
    from homevitals.credentials import get_password

    config_path = tmp_path / "config.yaml"
    with patch("builtins.input", side_effect=["e@example.com", "n", "n", "n"]), \
         patch("getpass.getpass", return_value="eufy-pw"), \
         pytest.raises(SystemExit):
        _first_run_setup(config_path)

    assert not config_path.exists()
    assert get_password("default:eufy") is None


@patch("homevitals.credentials._keyring_available", return_value=False)
def test_first_run_setup_failed_zwift_validation_writes_nothing(_keyring, tmp_path, capsys):
    from homevitals.cli.setup import _first_run_setup
    from homevitals.credentials import get_password

    config_path = tmp_path / "config.yaml"
    client = MagicMock()
    client.authenticate.side_effect = RuntimeError("account rejected")
    answers = ["e@example.com", "n", "n", "y", "z@example.com"]

    with patch("sys.stdin.isatty", return_value=True), \
         patch("builtins.input", side_effect=answers), \
         patch("getpass.getpass", side_effect=["eufy-pw", "zwift-pw"]), \
         patch("homevitals.zwift_client.ZwiftClient", return_value=client), \
         pytest.raises(SystemExit):
        _first_run_setup(config_path)

    assert not config_path.exists()
    assert get_password("default:eufy") is None
    assert get_password("default:zwift") is None
    output = capsys.readouterr().out
    assert "Retry with: homevitals\n" in output
    assert "--setup-zwift" not in output


@patch("homevitals.credentials._keyring_available", return_value=False)
def test_zwift_only_first_run_syncs_and_offers_scheduler(_keyring, tmp_path):
    from homevitals.cli.app import main

    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    client = MagicMock()
    answers = ["e@example.com", "n", "n", "y", "z@example.com"]
    argv = ["homevitals", "--config", str(config_path), "--db", str(db_path)]

    with patch("sys.argv", argv), \
         patch("sys.stdin.isatty", return_value=True), \
         patch("builtins.input", side_effect=answers), \
         patch("getpass.getpass", side_effect=["eufy-pw", "zwift-pw"]), \
         patch("homevitals.zwift_client.ZwiftClient", return_value=client), \
         patch("homevitals.eufy_client.EufyClient", side_effect=RuntimeError("offline")), \
         patch("homevitals.sync.sync_user", return_value=({"zwift": 1}, {})) as sync_user, \
         patch("homevitals.cli.updater._check_for_updates"), \
         patch("homevitals.cli.maintenance._offer_launch_agent") as offer_scheduler, \
         pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 0
    sync_user.assert_called_once()
    offer_scheduler.assert_called_once_with()


@patch("homevitals.credentials._keyring_available", return_value=False)
def test_migration_moves_strava_client_secret_to_the_vault(_keyring, tmp_path):
    """The Strava API client secret used to be the one secret left in plain
    text in config.yaml. The migration must move it into the credential store
    and delete the key, the same as the account passwords."""
    from homevitals.cli.setup import _migrate_config_passwords
    from homevitals.credentials import get_password

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, {
        "users": [{
            "name": "default",
            "eufy": {"email": "e@example.com"},
            "strava": {"client_id": "12345", "client_secret": "s3cr3t"},
        }],
    })

    _migrate_config_passwords(config_path)

    assert get_password("default:strava") == "s3cr3t"
    written = yaml.safe_load(config_path.read_text())
    assert "client_secret" not in written["users"][0]["strava"]
    # The public client id stays in the YAML.
    assert written["users"][0]["strava"]["client_id"] == "12345"


@patch("homevitals.credentials._keyring_available", return_value=True)
def test_migration_skips_env_var_reference_strava_secret(_keyring, tmp_path):
    """A ${VAR} client secret is a deliberate env-var reference, resolved by
    config.py's interpolation. Storing the literal placeholder would win over
    the YAML from then on and break the setup."""
    from homevitals.cli.setup import _migrate_config_passwords

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, {
        "users": [{
            "name": "default",
            "eufy": {"email": "e@example.com"},
            "strava": {"client_id": "12345", "client_secret": "${STRAVA_SECRET}"},
        }],
    })

    with patch("homevitals.credentials.store_password") as mock_store:
        _migrate_config_passwords(config_path)

    mock_store.assert_not_called()

    written = yaml.safe_load(config_path.read_text())
    assert written["users"][0]["strava"]["client_secret"] == "${STRAVA_SECRET}"


@patch("homevitals.credentials._keyring_available", return_value=False)
def test_migration_survives_a_bare_eufy_section(_keyring, tmp_path):
    """A hand-edited `eufy:` with nothing indented under it parses to None,
    and the {} default in user.get(service, {}) only covers a missing key. The
    migration runs on every command, so an AttributeError here took the whole
    tool down at startup."""
    from homevitals.cli.setup import _migrate_config_passwords
    from homevitals.credentials import get_password

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "users:\n"
        "  - name: default\n"
        "    eufy:\n"
        "    garmin:\n"
        "      email: g@example.com\n"
        "      password: pw\n"
    )

    _migrate_config_passwords(config_path)

    # The rest of the user migrated normally, and the bare section is left as
    # the user wrote it.
    assert get_password("default:garmin") == "pw"
    written = yaml.safe_load(config_path.read_text())
    assert written["users"][0]["eufy"] is None
    assert "password" not in written["users"][0]["garmin"]


@patch("homevitals.credentials._keyring_available", return_value=False)
def test_migration_survives_a_bare_strava_section(_keyring, tmp_path):
    """Same shape on the newer Strava client-secret lookup."""
    from homevitals.cli.setup import _migrate_config_passwords

    config_path = tmp_path / "config.yaml"
    original = (
        "users:\n"
        "  - name: default\n"
        "    eufy:\n"
        "      email: e@example.com\n"
        "    strava:\n"
    )
    config_path.write_text(original)

    with patch("homevitals.credentials.store_password") as mock_store:
        _migrate_config_passwords(config_path)

    # Nothing to move, so nothing is stored and the file is not rewritten.
    mock_store.assert_not_called()
    assert config_path.read_text() == original


@patch("homevitals.credentials._keyring_available", return_value=False)
def test_setup_strava_stores_secret_in_vault_and_strips_yaml_key(_keyring, tmp_path):
    """--setup-strava writes only the client id to the YAML, and clears any
    secret an earlier version already wrote there."""
    from homevitals.cli.setup import _setup_strava
    from homevitals.credentials import get_password

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, {
        "users": [{
            "name": "default",
            "eufy": {"email": "e@example.com"},
            "strava": {"client_id": "old-id", "client_secret": "old-plaintext"},
        }],
    })

    with patch("builtins.input", side_effect=["12345", "new-secret"]), \
         patch("homevitals.strava_client.authorize_strava", return_value={}):
        _setup_strava(config_path)

    assert get_password("default:strava") == "new-secret"
    written = yaml.safe_load(config_path.read_text())
    assert written["users"][0]["strava"] == {"client_id": "12345"}


@patch("homevitals.credentials._keyring_available", return_value=False)
def test_reauth_strava_resolves_secret_from_vault(_keyring, tmp_path):
    """_reauth built StravaConfig straight from the YAML, so after the
    migration removed the key it would have raised KeyError."""
    from homevitals.cli.maintenance import _reauth
    from homevitals.credentials import store_password

    store_password("default:strava", "vault-secret")
    config = {
        "users": [{
            "name": "default",
            "strava": {"client_id": "12345"},
        }],
    }

    with patch("homevitals.strava_client.authorize_strava") as mock_auth:
        _reauth(tmp_path / "config.yaml", config=config, target="strava")

    assert mock_auth.call_args.args[0].client_secret == "vault-secret"


def test_setup_strava_exits_cleanly_on_oauth_failure(tmp_path, capsys):
    """A Strava OAuth failure (timeout, denial, occupied port) must print the
    error message and exit 1 - not escape as a raw traceback."""
    from homevitals.cli.setup import _setup_strava

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, {
        "users": [{
            "name": "default",
            "eufy": {"email": "e@example.com", "password": "pw"},
        }],
    })

    with patch("builtins.input", side_effect=["12345", "secret"]), \
         patch("homevitals.strava_client.authorize_strava",
               side_effect=RuntimeError("Strava authorization timed out")), \
         pytest.raises(SystemExit) as exc:
        _setup_strava(config_path)

    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "Strava authorization timed out" in out
    assert "homevitals --setup-strava" in out


def test_reauth_strava_exits_cleanly_on_oauth_failure(capsys):
    """The Strava branch of _reauth must also catch OAuth failures instead
    of letting them escape as a raw traceback."""
    from homevitals.cli.maintenance import _reauth

    config = {
        "users": [{
            "name": "default",
            "strava": {"client_id": "12345", "client_secret": "secret"},
        }],
    }

    with patch("homevitals.strava_client.authorize_strava",
               side_effect=OSError("Address already in use")), \
         pytest.raises(SystemExit) as exc:
        _reauth(Path("/nonexistent"), config=config, force=True, target="strava")

    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "Address already in use" in out
    assert "homevitals --reauth strava" in out


def test_reauth_confirmation_on_non_tty_defaults_to_no():
    """The prompt's documented default is No ([y/N]). On a non-tty run there
    is no human to answer, so it must skip re-auth rather than proceeding as
    if 'yes' had been typed - that would destroy a valid token unattended."""
    from homevitals.cli.maintenance import _reauth

    config = {
        "users": [{
            "name": "default",
            "garmin": {"email": "g@example.com"},
        }],
    }

    mock_auth = MagicMock()
    mock_auth.token_status.return_value = {"state": "valid"}

    with patch("homevitals.garmin_auth.GarminAuth", return_value=mock_auth), \
         patch("homevitals.config._get_password", return_value="pw"), \
         patch("homevitals.cli.maintenance.sys.stdin") as mock_stdin:
        mock_stdin.isatty.return_value = False
        _reauth(Path("/nonexistent"), config=config, force=True)

    mock_auth.force_reauth.assert_not_called()


def test_reauth_confirmation_timeout_keeps_the_current_login(capsys):
    """An unanswered [y/N] once held this process (and a file lock on its own
    tool venv) open for over two hours. The prompt now expires and takes the
    documented default, No, so the run ends instead of parking."""
    from homevitals.cli.maintenance import _reauth

    config = {
        "users": [{
            "name": "default",
            "garmin": {"email": "g@example.com"},
        }],
    }

    mock_auth = MagicMock()
    mock_auth.token_status.return_value = {"state": "valid"}

    with patch("homevitals.garmin_auth.GarminAuth", return_value=mock_auth), \
         patch("homevitals.config._get_password", return_value="pw"), \
         patch("homevitals.cli.maintenance.input_with_timeout",
               return_value=None) as mock_prompt, \
         patch("homevitals.cli.maintenance.sys.stdin") as mock_stdin:
        mock_stdin.isatty.return_value = True
        _reauth(Path("/nonexistent"), config=config, force=True)

    mock_auth.force_reauth.assert_not_called()
    assert mock_prompt.call_args.args[1] == PROMPT_TIMEOUT_SECONDS
    assert "No answer after 5 minutes" in capsys.readouterr().out


def test_reauth_confirmation_still_honors_a_typed_yes():
    """The timeout must not have changed what a present user gets: a "y" still
    forces the re-auth the flag asked for."""
    from homevitals.cli.maintenance import _reauth

    config = {
        "users": [{
            "name": "default",
            "garmin": {"email": "g@example.com"},
        }],
    }

    mock_auth = MagicMock()
    mock_auth.token_status.return_value = {"state": "valid"}

    with patch("homevitals.garmin_auth.GarminAuth", return_value=mock_auth), \
         patch("homevitals.config._get_password", return_value="pw"), \
         patch("homevitals.cli.maintenance.input_with_timeout", return_value=" y "), \
         patch("homevitals.cli.maintenance.sys.stdin") as mock_stdin:
        mock_stdin.isatty.return_value = True
        _reauth(Path("/nonexistent"), config=config, force=True)

    mock_auth.force_reauth.assert_called_once()


def test_status_with_no_config_exits_without_wizard(tmp_path, capsys):
    """homevitals --status with no config must never launch the setup
    wizard - it should print a plain message and exit 1, matching the
    pattern used by --setup-strava, --select-profile, etc."""
    from homevitals.cli.app import main

    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"

    def boom_input(*a, **k):
        raise AssertionError("input() must not be called for --status with no config")

    argv = ["homevitals", "--config", str(config_path), "--db", str(db_path), "--status"]
    with patch("sys.argv", argv), \
         patch("builtins.input", side_effect=boom_input), \
         pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 1
    assert not config_path.exists()
    out = capsys.readouterr().out
    assert "No config found. Run homevitals first to set up." in out
    assert "first time setup" not in out


def test_history_with_no_config_exits_without_wizard(tmp_path, capsys):
    """Same guard as --status must apply to --history."""
    from homevitals.cli.app import main

    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"

    def boom_input(*a, **k):
        raise AssertionError("input() must not be called for --history with no config")

    argv = ["homevitals", "--config", str(config_path), "--db", str(db_path), "--history"]
    with patch("sys.argv", argv), \
         patch("builtins.input", side_effect=boom_input), \
         pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 1
    assert not config_path.exists()
    out = capsys.readouterr().out
    assert "No config found. Run homevitals first to set up." in out
    assert "first time setup" not in out


def test_status_with_corrupt_db_exits_cleanly(tmp_path):
    """If SyncState construction raises (locked keychain, corrupt DB file),
    --status must print a plain one-line error and exit 1 - not a raw
    traceback. This is the startup-guard pattern extended to the
    --status/--history handlers, which currently sit outside it."""
    from homevitals.cli.app import main

    config_path = _write_synced_config(tmp_path)
    db_path = tmp_path / "state.db"

    argv = ["homevitals", "--config", str(config_path), "--db", str(db_path), "--status"]
    # The migration is patched out so this test exercises only the corrupt-db
    # path; an earlier version left it running and it wrote fake passwords
    # into the real credentials file (now also blocked by conftest).
    with patch("sys.argv", argv), \
         patch("homevitals.credentials._keyring_available", return_value=False), \
         patch("homevitals.cli.setup._migrate_config_passwords"), \
         patch("homevitals.state.SyncState", side_effect=OSError("disk I/O error")), \
         pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 1


def test_upgrade_notice_file_is_hermetic(tmp_path):
    """setup.py freezes UPGRADE_NOTICE_FILE from shared.DATA_DIR at import
    time, so patching shared.DATA_DIR alone is not enough; the autouse
    fixture must repoint the constant itself or _show_upgrade_notice writes
    to the real ~/.homevitals."""
    from homevitals.cli import setup

    assert str(setup.UPGRADE_NOTICE_FILE).startswith(str(tmp_path))


@patch("homevitals.platform_support.notify")
def test_headless_first_run_refuses_wizard(mock_notify, tmp_path):
    """A headless run with no config must never call input() - it should
    print guidance, notify, and exit 1 instead of hanging in the wizard."""
    from homevitals.cli.app import main

    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"

    def boom_input(*a, **k):
        raise AssertionError("input() must not be called in headless first-run")

    argv = ["homevitals", "--config", str(config_path), "--db", str(db_path), "--headless"]
    with patch("sys.argv", argv), \
         patch("builtins.input", side_effect=boom_input), \
         pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 1
    assert not config_path.exists()
    mock_notify.assert_called()


@patch("homevitals.platform_support.notify")
def test_startup_failure_before_harness_notifies_and_exits(mock_notify, tmp_path):
    """A load_config failure (e.g. missing keychain entry -> ValueError) that
    happens before the sync try/except harness must still notify and exit 1,
    not escape as a raw traceback."""
    from homevitals.cli.app import main

    config_path = tmp_path / "config.yaml"
    config_path.write_text("users:\n  - name: default\n    eufy:\n      email: e@example.com\n")
    db_path = tmp_path / "state.db"

    argv = ["homevitals", "--config", str(config_path), "--db", str(db_path)]
    with patch("sys.argv", argv), \
         patch("homevitals.cli.setup._migrate_config_passwords"), \
         patch("homevitals.cli.setup._show_upgrade_notice"), \
         patch("homevitals.config.load_config", side_effect=ValueError("no password found")), \
         pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 1
    mock_notify.assert_called()


@patch("homevitals.platform_support.notify")
@patch("homevitals.cli.updater._check_for_updates")
@patch("homevitals.cli.setup._show_upgrade_notice")
@patch("homevitals.cli.setup._migrate_config_passwords")
@patch("homevitals.credentials._keyring_available", return_value=False)
@patch("homevitals.cli.app.sys.stdin")
def test_dry_run_does_not_notify_and_prints_preview_summary(
    mock_stdin, _keyring, _migrate, _notice, _updates, mock_notify, tmp_path, capsys
):
    """--dry-run must not fire the success notification or claim a real
    'Synced N' summary - it should print an honest preview summary instead."""
    from homevitals.cli.app import main

    mock_stdin.isatty.return_value = True
    config_path = _write_synced_config(tmp_path)
    db_path = tmp_path / "state.db"

    def fake_sync_user(user, state, **kwargs):
        assert kwargs.get("dry_run") is True
        return {"garmin": 2}, {}

    argv = ["homevitals", "--config", str(config_path), "--db", str(db_path), "--dry-run"]
    with patch("homevitals.sync.sync_user", side_effect=fake_sync_user), \
         patch("sys.argv", argv), \
         pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 0
    mock_notify.assert_not_called()

    out = capsys.readouterr().out
    assert "Synced" not in out
    assert "[DRY RUN] Syncs planned: Garmin 2." in out


@pytest.mark.parametrize("counts, expected, skip_dates", [
    ({"garmin": 1, "strava": 0, "zwift": 0}, "Synced to Garmin.", set()),
    ({"garmin": 0, "strava": 1, "zwift": 0}, "Synced to Strava.", set()),
    ({"garmin": 0, "strava": 0, "zwift": 1}, "Synced to Zwift.", set()),
    ({"garmin": 1, "strava": 1, "zwift": 0}, "Synced to Garmin and Strava.", set()),
    ({"garmin": 1, "strava": 0, "zwift": 1}, "Synced to Garmin and Zwift.", set()),
    ({"garmin": 0, "strava": 1, "zwift": 1}, "Synced to Strava and Zwift.", set()),
    ({"garmin": 1, "strava": 1, "zwift": 1}, "Synced to Garmin, Strava and Zwift.", set()),
    ({"garmin": 0, "strava": 1, "zwift": 1},
     "Synced to Strava and Zwift. Garmin already has a weigh-in dated 2026-05-10.",
     {("default", date(2026, 5, 10))}),
    ({"garmin": 0, "strava": 1}, "Synced to Strava. Garmin already has weigh-ins for 2 dates.",
     {("default", date(2026, 5, 10)), ("default", date(2026, 5, 11))}),
    ({"garmin": 0, "strava": 0}, None, set()),
    ({"garmin": 0}, None, {("default", date(2026, 5, 10))}),
])
def test_notification_reports_only_current_run_updates(
    counts, expected, skip_dates, tmp_path,
):
    from homevitals.cli.app import main

    config_path = _write_synced_config(tmp_path)
    argv = ["homevitals", "--config", str(config_path),
            "--db", str(tmp_path / "state.db"), "--headless"]

    def fake_sync_user(user, state, **kwargs):
        kwargs["report"].garmin_existing_dates.update(skip_dates)
        return counts, {}

    with patch("sys.argv", argv), \
         patch("homevitals.cli.setup._migrate_config_passwords"), \
         patch("homevitals.cli.setup._show_upgrade_notice"), \
         patch("homevitals.cli.updater._check_for_updates"), \
         patch("homevitals.cli.status._print_summary"), \
         patch("homevitals.sync.sync_user", side_effect=fake_sync_user), \
         patch("homevitals.platform_support.notify") as notify, \
         pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 0
    if expected is None:
        notify.assert_not_called()
    else:
        notify.assert_called_once_with("HomeVitals", expected)


def test_network_retry_preserves_report_object(skip_scheduled_retry_wait):
    from homevitals.cli.app import _sync_with_network_retry
    from homevitals.reporting import SyncReport

    report = SyncReport()
    seen_reports = []

    def fake_sync_user(user, state, **kwargs):
        seen_reports.append(kwargs["report"])
        if len(seen_reports) == 1:
            kwargs["report"].garmin_existing_dates.add(("default", date(2026, 5, 10)))
            return {"garmin": 0}, {"garmin": "connection timed out"}
        return {"garmin": 0, "strava": 1}, {}

    with patch("homevitals.sync.sync_user", side_effect=fake_sync_user):
        counts, errors = _sync_with_network_retry(
            None, None, headless=True, report=report,
        )

    assert counts == {"garmin": 0, "strava": 1}
    assert errors == {}
    assert seen_reports == [report, report]
    assert len(report.garmin_existing_dates) == 1


def test_dry_run_failure_never_notifies_or_touches_network_streak(tmp_path, capsys):
    from homevitals.cli import failure_notify
    from homevitals.cli.app import main

    config_path = _write_synced_config(tmp_path)
    argv = ["homevitals", "--config", str(config_path),
            "--db", str(tmp_path / "state.db"), "--dry-run", "--headless"]

    with patch("sys.argv", argv), \
         patch("homevitals.cli.setup._migrate_config_passwords"), \
         patch("homevitals.cli.setup._show_upgrade_notice"), \
         patch("homevitals.cli.updater._check_for_updates"), \
         patch("homevitals.sync.sync_user", return_value=(
             {"garmin": 0, "strava": 1}, {"garmin": "connection timed out"}
         )), \
         patch("homevitals.platform_support.notify") as notify, \
         patch.object(failure_notify, "record_network_failure") as record, \
         patch.object(failure_notify, "clear_network_failures") as clear, \
         pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 1
    notify.assert_not_called()
    record.assert_not_called()
    clear.assert_not_called()
    assert capsys.readouterr().out.strip().splitlines() == [
        "[DRY RUN] Syncs planned: Garmin 0, Strava 1.",
        "Could not check: default/garmin. Run with --verbose for details.",
    ]


def test_multi_user_run_marks_shared_report_as_multiple_users(tmp_path):
    from homevitals.cli.app import main

    config_path = _write_synced_config(tmp_path)
    users = [MagicMock(name="one", omron=None), MagicMock(name="two", omron=None)]
    config = MagicMock(users=users)
    reports = []

    def fake_sync_user(user, state, **kwargs):
        reports.append(kwargs["report"])
        return {"garmin": 0}, {}

    argv = ["homevitals", "--config", str(config_path), "--db", str(tmp_path / "state.db")]
    with patch("sys.argv", argv), \
         patch("homevitals.cli.setup._migrate_config_passwords"), \
         patch("homevitals.cli.setup._show_upgrade_notice"), \
         patch("homevitals.cli.updater._check_for_updates"), \
         patch("homevitals.config.load_config", return_value=config), \
         patch("homevitals.cli.status._print_summary"), \
         patch("homevitals.sync.sync_user", side_effect=fake_sync_user), \
         pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 0
    assert len(reports) == 2
    assert reports[0] is reports[1]
    assert reports[0].multiple_users is True


def test_first_run_partial_success_prints_counts_before_failure_guidance(tmp_path, capsys):
    from homevitals.cli.app import main

    config_path = tmp_path / "config.yaml"

    def fake_setup(path):
        _write_config(path, {"users": [{
            "name": "default",
            "eufy": {"email": "e@example.com", "password": "pw"},
            "garmin": {"email": "g@example.com", "password": "pw"},
        }]})

    argv = ["homevitals", "--config", str(config_path), "--db", str(tmp_path / "state.db")]
    with patch("sys.argv", argv), \
         patch("homevitals.cli.setup._first_run_setup", side_effect=fake_setup), \
         patch("homevitals.cli.updater._check_for_updates"), \
         patch("homevitals.cli.maintenance._offer_launch_agent") as offer, \
         patch("homevitals.platform_support.notify"), \
         patch("homevitals.sync.sync_user", return_value=(
             {"garmin": 0, "strava": 1}, {"garmin": "upload failed"}
         )), \
         pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 1
    offer.assert_not_called()
    output = capsys.readouterr().out
    assert "Syncs completed: Garmin 0, Strava 1." in output
    assert "First sync failed. Fix the issue above, then run homevitals again." in output


@patch("homevitals.cli.status._print_summary")
@patch("homevitals.platform_support.notify")
@patch("homevitals.cli.updater._check_for_updates")
@patch("homevitals.cli.setup._show_upgrade_notice")
@patch("homevitals.cli.setup._migrate_config_passwords")
@patch("homevitals.credentials._keyring_available", return_value=False)
@pytest.mark.parametrize("partial", [False, True])
def test_headless_transient_failure_silent_until_threshold(
    _keyring, _migrate, _notice, _updates, mock_notify, _summary, tmp_path, partial
):
    """A scheduled run that only hit a network blip must not fire a 'failed'
    notification. Only the third consecutive network failure escalates."""
    from homevitals.cli.app import main

    config_path = _write_synced_config(tmp_path)
    db_path = tmp_path / "state.db"

    def fake_sync_user(user, state, **kwargs):
        if partial:
            return {"garmin": 1}, {"strava": "connection timed out"}
        raise RuntimeError("[Errno 8] nodename nor servname provided, or not known")

    argv = ["homevitals", "--config", str(config_path), "--db", str(db_path), "--headless"]

    def run_once():
        with patch("homevitals.sync.sync_user", side_effect=fake_sync_user), \
             patch("sys.argv", argv), \
             pytest.raises(SystemExit) as exc:
            main()
        return exc.value.code

    assert run_once() == 1
    assert run_once() == 1
    mock_notify.assert_not_called()  # first two blips stay silent

    assert run_once() == 1
    assert mock_notify.call_count == 1  # third escalates, once
    title, msg = mock_notify.call_args[0][0], mock_notify.call_args[0][1]
    assert "network" in title.lower()
    assert msg == (
        "Sync has hit network errors for ~0h (3 runs). "
        "Pending measurements will retry automatically."
        + (" Synced to Garmin." if partial else "")
    )


@patch("homevitals.cli.status._print_summary")
@patch("homevitals.platform_support.notify")
@patch("homevitals.cli.updater._check_for_updates")
@patch("homevitals.cli.setup._show_upgrade_notice")
@patch("homevitals.cli.setup._migrate_config_passwords")
@patch("homevitals.credentials._keyring_available", return_value=False)
def test_headless_success_clears_network_streak(
    _keyring, _migrate, _notice, _updates, _notify, _summary, tmp_path
):
    """A clean run resets the streak so a later isolated blip starts from zero,
    not one short of escalating."""
    from homevitals.cli import shared
    from homevitals.cli.app import main

    config_path = _write_synced_config(tmp_path)
    db_path = tmp_path / "state.db"
    argv = ["homevitals", "--config", str(config_path), "--db", str(db_path), "--headless"]
    streak_file = shared.DATA_DIR / "network_fail_streak.json"

    def run_with(side):
        with patch("homevitals.sync.sync_user", side_effect=side), \
             patch("sys.argv", argv), \
             pytest.raises(SystemExit):
            main()

    boom = RuntimeError("_ssl.c:1063: The handshake operation timed out")
    run_with(boom)
    run_with(boom)
    assert streak_file.exists()  # streak building

    run_with(lambda user, state, **kw: ({"garmin": 1}, {}))  # clean run
    assert not streak_file.exists()  # cleared on success


@patch("homevitals.cli.status._print_summary")
@patch("homevitals.platform_support.notify")
@patch("homevitals.cli.updater._check_for_updates")
@patch("homevitals.cli.setup._show_upgrade_notice")
@patch("homevitals.cli.setup._migrate_config_passwords")
@patch("homevitals.credentials._keyring_available", return_value=False)
@pytest.mark.parametrize(("target_errors", "expected_command"), [
    ({"garmin": "Run: homevitals --reauth garmin"}, "homevitals --reauth garmin"),
    ({"zwift": "Run: homevitals --reauth zwift"}, "homevitals --reauth zwift"),
    ({
        "garmin": "Run: homevitals --reauth garmin",
        "zwift": "Run: homevitals --reauth zwift; command=unsafe-text",
    }, "homevitals --reauth"),
])
def test_per_target_upload_error_still_reaches_the_classifier(
    _keyring, _migrate, _notice, _updates, mock_notify, _summary, tmp_path,
    skip_scheduled_retry_wait, target_errors, expected_command,
):
    """A dead target session mid-upload is now contained inside sync_user and
    reported through the errors dict instead of raising. The message text must
    survive that trip, or the run ends on the generic 'failed' toast instead of
    the actionable re-login one. The clickable command comes only from the
    structured, allowlisted target names, never from exception text."""
    from homevitals.cli.app import main

    config_path = _write_synced_config(tmp_path)
    db_path = tmp_path / "state.db"

    def fake_sync_user(user, state, **kwargs):
        return {"strava": 2}, target_errors

    argv = ["homevitals", "--config", str(config_path), "--db", str(db_path), "--headless"]
    with patch("homevitals.sync.sync_user", side_effect=fake_sync_user), \
         patch("sys.argv", argv), \
         pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 1
    skip_scheduled_retry_wait.assert_not_called()
    mock_notify.assert_called_once_with(
        "HomeVitals: re-login needed", f"Synced to Strava. Run: {expected_command}",
        command=expected_command,
    )


@patch("homevitals.cli.status._print_summary")
@patch("homevitals.platform_support.notify")
@patch("homevitals.cli.updater._check_for_updates")
@patch("homevitals.cli.setup._show_upgrade_notice")
@patch("homevitals.cli.setup._migrate_config_passwords")
@patch("homevitals.credentials._keyring_available", return_value=False)
def test_zwift_password_failure_offers_password_repair(
    _keyring, _migrate, _notice, _updates, mock_notify, _summary, tmp_path,
    skip_scheduled_retry_wait,
):
    from homevitals.cli.app import main

    config_path = _write_synced_config(tmp_path)
    db_path = tmp_path / "state.db"

    def fake_sync_user(user, state, **kwargs):
        return {}, {"zwift": "Zwift login was rejected; run: homevitals --update-password"}

    argv = ["homevitals", "--config", str(config_path), "--db", str(db_path), "--headless"]
    with patch("homevitals.sync.sync_user", side_effect=fake_sync_user), \
         patch("sys.argv", argv), \
         pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 1
    skip_scheduled_retry_wait.assert_not_called()
    mock_notify.assert_any_call(
        "HomeVitals: Zwift login failed", "Run: homevitals --update-password",
        command="homevitals --update-password",
    )


def test_unspecified_reauth_failure_uses_generic_allowlisted_command():
    from homevitals.cli.app import _reauth_repair_command

    failures = [("default", "server advice says --reauth zwift; command=unsafe-text")]
    assert _reauth_repair_command(failures) == "homevitals --reauth"


@patch("homevitals.cli.status._print_summary")
@patch("homevitals.platform_support.notify")
@patch("homevitals.cli.updater._check_for_updates")
@patch("homevitals.cli.setup._show_upgrade_notice")
@patch("homevitals.cli.setup._migrate_config_passwords")
@patch("homevitals.credentials._keyring_available", return_value=False)
@patch("homevitals.cli.app.sys.stdin")
def test_interactive_transient_failure_notifies_immediately(
    mock_stdin, _keyring, _migrate, _notice, _updates, mock_notify, _summary, tmp_path
):
    """The silence is only for unattended runs. An interactive run surfaces a
    network failure right away, as before."""
    from homevitals.cli.app import main

    mock_stdin.isatty.return_value = True
    config_path = _write_synced_config(tmp_path)
    db_path = tmp_path / "state.db"

    def fake_sync_user(user, state, **kwargs):
        raise RuntimeError("[Errno 8] nodename nor servname provided, or not known")

    argv = ["homevitals", "--config", str(config_path), "--db", str(db_path)]
    with patch("homevitals.sync.sync_user", side_effect=fake_sync_user), \
         patch("sys.argv", argv), \
         pytest.raises(SystemExit):
        main()

    mock_notify.assert_called_once()
    assert "failed" in mock_notify.call_args[0][0].lower()


@patch("homevitals.cli.status._print_summary")
@patch("homevitals.platform_support.notify")
@patch("homevitals.cli.updater._check_for_updates")
@patch("homevitals.cli.setup._show_upgrade_notice")
@patch("homevitals.cli.setup._migrate_config_passwords")
@patch("homevitals.credentials._keyring_available", return_value=False)
@patch("homevitals.cli.app.sys.stdin")
def test_interactive_ambiguous_profile_resolves_and_syncs(
    mock_stdin, _keyring, _migrate, _notice, _updates, _notify, _summary, tmp_path
):
    from homevitals.cli.app import main
    from homevitals.eufy_client import AmbiguousProfileError

    mock_stdin.isatty.return_value = True
    config_path = _write_synced_config(tmp_path)
    db_path = tmp_path / "state.db"

    profiles = _ambiguous_profiles()
    seen_customer_ids = []

    def fake_sync_user(user, state, **kwargs):
        seen_customer_ids.append(user.eufy.customer_id)
        if len(seen_customer_ids) == 1:
            raise AmbiguousProfileError(profiles)
        return {"garmin": 1}, {}

    argv = ["homevitals", "--config", str(config_path), "--db", str(db_path)]
    with patch("homevitals.sync.sync_user", side_effect=fake_sync_user), \
         patch("sys.argv", argv), \
         patch("builtins.input", return_value="1"), \
         pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 0
    # The chosen (human) profile was persisted to config.
    written = yaml.safe_load(config_path.read_text())
    assert written["users"][0]["eufy"]["customer_id"] == "cid-human"
    # The sync was retried in-process with that customer_id set in memory.
    assert seen_customer_ids == [None, "cid-human"]


@patch("homevitals.cli.status._print_summary")
@patch("homevitals.platform_support.notify")
@patch("homevitals.cli.updater._check_for_updates")
@patch("homevitals.cli.setup._show_upgrade_notice")
@patch("homevitals.cli.setup._migrate_config_passwords")
@patch("homevitals.credentials._keyring_available", return_value=False)
@patch("homevitals.cli.app.sys.stdin")
def test_noninteractive_ambiguous_profile_bails(
    mock_stdin, _keyring, _migrate, _notice, _updates, _notify, _summary, tmp_path, capsys
):
    from homevitals.cli.app import main
    from homevitals.eufy_client import AmbiguousProfileError

    mock_stdin.isatty.return_value = False  # no human present
    config_path = _write_synced_config(tmp_path)
    db_path = tmp_path / "state.db"

    profiles = _ambiguous_profiles()

    def fake_sync_user(user, state, **kwargs):
        raise AmbiguousProfileError(profiles)

    def boom_input(*a, **k):
        raise AssertionError("input() must not be called with no TTY present")

    argv = ["homevitals", "--config", str(config_path), "--db", str(db_path)]
    with patch("homevitals.sync.sync_user", side_effect=fake_sync_user), \
         patch("sys.argv", argv), \
         patch("builtins.input", side_effect=boom_input), \
         pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "homevitals --select-profile" in out
    _notify.assert_any_call(
        "HomeVitals: choose your profile", "Run: homevitals --select-profile",
        command="homevitals --select-profile",
    )


@patch("homevitals.cli.status._print_summary")
@patch("homevitals.platform_support.notify")
@patch("homevitals.cli.updater._check_for_updates")
@patch("homevitals.cli.setup._show_upgrade_notice")
@patch("homevitals.cli.setup._migrate_config_passwords")
@patch("homevitals.credentials._keyring_available", return_value=False)
def test_repair_days_reaches_sync_user(
    _keyring, _migrate, _notice, _updates, _notify, _summary, tmp_path
):
    from homevitals.cli.app import main

    config_path = _write_synced_config(tmp_path)
    db_path = tmp_path / "state.db"
    seen = {}

    def fake_sync_user(user, state, **kwargs):
        seen.update(kwargs)
        return {"garmin": 3}, {}

    argv = ["homevitals", "--config", str(config_path), "--db", str(db_path), "--repair-days", "14"]
    with patch("homevitals.sync.sync_user", side_effect=fake_sync_user), \
         patch("sys.argv", argv), \
         pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 0
    assert seen["repair_days"] == 14


def test_repair_days_with_backfill_days_is_rejected(tmp_path, capsys):
    """The two set the same fetch window from different intents; taking both
    would silently honor one of them."""
    from homevitals.cli.app import main

    argv = ["homevitals", "--repair-days", "14", "--backfill-days", "30"]
    with patch("sys.argv", argv), pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 2
    assert "--repair-days and --backfill-days cannot be used together" in capsys.readouterr().err


def test_dunder_version_matches_pyproject():
    """The version lives in two places (pyproject.toml for packaging,
    homevitals.__version__ for --version and the update checker). 1.7.20
    shipped with the two out of sync, which made every up-to-date install
    nag about a phantom update. This keeps them locked together."""
    import tomllib
    from pathlib import Path

    import homevitals

    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    with open(pyproject, "rb") as f:
        declared = tomllib.load(f)["project"]["version"]
    assert homevitals.__version__ == declared


# ---------------------------------------------------------------------------
# Household: per-person password and re-login commands (WP8)
# ---------------------------------------------------------------------------

def _two_person_config(path: Path) -> dict:
    config = {"users": [
        {"name": "Chris", "eufy": {"email": "scale@example.com", "customer_id": "adult-a-0001"},
         "garmin": {"email": "adult-a@example.com"}},
        {"name": "Jane", "eufy": {"email": "Scale@Example.com", "customer_id": "adult-b-0002"},
         "garmin": {"email": "adult-b@example.com"}},
    ]}
    _write_config(path, config)
    return config


def test_update_password_single_user_clears_keyed_and_legacy_tokens(tmp_path: Path):
    from homevitals.cli.maintenance import _update_password
    from homevitals.credentials import get_password, get_token, store_token
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, {"users": [{"name": "default", "eufy": {"email": "scale@example.com"},
                                           "garmin": {"email": "adult-a@example.com"}}]})
    for name in ("eufy", "garmin", "eufy:scale@example.com", "garmin:adult-a@example.com"):
        store_token(name, {"x": 1})
    with patch("homevitals.cli.maintenance.getpass.getpass", side_effect=["new-eufy", "new-garmin"]), \
         patch("homevitals.cli.maintenance._reauth") as mock_reauth:
        _update_password(config_path)
    assert get_password("default:eufy") == "new-eufy"
    assert get_password("default:garmin") == "new-garmin"
    for name in ("eufy", "garmin", "eufy:scale@example.com", "garmin:adult-a@example.com"):
        assert get_token(name) is None, name
    assert mock_reauth.call_args.kwargs["user_name"] == "default"


def test_update_password_eufy_change_updates_every_person_sharing_the_login(tmp_path: Path):
    from homevitals.cli.maintenance import _update_password
    from homevitals.credentials import get_password, store_password
    config_path = tmp_path / "config.yaml"
    _two_person_config(config_path)
    store_password("Jane:garmin", "jane-garmin")
    with patch("homevitals.cli.maintenance.getpass.getpass", side_effect=["new-eufy", ""]):
        _update_password(config_path, user_name="Chris")
    assert get_password("Chris:eufy") == "new-eufy"
    assert get_password("Jane:eufy") == "new-eufy"
    assert get_password("Jane:garmin") == "jane-garmin"


def test_update_password_two_users_non_tty_refuses_and_names_the_gui(tmp_path: Path, capsys):
    from homevitals.cli.maintenance import _update_password
    config_path = tmp_path / "config.yaml"
    _two_person_config(config_path)
    with patch("homevitals.cli.maintenance.sys.stdin") as mock_stdin, \
         patch("homevitals.cli.maintenance.getpass.getpass") as mock_getpass:
        mock_stdin.isatty.return_value = False
        with pytest.raises(SystemExit) as exc:
            _update_password(config_path)
    assert exc.value.code == 1
    mock_getpass.assert_not_called()
    out = capsys.readouterr().out
    assert "HomeVitals window" in out
    assert "--update-password --user NAME" in out


def test_update_password_two_users_with_user_flag_targets_that_person(tmp_path: Path):
    from homevitals.cli.maintenance import _update_password
    from homevitals.credentials import get_password, get_token, store_token
    config_path = tmp_path / "config.yaml"
    _two_person_config(config_path)
    store_token("garmin:adult-a@example.com", {"x": 1})
    store_token("garmin:adult-b@example.com", {"x": 1})
    with patch("homevitals.cli.maintenance.getpass.getpass", side_effect=["", "jane-new"]), \
         patch("homevitals.cli.maintenance._reauth") as mock_reauth:
        _update_password(config_path, user_name="Jane")
    assert get_password("Jane:garmin") == "jane-new"
    assert get_password("Chris:garmin") is None
    assert get_token("garmin:adult-b@example.com") is None
    assert get_token("garmin:adult-a@example.com") == {"x": 1}
    assert mock_reauth.call_args.kwargs["user_name"] == "Jane"


def test_update_password_two_users_tty_prompts_for_person(tmp_path: Path):
    from homevitals.cli.maintenance import _update_password
    from homevitals.credentials import get_password
    config_path = tmp_path / "config.yaml"
    _two_person_config(config_path)
    with patch("homevitals.cli.maintenance.sys.stdin") as mock_stdin, \
         patch("homevitals.cli.maintenance.input_with_timeout", side_effect=["9", "2"]), \
         patch("homevitals.cli.maintenance.getpass.getpass", side_effect=["", "jane-new"]), \
         patch("homevitals.cli.maintenance._reauth"):
        mock_stdin.isatty.return_value = True
        _update_password(config_path)
    assert get_password("Jane:garmin") == "jane-new"


def test_reauth_with_user_flag_uses_that_persons_garmin_email(tmp_path: Path):
    from homevitals.cli.maintenance import _reauth
    config = _two_person_config(tmp_path / "config.yaml")
    mock_auth = MagicMock()
    with patch("homevitals.garmin_auth.GarminAuth", return_value=mock_auth) as ctor, \
         patch("homevitals.config._get_password", return_value="pw") as mock_pw:
        _reauth(tmp_path / "config.yaml", config=config, target="garmin", user_name="Jane")
    assert ctor.call_args.args[0] == "adult-b@example.com"
    assert mock_pw.call_args.args[0] == "Jane"
    mock_auth.force_reauth.assert_called_once()


def test_pick_user_unknown_name_exits_1(capsys):
    from homevitals.cli.maintenance import _pick_user
    config = {"users": [{"name": "Chris"}, {"name": "Jane"}]}
    with pytest.raises(SystemExit) as exc:
        _pick_user(config, "Sam", "--reauth")
    assert exc.value.code == 1
    assert "No person named 'Sam'" in capsys.readouterr().out


def test_pick_user_single_user_ignores_missing_name():
    from homevitals.cli.maintenance import _pick_user
    config = {"users": [{"name": "default"}]}
    assert _pick_user(config, None, "--reauth")["name"] == "default"


def test_store_password_for_email_matches_case_insensitively():
    from homevitals.cli.maintenance import _store_password_for_email
    from homevitals.credentials import get_password
    users = [{"name": "Chris", "eufy": {"email": "scale@example.com"}},
             {"name": "Jane", "eufy": {"email": " SCALE@example.com "}},
             {"name": "Sam", "eufy": {"email": "other@example.com"}}]
    assert _store_password_for_email(users, "eufy", "Scale@Example.com", "pw") == ["Chris", "Jane"]
    assert get_password("Jane:eufy") == "pw"
    assert get_password("Sam:eufy") is None


@patch("homevitals.platform_support.macos.LAUNCH_AGENT_PATH")
@patch("homevitals.platform_support.macos.subprocess.run")
@patch("homevitals.credentials._keyring_available", return_value=True)
@patch("homevitals.credentials.delete_token")
@patch("homevitals.credentials.delete_password")
@patch("homevitals.cli.maintenance.sys.stdin")
@patch("builtins.input", side_effect=["y", "n"])
def test_uninstall_deletes_keyed_tokens_for_every_user_and_legacy_names(
    mock_input, mock_stdin, mock_delete_pw, mock_delete_tok, mock_keyring, mock_run, mock_launch_path, tmp_path,
):
    mock_stdin.isatty.return_value = True
    mock_launch_path.exists.return_value = False
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _two_person_config(data_dir / "config.yaml")

    _uninstall(data_dir)

    passwords = {c.args[0] for c in mock_delete_pw.call_args_list}
    for name in ("Chris", "Jane"):
        for suffix in ("eufy", "garmin", "strava", "zwift", "omron"):
            assert f"{name}:{suffix}" in passwords
    tokens = {c.args[0] for c in mock_delete_tok.call_args_list}
    assert {"garmin:adult-a@example.com", "garmin:adult-b@example.com", "eufy:scale@example.com"} <= tokens
    assert {"eufy", "garmin", "strava", "zwift", "zwift_probe"} <= tokens


# ---------------------------------------------------------------------------
# Household: the sync run, profile saving and per-person lines (WP5)
# ---------------------------------------------------------------------------

def test_save_customer_id_writes_to_the_named_user_not_user_0(tmp_path: Path):
    import yaml

    from homevitals.cli.profiles import _save_customer_id
    cfg_path = tmp_path / "config.yaml"
    _two_person_config(cfg_path)
    _save_customer_id(cfg_path, "new-cid-0009", "Jane")
    users = yaml.safe_load(cfg_path.read_text())["users"]
    assert users[0]["eufy"]["customer_id"] == "adult-a-0001"
    assert users[1]["eufy"]["customer_id"] == "new-cid-0009"


def test_save_customer_id_unknown_user_raises(tmp_path: Path):
    from homevitals.cli.profiles import _save_customer_id
    cfg_path = tmp_path / "config.yaml"
    _two_person_config(cfg_path)
    with pytest.raises(ValueError, match="No user named 'Sam'"):
        _save_customer_id(cfg_path, "cid", "Sam")


def test_select_profile_refuses_multi_user_config(tmp_path: Path, capsys):
    from homevitals.cli.profiles import _select_profile
    from homevitals.credentials import store_password
    cfg_path = tmp_path / "config.yaml"
    _two_person_config(cfg_path)
    for name in ("Chris", "Jane"):
        store_password(f"{name}:eufy", "pw")
        store_password(f"{name}:garmin", "pw")
    with patch("homevitals.eufy_client.EufyClient") as mock_client, pytest.raises(SystemExit) as exc:
        _select_profile(cfg_path)
    assert exc.value.code == 1
    mock_client.assert_not_called()
    assert "HomeVitals window" in capsys.readouterr().out


def _run_main(argv_tail, tmp_path, config_path, **patches):
    from homevitals.cli.app import main
    argv = ["homevitals", "--config", str(config_path), "--db", str(tmp_path / "state.db"), *argv_tail]
    with patch("sys.argv", argv), \
         patch("homevitals.cli.setup._migrate_config_passwords"), \
         patch("homevitals.cli.setup._show_upgrade_notice"), \
         pytest.raises(SystemExit) as exc:
        main()
    return exc.value.code


def _household_with_passwords(tmp_path: Path) -> Path:
    from homevitals.credentials import store_password
    cfg_path = tmp_path / "config.yaml"
    _two_person_config(cfg_path)
    for name in ("Chris", "Jane"):
        store_password(f"{name}:eufy", "pw")
        store_password(f"{name}:garmin", "pw")
    return cfg_path


def test_main_calls_migrate_legacy_tokens_after_load_config(tmp_path: Path):
    cfg_path = _household_with_passwords(tmp_path)
    order = []
    import homevitals.config as config_module
    real_load = config_module.load_config

    def load(path):
        order.append("load")
        return real_load(path)

    with patch("homevitals.config.load_config", side_effect=load), \
         patch("homevitals.credentials.migrate_legacy_tokens", side_effect=lambda users: order.append(len(users))), \
         patch("homevitals.sync.sync_user", return_value=({"garmin": 0}, {})):
        _run_main(["--headless"], tmp_path, cfg_path)
    assert order[:2] == ["load", 2]


def test_ambiguous_profile_branch_saves_to_the_right_user(tmp_path: Path):
    from homevitals.credentials import store_password
    from homevitals.eufy_client import AmbiguousProfileError, EufyProfile
    cfg_path = _write_synced_config(tmp_path)
    store_password("default:eufy", "pw")
    store_password("default:garmin", "pw")
    profile = EufyProfile(customer_id="cid-human", last_measured=datetime.now(timezone.utc), last_weight_kg=70.0)
    calls = iter([AmbiguousProfileError([profile, profile]), ({"garmin": 1}, {})])

    def fake_sync(user, state, **kwargs):
        result = next(calls)
        if isinstance(result, Exception):
            raise result
        return result

    with patch("homevitals.sync.sync_user", side_effect=fake_sync), \
         patch("homevitals.cli.app.sys.stdin") as mock_stdin, \
         patch("homevitals.cli.profiles._prompt_profile_choice", return_value="cid-human"), \
         patch("homevitals.cli.profiles._save_customer_id") as mock_save:
        mock_stdin.isatty.return_value = True
        _run_main([], tmp_path, cfg_path)
    mock_save.assert_called_once_with(cfg_path, "cid-human", "default")


def test_multi_user_run_prints_a_line_per_person(tmp_path: Path, capsys):
    cfg_path = _household_with_passwords(tmp_path)
    results = {"Chris": ({"garmin": 2}, {}), "Jane": ({"garmin": 0}, {})}
    with patch("homevitals.sync.sync_user", side_effect=lambda user, state, **kw: results[user.name]):
        _run_main(["--headless"], tmp_path, cfg_path)
    out = capsys.readouterr().out
    assert "Chris: synced 2 weigh-ins to Garmin." in out
    assert "Jane: nothing new." in out


def test_multi_user_run_prints_failure_line_for_a_person(tmp_path: Path, capsys):
    cfg_path = _household_with_passwords(tmp_path)

    def fake_sync(user, state, **kwargs):
        if user.name == "Jane":
            raise RuntimeError("Garmin rejected the email or password. Run: homevitals --update-password")
        return {"garmin": 1}, {}

    with patch("homevitals.sync.sync_user", side_effect=fake_sync):
        _run_main(["--headless"], tmp_path, cfg_path)
    out = capsys.readouterr().out
    assert "Chris: synced 1 weigh-in to Garmin." in out
    assert "Jane: failed - Garmin rejected the email or password." in out


def test_single_user_run_prints_no_per_person_line(tmp_path: Path, capsys):
    from homevitals.credentials import store_password
    cfg_path = _write_synced_config(tmp_path)
    store_password("default:eufy", "pw")
    store_password("default:garmin", "pw")
    with patch("homevitals.sync.sync_user", return_value=({"garmin": 2}, {})):
        _run_main(["--headless"], tmp_path, cfg_path)
    assert "default:" not in capsys.readouterr().out


def test_user_flag_reaches_update_password(tmp_path: Path):
    cfg_path = _household_with_passwords(tmp_path)
    from homevitals.cli.app import main
    argv = ["homevitals", "--config", str(cfg_path), "--update-password", "--user", "Jane"]
    with patch("sys.argv", argv), patch("homevitals.cli.maintenance._update_password") as mock_update:
        try:
            main()
        except SystemExit as e:
            assert e.code in (0, None)
    assert mock_update.call_args.kwargs.get("user_name") == "Jane"


def test_user_flag_reaches_reauth(tmp_path: Path):
    cfg_path = _household_with_passwords(tmp_path)
    from homevitals.cli.app import main
    argv = ["homevitals", "--config", str(cfg_path), "--reauth", "garmin", "--user", "Jane"]
    with patch("sys.argv", argv), patch("homevitals.cli.maintenance._reauth") as mock_reauth:
        try:
            main()
        except SystemExit as e:
            assert e.code in (0, None)
    assert mock_reauth.call_args.kwargs.get("user_name") == "Jane"


def test_migration_moves_inline_omron_password_to_the_vault(tmp_path: Path):
    from homevitals.cli.setup import _migrate_config_passwords
    from homevitals.credentials import get_password
    cfg_path = tmp_path / "config.yaml"
    _write_config(cfg_path, {"users": [{"name": "Chris", "eufy": {"email": "e@example.com"},
                                        "garmin": {"email": "g@example.com"},
                                        "omron": {"email": "o@example.com", "country": "CA", "password": "omron-inline"}}]})
    _migrate_config_passwords(cfg_path)
    assert get_password("Chris:omron") == "omron-inline"
    assert "omron-inline" not in cfg_path.read_text()


# ---------------------------------------------------------------------------
# Part 4: the blood pressure step in the sync run (WP17)
# ---------------------------------------------------------------------------

def _omron_household(tmp_path: Path, fixtures) -> Path:
    import shutil

    from homevitals.credentials import store_password
    cfg_path = tmp_path / "config.yaml"
    shutil.copy(fixtures.path("config_household_omron.yaml"), cfg_path)
    for name in ("Chris", "Jane"):
        for service in ("eufy", "garmin", "omron"):
            store_password(f"{name}:{service}", "pw")
    return cfg_path


def _bp(**kwargs):
    from homevitals.bp_sync import BpSyncResult
    return BpSyncResult(**kwargs)


def _run_household(tmp_path, cfg_path, sync_user=None, bp=None, argv_tail=("--headless",)):
    calls = {"sync_user": [], "bp": []}

    def fake_sync(user, state, **kwargs):
        calls["sync_user"].append(user.name)
        return sync_user(user) if sync_user else ({"garmin": 0}, {})

    def fake_bp(user, state, **kwargs):
        calls["bp"].append((user.name, kwargs))
        if bp is None:
            return _bp()
        return bp(user)

    with patch("homevitals.sync.sync_user", side_effect=fake_sync), \
         patch("homevitals.bp_sync.sync_blood_pressure", side_effect=fake_bp):
        code = _run_main(list(argv_tail), tmp_path, cfg_path)
    return code, calls


def test_bp_step_runs_only_for_users_with_omron(tmp_path, fixtures):
    import yaml
    cfg_path = _omron_household(tmp_path, fixtures)
    raw = yaml.safe_load(cfg_path.read_text())
    del raw["users"][1]["omron"]
    cfg_path.write_text(yaml.dump(raw))
    with patch("homevitals.bp_sync.OmronClient") as omron_ctor, patch("homevitals.bp_sync.GarminClient"):
        omron_ctor.return_value.fetch_readings.return_value = []
        with patch("homevitals.sync.sync_user", return_value=({"garmin": 0}, {})):
            _run_main(["--headless"], tmp_path, cfg_path)
    assert [c.args[0].email for c in omron_ctor.call_args_list] == ["adult-a@example.com"]


def test_bp_failure_does_not_stop_scale_sync(tmp_path, fixtures, capsys):
    from homevitals.omron_client import OmronLoginError
    cfg_path = _omron_household(tmp_path, fixtures)

    def bp(user):
        if user.name == "Chris":
            raise OmronLoginError("OMRON connect rejected the login for adult-a@example.com (country CA). Check the "
                                  "email, the password, and the country the account was created in; tried h. "
                                  "Run: homevitals --update-password")
        return _bp(uploaded=1)

    code, calls = _run_household(tmp_path, cfg_path, sync_user=lambda u: ({"garmin": 1}, {}), bp=bp)
    assert code == 1
    assert calls["sync_user"] == ["Chris", "Jane"]
    out = capsys.readouterr().out
    assert "Chris: synced 1 weigh-in to Garmin." in out
    assert "Chris: blood pressure failed - OMRON connect rejected the login" in out
    assert "Jane: synced 1 weigh-in and 1 blood pressure reading to Garmin." in out
    assert "Chris/garmin_bp" in out


def test_scale_failure_does_not_stop_bp_sync(tmp_path, fixtures, capsys):
    cfg_path = _omron_household(tmp_path, fixtures)

    def scale(user):
        if user.name == "Chris":
            raise RuntimeError("Eufy is down")
        return {"garmin": 0}, {}

    code, calls = _run_household(tmp_path, cfg_path, sync_user=scale, bp=lambda u: _bp(uploaded=2))
    assert [name for name, _ in calls["bp"]] == ["Chris", "Jane"]
    out = capsys.readouterr().out
    assert "Chris: failed - Eufy is down" in out
    assert "Jane: synced 2 blood pressure readings to Garmin." in out


def test_target_zwift_skips_bp_and_target_garmin_runs_it(tmp_path, fixtures):
    from homevitals.cli.app import _run_blood_pressure
    from homevitals.reporting import SyncReport
    user = MagicMock()
    user.omron = object()
    args = MagicMock(headless=True, dry_run=False, target="zwift")
    with patch("homevitals.bp_sync.sync_blood_pressure") as bp:
        assert _run_blood_pressure(user, None, args, None, SyncReport(), []) is None
        bp.assert_not_called()
        args.target = "garmin"
        bp.return_value = _bp(uploaded=1)
        report = SyncReport()
        _run_blood_pressure(user, None, args, None, report, [])
        bp.assert_called_once()
        assert report.bp_uploaded == 1


def test_dry_run_prints_bp_count_only(tmp_path, fixtures, capsys):
    cfg_path = _omron_household(tmp_path, fixtures)
    _run_household(tmp_path, cfg_path, bp=lambda u: _bp(uploaded=2), argv_tail=("--dry-run",))
    out = capsys.readouterr().out
    assert "[DRY RUN] Would upload 2 blood pressure readings for Chris." in out
    assert "[DRY RUN] Would upload 2 blood pressure readings for Jane." in out
    assert "synced" not in out


def test_per_user_line_includes_bp_count(tmp_path, fixtures, capsys):
    cfg_path = _omron_household(tmp_path, fixtures)
    _run_household(tmp_path, cfg_path, sync_user=lambda u: ({"garmin": 2}, {}), bp=lambda u: _bp(uploaded=1))
    assert "Chris: synced 2 weigh-ins and 1 blood pressure reading to Garmin." in capsys.readouterr().out


def test_bp_detail_hint_printed_when_nothing_new(tmp_path, fixtures, capsys):
    cfg_path = _omron_household(tmp_path, fixtures)
    _run_household(tmp_path, cfg_path)
    out = capsys.readouterr().out
    assert "Chris: nothing new." in out
    assert "Chris: blood pressure - no new readings. Open the OMRON connect app" in out


def test_single_user_with_omron_gets_per_user_line_and_single_user_without_does_not(tmp_path, capsys):
    from homevitals.credentials import store_password
    cfg_path = tmp_path / "config.yaml"
    _write_config(cfg_path, {"users": [{"name": "default", "eufy": {"email": "e@example.com"},
                                        "garmin": {"email": "g@example.com"},
                                        "omron": {"email": "o@example.com", "country": "CA"}}]})
    for service in ("eufy", "garmin", "omron"):
        store_password(f"default:{service}", "pw")
    _run_household(tmp_path, cfg_path, sync_user=lambda u: ({"garmin": 1}, {}), bp=lambda u: _bp(uploaded=1))
    assert "default: synced 1 weigh-in and 1 blood pressure reading to Garmin." in capsys.readouterr().out

    _write_config(cfg_path, {"users": [{"name": "default", "eufy": {"email": "e@example.com"},
                                        "garmin": {"email": "g@example.com"}}]})
    _run_household(tmp_path, cfg_path, sync_user=lambda u: ({"garmin": 1}, {}))
    out = capsys.readouterr().out
    assert "default:" not in out
    assert "Syncs completed: Garmin 1." in out


def test_bp_only_run_prints_summary_line_and_toasts(tmp_path, fixtures, capsys):
    cfg_path = _omron_household(tmp_path, fixtures)
    with patch("homevitals.platform_support.notify") as notify:
        code, _ = _run_household(tmp_path, cfg_path, bp=lambda u: _bp(uploaded=1))
    assert code == 0
    assert "Syncs completed: blood pressure 2." in capsys.readouterr().out
    notify.assert_called_once_with("HomeVitals", "Synced 2 blood pressure readings to Garmin.")


def test_weigh_ins_and_bp_toast_mentions_both(tmp_path, fixtures):
    cfg_path = _omron_household(tmp_path, fixtures)
    with patch("homevitals.platform_support.notify") as notify:
        _run_household(tmp_path, cfg_path, sync_user=lambda u: ({"garmin": 1}, {}), bp=lambda u: _bp(uploaded=1))
    notify.assert_called_once_with("HomeVitals", "Synced to Garmin. Blood pressure: 2 readings.")


def test_bp_failure_sets_exit_code_and_failure_toast(tmp_path, fixtures):
    cfg_path = _omron_household(tmp_path, fixtures)
    with patch("homevitals.platform_support.notify") as notify:
        code, _ = _run_household(tmp_path, cfg_path, bp=lambda u: _bp(error="Garmin upload failed (API Error 400)"))
    assert code == 1
    assert notify.called
    assert "garmin_bp" in notify.call_args.args[1]


def test_bp_error_text_in_failures_is_sanitised(tmp_path, fixtures, capsys):
    from garminconnect import GarminConnectConnectionError
    cfg_path = _omron_household(tmp_path, fixtures)

    def bp(user):
        raise GarminConnectConnectionError("API Error 400 - SENTINEL-NEVER-LOG 137/89")

    with patch("homevitals.platform_support.notify") as notify:
        _run_household(tmp_path, cfg_path, bp=bp)
    out = capsys.readouterr().out
    assert "Garmin upload failed (API Error 400)" in out
    assert "SENTINEL" not in out and "137/89" not in out
    assert "SENTINEL" not in str(notify.call_args_list)


def test_update_password_prompts_for_omron_when_configured_and_clears_token(tmp_path, fixtures):
    from homevitals.cli.maintenance import _update_password
    from homevitals.credentials import get_password, get_token, store_token
    cfg_path = _omron_household(tmp_path, fixtures)
    store_token("omron:adult-a@example.com", {"access_token": "a", "refresh_token": "r", "base_url": "https://x"})
    store_token("omron:adult-b@example.com", {"access_token": "b", "refresh_token": "r", "base_url": "https://x"})
    with patch("homevitals.cli.maintenance.getpass.getpass", side_effect=["", "", "new-omron"]) as prompt:
        _update_password(cfg_path, user_name="Chris")
    assert prompt.call_args_list[2].args[0] == "New OMRON connect password: "
    assert get_password("Chris:omron") == "new-omron"
    assert get_password("Jane:omron") == "pw"
    assert get_token("omron:adult-a@example.com") is None
    assert get_token("omron:adult-b@example.com") is not None


def test_update_password_skips_omron_prompt_when_not_configured(tmp_path):
    from homevitals.cli.maintenance import _update_password
    cfg_path = tmp_path / "config.yaml"
    _write_config(cfg_path, {"users": [{"name": "default", "eufy": {"email": "e@example.com"},
                                        "garmin": {"email": "g@example.com"}}]})
    with patch("homevitals.cli.maintenance.getpass.getpass", side_effect=["", ""]) as prompt:
        _update_password(cfg_path)
    assert prompt.call_count == 2


@patch("homevitals.platform_support.macos.LAUNCH_AGENT_PATH")
@patch("homevitals.platform_support.macos.subprocess.run")
@patch("homevitals.credentials._keyring_available", return_value=True)
@patch("homevitals.credentials.delete_token")
@patch("homevitals.credentials.delete_password")
@patch("homevitals.cli.maintenance.sys.stdin")
@patch("builtins.input", side_effect=["y", "n"])
def test_uninstall_deletes_omron_password_and_keyed_token(
    mock_input, mock_stdin, mock_delete_pw, mock_delete_tok, mock_keyring, mock_run, mock_launch_path, tmp_path,
    fixtures,
):
    import shutil
    mock_stdin.isatty.return_value = True
    mock_launch_path.exists.return_value = False
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    shutil.copy(fixtures.path("config_household_omron.yaml"), data_dir / "config.yaml")
    _uninstall(data_dir)
    passwords = {c.args[0] for c in mock_delete_pw.call_args_list}
    assert {"Chris:omron", "Jane:omron"} <= passwords
    tokens = {c.args[0] for c in mock_delete_tok.call_args_list}
    assert {"omron:adult-a@example.com", "omron:adult-b@example.com"} <= tokens


# ---------------------------------------------------------------------------
# Bringing in older blood pressure readings (owner request, 2026-10-02)
# ---------------------------------------------------------------------------

def test_bp_backfill_days_reaches_the_bp_step_only(tmp_path, fixtures):
    cfg_path = _omron_household(tmp_path, fixtures)
    scale_kwargs = []

    def scale(user):
        return {"garmin": 0}, {}

    with patch("homevitals.sync.sync_user", side_effect=lambda user, state, **kw: scale_kwargs.append(kw) or scale(user)), \
         patch("homevitals.bp_sync.sync_blood_pressure", return_value=_bp()) as bp:
        _run_main(["--headless", "--bp-backfill-days", "3650"], tmp_path, cfg_path)
    assert [c.kwargs["backfill_days"] for c in bp.call_args_list] == [3650, 3650]
    assert all(kw.get("backfill_days") is None for kw in scale_kwargs)


def test_backfill_days_still_applies_to_both_steps(tmp_path, fixtures):
    cfg_path = _omron_household(tmp_path, fixtures)
    with patch("homevitals.sync.sync_user", return_value=({"garmin": 0}, {})) as scale, \
         patch("homevitals.bp_sync.sync_blood_pressure", return_value=_bp()) as bp:
        _run_main(["--headless", "--backfill-days", "9"], tmp_path, cfg_path)
    assert {c.kwargs["backfill_days"] for c in bp.call_args_list} == {9}
    assert {c.kwargs["backfill_days"] for c in scale.call_args_list} == {9}


# ---------------------------------------------------------------------------
# Part 5: partly set up people in the sync run (WP23)
# ---------------------------------------------------------------------------

def _partial_household(tmp_path: Path, fixtures) -> Path:
    import shutil

    from homevitals.credentials import store_password
    cfg_path = tmp_path / "config.yaml"
    shutil.copy(fixtures.path("config_household_partial.yaml"), cfg_path)
    for name, services in {"Chris": ("eufy", "garmin", "omron"), "Jane": ("garmin",), "Sam": ("eufy",)}.items():
        for service in services:
            store_password(f"{name}:{service}", "pw")
    return cfg_path


def _write_people(tmp_path: Path, users: list[dict]) -> Path:
    from homevitals.credentials import store_password
    cfg_path = tmp_path / "config.yaml"
    _write_config(cfg_path, {"users": users})
    for u in users:
        for service in ("eufy", "garmin", "omron"):
            if service in u:
                store_password(f"{u['name']}:{service}", "pw")
    return cfg_path


def _run_partial(tmp_path, cfg_path, argv_tail=("--headless",), sync=None, bp=None):
    calls = {"sync_user": [], "bp": []}

    def fake_sync(user, state, **kwargs):
        calls["sync_user"].append(user.name)
        return sync(user) if sync else ({"garmin": 0}, {})

    def fake_bp(user, state, **kwargs):
        calls["bp"].append(user.name)
        return bp(user) if bp else _bp()

    with patch("homevitals.sync.sync_user", side_effect=fake_sync), \
         patch("homevitals.bp_sync.sync_blood_pressure", side_effect=fake_bp), \
         patch("homevitals.platform_support.notify") as notify:
        code = _run_main(list(argv_tail), tmp_path, cfg_path)
    return code, calls, notify


def test_garmin_only_person_prints_not_ready_line_and_exits_zero(tmp_path, fixtures, capsys):
    code, calls, notify = _run_partial(tmp_path, _partial_household(tmp_path, fixtures))
    out = capsys.readouterr().out
    assert code == 0
    assert "Jane: not fully set up yet (connect the scale or the blood pressure monitor)." in out
    for call in notify.call_args_list:
        assert "fail" not in call.args[0].lower()


def test_eufy_only_person_prints_connect_garmin_hint(tmp_path, fixtures, capsys):
    _run_partial(tmp_path, _partial_household(tmp_path, fixtures))
    assert "Sam: not fully set up yet (connect Garmin)." in capsys.readouterr().out


def test_name_only_person_prints_not_set_up_line(tmp_path, capsys):
    cfg = _write_people(tmp_path, [
        {"name": "Chris", "eufy": {"email": "scale@example.com", "customer_id": "a"}, "garmin": {"email": "a@example.com"}},
        {"name": "Jane"},
    ])
    code, *_ = _run_partial(tmp_path, cfg)
    assert code == 0
    assert "Jane: not set up yet (connect Garmin and the scale or the blood pressure monitor)." in capsys.readouterr().out


def test_not_ready_person_runs_no_steps(tmp_path, fixtures):
    _, calls, _ = _run_partial(tmp_path, _partial_household(tmp_path, fixtures))
    assert calls["sync_user"] == ["Chris"]
    assert calls["bp"] == ["Chris"]


def test_omron_without_garmin_is_not_a_failure(tmp_path, capsys):
    cfg = _write_people(tmp_path, [{"name": "Jane", "omron": {"email": "o@example.com", "country": "CA"}}])
    code, calls, notify = _run_partial(tmp_path, cfg)
    assert code == 0
    assert calls["bp"] == [] and calls["sync_user"] == []
    assert "Jane: not fully set up yet (connect Garmin)." in capsys.readouterr().out
    notify.assert_not_called()


def test_single_garmin_only_person_gets_no_summary_one_liner(tmp_path, capsys):
    cfg = _write_people(tmp_path, [{"name": "Jane", "garmin": {"email": "b@example.com"}}])
    code, *_ = _run_partial(tmp_path, cfg)
    out = capsys.readouterr().out
    assert code == 0
    assert "No new measurements" not in out
    assert "open the Eufy app" not in out


def test_dry_run_prints_not_ready_line(tmp_path, fixtures, capsys):
    _run_partial(tmp_path, _partial_household(tmp_path, fixtures), argv_tail=("--dry-run",))
    assert "Jane: not fully set up yet" in capsys.readouterr().out


def test_summary_receives_only_ready_users(tmp_path, capsys):
    cfg = _write_people(tmp_path, [
        {"name": "Chris", "eufy": {"email": "scale@example.com", "customer_id": "a"}, "garmin": {"email": "a@example.com"}},
        {"name": "Jane", "garmin": {"email": "b@example.com"}},
    ])
    _run_partial(tmp_path, cfg)
    assert "No new measurements for 1 profile." in capsys.readouterr().out


def test_update_password_skips_eufy_prompt_when_no_scale(tmp_path):
    from homevitals.cli.maintenance import _update_password
    cfg = _write_people(tmp_path, [{"name": "Jane", "garmin": {"email": "b@example.com"}}])
    with patch("homevitals.cli.maintenance.getpass.getpass", side_effect=[""]) as prompt:
        _update_password(cfg)
    assert [c.args[0] for c in prompt.call_args_list] == ["New Garmin password: "]


def test_update_password_person_with_no_accounts_says_so(tmp_path, capsys):
    from homevitals.cli.maintenance import _update_password
    cfg = _write_people(tmp_path, [{"name": "Jane"}])
    with patch("homevitals.cli.maintenance.getpass.getpass") as prompt:
        _update_password(cfg)
    prompt.assert_not_called()
    assert "Jane has no accounts connected yet" in capsys.readouterr().out


def test_select_profile_refuses_when_no_scale_connected(tmp_path, capsys):
    from homevitals.cli.profiles import _select_profile
    cfg = _write_people(tmp_path, [{"name": "Jane", "garmin": {"email": "b@example.com"}}])
    with pytest.raises(SystemExit) as exc:
        _select_profile(cfg)
    assert exc.value.code == 1
    assert "No scale is connected for Jane" in capsys.readouterr().out


def test_status_handles_person_without_scale(tmp_path, capsys):
    cfg = _write_people(tmp_path, [{"name": "Jane", "garmin": {"email": "b@example.com"}}])
    with patch("homevitals.garmin_auth.GarminAuth.token_status", return_value={"state": "valid"}):
        code = None
        from homevitals.cli.app import main
        argv = ["homevitals", "--config", str(cfg), "--db", str(tmp_path / "state.db"), "--status"]
        with patch("sys.argv", argv), patch("homevitals.cli.setup._migrate_config_passwords"), \
             patch("homevitals.cli.setup._show_upgrade_notice"):
            try:
                main()
            except SystemExit as e:
                code = e.code
    assert code in (0, None)
    assert "Eufy auth: not configured (no scale connected)" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Progress markers for the window's people list (owner request, 2026-10-02)
# ---------------------------------------------------------------------------


def _markers(out: str) -> list[tuple[str, ...]]:
    return [tuple(line.split("\t")[1:]) for line in out.splitlines() if line.startswith("@progress\t")]


def test_progress_markers_only_when_the_window_asks(tmp_path, fixtures, capsys, monkeypatch):
    monkeypatch.delenv("EUFY_SYNC_PROGRESS", raising=False)
    _run_partial(tmp_path, _partial_household(tmp_path, fixtures))
    assert "@progress" not in capsys.readouterr().out


def test_progress_markers_follow_each_step(tmp_path, fixtures, capsys, monkeypatch):
    monkeypatch.setenv("EUFY_SYNC_PROGRESS", "1")
    code, *_ = _run_partial(tmp_path, _partial_household(tmp_path, fixtures),
                            sync=lambda user: ({"garmin": 2}, {}), bp=lambda user: _bp(uploaded=1))
    out = capsys.readouterr().out
    assert code == 0
    # Only Chris has anything to sync (Jane has Garmin only, Sam has no Garmin).
    assert _markers(out) == [("Chris", "scale", "start"), ("Chris", "scale", "done", "2"),
                             ("Chris", "bp", "start"), ("Chris", "bp", "done", "1")]
    # The person's normal line is still there for the message area.
    assert "Chris: synced 2 weigh-ins and 1 blood pressure reading to Garmin." in out


def test_progress_markers_report_failures(tmp_path, fixtures, capsys, monkeypatch):
    monkeypatch.setenv("EUFY_SYNC_PROGRESS", "1")

    def broken(user):
        raise RuntimeError("Eufy is down")

    _run_partial(tmp_path, _partial_household(tmp_path, fixtures), sync=broken,
                 bp=lambda user: _bp(error="Garmin upload failed"))
    assert _markers(capsys.readouterr().out) == [("Chris", "scale", "start"), ("Chris", "scale", "failed"),
                                                 ("Chris", "bp", "start"), ("Chris", "bp", "failed")]
