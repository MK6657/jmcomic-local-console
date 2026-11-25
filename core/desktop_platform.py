"""Optional desktop operations; callers retain all filesystem authorization."""
import ctypes
import os
from pathlib import Path
import subprocess
import threading
import time

_REAL_STARTFILE = getattr(os, "startfile", None)

_ASFW_ANY = -1
_SW_SHOW = 5
_SW_RESTORE = 9
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_SHOWWINDOW = 0x0040


class UnsupportedDesktopOperation(OSError):
    """This process has no native desktop folder opener."""


def supports_open_folder() -> bool:
    return callable(getattr(os, "startfile", None))


def _win32_user32():
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [enum_proc, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]
    user32.AllowSetForegroundWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]
    user32.SetWindowPos.restype = wintypes.BOOL
    return user32, enum_proc


def _snapshot_explorer_windows() -> dict[int, str]:
    try:
        user32, enum_proc = _win32_user32()
        windows: dict[int, str] = {}

        def callback(hwnd, _lparam):
            if user32.IsWindowVisible(hwnd):
                cls = ctypes.create_unicode_buffer(64)
                user32.GetClassNameW(hwnd, cls, 64)
                if cls.value == "CabinetWClass":
                    title = ctypes.create_unicode_buffer(512)
                    user32.GetWindowTextW(hwnd, title, 512)
                    windows[int(hwnd)] = title.value
            return True

        user32.EnumWindows(enum_proc(callback), 0)
        return windows
    except Exception:
        return {}


def _title_matches_folder(title: str, folder: Path) -> bool:
    title_cf = title.casefold().strip()
    if not title_cf:
        return False
    name_cf = folder.name.casefold()
    if not name_cf:
        return True
    full_cf = str(folder).casefold()
    return (
        title_cf == name_cf
        or title_cf.startswith(name_cf + " -")
        or title_cf.startswith(name_cf + " —")
        or full_cf in title_cf
        or ("\\" + name_cf) in title_cf
    )


def _force_window_foreground(hwnd: int) -> None:
    from ctypes import wintypes

    user32, _ = _win32_user32()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentThreadId.argtypes = []
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD

    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, _SW_RESTORE)
    else:
        user32.ShowWindow(hwnd, _SW_SHOW)

    fg_hwnd = user32.GetForegroundWindow()
    cur_tid = kernel32.GetCurrentThreadId()
    fg_tid = user32.GetWindowThreadProcessId(fg_hwnd, None) if fg_hwnd else 0
    target_tid = user32.GetWindowThreadProcessId(hwnd, None)

    attached_fg = bool(fg_tid and fg_tid != cur_tid and user32.AttachThreadInput(cur_tid, fg_tid, True))
    attached_target = bool(target_tid and target_tid not in (cur_tid, fg_tid)
                           and user32.AttachThreadInput(cur_tid, target_tid, True))
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        if user32.GetForegroundWindow() != hwnd:
            flags = _SWP_NOMOVE | _SWP_NOSIZE | _SWP_SHOWWINDOW
            user32.SetWindowPos(hwnd, wintypes.HWND(-1), 0, 0, 0, 0, flags)
            user32.SetWindowPos(hwnd, wintypes.HWND(-2), 0, 0, 0, 0, flags)
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
    finally:
        if attached_target:
            user32.AttachThreadInput(cur_tid, target_tid, False)
        if attached_fg:
            user32.AttachThreadInput(cur_tid, fg_tid, False)


def _focus_explorer_window(folder: Path, before_titles: dict[int, str]) -> None:
    deadline = time.monotonic() + 2.0
    fallback_deadline = time.monotonic() + 0.45
    while time.monotonic() < deadline:
        current = _snapshot_explorer_windows()
        created = [hwnd for hwnd in current if hwnd not in before_titles]
        if created:
            matched = next((hwnd for hwnd in created if _title_matches_folder(current[hwnd], folder)), created[0])
            time.sleep(0.08)
            _force_window_foreground(matched)
            return
        changed = [hwnd for hwnd, title in current.items() if before_titles.get(hwnd) != title]
        if changed:
            matched = next((hwnd for hwnd in changed if _title_matches_folder(current[hwnd], folder)), changed[0])
            _force_window_foreground(matched)
            return
        if time.monotonic() >= fallback_deadline:
            existing = next((hwnd for hwnd, title in current.items() if _title_matches_folder(title, folder)), None)
            if existing is not None:
                _force_window_foreground(existing)
                return
        time.sleep(0.05)


def _select_in_explorer_windows(select_path, fallback_target: str) -> None:
    """Highlight a file in Windows Explorer via SHOpenFolderAndSelectItems (handles spaces and commas)."""
    import ctypes
    from ctypes import wintypes

    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoUninitialize.argtypes = []
    ole32.CoUninitialize.restype = None
    shell32.ILCreateFromPathW.argtypes = [wintypes.LPCWSTR]
    shell32.ILCreateFromPathW.restype = ctypes.c_void_p
    shell32.SHOpenFolderAndSelectItems.argtypes = [
        ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p, wintypes.DWORD
    ]
    shell32.SHOpenFolderAndSelectItems.restype = ctypes.c_long
    shell32.ILFree.argtypes = [ctypes.c_void_p]
    shell32.ILFree.restype = None

    hr_init = ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
    need_uninit = hr_init in (0, 1)  # S_OK or S_FALSE
    pidl = None
    try:
        pidl = shell32.ILCreateFromPathW(os.path.abspath(os.fspath(select_path)))
        if pidl:
            hr = shell32.SHOpenFolderAndSelectItems(pidl, 0, None, 0)
            if hr == 0:
                return
    except Exception:
        pass
    finally:
        if pidl:
            try:
                shell32.ILFree(pidl)
            except Exception:
                pass
        if need_uninit:
            try:
                ole32.CoUninitialize()
            except Exception:
                pass
    _REAL_STARTFILE(fallback_target)


def open_folder(path, *, select=None) -> None:
    """Delegate one authorized path to the native opener without invoking a shell."""
    opener = getattr(os, "startfile", None)
    if not callable(opener):
        raise UnsupportedDesktopOperation("当前系统不支持直接打开文件夹，请手动前往相应目录")
    target = os.fspath(path)
    real_windows = os.name == "nt" and _REAL_STARTFILE is not None and opener is _REAL_STARTFILE
    before_titles = _snapshot_explorer_windows() if real_windows else None
    if real_windows:
        try:
            user32, _ = _win32_user32()
            user32.AllowSetForegroundWindow(_ASFW_ANY & 0xFFFFFFFF)
        except Exception:
            pass
    if select is not None and real_windows:
        _select_in_explorer_windows(select, target)
    else:
        opener(target)
    if real_windows and before_titles is not None:
        threading.Thread(
            target=_focus_explorer_window,
            args=(Path(target), before_titles),
            name="explorer-foreground",
            daemon=True,
        ).start()
