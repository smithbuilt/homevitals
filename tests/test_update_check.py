from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from homevitals.cli.shared import UPDATE_CHECK_INTERVAL
from homevitals.cli.updater import _check_for_updates


@pytest.fixture(autouse=True)
def _no_browser_extra(monkeypatch):
    # The dev venv may carry Playwright; the pins below assume the base install.
    monkeypatch.setitem(sys.modules, "playwright", None)


@pytest.fixture(autouse=True)
def _not_household(request, monkeypatch):
    """The upstream tests below check the PyPI update path, which the household
    build turns off. Pretend to be upstream unless a test opts out."""
    if "household" in request.node.name:
        return
    monkeypatch.setattr("homevitals.cli.updater._self_update_disabled", lambda: False, raising=False)


def _mock_pypi_response(version: str) -> MagicMock:
    """Create a mock urllib response returning the given version."""
    body = json.dumps({"info": {"version": version}}).encode()
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def test_skips_when_cache_is_fresh(tmp_path: Path):
    cache_file = tmp_path / "update_check"
    cache_file.write_text(str(time.time()))  # just checked

    with patch("homevitals.cli.shared.DATA_DIR", tmp_path), \
         patch("homevitals.cli.updater.urllib.request.urlopen") as mock_urlopen:

        _check_for_updates()

        mock_urlopen.assert_not_called()


def test_checks_pypi_when_cache_stale(tmp_path: Path, capsys):
    cache_file = tmp_path / "update_check"
    cache_file.write_text(str(time.time() - UPDATE_CHECK_INTERVAL - 1))

    with patch("homevitals.cli.shared.DATA_DIR", tmp_path), \
         patch("homevitals.cli.updater.urllib.request.urlopen", return_value=_mock_pypi_response("99.0.0")), \
         patch("homevitals.cli.updater.sys.stdin") as mock_stdin:
        mock_stdin.isatty.return_value = True

        _check_for_updates()

    output = capsys.readouterr().out
    assert "Update available" in output
    assert "99.0.0" in output
    assert "eufy-sync --update" in output


def test_no_message_when_up_to_date(tmp_path: Path, capsys):
    cache_file = tmp_path / "update_check"
    cache_file.write_text(str(time.time() - UPDATE_CHECK_INTERVAL - 1))

    from homevitals import __version__

    with patch("homevitals.cli.shared.DATA_DIR", tmp_path), \
         patch("homevitals.cli.updater.urllib.request.urlopen", return_value=_mock_pypi_response(__version__)):

        _check_for_updates()

    assert capsys.readouterr().out == ""


def test_no_message_when_local_is_newer(tmp_path: Path, capsys):
    cache_file = tmp_path / "update_check"
    cache_file.write_text(str(time.time() - UPDATE_CHECK_INTERVAL - 1))

    with patch("homevitals.cli.shared.DATA_DIR", tmp_path), \
         patch("homevitals.cli.updater.urllib.request.urlopen", return_value=_mock_pypi_response("0.0.1")):

        _check_for_updates()

    assert capsys.readouterr().out == ""


def test_silent_on_network_error(tmp_path: Path, capsys):
    cache_file = tmp_path / "update_check"
    cache_file.write_text(str(time.time() - UPDATE_CHECK_INTERVAL - 1))

    with patch("homevitals.cli.shared.DATA_DIR", tmp_path), \
         patch("homevitals.cli.updater.urllib.request.urlopen", side_effect=OSError("no network")):

        _check_for_updates()

    assert capsys.readouterr().out == ""


def test_cache_written_after_successful_check(tmp_path: Path):
    # No cache file initially
    from homevitals import __version__

    with patch("homevitals.cli.shared.DATA_DIR", tmp_path), \
         patch("homevitals.cli.updater.urllib.request.urlopen", return_value=_mock_pypi_response(__version__)):

        _check_for_updates()

    cache_file = tmp_path / "update_check"
    assert cache_file.exists()
    last_check = float(cache_file.read_text().strip())
    assert time.time() - last_check < 5


def test_cache_not_written_on_network_error(tmp_path: Path):
    with patch("homevitals.cli.shared.DATA_DIR", tmp_path), \
         patch("homevitals.cli.updater.urllib.request.urlopen", side_effect=OSError("no network")):

        _check_for_updates()

    cache_file = tmp_path / "update_check"
    assert not cache_file.exists()


def test_self_update_uses_pinned_pipx_when_available():
    # Pin a non-Windows platform: this covers the inline update path, which a
    # Windows host would otherwise skip for the detached-console branch.
    from homevitals.cli.updater import _self_update
    with patch("homevitals.cli.updater._latest_pypi_version", return_value="9.9.9"), \
         patch("homevitals.__version__", "1.0.0"), \
         patch("homevitals.cli.updater.platform.system", return_value="Darwin"), \
         patch("homevitals.install.shutil.which", return_value="/usr/local/bin/pipx"), \
         patch("homevitals.cli.updater.subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
        _self_update()

    # Installs the exact latest version so a stale index cannot no-op the update.
    assert mock_run.call_args.args[0] == ["pipx", "install", "--force", "homevitals==9.9.9"]


def test_self_update_noop_when_already_latest(capsys):
    from homevitals import __version__
    from homevitals.cli.updater import _self_update
    with patch("homevitals.cli.updater._latest_pypi_version", return_value=__version__), \
         patch("homevitals.cli.updater.subprocess.run") as mock_run:
        _self_update()

    assert "Already on the latest" in capsys.readouterr().out
    mock_run.assert_not_called()


def test_handles_pypi_prerelease_version(tmp_path: Path, capsys):
    """PyPI can return versions like '1.7.2rc1' or '1.7.2.dev0' - parser must not crash silently."""
    cache_file = tmp_path / "update_check"
    cache_file.write_text(str(time.time() - UPDATE_CHECK_INTERVAL - 1))

    with patch("homevitals.cli.shared.DATA_DIR", tmp_path), \
         patch("homevitals.cli.updater.urllib.request.urlopen", return_value=_mock_pypi_response("99.0.0rc1")), \
         patch("homevitals.cli.updater.sys.stdin") as mock_stdin:
        mock_stdin.isatty.return_value = True

        _check_for_updates()

    output = capsys.readouterr().out
    assert "Update available" in output
    assert "99.0.0rc1" in output

    # Cache should be written - we successfully evaluated the version
    assert cache_file.exists()


def test_self_update_uses_uv_when_installed_via_uv_tool():
    # A uv tool venv has no pip, and pipx would create a second copy; the
    # updater must recognize its own install method and use uv.
    # Pinned to Darwin so a Windows host exercises this inline path too.
    from homevitals.cli.updater import _self_update
    with patch("homevitals.cli.updater._latest_pypi_version", return_value="9.9.9"), \
         patch("homevitals.__version__", "1.0.0"), \
         patch("homevitals.cli.updater.platform.system", return_value="Darwin"), \
         patch("homevitals.install.sys.executable", "/Users/x/.local/share/uv/tools/eufy-sync/bin/python"), \
         patch("homevitals.install.shutil.which", return_value="/usr/local/bin/uv"), \
         patch("homevitals.cli.updater.subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
        _self_update()

    assert mock_run.call_args.args[0] == ["uv", "tool", "install", "--force", "homevitals==9.9.9"]


def test_self_update_uses_uv_for_windows_uv_tool_path():
    # A uv tool install on Windows lives under ...\uv\tools\... with
    # backslashes; the marker match must normalize those or it falls through to
    # a pip path that cannot work inside a uv venv.
    from homevitals.cli.updater import _self_update
    win_exe = r"C:\Users\x\AppData\Roaming\uv\tools\eufy-sync\Scripts\python.exe"
    with patch("homevitals.cli.updater._latest_pypi_version", return_value="9.9.9"), \
         patch("homevitals.__version__", "1.0.0"), \
         patch("homevitals.cli.updater.platform.system", return_value="Darwin"), \
         patch("homevitals.install.sys.executable", win_exe), \
         patch("homevitals.install.shutil.which", return_value="C:\\Users\\x\\uv.exe"), \
         patch("homevitals.cli.updater.subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
        _self_update()

    assert mock_run.call_args.args[0][:4] == ["uv", "tool", "install", "--force"]


def test_self_update_windows_quotes_spaced_executable_path():
    # The detached inner command is one cmd.exe string; a Python path with
    # spaces (e.g. C:\Program Files\...) must arrive double-quoted or it splits
    # into two arguments and the upgrade fails.
    from homevitals.cli.updater import _self_update
    spaced = "C:\\Program Files\\Python\\python.exe"
    with patch("homevitals.cli.updater._latest_pypi_version", return_value="9.9.9"), \
         patch("homevitals.__version__", "1.0.0"), \
         patch("homevitals.cli.updater.platform.system", return_value="Windows"), \
         patch("homevitals.install.sys.executable", spaced), \
         patch("homevitals.install.shutil.which", return_value=None), \
         patch("homevitals.cli.updater.subprocess.Popen") as mock_popen:
        _self_update()

    inner = mock_popen.call_args.args[0][-1]
    assert '"C:\\Program Files\\Python\\python.exe"' in inner
    assert "homevitals==9.9.9" in inner


def test_self_update_uses_pip_when_no_pipx():
    # Pinned to Darwin so a Windows host exercises this inline path too.
    import sys as _sys

    from homevitals.cli.updater import _self_update
    with patch("homevitals.cli.updater._latest_pypi_version", return_value="9.9.9"), \
         patch("homevitals.__version__", "1.0.0"), \
         patch("homevitals.cli.updater.platform.system", return_value="Darwin"), \
         patch("homevitals.install.shutil.which", return_value=None), \
         patch("homevitals.cli.updater.subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
        _self_update()

    cmd = mock_run.call_args.args[0]
    assert cmd[:3] == [_sys.executable, "-m", "pip"]
    assert "homevitals==9.9.9" in cmd


def test_self_update_silent_when_pypi_unreachable(capsys):
    from homevitals.cli.updater import _self_update
    with patch("homevitals.cli.updater._latest_pypi_version", return_value=None), \
         patch("homevitals.cli.updater.subprocess.run") as mock_run:
        _self_update()

    assert "Could not reach PyPI" in capsys.readouterr().out
    mock_run.assert_not_called()


def test_self_update_reports_failed_install(capsys):
    # Pinned to Darwin: the inline runner reports the failure; on a Windows
    # host the detached branch would print the new-window message instead.
    from homevitals.cli.updater import _self_update
    with patch("homevitals.cli.updater._latest_pypi_version", return_value="9.9.9"), \
         patch("homevitals.__version__", "1.0.0"), \
         patch("homevitals.cli.updater.platform.system", return_value="Darwin"), \
         patch("homevitals.install.shutil.which", return_value="/usr/local/bin/pipx"), \
         patch("homevitals.cli.updater.subprocess.run", return_value=MagicMock(returncode=1)):
        _self_update()

    assert "Update failed" in capsys.readouterr().out


def test_self_update_on_windows_uses_detached_console():
    # Windows cannot overwrite the running eufy-sync.exe, so the update must be
    # handed to a separate console that waits for this process to exit first.
    from homevitals.cli.updater import _self_update
    with patch("homevitals.cli.updater._latest_pypi_version", return_value="9.9.9"), \
         patch("homevitals.__version__", "1.0.0"), \
         patch("homevitals.cli.updater.platform.system", return_value="Windows"), \
         patch("homevitals.install.shutil.which", return_value="C:\\pipx\\pipx.exe"), \
         patch("homevitals.cli.updater.subprocess.run") as mock_run, \
         patch("homevitals.cli.updater.subprocess.Popen") as mock_popen:
        _self_update()

    # The in-process runner must not be used - it would try to replace a locked exe.
    mock_run.assert_not_called()
    mock_popen.assert_called_once()

    argv = mock_popen.call_args.args[0]
    assert argv[:4] == ["cmd", "/c", "start", "eufy-sync update"]

    # The pinned version is what the detached console actually installs.
    inner = argv[-1]
    assert "homevitals==9.9.9" in inner


def test_self_update_on_darwin_runs_inline():
    # Non-Windows platforms replace the package in place, exactly as before.
    from homevitals.cli.updater import _self_update
    with patch("homevitals.cli.updater._latest_pypi_version", return_value="9.9.9"), \
         patch("homevitals.__version__", "1.0.0"), \
         patch("homevitals.cli.updater.platform.system", return_value="Darwin"), \
         patch("homevitals.install.shutil.which", return_value="/usr/local/bin/pipx"), \
         patch("homevitals.cli.updater.subprocess.Popen") as mock_popen, \
         patch("homevitals.cli.updater.subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
        _self_update()

    mock_popen.assert_not_called()
    assert mock_run.call_args.args[0] == ["pipx", "install", "--force", "homevitals==9.9.9"]


def test_self_update_keeps_the_browser_extra_when_installed(monkeypatch):
    # Reinstalling "homevitals==X" through uv or pipx replaces the whole tool
    # venv, so an update would silently drop Playwright for anyone who opted
    # into the browser fallback. The pin has to carry the extra along.
    import importlib.machinery
    import types

    from homevitals.cli.updater import _self_update
    present = types.ModuleType("playwright")
    present.__spec__ = importlib.machinery.ModuleSpec("playwright", None)
    monkeypatch.setitem(sys.modules, "playwright", present)
    with patch("homevitals.cli.updater._latest_pypi_version", return_value="9.9.9"), \
         patch("homevitals.__version__", "1.0.0"), \
         patch("homevitals.cli.updater.platform.system", return_value="Darwin"), \
         patch("homevitals.install.sys.executable", "/Users/x/.local/share/uv/tools/eufy-sync/bin/python"), \
         patch("homevitals.install.shutil.which", return_value="/usr/local/bin/uv"), \
         patch("homevitals.cli.updater.subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
        _self_update()

    assert mock_run.call_args.args[0] == ["uv", "tool", "install", "--force", "homevitals[browser]==9.9.9"]


# ---------------------------------------------------------------------------
# Household build: upstream self-update is off (WP6)
# ---------------------------------------------------------------------------

def test_household_self_update_is_always_disabled():
    from homevitals.cli.updater import _self_update_disabled
    for version in ("1.0.0", "2.3.4", "1.13.2+household.9"):
        with patch("homevitals.__version__", version):
            assert _self_update_disabled() is True


def test_household_version_is_a_plain_release_number():
    import re

    from homevitals import __version__
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)


def test_household_build_skips_update_check_without_network(tmp_path: Path, capsys):
    with patch("homevitals.cli.shared.DATA_DIR", tmp_path),          patch("homevitals.cli.updater.urllib.request.urlopen") as mock_urlopen,          patch("homevitals.cli.updater._latest_pypi_version") as mock_latest:
        _check_for_updates()
    mock_urlopen.assert_not_called()
    mock_latest.assert_not_called()
    assert not (tmp_path / "update_check").exists()
    assert capsys.readouterr().out == ""


def test_household_build_update_prints_message_and_runs_nothing(capsys):
    from homevitals.cli.updater import _self_update
    with patch("homevitals.cli.updater._latest_pypi_version") as mock_latest,          patch("homevitals.cli.updater.subprocess.run") as mock_run,          patch("homevitals.cli.updater.subprocess.Popen") as mock_popen:
        _self_update()
    mock_latest.assert_not_called()
    mock_run.assert_not_called()
    mock_popen.assert_not_called()
    out = capsys.readouterr().out
    assert "doesn't update itself" in out
    assert "uv tool upgrade homevitals" in out


def test_main_module_runs_cli():
    import runpy
    with patch("homevitals.cli.main") as mock_main:
        runpy.run_module("homevitals", run_name="__main__")
    mock_main.assert_called_once()
