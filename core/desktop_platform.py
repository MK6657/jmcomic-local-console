"""Optional desktop operations; callers retain all filesystem authorization."""
import os


class UnsupportedDesktopOperation(OSError):
    """This process has no native desktop folder opener."""


def supports_open_folder() -> bool:
    return callable(getattr(os, "startfile", None))


def open_folder(path) -> None:
    """Delegate one authorized path to the native opener without invoking a shell."""
    opener = getattr(os, "startfile", None)
    if not callable(opener):
        raise UnsupportedDesktopOperation("当前系统不支持直接打开文件夹，请手动前往相应目录")
    opener(os.fspath(path))
