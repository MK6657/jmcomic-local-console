"""Durable whole-album maintenance, independent of download results and job snapshots."""
from contextlib import closing, nullcontext
import json
import os
from pathlib import Path, PureWindowsPath
import threading
import time
import uuid

from . import database as db
from .backup_paths import safe_parts
from .download_finalize import finalize_published
from .file_tree import is_link
from .local_availability import forget
from .logger import log
from .path_guard import get_download_root, is_safe_path
from .publication import VIEWS, WORK
from .storage import STAGING_DIRECTORY
from .task_commands import TaskCommandError
from .transfer import DownloadInterrupted
from .download_layout import author_output_name


RETRY_DELAYS = (2, 8)
MAX_ATTEMPTS = len(RETRY_DELAYS) + 1
_move_lock = threading.Lock()


def wait_for_active_move():
    """等待进行中的目录迁移提交到数据库，避免并发读取落入改名与提交之间。"""
    with _move_lock:
        return


def _public_path(relative):
    if not isinstance(relative, str) or not relative:
        raise ValueError('缺少公开目录')
    parts = relative.replace('\\', '/').split('/')
    if (PureWindowsPath(relative).anchor or (parts[0].casefold() == VIEWS and len(parts) < 3)
            or any(part.casefold() in (WORK, STAGING_DIRECTORY) for part in parts)):
        raise ValueError('公开目录路径无效')
    safe_parts(parts, context='公开目录')
    root = get_download_root()
    path = root
    for part in parts:
        path = path / part
        if os.path.lexists(path) and is_link(path):
            raise ValueError('公开目录不能经过链接或目录联接')
    if not is_safe_path(path, root):
        raise ValueError('公开目录路径越权')
    return path


def _identity(path):
    info = path.stat()
    if not info.st_ino:
        raise ValueError('无法确认公开目录的文件系统标识')
    return [info.st_dev, info.st_ino]


def _location(payload):
    source = _public_path(payload['directory'])
    intent = payload.get('_move')
    if intent:
        target = _public_path(intent['to'])
        if source.is_dir() and _identity(source) == intent['identity']:
            return source
        if target.is_dir() and _identity(target) == intent['identity']:
            return target
        raise ValueError('不能确认移动前后的公开目录，请检查目录位置')
    return source


def public_directory(album_id, conn=None):
    """Absolute public path (or None without a mapping); resolves a crash after rename."""
    with (closing(db.get_db()) if conn is None else nullcontext(conn)) as reader:
        row = reader.execute('SELECT payload FROM album_maintenance WHERE album_id=?', (album_id,)).fetchone()
        return _location(json.loads(row['payload'])) if row else None


def statuses(album_ids):
    ids = tuple(set(album_ids))
    if not ids:
        return {}
    with closing(db.get_db()) as conn:
        rows = conn.execute('SELECT album_id,state,error,generation, CASE WHEN json_valid(payload) THEN '
                            "json_extract(payload,'$._attempt') END AS attempts FROM album_maintenance "
                            'WHERE album_id IN (' + ','.join('?' for _ in ids) + ')', ids)
        return {row['album_id']: {'state': row['state'], 'error': row['error'], 'generation': row['generation'],
                                  'attempts': row['attempts'] or 0, 'max_attempts': MAX_ATTEMPTS} for row in rows}


def failed_without_history(limit=20):
    """Keep retry reachable after the user clears disposable job history."""
    with closing(db.get_db()) as conn:
        rows = conn.execute("SELECT m.album_id,m.error FROM album_maintenance m WHERE m.state='failed' "
                            "AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.album_id=m.album_id AND j.status='completed') "
                            'ORDER BY m.rowid LIMIT ?', (limit,))
        return [dict(row) for row in rows]


def retry(album_id):
    with db.transaction() as conn:
        row = conn.execute('SELECT state FROM album_maintenance WHERE album_id=?', (album_id,)).fetchone()
        if not row:
            raise TaskCommandError('not_found', '该漫画没有整理记录', 404)
        if row['state'] != 'failed':
            raise TaskCommandError('status', '只有失败的整理任务可以重试', 409)
        conn.execute("UPDATE album_maintenance SET state='pending',error='',payload=CASE WHEN json_valid(payload) "
                     "THEN json_remove(payload,'$._attempt','$._retry_at') ELSE payload END WHERE album_id=?", (album_id,))


def claim(excluded_albums=()):
    """Called under JobManager's admission gate; a draining executor also excludes its album."""
    excluded = tuple(excluded_albums)
    exclusion = (' AND m.album_id NOT IN (' + ','.join('?' for _ in excluded) + ')') if excluded else ''
    with db.transaction() as conn:
        row = conn.execute("SELECT m.* FROM album_maintenance m WHERE m.state='pending' "
                           "AND CASE WHEN json_valid(m.payload) THEN COALESCE(json_extract(m.payload,'$._retry_at'),0) "
                           "ELSE 0 END <= ? AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.album_id=m.album_id "
                           "AND j.status IN ('queued','running','paused'))" + exclusion + ' ORDER BY m.rowid LIMIT 1',
                           (time.time(), *excluded)).fetchone()
        if not row:
            return None
        work = dict(row)
        try:
            payload = json.loads(row['payload'])
            attempts = payload.get('_attempt', 0)
            if type(attempts) is not int or not 0 <= attempts < MAX_ATTEMPTS:
                raise ValueError('整理次数无效')
            payload['_attempt'] = attempts + 1
            payload.pop('_retry_at', None)
        except (ValueError, TypeError, AttributeError):
            conn.execute("UPDATE album_maintenance SET state='failed',error='整理记录无效' WHERE album_id=?",
                         (row['album_id'],))
            return None
        work['payload'] = payload
        _save(conn, work, 'running', previous='pending')
        work.update(state='running', error='')
        return work


def _save(conn, work, state, error='', previous='running'):
    return conn.execute('UPDATE album_maintenance SET payload=?, state=?, error=? '
                        'WHERE album_id=? AND generation=? AND state=?',
                        (json.dumps(work['payload'], ensure_ascii=False), state, error[:500], work['album_id'],
                         work['generation'], previous)).rowcount


def _record(work, state='running', error=''):
    with db.transaction() as conn:
        if not _save(conn, work, state, error):
            raise DownloadInterrupted('superseded')


def _apply_move(conn, work, *, recover_only=False):
    payload = work['payload']
    source = _public_path(payload['directory'])
    intent = payload['_move']
    target = _public_path(intent['to'])
    if _location(payload) == source:
        if recover_only:
            if 'displaced' in intent and not os.path.lexists(target):
                retained = _public_path(intent['displaced'])
                if retained.is_dir() and _identity(retained) == intent['old_identity']:
                    retained.rename(target)
            return
        if 'displaced' in intent:
            retained = _public_path(intent['displaced'])
            if not retained.exists():
                if not target.is_dir() or _identity(target) != intent['old_identity']:
                    raise ValueError('原作者目录已改变，停止整理')
                retained.parent.mkdir(parents=True, exist_ok=True)
                target.rename(retained)
            elif _identity(retained) != intent['old_identity']:
                raise ValueError('保留的旧版本目录已改变，停止整理')
        if os.path.lexists(target):
            raise FileExistsError('作者目录已有同名资源，保留两个目录')
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            source.rename(target)
        except OSError:
            if 'displaced' in intent and not os.path.lexists(target):
                retained.rename(target)
            raise
    different = conn.execute('SELECT c.job_id,c.output_path,c.superseded_at FROM resource_catalog c '
                             'JOIN jobs j USING(job_id) WHERE j.album_id=? AND j.status=\'completed\' AND '
                             '(j.output_path IS NOT c.output_path OR j.superseded_at IS NOT c.superseded_at)',
                             (work['album_id'],)).fetchall()
    conn.execute("UPDATE jobs SET output_path=? WHERE album_id=? AND status='completed' AND job_id IN ("
                 'SELECT p.job_id FROM published_tasks p JOIN resource_catalog c USING(job_id) '
                 'WHERE p.album_id=? AND c.output_path=? AND c.superseded_at IS NULL)',
                 (str(target), work['album_id'], work['album_id'], str(source)))
    for table in ('jobs', 'resource_catalog'):
        if table == 'resource_catalog':
            conn.executemany('UPDATE resource_catalog SET output_path=?,superseded_at=? WHERE job_id=?',
                             ((row['output_path'], row['superseded_at'], row['job_id']) for row in different))
        paths = conn.execute(f'SELECT DISTINCT output_path FROM {table} WHERE album_id=? AND output_path IS NOT NULL',
                             (work['album_id'],)).fetchall()
        for row in paths:
            if row['output_path'] and Path(row['output_path']) == source:
                conn.execute(f'UPDATE {table} SET output_path=? WHERE album_id=? AND output_path=?',
                             (str(target), work['album_id'], row['output_path']))
    moved = {**payload, 'directory': intent['to'], 'preferred_directory': intent['to']}
    del moved['_move']
    previous = work['state']
    _save(conn, {**work, 'payload': moved}, previous, work['error'], previous=previous)
    forget(source)
    forget(target)
    return moved


def _previous_view(path, album_id):
    marker = path / '.jm-view.json'
    if not marker.is_file() or is_link(marker):
        return False
    try:
        owner = json.loads(marker.read_bytes())
    except ValueError:
        return False
    if not isinstance(owner, dict) or not isinstance(owner.get('job'), str):
        return False
    with closing(db.get_db()) as conn:
        return conn.execute('SELECT 1 FROM published_tasks WHERE album_id=? AND job_id=?',
                            (album_id, owner['job'])).fetchone() is not None


def _organize_location(work, check_control):
    payload = work['payload']
    root = get_download_root()
    source = _public_path(payload['directory'])
    if '_move' not in payload:
        if not source.is_dir():
            raise FileNotFoundError('已发布的漫画目录不存在')
        target = _public_path(payload.get('preferred_directory') or payload['directory'])
        versioned = source.relative_to(root).parts[0].casefold() == VIEWS
        if not versioned:
            target = source
        if (payload.get('organize_mode') == 'by_author' and payload.get('keep_location') not in (True, 'true')
                and (target.parent == root or target.relative_to(root).parts[0].casefold() == VIEWS)):
            name = payload.get('title_dir') or source.name
            safe_parts([name], context='按作者整理')
            author = author_output_name(root, source, name, str(payload.get('author') or 'unknown_author'),
                                        pack=payload.get('auto_pack') == 'true', check_control=check_control)
            target = _public_path((Path(author) / name).as_posix())
        if source == target:
            return source
        relative = target.relative_to(root).as_posix()
        if target.is_relative_to(source) or source.is_relative_to(target) or relative.split('/')[0].casefold() == VIEWS:
            raise ValueError('目标目录与源目录冲突')
        payload['_move'] = {'to': relative, 'identity': _identity(source)}
        if os.path.lexists(target):
            if not _previous_view(target, work['album_id']):
                del payload['_move']
                log.warning(f"保留现有同名目录 album_id={work['album_id']} path={target}")
                return source
            payload['_move'].update(displaced=(Path(VIEWS) / uuid.uuid4().hex / target.name).as_posix(),
                                    old_identity=_identity(target))
        _record(work)
    check_control()
    with _move_lock, db.transaction() as conn:
        if not _save(conn, work, 'running'):
            raise DownloadInterrupted('superseded')
        moved = _apply_move(conn, work)
    work['payload'] = moved
    return _public_path(moved['directory'])


def recover():
    """Before new admissions, reconcile already-renamed paths and recover lost leases."""
    with _move_lock, db.transaction() as conn:
        for row in conn.execute("SELECT * FROM album_maintenance WHERE state='running' OR state IN ('pending','failed') "
                                "AND CASE WHEN json_valid(payload) "
                                "THEN json_type(payload,'$._move')='object' ELSE 0 END").fetchall():
            work = dict(row)
            try:
                work['payload'] = json.loads(work['payload'])
                if work['payload'].get('_move'):
                    moved = _apply_move(conn, work, recover_only=True)
                    if moved:
                        work['payload'] = moved
                if row['state'] != 'running':
                    continue
                exhausted = work['payload'].get('_attempt', 0) >= MAX_ATTEMPTS
                _save(conn, work, 'failed' if exhausted else 'pending', '上次整理被中断')
            except (OSError, KeyError, ValueError, AttributeError, TypeError) as error:
                conn.execute("UPDATE album_maintenance SET state='failed',error=? WHERE album_id=?",
                             (str(error)[:500], row['album_id']))


def _published_pages(work):
    payload = work['payload']
    if payload.get('organize_mode') != 'flat' and payload.get('auto_pack') != 'true':
        return {}
    # Match Workspace.project: newest creation order wins per page, even when an
    # older task finished last. All pages of a renamed chapter use its latest folder.
    directory = _public_path(payload['directory'])
    pages, folders = {}, {}
    with closing(db.get_db()) as conn:
        rows = conn.execute('SELECT p.manifest,c.output_path FROM published_tasks p JOIN resource_catalog c '
                            'USING(job_id,album_id) WHERE p.album_id=? AND c.superseded_at IS NULL '
                            'ORDER BY p.job_order', (work['album_id'],))
        for row in rows:
            if not row['output_path'] or Path(row['output_path']) != directory:
                continue
            for chapter in json.loads(row['manifest']):
                pid = chapter['photo_id']
                folders[pid] = chapter.get('folder')
                digests = pages.setdefault(pid, {})
                for name in chapter['files']:
                    # Missing evidence in a newer row cannot revive an older digest.
                    digests[name] = chapter.get('digests', {}).get(name)
    return {pid: (folders[pid], digests) for pid, digests in pages.items()}


def run(work, stop_event):
    def check_control():
        if stop_event.is_set():
            raise DownloadInterrupted('stopped')

    try:
        check_control()
        _record(work)
        directory = _organize_location(work, check_control)
        try:
            finalize_published(directory, work['payload'], check_control, lambda: _record(work), _published_pages(work))
        finally:
            forget(directory)
        _record(work, 'completed')
    except DownloadInterrupted as error:
        if error.reason != 'superseded':
            work['payload']['_attempt'] -= 1
            _record(work, 'pending')
    except Exception as error:
        attempts = work['payload']['_attempt']
        state = 'failed' if attempts >= MAX_ATTEMPTS else 'pending'
        if state == 'pending':
            work['payload']['_retry_at'] = time.time() + RETRY_DELAYS[attempts - 1]
        log.warning(f"整册整理失败 album_id={work['album_id']} attempt={attempts} error={error}")
        _record(work, state, str(error) or type(error).__name__)
