"""How eufy-sync learns which installer put it here, and the command that
adds an extra or upgrades through that same installer."""
from __future__ import annotations

from unittest.mock import patch

from homevitals.install import install_command, installer


def test_installer_detects_uv_tool_from_executable_path():
    with patch("homevitals.install.sys.executable", "/Users/x/.local/share/uv/tools/eufy-sync/bin/python"), \
         patch("homevitals.install.shutil.which", return_value="/usr/local/bin/uv"):
        assert installer() == "uv"


def test_installer_detects_uv_tool_on_windows_paths():
    with patch("homevitals.install.sys.executable", "C:\\Users\\x\\AppData\\Roaming\\uv\\tools\\eufy-sync\\Scripts\\python.exe"), \
         patch("homevitals.install.shutil.which", return_value="C:\\uv.exe"):
        assert installer() == "uv"


def test_installer_falls_back_to_pipx_then_pip():
    with patch("homevitals.install.sys.executable", "/opt/venv/bin/python"), \
         patch("homevitals.install.shutil.which", side_effect=lambda n: "/usr/bin/pipx" if n == "pipx" else None):
        assert installer() == "pipx"
    with patch("homevitals.install.sys.executable", "/opt/venv/bin/python"), \
         patch("homevitals.install.shutil.which", return_value=None):
        assert installer() == "pip"


def test_install_command_quotes_the_extra_for_each_installer():
    # The brackets are shell glob characters; every command must be safe to
    # paste as printed.
    with patch("homevitals.install.installer", return_value="uv"):
        assert install_command("eufy-sync[browser]") == "uv tool install --force 'eufy-sync[browser]'"
    with patch("homevitals.install.installer", return_value="pipx"):
        assert install_command("eufy-sync[browser]") == "pipx install --force 'eufy-sync[browser]'"
    with patch("homevitals.install.installer", return_value="pip"):
        assert install_command("eufy-sync[browser]") == "pip install 'eufy-sync[browser]'"



def test_commands_name_this_package_never_the_original_on_pypi():
    from homevitals import install
    for which in ("uv", "pipx", "pip"):
        with patch("homevitals.install.installer", return_value=which):
            assert "homevitals" in install.uninstall_command()
            assert "eufy-sync" not in install.uninstall_command()
            assert "eufy-sync" not in install.browser_install_command()
            assert "playwright" in install.browser_install_command()
