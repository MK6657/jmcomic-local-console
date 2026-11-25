"""Portable path rules shared by ordinary backup creation and instance restore."""
from pathlib import PurePath, PurePosixPath, PureWindowsPath


class BackupPathError(ValueError):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


_WINDOWS_RESERVED = {"con", "prn", "aux", "nul", "conin$", "conout$",
                     *(prefix + digit for prefix in ("com", "lpt") for digit in "123456789¹²³")}


def safe_parts(parts, *, context="备份") -> tuple[str, ...]:
    result = tuple(parts)
    for part in result:
        if (not part or part in (".", "..") or part.endswith((".", " "))
                or len(part.encode("utf-16-le", "surrogatepass")) // 2 > 255
                or any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF or char in '<>:"/\\|?*'
                       for char in part)
                or part.split(".", 1)[0].rstrip(" ").casefold() in _WINDOWS_RESERVED):
            raise BackupPathError("unsafe_path", f"{context}：包含无法安全迁移的路径名称")
    return result


def parse_source_root(value: str) -> PurePath:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise BackupPathError("missing_source_root", "备份未记录源下载根目录，不能自动恢复到新实例")
    windows = PureWindowsPath(value)
    root = windows if windows.is_absolute() else PurePosixPath(value)
    if not root.is_absolute() or any(part in (".", "..") for part in value.replace("\\", "/").split("/")):
        raise BackupPathError("unsafe_path", "备份的源下载根目录无效")
    safe_parts(root.parts[1:], context="源下载根目录")
    return root


def output_path_tables(version: int) -> tuple[str, ...]:
    return ("jobs", "resource_catalog") if version >= 3 else ("jobs",)


def output_parts(table: str, value, source_root: PurePath) -> tuple[str, ...] | None:
    """Return the relative components; NULL/empty need no rewrite, the root maps to ()."""
    if value is None or value == "":
        return None
    column = f"{table}.output_path"
    if (not isinstance(value, str) or "\x00" in value
            or any(part in (".", "..") for part in value.replace("\\", "/").split("/"))):
        raise BackupPathError("unmapped_path", f"{column} 包含不能映射的下载路径")
    path = type(source_root)(value)
    try:
        if not path.is_absolute():
            raise ValueError()
        relative = path.relative_to(source_root)
    except ValueError:
        raise BackupPathError("unmapped_path", f"{column} 不是源下载根目录内的绝对路径") from None
    return safe_parts(relative.parts, context=column)
