"""Download directory names shared by workers, snapshot publication and organization."""
import hashlib
import json
import os
import re
from pathlib import Path

from .file_tree import is_link
from .logger import log
from .path_guard import is_safe_path
from .storage import STAGING_DIRECTORY, staged_file
from . import validation
from .validation import (directory_budget, fit_dirname, path_units,
                         safe_dirname, short_root_error, validate_numeric)

CHAPTER_MARKER = ".jm-chapter.json"
CHAPTER_FORMAT = 2
# staged_file uses independent eight-character tempfile names. Both the private
# image and the chapter marker have a five-character extension (.webp/.json).
_STAGED_PAGE = Path(STAGING_DIRECTORY, 'attempt-xxxxxxxx', 'page-xxxxxxxx.webp')
_STAGED_ARCHIVE = _STAGED_PAGE.with_suffix('.cbz')  # .zip has the same length


def _workspace_chapter_budget(work):
    # Both seal() and project() copy the complete chapter into a fresh temporary
    # tree on this volume. Their marker is longer than the numbered page name.
    prepare = work.parent / STAGING_DIRECTORY / 'pub-xxxxxxxx'
    return min(directory_budget(work, _STAGED_PAGE), directory_budget(prepare, CHAPTER_MARKER),
               directory_budget(work.parent / 'snapshot', CHAPTER_MARKER))


def _public_chapter_budget(target, view_parent):
    return min(directory_budget(parent, CHAPTER_MARKER) for parent in (target, view_parent / target.name))


def album_output_dir(root, job_id, title, album_id, photos, *, preferred=None, pack=False):
    """Preflight the selected chapters before workers start, including on resume."""
    from .publication import WORK, VIEWS, _key, _load, checked
    root = Path(root)
    if not validate_numeric(str(album_id)):
        raise ValueError('专辑 ID 无效')
    title = safe_dirname(title)
    suffix = f'_{album_id}'
    job_key = _key(job_id)
    work = root / WORK / job_key / 'pages'
    work_budget = _workspace_chapter_budget(work)
    # Check the fixed cost before even querying a potentially unaddressable root.
    ids = [str(photo.photo_id) for photo in photos]
    if not all(map(validate_numeric, ids)):
        raise ValueError('章节 ID 无效')
    required = 2 + max(map(len, ids))
    if work_budget < required:
        raise short_root_error()
    view_parent = root / VIEWS / job_key
    original = root / fit_dirname(title, suffix, 255)
    if preferred is None and os.path.isdir(original):
        preferred = original
    # Stable across later selections: half the public path can be needed by the
    # repeated album name in Book/Book.cbz. Chapters share the remaining space.
    stable = directory_budget(view_parent, _STAGED_ARCHIVE, archive=True)
    requested = preferred or root / fit_dirname(title, suffix, max(len(suffix), stable), archive=True)
    if not validation.legacy_windows_paths():
        return requested
    target = requested
    if root.is_dir():
        checked(root, work.relative_to(root))  # preflight must not read through a redirected workspace
        meta = work.parent / 'workspace.json'
        if meta.exists():
            target = checked(root, _load(meta)['target'])
    if pack:
        for parent in ((target.parent, view_parent) if target.exists() else (target.parent,)):
            if path_units(target.name) > directory_budget(parent, _STAGED_ARCHIVE, archive=True):
                raise short_root_error()
    budget = min(work_budget, _public_chapter_budget(target, view_parent))
    if budget < required:
        raise short_root_error()
    if not work.is_dir():  # no previous chapter name or collision suffix to retain
        return requested
    for photo in photos:
        # Mirror creation without touching disk, including collision suffixes and
        # the old marked name of a paused chapter. Never rename it to make it fit.
        folder = _chapter_directory(work, photo, create=False, plan=True, budget=budget)
        if path_units(folder.name) > budget:
            raise short_root_error()
    return requested


def author_output_name(root, source, name, author, *, pack, check_control):
    """Keep every existing descendant addressable after moving a public directory."""
    title = safe_dirname(author)
    leaves = [Path(name) / '.jm-view.json']
    if pack:
        leaves.extend((Path(name) / (name + '.cbz'), Path(name) / _STAGED_ARCHIVE))
    budget = directory_budget(root, *leaves)
    if validation.legacy_windows_paths() and path_units(Path(root) / title / name) > path_units(source):
        def fail(err):
            raise err
        for folder, dirs, files in os.walk(source, onerror=fail):
            check_control()
            relative = Path(name) / Path(folder).relative_to(source)
            # Include empty directories; don't traverse links or Windows junctions.
            longest_dir = max(dirs, key=path_units, default='')
            longest_file = max(files, key=path_units, default='x')
            budget = min(budget, directory_budget(root, relative / longest_dir / 'x', relative / longest_file))
            dirs[:] = [d for d in dirs if not is_link(Path(folder) / d)]
    short = fit_dirname(title, '', budget)
    if short == title:
        return title
    # Two long author names sharing a display prefix should not land on the same shelf.
    digest = hashlib.sha256(author.encode('utf-8', errors='surrogatepass')).hexdigest()[:8]
    return fit_dirname(title, '~' + digest, budget) if budget > len(digest) else digest[:budget]


def _marker(directory):
    marker = directory / CHAPTER_MARKER
    try:
        if is_link(directory) or is_link(marker):
            return {}
        record = json.loads(marker.read_text(encoding="utf-8"))
        return record if isinstance(record, dict) else {}
    except (OSError, ValueError):
        return {}


def chapter_output_dir(album_dir, photo, *, create=True):
    """Never adopt an unmarked folder. A read-only lookup follows the writer's exact suffix rules."""
    from .publication import WORK, VIEWS, _load, checked
    album_dir = Path(album_dir)
    budget = directory_budget(album_dir, _STAGED_PAGE)
    if not is_safe_path(album_dir):
        raise ValueError('章节输出路径越权')
    if (validation.legacy_windows_paths() and album_dir.name == 'pages' and album_dir.parent.parent.name == WORK
            and (album_dir.parent / 'workspace.json').exists()):
        root = album_dir.parents[2]
        meta = checked(root, (album_dir.parent / 'workspace.json').relative_to(root))
        target = checked(root, _load(meta)['target'])
        budget = min(_workspace_chapter_budget(album_dir), _public_chapter_budget(target, root / VIEWS / album_dir.parent.name))
    return _chapter_directory(album_dir, photo, create=create, budget=budget)


def _chapter_directory(album_dir, photo, *, create, budget, plan=False):
    photo_id = str(photo.photo_id)
    if not validate_numeric(photo_id):
        raise ValueError("章节 ID 无效")
    title = safe_dirname(photo.name or photo_id, max_len=100)
    identity = f'__{photo_id}'
    # Published folders can have been created under a different path budget
    # (or before budgeting existed). Only accept the same title prefix AND ID marker.
    if not create and not plan and '.jmcomic-work' not in (p.casefold() for p in album_dir.parts):
        if not album_dir.is_dir() or is_link(album_dir):
            return None
        old = album_dir / (title + identity)
        if _marker(old).get('photo_id') == photo_id:
            return old
        ending = re.compile(re.escape(identity) + r'(?:_[2-9]|_[1-9][0-9]+)?\Z')
        for prior in sorted(album_dir.iterdir()):
            match = ending.search(prior.name)
            if (match and fit_dirname(title, match[0], path_units(prior.name)) == prior.name
                    and _marker(prior).get('photo_id') == photo_id):
                return prior
        return None
    base = fit_dirname(title, identity, budget)
    if '.jmcomic-work' in (part.casefold() for part in Path(album_dir).parts) and Path(album_dir).is_dir():
        # A paused task owns these chapter folders. Upstream may rename a chapter
        # while paused; keep its verified page identities under the original name.
        if not (Path(album_dir) / base).exists():
            for prior in Path(album_dir).iterdir():
                if prior.is_dir() and not is_link(prior) and _marker(prior).get('photo_id') == photo_id:
                    return prior
    suffix = 1
    while True:
        name = base if suffix == 1 else fit_dirname(title, f'{identity}_{suffix}', budget)
        directory = Path(album_dir) / name
        marker = directory / CHAPTER_MARKER
        if not is_safe_path(directory) or not is_safe_path(marker):
            raise ValueError("章节输出路径越权")
        if os.path.lexists(directory):
            if _marker(directory).get("photo_id") == photo_id:
                return directory
            suffix += 1
            continue
        if not create:
            return directory if plan else None
        try:
            directory.mkdir()
        except FileExistsError:
            continue
        with staged_file(marker) as staged:
            staged.path.write_text(json.dumps({'photo_id': photo_id, 'format': CHAPTER_FORMAT}), encoding='utf-8')
            staged.publish(lambda p: p.stat().st_size > 0)
        return directory


def chapter_format(directory):
    value = _marker(Path(directory)).get("format", 1)
    return value if type(value) is int else 1


def mark_chapter_current(album_dir, photo):
    """Only upgrade the GIF format marker once every page has been confirmed."""
    try:
        directory = chapter_output_dir(album_dir, photo)
        if chapter_format(directory) < CHAPTER_FORMAT:
            with staged_file(directory / CHAPTER_MARKER) as staged:
                staged.path.write_text(json.dumps({"photo_id": str(photo.photo_id), "format": CHAPTER_FORMAT}),
                                       encoding="utf-8")
                staged.publish(lambda path: path.stat().st_size > 0)
    except (OSError, ValueError) as error:
        log.warning(f"更新章节目录标记失败 photo_id={photo.photo_id} error={error}")
