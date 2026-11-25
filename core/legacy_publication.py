"""Read a v4 public album into a fresh, unpublished v5 view. Never modify the source."""
import json
import os
from pathlib import Path
import re
import shutil
import stat
import zipfile

from . import archive_pages
from .download_layout import CHAPTER_MARKER, _marker
from .file_tree import is_link, natural_key
from .flat_pages import read_flat_page_name
from .publication import VIEWS, WORK, checked, private_path
from .storage import STAGING_DIRECTORY, is_temporary_path
from .validation import EXPORT_IMAGE_EXTENSIONS, validate_numeric


def _ids(selection):
    try:
        ids = json.loads(selection)
    except (ValueError, TypeError):
        return set()
    if not isinstance(ids, list) or not all(validate_numeric(str(i)) for i in ids):
        return set()
    return {str(i) for i in ids}


def legacy_source(conn, root, album, job, previous):
    """(directory, eligible chapter ids or None for the original whole-tree view)."""
    rows = conn.execute('SELECT c.output_path,c.selected_photo_ids FROM resource_catalog c '
                        'LEFT JOIN published_tasks p USING(job_id) WHERE c.album_id=? AND '
                        'c.superseded_at IS NULL AND p.job_id IS NULL ORDER BY c.created_at DESC,c.job_order DESC',
                        (album,)).fetchall()
    if not rows or not rows[0]['output_path']:
        return None
    old = Path(rows[0]['output_path'])
    if (not old.is_relative_to(root) or old == root
            or any(part.casefold() in (WORK, VIEWS) for part in old.relative_to(root).parts)):
        return None
    old = checked(root, old.relative_to(root))
    writers = conn.execute('SELECT job_id,output_path,selected_photo_ids FROM jobs WHERE album_id=? '
                           "AND status!='completed' AND (job_id=? OR (output_path IS NOT NULL AND total_pages>0))",
                           (album, job))
    pending = set()
    for writer in writers:
        # A resumed v4 job now writes privately, but its old public pages still exist.
        path = previous if writer['job_id'] == job else writer['output_path']
        if not path or private_path(path, root):
            continue
        base, path = old, Path(path)
        if path != base:
            # Two different recorded paths may name the same directory via a Windows junction.
            try:
                base, path = (Path(os.path.realpath(p, strict=True)) for p in (base, path))
            except FileNotFoundError:
                continue
            if not (base.is_relative_to(path) or path.is_relative_to(base)):
                continue
        ids = _ids(writer['selected_photo_ids'])
        if not ids or path != base:
            return None  # whole album / malformed selection / overlapping roots: no safe ownership
        pending.update(ids)
    if not pending:
        return old, None
    proven = set()
    for row in rows:
        if row['output_path'] and Path(row['output_path']) == old:
            # A v4 "all chapters" request did not record which chapters existed at the time.
            proven.update(_ids(row['selected_photo_ids']))
    eligible = proven - pending
    return (old, eligible) if eligible else None


def _entries(folder, check):
    """Unlike a best-effort reader scan, any listing or stat error aborts publication."""
    if is_link(folder):
        raise OSError('旧资源目录已变成链接')
    with os.scandir(folder) as entries:
        for entry in entries:
            check()
            if entry.name.casefold() in (WORK, VIEWS, STAGING_DIRECTORY):
                continue
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                continue
            if (stat.S_ISDIR(info.st_mode) or
                    (stat.S_ISREG(info.st_mode) and info.st_size and not is_temporary_path(entry.name))):
                yield Path(entry.path), info


def _copy(path, dest, info):
    if is_link(path):
        raise OSError('旧资源文件已变成链接')
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, dest)
    after = path.lstat()
    if ((info.st_size, info.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
            or dest.stat().st_size != info.st_size):
        raise OSError('旧资源文件在发布期间发生变化')


def _archives(old, name, files):
    """Preserve the reader's own-CBZ/ZIP (newest first), then other-CBZ order across a rename."""
    own = [p for p in files if p.stem.lower() == old.name.lower()]
    if len({p.suffix.lower() for p in own}) != len(own):
        raise OSError('旧整册包的文件名大小写冲突')
    own.sort(key=lambda p: (-files[p].st_mtime_ns, p.suffix.lower()))
    other = sorted((p for p in files if p not in own and p.suffix.lower() == '.cbz'), key=natural_key)
    # If a fallback CBZ would become the new preferred pack, give the fallback series
    # neutral names in the same order. An unrelated ZIP is never a reader candidate.
    collision = any(p.stem.lower() == name.lower() for p in other)
    prefix = 'legacy-archive-' if not name.casefold().startswith('legacy-archive-') else 'archive-'
    return ([(p, name + p.suffix.lower()) for p in own] +
            [(p, f'{prefix}{i}.cbz' if collision else p.name) for i, p in enumerate(other, 1)])


def _marked(folders, eligible, check):
    owners = {}
    for folder in folders:
        check()
        match = re.search(r'__([0-9]+)(?:_[0-9]+)?\Z', folder.name)
        if not match or match[1] not in eligible:
            continue
        marker = folder / CHAPTER_MARKER
        try:
            info = marker.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode) or is_link(marker) or not 0 < info.st_size <= 4096:
            continue
        with marker.open('rb') as stream:
            data = stream.read(4097)
        if len(data) != info.st_size:
            raise OSError('旧章节标记在发布期间发生变化')
        try:
            record = json.loads(data)
        except ValueError:
            continue
        if isinstance(record, dict) and str(record.get('photo_id')) == match[1]:
            # Two marked directories for one id cannot both be attributed to the successful request.
            owners[match[1]] = (folder.name, data) if match[1] not in owners else None
    return dict(item for item in owners.values() if item is not None)


def _numbered_page(name):
    stem, suffix = os.path.splitext(name)
    return (suffix.lower() in EXPORT_IMAGE_EXTENSIONS and stem != '00000'
            and re.fullmatch(r'(?:[0-9]{5}|[1-9][0-9]{5,})', stem) is not None)


def _chapter_id(name):
    match = re.search(r'__([0-9]{1,20})(?:_[0-9]+)?\Z', name)
    return match[1] if match else None


def _page_target(name, owners, chapters, replacements, *, packed=False):
    parts = name.split('/')
    page = parts[-1]
    if len(parts) == 2 and _numbered_page(page):
        folder = parts[0]
        photo_id = owners.get(folder, _chapter_id(folder) if packed else None)
        stem = Path(page).stem
    elif len(parts) == 1 and (flat := read_flat_page_name(page)):
        photo_id, stem = flat
        page = stem + Path(page).suffix
    else:
        return name
    if (photo_id, stem) in replacements:
        return None
    if photo_id in chapters:
        return f"{chapters[photo_id]['folder']}/{page}"
    return name


def _write_pack(index, selected, target, check):
    names = [name.casefold() for _, name in selected if name is not None]
    if len(names) != len(set(names)):
        raise OSError('旧归档页重名，保留原书架')
    with archive_pages.open_pages(index) as read, zipfile.ZipFile(target, 'x') as dest:
        for page, name in selected:
            if name is not None:
                check()
                dest.writestr(name, read(page))


def _filtered_archive(archives, prepare, marked, transform, check):
    # Never carry an opaque archive (or extract untrusted member paths) out of a shared v4 tree.
    for path, name in archives:
        check()
        index = archive_pages.read_index(path)
        if index.status == 'corrupt':
            raise OSError(f'无法核对旧资源包 {path.name}: {index.reason}')
        if index.status != 'ok':
            continue
        pages = [(p, transform(p.name, packed=True)) for p in index.pages
                 if p.chapter in marked and _numbered_page(p.name.rsplit('/', 1)[-1])]
        if any(name is not None for _, name in pages):
            _write_pack(index, pages, prepare / name, check)
        break  # the reader used this archive, never a union of older alternatives


def copy_legacy(source, prepare, name, check, *, chapters, replacement_pages):
    if source is None:
        return
    old, eligible = source
    try:
        old.lstat()
    except FileNotFoundError:
        return  # a removed legacy folder has no shelf to carry over
    folders, archives, loose = [], {}, {}
    for path, info in _entries(old, check):
        if stat.S_ISDIR(info.st_mode):
            folders.append(path)
        elif path.suffix.lower() in archive_pages.ARCHIVE_SUFFIXES:
            archives[path] = info
        elif eligible is None and path.suffix.lower() in EXPORT_IMAGE_EXTENSIONS:
            loose[path] = info
    packs = _archives(old, name, archives)
    marked = None if eligible is None else _marked(folders, eligible, check)
    owners = {}
    for folder in folders:
        photo_id = _chapter_id(folder.name)
        marker = _marker(folder)
        if marker:
            owners[folder.name] = photo_id if str(marker.get('photo_id')) == photo_id else None

    def transform(relative, *, packed=False):
        return _page_target(relative, owners, chapters, replacement_pages, packed=packed)

    def copy_page(path, info):
        dest = transform(path.relative_to(old).as_posix())
        if dest is not None:
            if (prepare / dest).exists():
                raise OSError('旧章节页重名，保留原书架')
            _copy(path, prepare / dest, info)

    for path, info in loose.items():
        copy_page(path, info)
    if eligible is None:
        for path, dest in packs:
            check()
            index = archive_pages.read_index(path)
            if index.status == 'corrupt':
                raise OSError(f'无法核对旧资源包 {path.name}: {index.reason}')
            selected = [(page, transform(page.name, packed=True)) for page in index.pages]
            if any(page.name != target for page, target in selected):
                if index.skipped:
                    raise OSError(f'无法完整核对旧资源包 {path.name}，保留原书架')
                if any(target is not None for _, target in selected):
                    _write_pack(index, selected, prepare / dest, check)
                    info = archives[path]
                    os.utime(prepare / dest, ns=(info.st_atime_ns, info.st_mtime_ns))
            else:
                _copy(path, prepare / dest, archives[path])
        # Keep other ZIP files as before, unless the new directory name would promote
        # an unrelated ZIP into a reader archive. The source always stays untouched.
        for path in archives.keys() - {p for p, _ in packs}:
            if path.stem.lower() != name.lower():
                check()
                _copy(path, prepare / path.name, archives[path])
    else:
        folders = [folder for folder in folders if folder.name in marked]
        for folder in folders:
            check()
            dest = prepare / folder.name
            dest.mkdir(exist_ok=True)
            (dest / CHAPTER_MARKER).write_bytes(marked[folder.name])
        if marked:
            _filtered_archive(packs, prepare, marked, transform, check)
    while folders:
        for path, info in _entries(folders.pop(), check):
            if stat.S_ISDIR(info.st_mode):
                if marked is None:
                    folders.append(path)
                continue
            if marked is None:
                keep = (path.name == CHAPTER_MARKER or path.suffix.lower() in EXPORT_IMAGE_EXTENSIONS
                        or path.suffix.lower() in archive_pages.ARCHIVE_SUFFIXES)
            else:
                keep = _numbered_page(path.name)
            if keep:
                copy_page(path, info)
