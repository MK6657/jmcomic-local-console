"""Keep unfinished writes out of the library, then publish validated files atomically.

Staging lives below the destination directory, on the same filesystem.  Its reserved
directory name is also understood by file discovery and archive readers.  This module
does not decide where the download root is: callers still authorize their destination.
"""
import os
import re
import shutil
import stat
import tempfile
import threading
from contextlib import contextmanager, nullcontext
from pathlib import Path

STAGING_DIRECTORY = ".jmcomic-staging"
_REPARSE_POINT = 0x400
# tempfile uses exactly eight lowercase letters/digits/underscores.  Restrict this
# legacy rule to the numeric page names and two formats our downloader generated;
# e.g. 00001.foo.webp, cover.draft.webp and ordinary dotted chapter names stay valid.
_TEMP_PAGE = re.compile(r"[0-9]{5,}\.(?:tmp-)?[a-z0-9_]{8}\.(?i:webp|gif)\Z", re.ASCII)
_SUFFIX = re.compile(r"\.[A-Za-z0-9]+\Z", re.ASCII)
_staging_lock = threading.Lock()  # create an attempt / remove an empty root, never the network write


def use_root_staging(root: Path) -> bool:
    """Keep legacy Win32 temporary names off deep chapter paths without moving any durable data."""
    from .validation import legacy_windows_paths, path_units
    if not legacy_windows_paths() or str(root).startswith('\\\\?\\'):
        return False
    # Even an ID-only chapter may require twenty digits; the other components
    # are the existing v5 workspace layout and the names mkstemp/mkdtemp emit.
    longest = (Path(root) / '.jmcomic-work' / ('0' * 32) / 'pages' / ('__' + '9' * 20)
               / STAGING_DIRECTORY / 'attempt-xxxxxxxx' / 'page-xxxxxxxx.webp')
    return path_units(longest) > 259


def staging_parent(parent: Path) -> Path:
    from .path_guard import DOWNLOAD_ROOT
    # Backup/checkpoint files outside the download tree keep their own volume.
    root = Path(DOWNLOAD_ROOT)
    return root if parent.is_relative_to(root) and use_root_staging(root) else parent


@contextmanager
def publication_tree(base: Path, root: Path):
    """Create a unique private directory, coordinating its parent with page/CBZ writers."""
    parent = root if use_root_staging(root) else base
    staging = parent / STAGING_DIRECTORY
    with _staging_lock:
        ancestors = {path: _directory_identity(path) for path in reversed((parent, *parent.parents))}
        staging.mkdir(exist_ok=True)
        identity = _directory_identity(staging)
        temp = Path(tempfile.mkdtemp(prefix='pub-', dir=staging))
        temp_identity = _directory_identity(temp)
    try:
        yield temp
    finally:
        # A successful seal/project has already renamed the tree. Otherwise
        # remove only this attempt, never a substituted directory or junction.
        try:
            _check_directories(ancestors)
            if (_directory_identity(staging) == identity and temp.parent == staging
                    and _directory_identity(temp) == temp_identity):
                shutil.rmtree(temp)
        except FileNotFoundError:
            pass
        with _staging_lock:
            try:
                _check_directories(ancestors)
                if _directory_identity(staging) == identity:
                    staging.rmdir()  # only if empty; concurrent or interrupted attempts stay intact
            except OSError:
                pass


def is_temporary_path(path: str | Path) -> bool:
    """Identify reserved staging paths and known old page temps; never delete them.

    Accept both filesystem paths and ZIP member names, including Windows separators.
    The old tempfile pattern is deliberately narrow rather than hiding dotted images.
    """
    parts = str(path).replace("\\", "/").split("/")
    return (any(part.casefold() == STAGING_DIRECTORY for part in parts)
            or bool(_TEMP_PAGE.fullmatch(parts[-1])))


def _redirected(info) -> bool:
    return (stat.S_ISLNK(info.st_mode)
            or bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT))


def _directory_identity(path: Path) -> tuple:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or _redirected(info):
        raise ValueError("暂存与发布目录不能是符号链接或目录联接")
    return info.st_dev, info.st_ino


def _check_directories(identities: dict[Path, tuple]) -> None:
    for path, identity in identities.items():
        if _directory_identity(path) != identity:
            raise ValueError("暂存或发布目录在写入期间已被替换")


def _plain_file(path: Path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or _redirected(info):
        raise ValueError("暂存与发布文件不能是符号链接或特殊文件")
    return info


def _check_target(target: Path) -> None:
    try:
        _plain_file(target)
    except FileNotFoundError:
        pass


class StagedFile:
    """One private write. ``path`` preserves the suffix needed by the SDK/Pillow."""

    def __init__(self, path: Path, target: Path, identities: dict[Path, tuple]):
        self.path = path
        self.target = target
        self._identities = identities
        self._published = False
        self._closed = False

    def publish(self, validate, *, lock=None, before_replace=None) -> Path:
        """Validate a complete regular file, then replace the destination on this disk.

        ``validate(path)`` must return true; rejection or any exception preserves the
        old destination.  Directory identities and link checks are repeated before
        publication. An optional publication lock covers only the final checks and
        rename, so a full archive CRC check does not serialize unrelated packers.
        ``before_replace()`` may raise to veto publication after all checks, inside the lock.
        This guards replacement of staging directories between calls;
        it is not a promise of power-loss durability or protection from a privileged
        process racing the final filesystem operation.
        """
        if self._closed or self._published:
            raise RuntimeError("暂存文件已经关闭或发布")
        _check_directories(self._identities)
        before = _plain_file(self.path)
        if before.st_size <= 0 or not validate(self.path):
            raise IOError("暂存文件校验失败，原文件已保留")
        with (lock if lock is not None else nullcontext()):
            _check_directories(self._identities)
            after = _plain_file(self.path)
            signature = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
            if signature(before) != signature(after):
                raise IOError("暂存文件在校验期间发生变化，原文件已保留")
            _check_target(self.target)
            if before_replace is not None:
                before_replace()
            os.replace(self.path, self.target)
            self._published = True
        return self.target

    def discard(self) -> None:
        """Clean up only this attempt; never traverse or recursively delete a folder."""
        if self._closed:
            return
        self._closed = True
        try:
            with _staging_lock:
                _check_directories(self._identities)
                self.path.unlink(missing_ok=True)
                self.path.parent.rmdir()
                # Another attempt keeps this root nonempty. Creation takes the
                # same lock so it cannot race this final empty-directory cleanup.
                self.path.parents[1].rmdir()
        except (OSError, ValueError):
            # Leave anything whose directory ownership changed in place.  In
            # particular, never follow a substituted link to clean another folder.
            pass


@contextmanager
def staged_file(target: str | Path, *, suffix: str | None = None):
    """Yield a private file and discard it on exit unless explicitly published.

    The destination's parent must already exist. All of its ancestors, the staging
    root and the per-attempt directory must be ordinary directories. ``suffix`` is
    for an auxiliary raw GIF: its context can simply exit without publishing it.
    On deep legacy Win32 download roots, stage under that same root rather than
    repeating the task, shelf and chapter components in every temporary path.
    """
    target = Path(os.path.abspath(target))
    if is_temporary_path(target):
        raise ValueError("发布目标不能是暂存路径")
    suffix = target.suffix if suffix is None else suffix
    if not _SUFFIX.fullmatch(suffix):
        raise ValueError("暂存文件需要一个普通文件扩展名")
    identities = {path: _directory_identity(path)
                  for path in reversed((target.parent, *target.parent.parents))}
    _check_target(target)
    staging = staging_parent(target.parent) / STAGING_DIRECTORY
    with _staging_lock:
        try:
            staging.mkdir()
        except FileExistsError:
            pass
        identities[staging] = _directory_identity(staging)
        _check_directories(identities)
        attempt = Path(tempfile.mkdtemp(prefix="attempt-", dir=staging))
        identities[attempt] = _directory_identity(attempt)
    staged = None
    try:
        _check_directories(identities)
        fd, name = tempfile.mkstemp(prefix="page-", suffix=suffix, dir=attempt)
        os.close(fd)
        staged = StagedFile(Path(name), target, identities)
        yield staged
    finally:
        if staged is not None:
            staged.discard()
        else:
            try:
                with _staging_lock:
                    _check_directories(identities)
                    attempt.rmdir()
                    staging.rmdir()
            except (OSError, ValueError):
                pass
