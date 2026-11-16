"""Private Windows shutdown notification; business cleanup stays on the main thread.

Only an existing, randomly named event can be signalled by another local process.
The watcher delivers one SIGINT through Python and never touches a task or curl.
Importing this module has no application, filesystem or WinAPI side effects.
"""
import _thread
import ctypes
import math
import os
import re
import threading
import uuid

SHUTDOWN_PROTOCOL = "windows-event-v1"
_PREFIX = "Local\\JMConsoleShutdown_"
_TOKEN = re.compile(r"[0-9a-fA-F]{32}\Z")
_EVENT_MODIFY_STATE = 0x0002
_ERROR_FILE_NOT_FOUND = 2
_ERROR_ALREADY_EXISTS = 183
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258
_WAIT_FAILED = 0xFFFFFFFF
_POLL_MS = 150


def _valid_token(token):
    return isinstance(token, str) and _TOKEN.fullmatch(token) is not None


def _kernel32():
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateEventW.restype = wintypes.HANDLE
    kernel.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.OpenEventW.restype = wintypes.HANDLE
    kernel.SetEvent.argtypes = [wintypes.HANDLE]
    kernel.SetEvent.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel


def _close_handle(kernel, handle):
    if not kernel.CloseHandle(handle):
        raise ctypes.WinError(ctypes.get_last_error())


def request_shutdown(token) -> bool:
    """Signal an existing private event; malformed tokens and WinAPI errors raise.

    False means that this platform has no Windows event or the named event no
    longer exists. No process is terminated and no HTTP request is sent.
    """
    if not _valid_token(token):
        raise ValueError("shutdown token must contain exactly 32 hexadecimal characters")
    if os.name != "nt":
        return False
    kernel = _kernel32()
    handle = kernel.OpenEventW(_EVENT_MODIFY_STATE, False, _PREFIX + token)
    if not handle:
        error = ctypes.get_last_error()
        if error == _ERROR_FILE_NOT_FOUND:
            return False
        raise ctypes.WinError(error)
    try:
        if not kernel.SetEvent(handle):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        _close_handle(kernel, handle)
    return True


class _ShutdownControl:
    def __init__(self, token, handle, kernel):
        self.token = token
        self._handle = handle
        self._kernel = kernel
        self._stop_requested = threading.Event()
        self._lock = threading.Lock()
        self._error = None
        self._callback = _thread.interrupt_main
        self._watcher = threading.Thread(target=self._watch, name="shutdown-event", daemon=True)

    @property
    def error(self):
        """First watcher/callback/CloseHandle error, retained even after retry."""
        with self._lock:
            return self._error

    def _remember_error(self, error):
        with self._lock:
            if self._error is None:
                self._error = error

    def _watch(self):
        try:
            while not self._stop_requested.is_set():
                result = self._kernel.WaitForSingleObject(self._handle, _POLL_MS)
                if result == _WAIT_TIMEOUT:
                    continue
                if result == _WAIT_OBJECT_0:
                    if not self._stop_requested.is_set():
                        self._callback()
                    return
                if result == _WAIT_FAILED:
                    raise ctypes.WinError(ctypes.get_last_error())
                raise OSError(f"unexpected shutdown event wait result: {result}")
        except BaseException as error:
            self._remember_error(error)

    def stop(self, timeout=1) -> bool:
        """Drain the watcher before closing its handle; retry if it is still busy.

        A timed-out callback keeps the handle open. WinAPI/callback errors remain
        visible through error and make every later stop return False as well.
        """
        timeout = float(timeout)
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("shutdown stop timeout must be finite and nonnegative")
        self._stop_requested.set()
        if threading.current_thread() is self._watcher:
            return False
        self._watcher.join(timeout)
        if self._watcher.is_alive():
            return False
        with self._lock:
            if self._handle is not None:
                try:
                    _close_handle(self._kernel, self._handle)
                except Exception as error:
                    if self._error is None:
                        self._error = error
                else:
                    self._handle = None
            return self._handle is None and self._error is None


def create_shutdown_control(token=None):
    """Start a Windows event watcher without reusing any existing named event.

    An absent/invalid token is replaced with uuid4().hex; other platforms return
    None. A pre-existing event is an error, even when a caller supplied its token.
    """
    if os.name != "nt":
        return None
    token = token if _valid_token(token) else uuid.uuid4().hex
    kernel = _kernel32()
    ctypes.set_last_error(0)
    handle = kernel.CreateEventW(None, True, False, _PREFIX + token)
    error = ctypes.get_last_error()
    if not handle:
        raise ctypes.WinError(error)
    try:
        if error == _ERROR_ALREADY_EXISTS:
            raise FileExistsError("shutdown event already exists")
        control = _ShutdownControl(token, handle, kernel)
        control._watcher.start()
    except BaseException:
        _close_handle(kernel, handle)
        raise
    return control
