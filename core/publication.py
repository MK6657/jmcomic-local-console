"""Durable task workspaces and immutable publications, all relative to the download root."""
from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil

from . import database as db
from .backup_paths import safe_parts
from .download_layout import CHAPTER_FORMAT, CHAPTER_MARKER, chapter_format, chapter_output_dir
from .file_tree import is_link
from .storage import STAGING_DIRECTORY, is_temporary_path, publication_tree, staged_file

WORK = '.jmcomic-work'
VIEWS = '.jmcomic-views'


def _key(job):
    return hashlib.sha256(str(job).encode()).hexdigest()[:32]


def private_path(path, root):
    try:
        parts = Path(path).absolute().relative_to(Path(root).absolute()).parts
    except ValueError:
        return True
    return any(part.casefold() == WORK for part in parts)


def checked(root, relative, *, create=False):
    root = Path(root)
    parts = safe_parts(Path(relative).parts, context='发布目录')
    if not parts or Path(relative).anchor or is_temporary_path(relative) or is_link(root):
        raise ValueError('发布路径无效')
    current = root
    for part in parts:
        current = current / part
        if os.path.lexists(current):
            if is_link(current):
                raise ValueError('发布路径包含链接')
        elif create:
            current.mkdir(exist_ok=True)
            if is_link(current):
                raise ValueError('发布目录无效')
    return current


def _json_write(path, value):
    data = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()
    if STAGING_DIRECTORY in path.parts:
        # This fresh directory itself is published by rename after every file closes.
        path.write_bytes(data)
        return
    with staged_file(path) as staged:
        staged.path.write_bytes(data)
        staged.publish(lambda p: p.stat().st_size == len(data))


def _load(path):
    if is_link(path) or not path.is_file():
        raise OSError('任务文件清单无效')
    return json.loads(path.read_bytes())


def job_snapshot(job_id, root):
    with closing(db.get_db()) as conn:
        row = conn.execute('SELECT snapshot FROM published_tasks WHERE job_id=?', (job_id,)).fetchone()
    if not row:
        return None
    wanted = Path(WORK, _key(job_id), 'snapshot')
    if Path(row[0]) != wanted:
        raise OSError('任务成品记录无效')
    path = checked(root, wanted)
    if not path.is_dir():
        raise OSError('任务成品已不在原位置')
    return path


def _copy(source, dest):
    if is_link(source) or not source.is_file():
        raise OSError('发布源文件不是普通文件')
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)


def _digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


class Workspace:
    def __init__(self, root, job_id, album_id, target, checkpoint):
        self.root, self.job, self.album = Path(root), job_id, str(album_id)
        if checkpoint.directory is not None and not checkpoint.directory.is_relative_to(self.root):
            raise ValueError('无法续传：原输出目录不在当前下载根目录内')
        self.base = checked(root, Path(WORK, _key(job_id)), create=True)
        self.work = checked(root, self.base.relative_to(root) / 'pages', create=True)
        if not self.work.is_dir():
            raise ValueError('无法续传：工作目录被文件占用')
        self.snapshot = checked(root, self.base.relative_to(root) / 'snapshot')
        meta = self.base / 'workspace.json'
        if meta.exists():
            info = _load(meta)
            if (info['job'], info['album']) != (job_id, self.album):
                raise ValueError('任务目录身份不匹配')
            self.target = checked(root, info['target'])
        else:
            self.target = checked(root, Path(target).relative_to(root))
            _json_write(meta, {'job': job_id, 'album': self.album, 'target': self.target.relative_to(root).as_posix()})
        if private_path(self.target, root):
            raise ValueError('公开书目不能指向任务工作区')
        self.previous_dir = checkpoint.directory
        if self.previous_dir is not None:
            try:
                prior = checked(root, self.previous_dir.relative_to(root))
            except (ValueError, OSError) as error:
                raise ValueError('无法续传：路径不是当前下载根目录内的普通目录') from error
            if private_path(prior, root) and prior != self.work:
                raise ValueError('无法续传：工作目录不属于该任务')
            if prior.exists() and not prior.is_dir():
                raise ValueError('无法续传：目录已被文件占用')
        self.legacy = checkpoint

    @contextmanager
    def temporary_tree(self):
        # Only this invocation's temporary children are removed on failure. A hard
        # exit may leave one orphan, which ordinary full backups explicitly skip.
        with publication_tree(self.base, self.root) as path:
            yield path

    def seed(self, photos, existing, skip, valid, is_gif):
        copied = []
        sources = [p for p in (self.previous_dir, existing) if p and p != self.work]
        for source in dict.fromkeys(sources):
            source = checked(self.root, Path(source).relative_to(self.root))
            if private_path(source, self.root) or not source.is_dir():
                continue
            for photo in photos:
                old = chapter_output_dir(source, photo, create=False)
                if not old:
                    continue
                legacy_format = chapter_format(old) < CHAPTER_FORMAT
                for idx, image in enumerate(photo, 1):
                    file = old / f'{idx:05d}.webp'
                    if not file.is_file() or is_link(file):
                        continue
                    resuming_old = source == self.previous_dir
                    mine = resuming_old and self.legacy.contains(file)
                    # Old writers flattened GIFs to one frame; even a valid WebP or
                    # an old receipt cannot certify that the animation survived.
                    if (legacy_format and is_gif(image)) or (not mine and (resuming_old or not skip)):
                        continue
                    dest = chapter_output_dir(self.work, photo) / file.name
                    if not dest.exists() and valid(file):
                        with staged_file(dest) as staged:
                            shutil.copyfile(file, staged.path)
                            if mine and not self.legacy.contains(file):
                                continue  # the file changed while copying an old v4 receipt
                            staged.publish(valid)
                        copied.append(dest)
        return copied

    def seal(self, photos, check):
        manifest = []
        for photo in photos:
            chapter = chapter_output_dir(self.work, photo, create=False)
            if chapter is None:
                raise OSError('章节未完整写入')
            files, digests = [], {}
            for idx in range(1, len(photo) + 1):
                page = chapter / f'{idx:05d}.webp'
                if is_link(page) or not page.is_file() or page.stat().st_size == 0:
                    raise OSError('章节有缺页')
                files.append(page.name)
                digests[page.name] = _digest(page)
                if getattr(self.legacy, 'isolated', False):
                    receipt = self.legacy.receipts.get(page.relative_to(self.work).as_posix())
                    if receipt != digests[page.name]:
                        raise OSError('已确认的页面在发布前发生变化')
            manifest.append({'photo_id': str(photo.photo_id), 'name': str(photo.name or photo.photo_id),
                             'folder': chapter.name, 'files': files, 'digests': digests})
        if not self.snapshot.exists():
            with self.temporary_tree() as new:
                for p in manifest:
                    _copy(self.work / p['folder'] / CHAPTER_MARKER, new / p['folder'] / CHAPTER_MARKER)
                    for name, digest in p['digests'].items():
                        check()
                        _copy(self.work / p['folder'] / name, new / p['folder'] / name)
                        if _digest(new / p['folder'] / name) != digest:
                            raise OSError('封存的页面与确认记录不一致')
                _json_write(new / '.jm-publication.json', manifest)
                new.rename(self.snapshot)
        else:
            for photo in manifest:
                for name, digest in photo['digests'].items():
                    check()
                    source = checked(self.root, (self.snapshot / photo['folder'] / name).relative_to(self.root))
                    if _digest(source) != digest:
                        raise OSError('中断后任务成品内容已改变')
        if _load(self.snapshot / '.jm-publication.json') != manifest:
            raise OSError('待发布成品与当前章节不一致')
        return manifest

    def release_pages(self, manifest):
        """Only after commit, remove our known, byte-identical work copies; preserve anything else."""
        for photo in manifest:
            folder = checked(self.root, self.work.relative_to(self.root) / photo['folder'])
            for name, digest in photo['digests'].items():
                src = checked(self.root, (folder / name).relative_to(self.root))
                if src.is_file() and _digest(src) == digest:
                    src.unlink()
            marker = folder / CHAPTER_MARKER
            if marker.exists() and _load(marker).get('photo_id') == photo['photo_id']:
                marker.unlink()
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()

    def project(self, manifest, album, settings, check):
        """Build out of sight. A single DB commit makes the complete generation discoverable."""
        from .legacy_publication import copy_legacy, legacy_source
        with closing(db.get_db()) as conn:
            order = conn.execute('SELECT id FROM jobs WHERE job_id=?', (self.job,)).fetchone()[0]
            records = conn.execute('SELECT p.* FROM published_tasks p JOIN resource_catalog c USING(job_id) '
                                   'WHERE p.album_id=? AND c.superseded_at IS NULL ORDER BY p.job_order',
                                   (self.album,)).fetchall()
            legacy = legacy_source(conn, self.root, self.album, self.job, self.previous_dir)
        # Recovery may requeue this task behind another task for the same book.
        # A view renamed before the crash represents only the earlier membership.
        generation = _key(','.join(map(str, sorted([order, *(r['job_order'] for r in records)]))))
        identity = {'job': self.job, 'generation': generation}
        target = self.target
        if target.exists():
            # Never replace a user's own folder or mutate the previous committed view.
            marker = target / '.jm-view.json'
            if not (marker.is_file() and _load(marker) == identity):
                target = checked(self.root, Path(VIEWS, _key(self.job + ':' + generation), self.target.name))
        entries = [(r['job_order'], job_snapshot(r['job_id'], self.root), json.loads(r['manifest'])) for r in records]
        entries.append((order, self.snapshot, manifest))
        pages, chapters = {}, {}
        for _, folder, chapter_list in sorted(entries, key=lambda e: e[0]):
            for p in chapter_list:
                pid = p['photo_id']
                safe_parts([p['folder'], *p['files']])
                chapters[pid] = {'photo_id': pid, 'name': p['name'], 'folder': p['folder']}
                for name in p['files']:
                    pages[pid, name] = (checked(self.root, (folder / p['folder'] / name).relative_to(self.root)),
                                        p['digests'][name])
        if not target.exists():
            with self.temporary_tree() as prepare:
                copy_legacy(legacy, prepare, target.name, check)
                for pid, p in chapters.items():
                    (prepare / p['folder']).mkdir(exist_ok=True)
                    _json_write(prepare / p['folder'] / CHAPTER_MARKER, {'format': 2, 'photo_id': pid})
                for (pid, name), (source, digest) in pages.items():
                    check()
                    dest = prepare / chapters[pid]['folder'] / name
                    _copy(source, dest)
                    if _digest(dest) != digest:
                        raise OSError('任务成品页内容与发布记录不一致，旧书架保留')
                _json_write(prepare / '.jm-view.json', identity)
                check()
                if target.parent != self.root:
                    checked(self.root, target.parent.relative_to(self.root), create=True)
                prepare.rename(target)
        else:
            if _load(target / '.jm-view.json') != identity:
                raise OSError('发布目录已被占用')
            # A previous process may have renamed this view and exited before
            # committing. Its marker proves identity, not that every byte survived.
            for (pid, name), (source, digest) in pages.items():
                check()
                dest = checked(self.root, (target / chapters[pid]['folder'] / name).relative_to(self.root))
                if _digest(source) != digest or _digest(dest) != digest:
                    raise OSError('中断后发布文件内容已改变，保留文件供检查')
        payload = {key: settings.get(key) for key in ('organize_mode','auto_pack','pack_format','delete_originals')}
        payload.update(directory=target.relative_to(self.root).as_posix(), preferred_directory=self.target.relative_to(self.root).as_posix(), title_dir=self.target.name,
                       author=str(getattr(album, 'author', '') or 'unknown_author'), photos=list(chapters.values()))
        return target, (self.snapshot.relative_to(self.root).as_posix(), manifest, payload)


def record(conn, job, album, publication):
    snapshot, manifest, payload = publication
    def dump(value):
        return json.dumps(value, ensure_ascii=False, separators=(',', ':'))

    # Several versions may finish while another job is paused. The versioned
    # view is readable immediately, but must never become the intended shelf.
    if Path(payload['preferred_directory']).parts[0].casefold() == VIEWS:
        previous = conn.execute('SELECT payload FROM album_maintenance WHERE album_id=?', (album,)).fetchone()
        prior = json.loads(previous[0]) if previous else {}
        preferred = prior.get('preferred_directory') or payload['title_dir']
        if Path(preferred).parts[0].casefold() == VIEWS:
            preferred = payload['title_dir']
        payload = dict(payload, preferred_directory=preferred, keep_location=prior.get('keep_location', False))
    conn.execute('INSERT INTO published_tasks SELECT job_id,album_id,id,?,? FROM jobs WHERE job_id=?',
                 (snapshot, dump(manifest), job))
    conn.execute('DELETE FROM task_page_receipts WHERE job_id=?', (job,))
    # A can finish after B despite having been created first. Both catalog entries
    # must resolve the new complete album; their exact task exports remain separate.
    path = conn.execute('SELECT output_path FROM jobs WHERE job_id=?', (job,)).fetchone()[0]
    conn.execute("UPDATE jobs SET output_path=? WHERE album_id=? AND status='completed' AND superseded_at IS NULL "
                 'AND output_path!=? AND job_id IN (SELECT job_id FROM published_tasks WHERE album_id=?)',
                 (path, album, path, album))
    conn.execute('UPDATE resource_catalog SET output_path=? WHERE album_id=? AND superseded_at IS NULL '
                 'AND job_id IN (SELECT job_id FROM published_tasks WHERE album_id=?)', (path, album, album))
    if payload['auto_pack'] == 'true' or payload['organize_mode'] in ('flat', 'by_author') or payload['directory'] != payload['preferred_directory']:
        conn.execute("INSERT INTO album_maintenance(album_id,state,payload) VALUES(?,'pending',?) "
                     "ON CONFLICT(album_id) DO UPDATE SET state='pending',payload=excluded.payload,error='',"
                     'generation=generation+1', (album, dump(payload)))
    else:
        conn.execute('DELETE FROM album_maintenance WHERE album_id=?', (album,))
