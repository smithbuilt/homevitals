"""Notification-area (tray) icon for the household window. Windows only, standard library (ctypes) only.

The icon lives on its own thread with a small hidden window that receives the
icon's clicks. Its callbacks run on that thread, so the window code passes in
callbacks that only hand work to the Tk main thread (App.post).

A second copy of the program finds the hidden window by its class name and
asks it to open the main window, so there is never more than one copy running.
"""
from __future__ import annotations

import ctypes
import logging
import sys
import threading
from collections.abc import Callable

logger = logging.getLogger(__name__)

WM_APP = 0x8000
WM_TRAY = WM_APP + 1            # the icon's own callback message
WM_OPEN_REQUEST = WM_APP + 2    # sent by a second copy of the program
WM_NULL = 0x0000
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_CONTEXTMENU = 0x007B
WM_COMMAND = 0x0111
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205

MENU_OPEN, MENU_SYNC, MENU_QUIT = 1, 2, 3
MENU_ITEMS = [(MENU_OPEN, "Open"), (MENU_SYNC, "Sync now"), (MENU_QUIT, "Quit")]
WINDOW_CLASS = "HomeVitalsTray"

_NIM_ADD, _NIM_DELETE = 0, 2
_NIF_MESSAGE, _NIF_ICON, _NIF_TIP = 0x1, 0x2, 0x4
_IMAGE_ICON, _LR_LOADFROMFILE = 1, 0x10
_SM_CXSMICON, _SM_CYSMICON = 49, 50
_TPM_RIGHTBUTTON = 0x2


def action_for(msg: int, wparam: int, lparam: int) -> str | None:
    """What a message to the hidden window means: "open", "menu", "sync", "quit" or None."""
    if msg == WM_TRAY:
        event = lparam & 0xFFFF
        if event in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
            return "open"
        if event in (WM_RBUTTONUP, WM_CONTEXTMENU):
            return "menu"
        return None
    if msg == WM_OPEN_REQUEST:
        return "open"
    if msg == WM_COMMAND:
        return {MENU_OPEN: "open", MENU_SYNC: "sync", MENU_QUIT: "quit"}.get(wparam & 0xFFFF)
    return None


def _api():
    """Win32 functions with explicit types (so 64-bit handles are never truncated)."""
    from ctypes import wintypes as w

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    lresult = ctypes.c_ssize_t

    def sig(fn, restype, *argtypes):
        fn.restype = restype
        fn.argtypes = list(argtypes)
        return fn

    sig(user32.DefWindowProcW, lresult, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
    sig(user32.RegisterClassExW, w.ATOM, ctypes.c_void_p)
    sig(user32.CreateWindowExW, w.HWND, w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID)
    sig(user32.DestroyWindow, w.BOOL, w.HWND)
    sig(user32.GetMessageW, w.BOOL, ctypes.c_void_p, w.HWND, w.UINT, w.UINT)
    sig(user32.TranslateMessage, w.BOOL, ctypes.c_void_p)
    sig(user32.DispatchMessageW, lresult, ctypes.c_void_p)
    sig(user32.PostMessageW, w.BOOL, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
    sig(user32.PostQuitMessage, None, ctypes.c_int)
    sig(user32.FindWindowW, w.HWND, w.LPCWSTR, w.LPCWSTR)
    sig(user32.LoadImageW, w.HANDLE, w.HINSTANCE, w.LPCWSTR, w.UINT, ctypes.c_int, ctypes.c_int, w.UINT)
    sig(user32.DestroyIcon, w.BOOL, w.HICON)
    sig(user32.GetSystemMetrics, ctypes.c_int, ctypes.c_int)
    sig(user32.RegisterWindowMessageW, w.UINT, w.LPCWSTR)
    sig(user32.CreatePopupMenu, w.HMENU)
    sig(user32.AppendMenuW, w.BOOL, w.HMENU, w.UINT, ctypes.c_size_t, w.LPCWSTR)
    sig(user32.TrackPopupMenu, w.BOOL, w.HMENU, w.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HWND,
        ctypes.c_void_p)
    sig(user32.DestroyMenu, w.BOOL, w.HMENU)
    sig(user32.SetForegroundWindow, w.BOOL, w.HWND)
    sig(user32.GetCursorPos, w.BOOL, ctypes.c_void_p)
    sig(kernel32.GetModuleHandleW, w.HMODULE, w.LPCWSTR)
    sig(shell32.Shell_NotifyIconW, w.BOOL, w.DWORD, ctypes.c_void_p)
    return user32, shell32, kernel32


def signal_running_instance() -> bool:
    """If the program is already running, ask it to open its window and return True."""
    if sys.platform != "win32":
        return False
    try:
        user32, _, _ = _api()
        hwnd = user32.FindWindowW(WINDOW_CLASS, None)
        if hwnd:
            user32.PostMessageW(hwnd, WM_OPEN_REQUEST, 0, 0)
            return True
    except Exception:
        logger.exception("Could not look for a running copy of the window")
    return False


class TrayIcon:
    """The logo by the clock. callbacks maps "open", "sync" and "quit" to thread-safe functions."""

    def __init__(self, icon_path: str, tooltip: str, callbacks: dict[str, Callable[[], None]]) -> None:
        self.icon_path = icon_path
        self.tooltip = tooltip[:127]
        self.callbacks = callbacks
        self._hwnd = None
        self._ready = threading.Event()
        self._ok = False
        self._thread: threading.Thread | None = None

    def start(self, timeout: float = 5.0) -> bool:
        """Show the icon. False if it couldn't be shown (then the window just behaves normally)."""
        if sys.platform != "win32":
            return False
        self._thread = threading.Thread(target=self._run, name="tray-icon", daemon=True)
        self._thread.start()
        self._ready.wait(timeout)
        return self._ok

    def stop(self, timeout: float = 5.0) -> None:
        if self._hwnd:
            try:
                user32, _, _ = _api()
                user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)
            except Exception:
                logger.exception("Could not remove the tray icon")
        if self._thread is not None:
            self._thread.join(timeout)

    # -- tray thread ----------------------------------------------------------

    def _run(self) -> None:
        try:
            self._loop()
        except Exception:
            logger.exception("The tray icon stopped")
        finally:
            self._ready.set()

    def _loop(self) -> None:
        from ctypes import wintypes as w

        user32, shell32, kernel32 = _api()

        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [("cbSize", w.DWORD), ("hWnd", w.HWND), ("uID", w.UINT), ("uFlags", w.UINT),
                        ("uCallbackMessage", w.UINT), ("hIcon", w.HICON), ("szTip", w.WCHAR * 128),
                        ("dwState", w.DWORD), ("dwStateMask", w.DWORD), ("szInfo", w.WCHAR * 256),
                        ("uVersion", w.UINT), ("szInfoTitle", w.WCHAR * 64), ("dwInfoFlags", w.DWORD),
                        ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", w.HICON)]

        WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)

        class WNDCLASSEXW(ctypes.Structure):
            _fields_ = [("cbSize", w.UINT), ("style", w.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                        ("cbWndExtra", ctypes.c_int), ("hInstance", w.HINSTANCE), ("hIcon", w.HICON),
                        ("hCursor", w.HANDLE), ("hbrBackground", w.HBRUSH), ("lpszMenuName", w.LPCWSTR),
                        ("lpszClassName", w.LPCWSTR), ("hIconSm", w.HICON)]

        hinstance = kernel32.GetModuleHandleW(None)
        hicon = user32.LoadImageW(None, self.icon_path, _IMAGE_ICON, user32.GetSystemMetrics(_SM_CXSMICON),
                                  user32.GetSystemMetrics(_SM_CYSMICON), _LR_LOADFROMFILE)
        taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")

        def notify_data(hwnd) -> NOTIFYICONDATAW:
            data = NOTIFYICONDATAW()
            data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            data.hWnd = hwnd
            data.uID = 1
            data.uFlags = _NIF_MESSAGE | _NIF_ICON | _NIF_TIP
            data.uCallbackMessage = WM_TRAY
            data.hIcon = hicon
            data.szTip = self.tooltip
            return data

        def add_icon(hwnd) -> bool:
            return bool(shell32.Shell_NotifyIconW(_NIM_ADD, ctypes.byref(notify_data(hwnd))))

        def show_menu(hwnd) -> None:
            menu = user32.CreatePopupMenu()
            for item_id, label in MENU_ITEMS:
                user32.AppendMenuW(menu, 0, item_id, label)
            point = w.POINT()
            user32.GetCursorPos(ctypes.byref(point))
            user32.SetForegroundWindow(hwnd)     # so the menu closes when you click elsewhere
            user32.TrackPopupMenu(menu, _TPM_RIGHTBUTTON, point.x, point.y, 0, hwnd, None)
            user32.PostMessageW(hwnd, WM_NULL, 0, 0)
            user32.DestroyMenu(menu)

        def wndproc(hwnd, msg, wparam, lparam):
            try:
                if msg == taskbar_created:       # Explorer restarted: put the icon back
                    add_icon(hwnd)
                    return 0
                action = action_for(msg, wparam, lparam)
                if action == "menu":
                    show_menu(hwnd)
                    return 0
                if action in self.callbacks:
                    self.callbacks[action]()
                    return 0
                if msg == WM_CLOSE:
                    shell32.Shell_NotifyIconW(_NIM_DELETE, ctypes.byref(notify_data(hwnd)))
                    user32.DestroyWindow(hwnd)
                    return 0
                if msg == WM_DESTROY:
                    user32.PostQuitMessage(0)
                    return 0
            except Exception:
                logger.exception("Tray icon message failed")
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc = WNDPROC(wndproc)        # keep a reference for the lifetime of the window
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinstance
        wc.lpszClassName = WINDOW_CLASS
        user32.RegisterClassExW(ctypes.byref(wc))
        hwnd = user32.CreateWindowExW(0, WINDOW_CLASS, "HomeVitals", 0, 0, 0, 0, 0, None, None,
                                      hinstance, None)
        if not hwnd or not add_icon(hwnd):
            logger.error("Could not show the tray icon")
            if hwnd:
                user32.DestroyWindow(hwnd)
            return
        self._hwnd = hwnd
        self._ok = True
        self._ready.set()
        logger.info("Tray icon shown")

        msg = w.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        if hicon:
            user32.DestroyIcon(hicon)
        self._hwnd = None
        logger.info("Tray icon removed")
