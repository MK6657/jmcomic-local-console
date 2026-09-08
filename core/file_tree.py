"""Deterministic file discovery that never follows symlinks or Windows junctions."""
import os
import re
import stat
from pathlib import Path


def natural_key(path):
    return [int(part) if part.isascii() and part.isdigit() else part.lower()
            for part in re.split(r"([0-9]+)", str(path))]


def is_link(path):
    info = Path(path).lstat()
    return (stat.S_ISLNK(info.st_mode)
            or bool(getattr(info, "st_file_attributes", 0) & 0x400))


def safe_files(root):
    """Return regular descendants only; reject links instead of exporting their targets."""
    root = Path(root)
    if is_link(root):
        raise ValueError("源目录不能是符号链接或目录联接")
    result = []
    for parent, directories, filenames in os.walk(root, followlinks=False):
        directories[:] = [name for name in directories if not is_link(Path(parent) / name)]
        for name in filenames:
            path = Path(parent) / name
            if not is_link(path) and path.is_file():
                result.append(path)
    return sorted(result, key=lambda path: natural_key(path.relative_to(root)))
