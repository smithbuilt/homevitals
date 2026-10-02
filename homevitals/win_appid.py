"""One taskbar button for the window, its shortcuts and its pinned icon (Windows only).

Windows groups a running window with a shortcut only when both carry the same
AppUserModelID. The process sets it (gui.main); here the same ID is written
into our shortcuts, and the window gets "relaunch" properties so that pinning
its taskbar button pins something that actually starts the program (without
them Windows pins bare pythonw.exe, which does nothing).

Plain ctypes COM (IPropertyStore), no extra packages. Every function returns
False instead of raising, so a failure here never stops the window.
"""
from __future__ import annotations

import ctypes
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

APP_ID = "HomeVitals.App"

_FMTID_APPUSERMODEL = "{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}"
_PID_RELAUNCH_COMMAND = 2
_PID_RELAUNCH_ICON = 3
_PID_RELAUNCH_NAME = 4
_PID_ID = 5
_IID_IPROPERTYSTORE = "{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}"
_VT_LPWSTR = 31
_GPS_READWRITE = 2
_COINIT_APARTMENTTHREADED = 2
_CSIDL_STARTUP = 0x0007
_CSIDL_DESKTOPDIRECTORY = 0x0010


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort), ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8)]


class _PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", ctypes.c_ulong)]


class _PROPVARIANT(ctypes.Structure):
    # vt, three reserved words, then the value; the padding gives the real size (16 or 24 bytes).
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort), ("r3", ctypes.c_ushort),
                ("value", ctypes.c_void_p), ("pad", ctypes.c_void_p)]


def _guid(text: str) -> _GUID:
    guid = _GUID()
    hr = ctypes.windll.ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(guid))
    if hr != 0:
        raise OSError(f"CLSIDFromString failed ({hr:#x})")
    return guid


def _set_strings(store: ctypes.c_void_p, values: dict[int, str], commit: bool) -> None:
    """IPropertyStore::SetValue for each (pid, text), then Commit (files only), then Release."""
    vtable = ctypes.cast(store, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    hresult = ctypes.c_long
    set_value = ctypes.WINFUNCTYPE(hresult, ctypes.c_void_p, ctypes.POINTER(_PROPERTYKEY),
                                   ctypes.POINTER(_PROPVARIANT))(vtable[6])
    commit_fn = ctypes.WINFUNCTYPE(hresult, ctypes.c_void_p)(vtable[7])
    release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])
    try:
        fmtid = _guid(_FMTID_APPUSERMODEL)
        for pid, text in values.items():
            buffer = ctypes.create_unicode_buffer(text)
            value = _PROPVARIANT(vt=_VT_LPWSTR, value=ctypes.cast(buffer, ctypes.c_void_p))
            hr = set_value(store, ctypes.byref(_PROPERTYKEY(fmtid, pid)), ctypes.byref(value))
            if hr < 0:
                raise OSError(f"SetValue({pid}) failed ({hr & 0xFFFFFFFF:#x})")
        if commit:
            hr = commit_fn(store)
            if hr < 0:
                raise OSError(f"Commit failed ({hr & 0xFFFFFFFF:#x})")
    finally:
        release(store)


class _Com:
    """CoInitializeEx for this thread (workers need it too); undone only if we did it."""

    def __enter__(self) -> _Com:
        self.ours = ctypes.windll.ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED) in (0, 1)
        return self

    def __exit__(self, *_exc) -> None:
        if self.ours:
            ctypes.windll.ole32.CoUninitialize()


def set_shortcut_app_id(lnk: Path | str) -> bool:
    """Write APP_ID into a .lnk file, so the window and the shortcut share one taskbar button."""
    if sys.platform != "win32":
        return False
    try:
        with _Com():
            store = ctypes.c_void_p()
            iid = _guid(_IID_IPROPERTYSTORE)
            hr = ctypes.windll.shell32.SHGetPropertyStoreFromParsingName(
                ctypes.c_wchar_p(str(Path(lnk).resolve())), None, _GPS_READWRITE, ctypes.byref(iid), ctypes.byref(store))
            if hr < 0 or not store:
                raise OSError(f"SHGetPropertyStoreFromParsingName failed ({hr & 0xFFFFFFFF:#x})")
            _set_strings(store, {_PID_ID: APP_ID}, commit=True)
        return True
    except Exception:
        logger.warning("Could not set the taskbar ID on %s", lnk, exc_info=True)
        return False


def read_shortcut_app_id(lnk: Path | str) -> str | None:
    """The AppUserModelID stored in a .lnk file, or None (used to check old shortcuts and in tests)."""
    if sys.platform != "win32":
        return None
    try:
        with _Com():
            store = ctypes.c_void_p()
            iid = _guid(_IID_IPROPERTYSTORE)
            hr = ctypes.windll.shell32.SHGetPropertyStoreFromParsingName(
                ctypes.c_wchar_p(str(Path(lnk).resolve())), None, 0, ctypes.byref(iid), ctypes.byref(store))
            if hr < 0 or not store:
                return None
            vtable = ctypes.cast(store, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
            get_value = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(_PROPERTYKEY),
                                           ctypes.POINTER(_PROPVARIANT))(vtable[5])
            release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])
            value = _PROPVARIANT()
            try:
                hr = get_value(store, ctypes.byref(_PROPERTYKEY(_guid(_FMTID_APPUSERMODEL), _PID_ID)),
                               ctypes.byref(value))
                text = ctypes.wstring_at(value.value) if hr >= 0 and value.vt == _VT_LPWSTR and value.value else None
                ctypes.windll.ole32.PropVariantClear(ctypes.byref(value))
                return text
            finally:
                release(store)
    except Exception:
        logger.warning("Could not read the taskbar ID of %s", lnk, exc_info=True)
        return None


def set_window_identity(hwnd: int, relaunch_command: str, display_name: str, icon: Path | None) -> bool:
    """Give the window our ID and a relaunch command, so pinning its taskbar button pins a working shortcut."""
    if sys.platform != "win32":
        return False
    try:
        with _Com():
            store = ctypes.c_void_p()
            iid = _guid(_IID_IPROPERTYSTORE)
            hr = ctypes.windll.shell32.SHGetPropertyStoreForWindow(
                ctypes.c_void_p(hwnd), ctypes.byref(iid), ctypes.byref(store))
            if hr < 0 or not store:
                raise OSError(f"SHGetPropertyStoreForWindow failed ({hr & 0xFFFFFFFF:#x})")
            values = {_PID_ID: APP_ID, _PID_RELAUNCH_COMMAND: relaunch_command, _PID_RELAUNCH_NAME: display_name}
            if icon is not None:
                values[_PID_RELAUNCH_ICON] = f"{icon},0"
            _set_strings(store, values, commit=False)
        return True
    except Exception:
        logger.warning("Could not set the window's taskbar identity", exc_info=True)
        return False


def _special_folder(csidl: int) -> Path | None:
    buffer = ctypes.create_unicode_buffer(260)
    if ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buffer) != 0:
        return None
    return Path(buffer.value)


def our_shortcuts(name: str) -> list[Path]:
    """Existing shortcuts called "<name>.lnk" (or Windows' copies "<name> (2).lnk"): desktop, Startup
    folder and the pinned taskbar copies."""
    if sys.platform != "win32":
        return []
    folders = [_special_folder(_CSIDL_DESKTOPDIRECTORY), _special_folder(_CSIDL_STARTUP)]
    appdata = os.environ.get("APPDATA")
    if appdata:
        folders.append(Path(appdata) / "Microsoft" / "Internet Explorer" / "Quick Launch" / "User Pinned" / "TaskBar")
    found = []
    for folder in folders:
        if folder is None or not folder.is_dir():
            continue
        for lnk in sorted(folder.glob("*.lnk")):
            stem = lnk.stem
            if stem == name or (stem.startswith(f"{name} (") and stem.endswith(")") and stem[len(name) + 2:-1].isdigit()):
                found.append(lnk)
    return found
