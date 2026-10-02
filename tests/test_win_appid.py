"""One taskbar button for the window and its shortcuts (homevitals/win_appid.py).

The round trip writes a real shortcut in a temporary folder (Windows only);
nothing here touches the owner's own shortcuts or the network.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from homevitals import win_appid

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


def _make_lnk(path: Path) -> None:
    script = (f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{path}'); "
              "$s.TargetPath = 'C:\\Windows\\notepad.exe'; $s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], check=True, timeout=30)


@windows_only
def test_shortcut_app_id_round_trip_keeps_the_target(tmp_path):
    import json
    import sys

    lnk = tmp_path / "Household scale sync.lnk"
    _make_lnk(lnk)
    # In a fresh process: the shell call messages every open window and waits for answers, and
    # the window tests leave a hidden Tk window in this process that can't answer while this
    # thread waits (a deadlock). The app is safe: its window keeps answering while a background
    # thread does this.
    code = ("import json, sys; from homevitals import win_appid as w; p = sys.argv[1]; "
            "print(json.dumps([w.read_shortcut_app_id(p), w.set_shortcut_app_id(p), w.read_shortcut_app_id(p)]))")
    done = subprocess.run([sys.executable, "-c", code, str(lnk)], capture_output=True, text=True, timeout=60,
                          cwd=Path(__file__).parent.parent)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == [None, True, win_appid.APP_ID]
    assert win_appid.APP_ID == "HomeVitals.App"
    target = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                             f"(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}').TargetPath"],
                            capture_output=True, text=True, timeout=30).stdout.strip()
    assert target.lower() == r"c:\windows\notepad.exe"


@windows_only
def test_missing_shortcut_fails_quietly(tmp_path):
    assert win_appid.set_shortcut_app_id(tmp_path / "nope.lnk") is False
    assert win_appid.read_shortcut_app_id(tmp_path / "nope.lnk") is None


@windows_only
def test_our_shortcuts_finds_the_pinned_copy(tmp_path, monkeypatch):
    pinned = tmp_path / "Microsoft" / "Internet Explorer" / "Quick Launch" / "User Pinned" / "TaskBar"
    pinned.mkdir(parents=True)
    (pinned / "Household scale sync.lnk").write_bytes(b"")
    (pinned / "Something else.lnk").write_bytes(b"")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr(win_appid, "_special_folder", lambda csidl: None)
    assert win_appid.our_shortcuts("Household scale sync") == [pinned / "Household scale sync.lnk"]
    # Windows' numbered copies are ours too; look-alike names are not.
    (pinned / "Household scale sync (2).lnk").write_bytes(b"")
    (pinned / "Household scale sync backup.lnk").write_bytes(b"")
    assert win_appid.our_shortcuts("Household scale sync") == [pinned / "Household scale sync (2).lnk",
                                                               pinned / "Household scale sync.lnk"]


def test_apply_taskbar_identity_sets_relaunch_and_tags_only_untagged_shortcuts(monkeypatch, tmp_path):
    tk = pytest.importorskip("tkinter")
    from homevitals import gui, gui_logic

    monkeypatch.setattr(gui.sys, "platform", "win32")
    monkeypatch.setattr(gui_logic, "shortcut_plan",
                        lambda: gui_logic.ShortcutPlan(r"C:\bin\homevitals-gui.exe", "", r"C:\bin"))
    windows, tagged = [], []
    monkeypatch.setattr(win_appid, "set_window_identity", lambda *a: windows.append(a) or True)
    old, new = tmp_path / "old.lnk", tmp_path / "new.lnk"
    monkeypatch.setattr(win_appid, "our_shortcuts", lambda name: [old, new])
    monkeypatch.setattr(win_appid, "read_shortcut_app_id", lambda lnk: win_appid.APP_ID if lnk == new else None)
    monkeypatch.setattr(win_appid, "set_shortcut_app_id", lambda lnk: tagged.append(lnk) or True)
    started = []
    monkeypatch.setattr(gui.threading, "Thread", lambda target, daemon: type("T", (), {
        "start": lambda self: (started.append(1), target())})())

    class FakeRoot:
        def update_idletasks(self):
            pass

        def wm_frame(self):
            return "0x1234"

    app = type("A", (), {"root": FakeRoot()})()
    gui.apply_taskbar_identity(app)
    assert windows and windows[0][0] == 0x1234
    assert windows[0][1] == r'"C:\bin\homevitals-gui.exe"'
    assert windows[0][2] == gui.TITLE
    assert started == [1]
    assert tagged == [old]
    assert tk  # tkinter importable for gui
