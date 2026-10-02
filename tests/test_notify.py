"""_notify click actions: with terminal-notifier installed, notifications
that carry a fix command open Terminal and run it when clicked; everything
else keeps the plain osascript path."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from homevitals.platform_support import macos

# conftest's autouse _mute_notifications fixture replaces platform_support.notify
# with a MagicMock so tests never fire real notifications. These tests exercise
# the real macOS function, captured here at import time, with subprocess.run
# mocked out.
_real_notify = macos._notify


def _run_notify(which_return, command=None):
    run = MagicMock()
    with patch("homevitals.platform_support.macos.shutil.which", return_value=which_return), \
         patch("homevitals.platform_support.macos.os.path.exists", return_value=False), \
         patch("homevitals.platform_support.macos.subprocess.run", run):
        _real_notify("homevitals: re-login needed", "Run: homevitals --reauth garmin", command=command)
    return run


def test_command_with_terminal_notifier_attaches_click_action():
    run = _run_notify("/opt/homebrew/bin/terminal-notifier", command="homevitals --reauth garmin")

    argv = run.call_args.args[0]
    assert argv[0] == "/opt/homebrew/bin/terminal-notifier"
    assert "-execute" in argv
    execute = argv[argv.index("-execute") + 1]
    assert "homevitals --reauth garmin" in execute
    assert "Terminal" in execute


def test_command_without_terminal_notifier_falls_back_to_osascript():
    run = _run_notify(None, command="homevitals --reauth garmin")

    argv = run.call_args.args[0]
    assert argv[0] == "osascript"
    assert "display notification" in argv[2]


def test_plain_notification_ignores_terminal_notifier():
    run = _run_notify("/opt/homebrew/bin/terminal-notifier", command=None)

    argv = run.call_args.args[0]
    assert argv[0] == "osascript"


def test_homebrew_path_is_checked_when_which_misses():
    """launchd runs with a minimal PATH that excludes Homebrew, so the
    lookup must also try the standard install locations directly."""
    run = MagicMock()
    with patch("homevitals.platform_support.macos.shutil.which", return_value=None), \
         patch("homevitals.platform_support.macos.os.path.exists",
               side_effect=lambda p: p == "/opt/homebrew/bin/terminal-notifier"), \
         patch("homevitals.platform_support.macos.subprocess.run", run):
        _real_notify("t", "m", command="homevitals --update")

    argv = run.call_args.args[0]
    assert argv[0] == "/opt/homebrew/bin/terminal-notifier"


def test_notify_still_fails_silently():
    with patch("homevitals.platform_support.macos.shutil.which", side_effect=RuntimeError("boom")):
        _real_notify("t", "m", command="homevitals --update")  # must not raise
