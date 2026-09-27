"""Deterministic file discovery that never follows symlinks or Windows junctions."""
import os
import re
import stat
from pathlib import Path

_REPARSE_POINT = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT: junctions, symlinks and other redirections


def natural_key(path):
    return [int(part) if part.isascii() and part.isdigit() else part.lower()
            for part in re.split(r"([0-9]+)", str(path))]


def is_link(path):
    info = Path(path).lstat()
    return (stat.S_ISLNK(info.st_mode)
            or bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT))


def _entry_is_link(entry):
    """is_link() for an os.scandir entry, answered from what the folder listing already returned
    (on Windows DirEntry.stat(follow_symlinks=False) needs no extra system call)."""
    if entry.is_symlink():
        return True
    if os.name != "nt":
        return False
    return bool(entry.stat(follow_symlinks=False).st_file_attributes & _REPARSE_POINT)


def iter_safe_files(root, accept=None):
    """Yield regular descendants lazily, top-down in folder-listing order (not sorted); never follows links.

    Linked entries (symlinks, junctions, other reparse points) are skipped whether they are files or folders;
    unreadable folders are skipped like os.walk does. accept(path), if given, filters the files. Each folder is
    listed once and its entries are classified from the listing itself, so a caller that only needs the first
    match (next(...)) pays for one listing per level it descends and nothing for the rest of the tree.
    A linked root raises ValueError on the first next(), as safe_files always did.
    """
    root = Path(root)
    if is_link(root):
        raise ValueError("源目录不能是符号链接或目录联接")
    pending = [str(root)]
    while pending:
        folder = pending.pop()
        subfolders = []
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    try:
                        if _entry_is_link(entry):
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            subfolders.append(entry.path)
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    path = Path(entry.path)
                    if accept is None or accept(path):
                        yield path
        except OSError:
            continue
        pending.extend(reversed(subfolders))  # depth-first, in listing order


def safe_files(root):
    """Return regular descendants only; reject links instead of exporting their targets."""
    root = Path(root)
    return sorted(iter_safe_files(root), key=lambda path: natural_key(path.relative_to(root)))
