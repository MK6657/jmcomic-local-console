"""
路径工具模块 —— 处理 PyInstaller 打包后的资源路径

在开发模式（未打包）时，资源根目录为项目根目录。
在 PyInstaller 打包后，资源根目录为 sys._MEIPASS（exe 内临时解压目录）。

注意：
  - 所有需要读写的运行时目录（data/ downloads/ logs/）应在 exe 同级目录。
  - 只有 Flask 模板/静态文件等只读资源走 _MEIPASS。
"""
import sys
from pathlib import Path


def get_resource_root() -> Path:
    """获取只读资源根目录（Flask templates/static 所在目录）。

    打包后：sys._MEIPASS（exe 内部的临时解压目录）
    开发时：项目根目录
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent


def get_app_root() -> Path:
    """获取应用数据根目录（exe 同级，用于运行时写入 runtime/data/ runtime/logs/ downloads/）。

    打包后：exe 所在目录
    开发时：项目根目录
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def ensure_dirs() -> None:
    """确保运行时目录存在"""
    root = get_app_root()
    for name in ("runtime/data", "runtime/logs", "downloads"):
        (root / name).mkdir(parents=True, exist_ok=True)
