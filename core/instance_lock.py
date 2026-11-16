"""One Windows owner for serving and offline maintenance of the local instance.

Keep the existing mutex name so maintenance also excludes older app versions.
The OS releases handles when the process exits; no port/process-name killing is
used. Importing this module has no filesystem or application side effects.
"""
from contextlib import contextmanager
import ctypes
import os
import threading

DEFAULT_MUTEX_NAME = "Local\\JMComicDownloader_AppInstance"
_registry_lock = threading.RLock()
_owned = {}


class InstanceBusyError(RuntimeError):
    pass


class InstanceLock:
    def __init__(self, name, handle, kernel):
        self.name = name
        self._handle = handle
        self._kernel = kernel
        self._references = 1
        self._closed = False

    def close(self):
        with _registry_lock:
            if self._closed:
                return
            self._closed = True
            _release_reference(self)


def _release_reference(lease):
    with _registry_lock:
        lease._references -= 1
        if lease._references == 0:
            lease._kernel.CloseHandle(lease._handle)
            _owned.pop(lease.name, None)


def acquire_instance_lock(name=DEFAULT_MUTEX_NAME):
    """Acquire a new ownership lease; a duplicate acquisition is always rejected."""
    if os.name != "nt":
        raise RuntimeError("当前维护入口仅支持 Windows 单实例锁")
    from ctypes import wintypes
    with _registry_lock:
        if name in _owned:
            raise InstanceBusyError("程序或维护操作已在运行，请先关闭后重试")
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.CreateMutexW(None, False, name)
        error = ctypes.get_last_error()
        if not handle:
            raise ctypes.WinError(error)
        if error == 183:
            kernel.CloseHandle(handle)
            raise InstanceBusyError("程序或维护操作已在运行，请先关闭后重试")
        result = InstanceLock(name, handle, kernel)
        _owned[name] = result
        return result


@contextmanager
def maintenance_lock(name=DEFAULT_MUTEX_NAME):
    """Borrow startup ownership for migration, or exclude all running app versions.

    Borrowing keeps the OS handle alive even if its original owner closes its own
    lease before maintenance finishes. Offline CLI commands acquire a fresh lease.
    """
    with _registry_lock:
        lease = _owned.get(name)
        borrowed = lease is not None
        if lease is None:
            lease = acquire_instance_lock(name)
        else:
            lease._references += 1
    try:
        yield lease
    finally:
        if borrowed:
            _release_reference(lease)
        else:
            lease.close()
