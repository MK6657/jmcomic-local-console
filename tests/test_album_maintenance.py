"""Exercise the real v5 queue, public filesystem, scheduler, restart and HTTP boundaries offline."""
import hashlib
import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace
import zipfile

import pytest

from core import album_maintenance as maintenance, database as db, path_guard
from core.job_manager import JobManager


@pytest.fixture
def root(client, tmp_path, monkeypatch):
    directory = tmp_path / 'downloads'
    directory.mkdir()
    monkeypatch.setattr(path_guard, 'DOWNLOAD_ROOT', directory)
    return directory


def queue(directory='Book_123', **fields):
    payload = dict(directory=directory, organize_mode='none', auto_pack='true', pack_format='cbz',
                   delete_originals='true', author='Writer', photos=[])
    payload.update(fields)
    with db.transaction() as conn:
        conn.execute("INSERT INTO album_maintenance(album_id,state,payload) VALUES('123','pending',?) "
                     "ON CONFLICT(album_id) DO UPDATE SET state='pending',payload=excluded.payload,"
                     "error='',generation=generation+1", (json.dumps(payload),))


def tick():
    work = maintenance.claim()
    assert work is not None
    maintenance.run(work, threading.Event())
    return status()['state']


def status():
    return maintenance.statuses(['123'])['123']


def job(job_id, directory, state='completed', album='123'):
    db.insert_job(job_id, album, 'Book', [])
    db.transition_job_status(job_id, ['queued'], state, output_path=str(directory))


def manifest(job_id, folder, pages):
    chapter = dict(photo_id='7', folder=folder, name='Chapter', files=list(pages),
                   digests={name: hashlib.sha256(data).hexdigest() for name, data in pages.items()})
    with db.transaction() as conn:
        conn.execute('INSERT INTO published_tasks SELECT job_id,album_id,id,?,? FROM jobs WHERE job_id=?',
                     ('.jmcomic-work/unread', json.dumps([chapter]), job_id))


@pytest.mark.parametrize('blocking', ['paused', 'queued', 'running'])
def test_waits_for_last_active_and_retiring_executor(root, blocking):
    public = root / 'Book_123'
    public.mkdir()
    page = public / '1.webp'
    page.write_bytes(b'published')
    private = root / '.jmcomic-work/task/snapshot/1.webp'
    private.parent.mkdir(parents=True)
    private.write_bytes(b'private')
    job('first', public)
    job('blocking', private.parent, blocking)
    queue()
    assert not maintenance.claim() and page.exists()
    assert db.transition_job_status('blocking', [blocking], 'canceled')
    assert not maintenance.claim(['123']) and page.exists()
    assert tick() == 'completed'
    with zipfile.ZipFile(public / 'Book_123.cbz') as archive:
        assert archive.read('1.webp') == b'published' and len(archive.namelist()) == 2
    assert page.read_bytes() == b'published' and private.read_bytes() == b'private'  # no manifest


def test_reservation_releases_other_albums_and_waits_for_shutdown(root, monkeypatch):
    import core.job_manager as jobs
    directory = root / 'Book_123'
    directory.mkdir()
    queue()
    entered, release, other = (threading.Event() for _ in range(3))
    def holding(*args):
        entered.set()
        assert release.wait(8)
        args[2]()
    def download(job_id, *args, **kwargs):
        assert job_id == 'other'
        db.transition_job_status('other', ['running'], 'completed')
        other.set()
    monkeypatch.setattr(maintenance, 'finalize_published', holding)
    monkeypatch.setattr(jobs, 'download_album_job', download)
    manager = JobManager()
    manager.start()
    try:
        assert entered.wait(4)
        job('next', directory, 'queued')
        job('other', directory, 'queued', album='999')
        manager.schedule_next()
        assert other.wait(3) and db.get_job('next')['status'] == 'queued'
        assert not manager.stop(timeout=0)
    finally:
        release.set()
        assert manager.stop(timeout=4)
    assert status()['state'] == 'pending' and status()['attempts'] == 0


def test_persistent_retry_budget_and_separate_manual_retry(client, root, monkeypatch):
    public = root / 'Book_123'
    public.mkdir()
    job('done', public)
    queue()
    url = '/api/albums/123/maintenance'
    now = [1000]
    monkeypatch.setattr(maintenance, 'time', SimpleNamespace(time=lambda: now[0]))
    for attempt in range(1, maintenance.MAX_ATTEMPTS + 1):
        state = tick()
        assert status()['attempts'] == attempt and status()['error']
        assert not maintenance.claim()
        maintenance.recover()
        assert status()['state'] == state and not maintenance.claim()
        now[0] += 100
    assert state == 'failed' and not maintenance.claim()
    assert client.get(url).json['maintenance'] == client.get('/api/jobs/done').json['job']['maintenance'] == status()
    assert client.get('/api/jobs').json['jobs'][0]['maintenance'] == status()
    assert client.post(url + '/retry').status_code == 202
    assert client.post(url + '/retry').status_code == 409
    assert client.post('/api/albums/404/maintenance/retry').status_code == 404
    (public / '1.webp').write_bytes(b'page')
    assert tick() == 'completed' and status()['attempts'] == 1 and not status()['error']
    assert [row['job_id'] for row in db.get_all_jobs()] == ['done'] and db.get_job('done')['status'] == 'completed'


@pytest.mark.parametrize('relative', ['.', '../other', '.jmcomic-views', '.jmcomic-views/version',
                                      '.JmComic-Work/old/snapshot', 'C:/outside'])
def test_rejects_private_or_invalid_paths(root, relative):
    private = root / '.jmcomic-work/old/snapshot'
    private.mkdir(parents=True)
    page = private / '1.webp'
    page.write_bytes(b'private')
    queue(relative)
    assert tick() == 'pending' and status()['error']
    assert page.read_bytes() == b'private' and not list(root.rglob('*.cbz'))


@pytest.mark.parametrize('crash', ['before_rename', 'after_rename'])
def test_move_intent_repairs_paths_and_history_after_restart(root, monkeypatch, crash):
    source, target = root / 'Book_123', root / 'Writer/Book_123'
    source.mkdir()
    (source / '1.webp').write_bytes(b'owned')
    for job_id in ('cleared', 'retained'):
        job(job_id, source)
    db.delete_job('cleared')
    job('different_path', root / 'elsewhere')
    job('different_album', source, album='678')
    queue(organize_mode='by_author')
    work = maintenance.claim()
    class PowerLoss(BaseException):
        pass
    rename = Path.rename
    def interrupt(path, dest):
        if crash == 'after_rename':
            rename(path, dest)
        raise PowerLoss()
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'rename', interrupt)
        with pytest.raises(PowerLoss):
            maintenance.run(work, threading.Event())
    assert maintenance.public_directory('123') == (source if crash == 'before_rename' else target)
    assert db.get_job('retained')['output_path'] == str(source)
    maintenance.recover()
    assert db.get_job('retained')['output_path'] == str(maintenance.public_directory('123'))
    assert tick() == 'completed' and maintenance.public_directory('123') == target
    assert db.get_job('retained')['output_path'] == str(target)
    assert {row['job_id']: row['output_path'] for row in db.resource_records(['123'])} == {
        'retained': str(target), 'cleared': str(target), 'different_path': str(root / 'elsewhere')}
    assert db.get_job('different_album')['output_path'] == str(source)
    assert not source.exists() and (target / 'Book_123.cbz').is_file()


@pytest.mark.parametrize('owned', [True, False])
@pytest.mark.parametrize('mode', ['none', 'by_author', 'flat'])
def test_shelf_replacement_preserves_old_contents_and_foreign_paths(root, mode, owned):
    relative = '.jmcomic-views/new/Book_123'
    source = root / relative
    source.mkdir(parents=True)
    (source / 'new.txt').write_text('v2')
    preferred = 'Writer/Book_123' if mode == 'by_author' else 'Book_123'
    shelf = root / preferred
    shelf.mkdir(parents=True)
    (shelf / 'old.txt').write_text('v1')
    (shelf / '.jm-view.json').write_text('{"job":"old"}')
    job('old', shelf)
    job('new', source)
    if owned:
        with db.transaction() as conn:
            conn.execute("INSERT INTO published_tasks VALUES('old','123',1,'irrelevant','[]')")
    queue(relative, preferred_directory='Book_123', organize_mode=mode, auto_pack='false')
    assert tick() == 'completed'
    actual = shelf if owned else source
    assert maintenance.public_directory('123') == actual and db.get_job('new')['output_path'] == str(actual)
    assert (actual / 'new.txt').read_text() == 'v2'
    assert len(list(root.rglob('old.txt'))) == 1
    assert (shelf / 'old.txt').exists() == (not owned)


def test_superseded_claim_cannot_acknowledge_new_generation(root):
    (root / 'Book_123').mkdir()
    (root / 'Book_123/page.webp').write_bytes(b'original')
    queue()
    stale = maintenance.claim()
    queue(organize_mode='by_author', auto_pack='false')
    maintenance.run(stale, threading.Event())
    assert (status()['state'], status()['generation'], status()['attempts']) == ('pending', 2, 0)
    assert (root / 'Book_123/page.webp').is_file() and not list(root.rglob('*.cbz'))
    assert tick() == 'completed'


def test_interrupted_shelf_swap_restores_displaced_directory_before_download_admission(root, monkeypatch):
    shelf = root / 'Book_123'
    shelf.mkdir()
    (shelf / '.jm-view.json').write_text('{"job":"old"}')
    (shelf / 'old.txt').write_text('v1')
    version = root / '.jmcomic-views/new/Book_123'
    version.mkdir(parents=True)
    (version / 'new.txt').write_text('v2')
    job('old', shelf)
    job('new', version)
    with db.transaction() as conn:
        conn.execute("INSERT INTO published_tasks VALUES('old','123',1,'unread','[]')")
    queue(version.relative_to(root).as_posix(), preferred_directory='Book_123', auto_pack='false')
    work = maintenance.claim()
    class PowerLoss(BaseException):
        pass
    rename = Path.rename
    def crash_after_first(path, destination):
        rename(path, destination)
        raise PowerLoss()
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'rename', crash_after_first)
        with pytest.raises(PowerLoss):
            maintenance.run(work, threading.Event())
    assert not shelf.exists() and maintenance.public_directory('123') == version
    job('paused', version, 'paused')
    maintenance.recover()
    assert (shelf / 'old.txt').read_text() == 'v1' and not maintenance.claim()
    assert db.transition_job_status('paused', ['paused'], 'canceled') and tick() == 'completed'
    assert maintenance.public_directory('123') == shelf and (shelf / 'new.txt').read_text() == 'v2'
    assert not version.exists() and len(list(root.rglob('old.txt'))) == 1


def test_failure_can_be_retried_from_downloads_after_history_cleanup(root, client):
    directory = root / 'Book_123'
    directory.mkdir()
    job('finished', directory)
    queue()
    with db.transaction() as conn:
        conn.execute("UPDATE album_maintenance SET state='failed',error='locked'")
    assert client.get('/api/jobs').json['maintenance_failures'] == []
    assert client.post('/api/jobs/clear/completed').status_code == 200
    assert client.get('/api/jobs').json['maintenance_failures'] == [{'album_id': '123', 'error': 'locked'}]
    assert client.post('/api/albums/123/maintenance/retry').status_code == 202
    assert client.get('/api/jobs').json['maintenance_failures'] == [] and not db.get_all_jobs()


def test_flat_retry_keeps_unknown_pages_and_cleans_only_receipted_flat_files(root, monkeypatch):
    public = root / 'Book_123'
    chapter = public / 'Chapter__7'
    chapter.mkdir(parents=True)
    (chapter / '.jm-chapter.json').write_text('{"photo_id":"7","format":2}')
    (chapter / '00001.webp').write_bytes(b'published')
    (chapter / '00099.webp').write_bytes(b'user extra')
    user_page = public / 'cover.webp'
    user_page.write_bytes(b'different')
    archive = public / 'Book_123.cbz'
    with zipfile.ZipFile(archive, 'w') as base:
        base.writestr('cover.webp', b'previous')
    before = archive.read_bytes()
    job('done', public)
    manifest('done', chapter.name, {'00001.webp': b'published'})
    queue(organize_mode='flat', photos=[{'photo_id': '7', 'name': 'Chapter', 'folder': chapter.name}])
    now = [1000]
    monkeypatch.setattr(maintenance, 'time', SimpleNamespace(time=lambda: now[0]))
    assert tick() == 'pending' and '内容不同' in status()['error'] and archive.read_bytes() == before
    flat = public / 'Chapter__7_p00001.webp'
    assert flat.read_bytes() == b'published' and user_page.read_bytes() == b'different'
    user_page.write_bytes(b'previous')
    now[0] += 100
    assert tick() == 'completed' and not flat.exists() and not (chapter / '00001.webp').exists()
    assert (chapter / '00099.webp').read_bytes() == b'user extra' and user_page.read_bytes() == b'previous'
    with zipfile.ZipFile(archive) as packed:
        assert packed.read(flat.name) == b'published' and packed.read('Chapter__7/00099.webp') == b'user extra'


@pytest.mark.parametrize('mode', ['none', 'by_author', 'flat'])
def test_only_latest_published_bytes_can_be_moved_or_deleted(root, mode):
    public = root / 'Book_123'
    chapter = public / 'Chapter__7'
    chapter.mkdir(parents=True)
    (chapter / '.jm-chapter.json').write_text('{"photo_id":"7","format":2}')
    for job_id in ('older', 'newer'):
        job(job_id, public)
    # Finish in reverse order. Page 1 was edited back to the older task's bytes.
    manifest('newer', chapter.name, {'00001.webp': b'new', '00002.webp': b'published'})
    manifest('older', 'Former__7', {'00001.webp': b'old', '00002.webp': b'prior'})
    keep = {'Chapter__7/00001.webp': b'old', 'Chapter__7/00003.webp': b'extra', 'cover.png': b'cover'}
    for name, data in keep.items():
        (public / name).write_bytes(data)
    (chapter / '00002.webp').write_bytes(b'published')
    if mode == 'flat':
        # Identical bytes at a user-created flat path don't transfer ownership of that file.
        (public / 'Alias__7_p00002.webp').write_bytes(b'published')
    queue(organize_mode=mode, photos=[{'photo_id': '7', 'folder': chapter.name, 'name': 'Chapter', 'page_count': 3}])
    assert tick() == 'completed'
    public = maintenance.public_directory('123')
    assert not (public / 'Chapter__7/00002.webp').exists()
    for name, data in keep.items():
        assert (public / name).read_bytes() == data
    with zipfile.ZipFile(public / 'Book_123.cbz') as packed:
        assert packed.testzip() is None
        assert all(packed.read(name) == data for name, data in keep.items())
        if mode == 'flat':
            assert (public / 'Alias__7_p00002.webp').read_bytes() == packed.read('Alias__7_p00002.webp') == b'published'
        else:
            assert packed.read('Chapter__7/00002.webp') == b'published'


@pytest.mark.parametrize('mode', ['flat', 'none'])
def test_conflicting_edit_preserves_old_archive_and_original_even_on_flat_retry(root, mode):
    public = root / 'Book_123'
    chapter = public / 'Chapter__7'
    chapter.mkdir(parents=True)
    (chapter / '.jm-chapter.json').write_text('{"photo_id":"7"}')
    source = chapter / '00001.webp'
    source.write_bytes(b'first')
    job('done', public)
    manifest('done', chapter.name, {source.name: b'first'})
    page = source if mode == 'none' else public / 'Chapter__7_p00001.webp'
    archive = public / 'Book_123.cbz'
    with zipfile.ZipFile(archive, 'w') as packed:
        packed.writestr(page.relative_to(public).as_posix(), b'first')
        packed.writestr('cover.webp', b'old')
    previous = archive.read_bytes()
    queue(organize_mode=mode, photos=[{'photo_id': '7', 'name': 'Chapter', 'folder': chapter.name}])
    if mode == 'flat':
        # Persist a real rename receipt, then edit the flat output before retry.
        (public / 'cover.webp').write_bytes(b'new')
        assert tick() == 'pending' and page.is_file() and archive.read_bytes() == previous
        (public / 'cover.webp').write_bytes(b'old')
        with db.transaction() as conn:
            conn.execute("UPDATE album_maintenance SET payload=json_remove(payload,'$._retry_at')")
    stat = page.stat()
    page.write_bytes(b'other')
    os.utime(page, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert tick() == 'pending' and '内容不同' in status()['error']
    assert archive.read_bytes() == previous and page.read_bytes() == b'other'


@pytest.mark.parametrize('when', ['before_pack', 'after_pack'])
def test_same_size_edit_across_pack_validation_is_not_deleted(root, monkeypatch, when):
    from core.packer import CbzPacker
    public = root / 'Book_123'
    chapter = public / 'Chapter__7'
    chapter.mkdir(parents=True)
    (chapter / '.jm-chapter.json').write_text('{"photo_id":"7"}')
    page = chapter / '00001.webp'
    page.write_bytes(b'first')
    job('done', public)
    manifest('done', chapter.name, {page.name: b'first'})
    target = public / 'Book_123.cbz'
    with zipfile.ZipFile(target, 'w') as archive:
        archive.writestr('retained.webp', b'old')
    previous = target.read_bytes()
    pack = CbzPacker.pack
    def edit():
        stat = page.stat()
        page.write_bytes(b'other')
        os.utime(page, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    def change_during_maintenance(self, *args, **kwargs):
        if when == 'before_pack':
            edit()
        result = pack(self, *args, **kwargs)
        if when == 'after_pack':
            edit()
        return result
    monkeypatch.setattr(CbzPacker, 'pack', change_during_maintenance)
    queue()
    state = tick()
    assert page.read_bytes() == b'other'
    if when == 'before_pack':
        assert state == 'pending' and target.read_bytes() == previous
    else:
        assert state == 'completed'
        with zipfile.ZipFile(target) as archive:
            assert archive.read('Chapter__7/00001.webp') == b'first'
            assert archive.read('retained.webp') == b'old'


@pytest.mark.skipif(os.name != 'nt', reason='requires Windows file sharing semantics')
@pytest.mark.parametrize('leave_locked', [False, True])
def test_real_windows_sharing_denial_preserves_originals_and_prior_archive(root, monkeypatch, leave_locked):
    public = root / 'Book_123'
    public.mkdir()
    archive = public / 'Book_123.cbz'
    with zipfile.ZipFile(archive, 'w') as initial:
        initial.writestr('old.webp', b'old')
    original = archive.read_bytes()
    page = public / 'new.webp'
    page.write_bytes(b'new')
    queue()
    now = [1000]
    monkeypatch.setattr(maintenance, 'time', SimpleNamespace(time=lambda: now[0]))
    with archive.open('rb') as held:
        for attempt in range(maintenance.MAX_ATTEMPTS if leave_locked else 1):
            state = tick()
            assert status()['attempts'] == attempt + 1 and status()['error']
            assert state == ('failed' if attempt + 1 == maintenance.MAX_ATTEMPTS else 'pending')
            assert archive.read_bytes() == original and page.read_bytes() == b'new'
            assert not maintenance.claim()
            now[0] += 100
        assert held.read() == original
    if leave_locked:
        assert not maintenance.claim()
        maintenance.retry('123')
    assert tick() == 'completed' and page.read_bytes() == b'new'  # no manifest after unlocking either
    with zipfile.ZipFile(archive) as packed:
        assert packed.testzip() is None and packed.read('old.webp') == b'old' and packed.read('new.webp') == b'new'
