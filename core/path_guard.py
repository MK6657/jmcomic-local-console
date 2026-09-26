"""路径安全检查模块
方案A：第一版下载目录固定为 PROJECT_ROOT/downloads/
不允许用户修改到外部路径。

安全说明：
- 使用 os.path.realpath() 而非 Path.resolve() 来解析路径，
  因为 Path.resolve() 在 Windows 上不解析 junction 点，
  而 os.path.realpath() 使用 GetFinalPathNameByHandleW 内核调用，
  可正确解析符号链接和 junction 点，防止路径遍历绕过。
"""
import os
from pathlib import Path

from .path_utils import get_app_root

PROJECT_ROOT = get_app_root()
DOWNLOAD_ROOT = PROJECT_ROOT / "downloads"


def _real_resolve(path: str | Path) -> Path:
    """解析路径的真实路径（处理符号链接和 Windows junction 点）"""
    return Path(os.path.realpath(str(path), strict=False))


def is_safe_path(path: str | Path, root: Path | None = None) -> bool:
    """检查路径是否在 DOWNLOAD_ROOT 内。
    root：调用方已用 get_download_root() 解析好的下载目录（批量检查很多路径时只解析一次）；不传则现场解析。"""
    root = _real_resolve(DOWNLOAD_ROOT) if root is None else root
    target = _real_resolve(path)
    try:
        target.relative_to(root)
        return True
    except ValueError:
        return False


def safe_resolve(path: str | Path) -> Path | None:
    """安全解析路径，返回 Path（安全）或 None（不安全）"""
    root = _real_resolve(DOWNLOAD_ROOT)
    target = _real_resolve(path)
    try:
        target.relative_to(root)
        return target
    except ValueError:
        return None


def get_download_root() -> Path:
    return _real_resolve(DOWNLOAD_ROOT)
