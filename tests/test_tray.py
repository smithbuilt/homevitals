"""Tests for the notification-area (tray) icon (homevitals/tray.py).

The Win32 calls themselves are exercised by hand; these cover the pure parts
and run on any machine.
"""
from __future__ import annotations

import sys

import pytest

from homevitals import tray


def test_left_click_and_double_click_open_the_window():
    assert tray.action_for(tray.WM_TRAY, 0, tray.WM_LBUTTONUP) == "open"
    assert tray.action_for(tray.WM_TRAY, 0, tray.WM_LBUTTONDBLCLK) == "open"


def test_right_click_shows_the_menu():
    assert tray.action_for(tray.WM_TRAY, 0, tray.WM_RBUTTONUP) == "menu"
    assert tray.action_for(tray.WM_TRAY, 0, tray.WM_CONTEXTMENU) == "menu"


def test_menu_items_map_to_actions():
    assert tray.action_for(tray.WM_COMMAND, tray.MENU_OPEN, 0) == "open"
    assert tray.action_for(tray.WM_COMMAND, tray.MENU_SYNC, 0) == "sync"
    assert tray.action_for(tray.WM_COMMAND, tray.MENU_QUIT, 0) == "quit"
    assert tray.action_for(tray.WM_COMMAND, 99, 0) is None


def test_a_second_launch_asks_the_first_one_to_open():
    assert tray.action_for(tray.WM_OPEN_REQUEST, 0, 0) == "open"


def test_other_messages_are_ignored():
    assert tray.action_for(0x0200, 0, 0) is None        # mouse move over the window
    assert tray.action_for(tray.WM_TRAY, 0, 0x0200) is None


def test_menu_labels_are_plain():
    assert [label for _, label in tray.MENU_ITEMS] == ["Open", "Sync now", "Quit"]


@pytest.mark.skipif(sys.platform == "win32", reason="non-Windows behaviour")
def test_no_tray_off_windows():
    assert tray.signal_running_instance() is False
    assert tray.TrayIcon("x.ico", "tip", {}).start() is False
