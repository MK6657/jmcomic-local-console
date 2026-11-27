"""Read boundaries shared by album inventory, export and image URLs."""
from contextlib import closing
import json
import os
from pathlib import Path
import re
import stat

from . import database as db, publication
from .backup_paths import safe_parts
from .export_budget import ExportLimit
from .file_tree import is_link
from .storage import is_temporary_path
from .validation import validate_job_id


def snapshot_page_digests(job_id, snapshot, budget):
    """Bind the on-disk seal to its committed record. Keys are names, never paths to open."""
    limit = budget.limits.archive_metadata_bytes
    with closing(db.get_db()) as conn:
        row = conn.execute('SELECT substr(CAST(manifest AS BLOB), 1, ?) FROM published_tasks WHERE job_id=?',
                           (limit + 1, job_id)).fetchone()
    if not row or not row[0]:
        raise OSError('任务成品缺少已提交的清单')
    marker = snapshot / '.jm-publication.json'
    info = marker.lstat()
    if not stat.S_ISREG(info.st_mode) or is_link(marker):
        raise OSError('任务成品清单不是普通文件')
    if max(len(row[0]), info.st_size) > limit:
        raise ExportLimit('任务成品清单超过导出限制')
    with marker.open('rb') as stream:
        local = stream.read(info.st_size + 1)
    try:
        manifest = json.loads(row[0])
        if (len(local) != info.st_size or not isinstance(manifest, list)
                or (local != row[0] and json.loads(local) != manifest)):
            raise ValueError('任务成品清单与发布记录不符')
        pages = {}
        for chapter in manifest:
            budget.check()
            folder, files, digests = chapter['folder'], chapter['files'], chapter['digests']
            if (not isinstance(folder, str) or not isinstance(files, list) or not isinstance(digests, dict)
                    or not all(isinstance(name, str) for name in files) or set(files) != digests.keys()):
                raise ValueError('页摘要不完整')
            safe_parts([folder, *files], context='任务成品清单')
            for name in files:
                key = f'{folder}/{name}'
                if key in pages or not re.fullmatch('[0-9a-f]{64}', digests[name]):
                    raise ValueError('页名重复或摘要无效')
                pages[key] = digests[name]
                if len(pages) > budget.limits.pages:
                    raise ExportLimit('任务成品页数超过导出限制')
        return pages
    except (KeyError, TypeError, ValueError, RecursionError) as error:
        raise OSError('任务成品清单无效') from error


def _view_job(marker: Path) -> str | None:
    try:
        info = marker.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return None
    if not stat.S_ISREG(info.st_mode) or is_link(marker) or not 0 < info.st_size <= 1024:
        raise ValueError("发布视图标记无效")
    with marker.open('rb') as stream:
        record = json.loads(stream.read(1025))
    if not isinstance(record, dict) or not validate_job_id(record.get('job')):
        raise ValueError("发布视图标记无效")
    return record['job']


def published_path(path: Path, root: Path, *, directory=False, conn=None, _resolved_root=None) -> Path | None:
    """Legacy paths need no DB row. A managed view is public only while its catalog row points here."""
    abs_path = Path(path).absolute()
    if _resolved_root is not None:
        abs_root, real_root = _resolved_root
    else:
        abs_root, real_root = Path(root).absolute(), Path(os.path.realpath(root))
    lexical_rel = abs_path.is_relative_to(abs_root)
    real = Path(os.path.realpath(path))
    if (real == real_root or not real.is_relative_to(real_root)
            or (lexical_rel and publication.private_path(abs_path, abs_root))
            or publication.private_path(real, real_root) or is_temporary_path(path) or is_temporary_path(real)):
        return None
    seen, managed, registered = set(), False, False
    passes = ((abs_path, abs_root), (real, real_root)) if lexical_rel else ((real, real_root),)
    try:
        for candidate, base in passes:
            parts = candidate.relative_to(base).parts
            managed |= any(part.casefold() == publication.VIEWS for part in parts)
            folders = (candidate, *candidate.parents) if directory else candidate.parents
            for folder in folders:
                if folder == base:
                    break
                if folder in seen:
                    continue
                seen.add(folder)
                job = _view_job(folder / '.jm-view.json')
                if job is None:
                    continue
                if conn is not None:
                    row = conn.execute('SELECT c.output_path FROM published_tasks p JOIN resource_catalog c '
                                       'ON c.job_id=p.job_id AND c.album_id=p.album_id '
                                       'WHERE p.job_id=? AND c.superseded_at IS NULL', (job,)).fetchone()
                else:
                    with closing(db.get_db()) as owned_conn:
                        row = owned_conn.execute('SELECT c.output_path FROM published_tasks p JOIN resource_catalog c '
                                                 'ON c.job_id=p.job_id AND c.album_id=p.album_id '
                                                 'WHERE p.job_id=? AND c.superseded_at IS NULL', (job,)).fetchone()
                location = Path(os.path.realpath(folder))
                if (not row or not row[0] or Path(os.path.realpath(row[0])) != location
                        or not real.is_relative_to(location)):
                    return None
                registered = True
    except (OSError, ValueError):
        return None
    return real if registered or not managed else None
