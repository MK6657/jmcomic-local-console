"""An older download may publish last without replacing the newer book pages."""
import io
import json
import zipfile
from pathlib import Path, PurePosixPath

import pikepdf
import pytest
from PIL import Image

from publication_support import maintenance_tick
from test_download_kernel import Book, Photo, image_bytes, pipeline


def _run_next(job_id, *, before_publish=None):
    from core import database as db, jm_service
    from core.progress import progress_manager

    job = db.claim_next_queued_job()
    assert job and job['job_id'] == job_id
    progress_manager.create_tracker(job_id)
    try:
        checkpoint = jm_service.download_album_job(
            job_id, job['album_id'], json.loads(job['selected_photo_ids']),
            begin_file_finalization=before_publish,
        )
        result = db.get_job(job_id)
        checkpoint.retire(result['status'])
        return result
    finally:
        progress_manager.remove_tracker(job_id)


def _zip_pages(client, url):
    pages = {}
    with client.post(url) as response:
        assert response.status_code == 200, url
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            for entry in archive.infolist():
                path = PurePosixPath(entry.filename)
                if path.suffix != '.webp':
                    continue
                key = (path.parent.name.rsplit('__', 1)[-1], int(path.stem))
                assert key not in pages, (url, entry.filename)
                pages[key] = archive.read(entry)
    return pages


def test_older_job_finishes_last_but_newer_job_owns_overlapping_pages(pipeline, client):
    from core import album_maintenance, database as db
    from core.job_manager import JobManager
    from core.settings import update_settings
    from core.task_commands import enqueue_download, resume_download

    update_settings({'organize_mode': 'none', 'auto_pack': True, 'delete_originals': True})
    first, shared, last = Photo('71', 'First', 1), Photo('72', 'Shared', 2), Photo('73', 'Last', 1)
    pipeline.books['100'] = Book([first, shared, last])
    a_pages = {('71', 1): image_bytes('red'), ('72', 1): image_bytes('blue'), ('72', 2): image_bytes('navy')}
    b_pages = {('72', 1): image_bytes('green'), ('72', 2): image_bytes('lime'), ('73', 1): image_bytes('gold')}

    def serve(pages):
        for photo in pipeline.books['100']:
            for number, image in enumerate(photo, 1):
                if (photo.photo_id, number) in pages:
                    pipeline.payloads[image.download_url] = pages[photo.photo_id, number]

    a = enqueue_download('100', 'Book', ['71', '72']).job_id
    b = enqueue_download('100', 'Book', ['72', '73']).job_id

    def pause_before_publish(job_id):
        assert JobManager().pause_job(job_id)[0]
        return False

    serve(a_pages)
    paused = _run_next(a, before_publish=pause_before_publish)
    assert (paused['status'], paused['done_pages']) == ('paused', len(a_pages))
    serve(b_pages)
    assert _run_next(b)['status'] == 'completed'
    assert db.get_job(a)['status'] == 'paused'
    assert resume_download(a)[0]
    assert _run_next(a)['status'] == 'completed'
    maintenance_tick()
    assert album_maintenance.statuses(['100'])['100']['state'] == 'completed'

    assert _zip_pages(client, f'/api/export/{a}/zip') == a_pages
    assert _zip_pages(client, f'/api/export/{b}/zip') == b_pages
    library = a_pages | b_pages
    assert _zip_pages(client, '/api/library/100/export/zip') == library
    preview = client.get('/api/preview/100')
    assert preview.status_code == 200
    assert preview.json['total_pages'] == len(library)
    pages = {}
    for page in preview.json['pages']:
        key = (page['photo_id'], page['offset'] + 1)
        assert key not in pages, key
        with client.get(page['url']) as image:
            assert image.status_code == 200
            pages[key] = image.data
    assert pages == library


@pytest.mark.parametrize(('suffix', 'static_format'), [('webp', 'WEBP'), ('jpg', 'JPEG'), ('png', 'PNG')])
@pytest.mark.parametrize('pack_format', ['zip', 'cbz'])
def test_formatted_download_pause_resume_preview_pack_and_export(pipeline, client, suffix, static_format, pack_format):
    from core.job_manager import JobManager
    from core.settings import update_settings
    from core.task_commands import enqueue_download, resume_download

    update_settings({'image_format': suffix, 'auto_pack': True, 'delete_originals': True,
                     'pack_format': pack_format, 'organize_mode': 'none'})
    photo = pipeline.books['100'][1]
    photo[1].is_gif = True
    for index, page in enumerate(photo):
        with io.BytesIO() as buffer, Image.new('RGB', (8, 8), ('red', 'blue', 'green')[index]) as frame:
            if index == 1:
                with Image.new('RGB', (8, 8), 'yellow') as another:
                    frame.save(buffer, 'GIF', save_all=True, append_images=[another], duration=[70, 130], loop=0)
            else:
                frame.save(buffer, static_format)
            pipeline.payloads[page.download_url] = buffer.getvalue()

    job = enqueue_download('100', 'Book', ['72']).job_id

    def pause(job_id):
        assert JobManager().pause_job(job_id) == (True, '已暂停')
        return False

    paused = _run_next(job, before_publish=pause)
    assert paused['status'] == 'paused' and paused['done_pages'] == 3
    assert client.get('/api/preview/100').status_code == 404
    assert resume_download(job)[0]
    completed = _run_next(job)
    assert completed['status'] == 'completed', completed['error_message']
    assert set(pipeline.calls.values()) == {1} and len(pipeline.calls) == 3
    maintenance_tick()

    from core.database import get_job
    folder = Path(get_job(job)['output_path'])
    preview = client.get('/api/preview/100')
    assert preview.status_code == 200 and preview.json['source'] == 'archive'
    expected = {'00001.' + suffix: (static_format, 1),
                '00002.webp': ('WEBP', 2), '00003.' + suffix: (static_format, 1)}
    from_reader = []
    for page in preview.json['pages']:
        with client.get(page['url']) as response:
            assert response.status_code == 200
            from_reader.append(response.data)
    assert len(from_reader) == len(expected)
    for data, (fmt, frames) in zip(from_reader, expected.values()):
        with Image.open(io.BytesIO(data)) as opened:
            assert (opened.format, getattr(opened, 'n_frames', 1)) == (fmt, frames)
    with zipfile.ZipFile(folder / f'{folder.name}.{pack_format}') as archive:
        assert archive.testzip() is None
        pages = [entry for entry in archive.infolist() if Path(entry.filename).name in expected]
        assert [archive.read(entry) for entry in pages] == from_reader
        assert all(entry.compress_type == zipfile.ZIP_STORED for entry in pages)

    for base in (f'/api/export/{job}', '/api/library/100/export'):
        for fmt in ('cbz', 'zip', 'pdf'):
            with client.post(f'{base}/{fmt}') as response:
                assert response.status_code == 200, response.json
                with io.BytesIO(response.data) as content:
                    if fmt == 'pdf':
                        with pikepdf.Pdf.open(content) as document:
                            assert len(document.pages) == 3
                    else:
                        with zipfile.ZipFile(content) as archive:
                            assert archive.testzip() is None
                            pages = [entry for entry in archive.infolist() if Path(entry.filename).name in expected]
                            assert [archive.read(entry) for entry in pages] == from_reader
                            if fmt == 'cbz':
                                assert all(entry.compress_type == zipfile.ZIP_STORED for entry in pages)


@pytest.mark.parametrize(('chapter', 'message'), [('73', '缺失'), ('74', '无图片')])
def test_incomplete_explicit_selection_stops_before_fetching_any_page(pipeline, client, chapter, message):
    pipeline.books['100'].append(Photo('74', 'Empty', 0))
    result = pipeline.run('selected', selection=['71', chapter])
    assert result['status'] == 'failed'
    assert message in result['error_message'] and chapter in result['error_message']
    assert '重试' in result['error_message'] and not pipeline.calls
    assert not list(pipeline.root.iterdir())
    assert client.get('/api/preview/100').status_code == 404


def test_resume_uses_confirmed_new_format_even_with_old_receipt(pipeline, client):
    from core.job_manager import JobManager
    from core.settings import update_settings
    from core.task_commands import enqueue_download, resume_download

    update_settings({'image_format': 'webp', 'auto_pack': False, 'organize_mode': 'none'})
    job = enqueue_download('100', 'Book', ['71']).job_id

    def pause(job_id):
        assert JobManager().pause_job(job_id)[0]
        return False

    paused = _run_next(job, before_publish=pause)
    work = Path(paused['output_path'])
    assert list(work.rglob('00001.webp'))
    update_settings({'image_format': 'png'})
    with io.BytesIO() as data, Image.new('RGB', (8, 8), 'blue') as image:
        image.save(data, 'PNG')
        payload = data.getvalue()
    pipeline.payloads[pipeline.books['100'][0][0].download_url] = payload
    assert resume_download(job)[0]
    result = _run_next(job)
    assert result['status'] == 'completed', result['error_message']
    assert set(pipeline.calls.values()) == {2}
    assert not list(work.rglob('00001.*'))
    pages = client.get('/api/preview/100').json['pages']
    assert len(pages) == 1 and pages[0]['url'].endswith('00001.png')
    with client.get(pages[0]['url']) as response:
        assert response.data == payload


def test_requeued_job_can_recover_an_already_sealed_snapshot_after_format_change(pipeline, client, monkeypatch):
    from core.publication import Workspace
    from core.settings import update_settings
    from core.task_commands import enqueue_download, requeue_interrupted
    from core.transfer import DownloadInterrupted

    update_settings({'image_format': 'webp', 'auto_pack': False, 'organize_mode': 'none'})
    job = enqueue_download('100', 'Book', ['71']).job_id

    def interrupted(*args):
        raise DownloadInterrupted('stopped')

    with monkeypatch.context() as patch:
        patch.setattr(Workspace, 'project', interrupted)
        stopped = _run_next(job)
    assert stopped['status'] == 'running'
    work = Path(stopped['output_path'])
    sealed, = (work.parent / 'snapshot').rglob('*.webp')
    original = sealed.read_bytes()
    assert requeue_interrupted(job)

    update_settings({'image_format': 'png'})
    with io.BytesIO() as data, Image.new('RGB', (8, 8), 'blue') as image:
        image.save(data, 'PNG')
        pipeline.payloads[pipeline.books['100'][0][0].download_url] = data.getvalue()
    finished = _run_next(job)
    assert finished['status'] == 'completed', finished['error_message']
    page, = client.get('/api/preview/100').json['pages']
    with client.get(page['url']) as response:
        assert response.data == original
    assert sealed.read_bytes() == original and not list(work.rglob('00001.*'))


def test_clear_finished_reclaims_proven_work_only(pipeline, client):
    from core import database as db
    from core.download_checkpoint import DownloadCheckpoint
    from core.download_layout import chapter_output_dir
    from core.publication import Workspace

    def workspace(job_id, status):
        db.insert_job(job_id, '100', 'Book', ['71'])
        assert db.transition_job_status(job_id, ['queued'], status)
        receipt = DownloadCheckpoint(job_id, {})
        ws = Workspace(pipeline.root, job_id, '100', pipeline.root / 'Book_100', receipt)
        receipt.own(ws.work)
        folder = chapter_output_dir(ws.work, pipeline.books['100'][0])
        kept = folder / '00001.webp'
        kept.write_bytes(image_bytes('red'))
        receipt.confirm(kept)
        db.update_job(job_id, output_path=str(ws.work), total_pages=1)
        return ws, kept, receipt

    failed, owned, receipt = workspace('failed_work', 'failed')
    changed = owned.with_name('00002.webp')
    changed.write_bytes(image_bytes('blue'))
    receipt.confirm(changed)
    changed.write_bytes(image_bytes('orange'))
    unknown = owned.with_name('notes.txt')
    unknown.write_text('local notes', encoding='utf-8')
    failed.snapshot.mkdir()
    snap = failed.snapshot / 'archived.webp'
    snap.write_bytes(image_bytes('green'))
    _, paused, _ = workspace('paused_work', 'paused')
    linked, referenced, _ = workspace('referenced_work', 'canceled')
    db.insert_job('still_references', '100', 'Book', ['72'])
    db.update_job('still_references', output_path=str(linked.work))
    public = pipeline.root / 'Book_100' / '00001.webp'
    public.parent.mkdir()
    public.write_bytes(image_bytes('green'))

    response = client.post('/api/jobs/clear/finished')
    assert response.status_code == 200 and response.json['deleted'] == 2
    assert not owned.exists()
    assert all(path.is_file() for path in (changed, unknown, snap, paused, referenced, public))
    assert db.get_job('paused_work')['status'] == 'paused'


def test_removed_canceled_job_keeps_work_until_its_writer_retires(pipeline, client, monkeypatch):
    import threading
    from core import database as db
    from core.job_manager import JobManager
    from core.settings import update_settings
    from routes import api_jobs

    manager = JobManager()
    monkeypatch.setattr(api_jobs, 'job_manager', manager)
    update_settings({'image_threads': 1, 'photo_threads': 1, 'auto_pack': False})
    photo = pipeline.books['100'][1]
    entered, finish = threading.Event(), threading.Event()

    def hold_second(url):
        if url == photo[1].download_url:
            entered.set()
            assert finish.wait(10)

    pipeline.on_fetch = hold_second
    job_id = manager.create_job('100', 'Book', ['72'])
    manager.schedule_next()
    try:
        assert entered.wait(5)
        work = Path(db.get_job(job_id)['output_path'])
        page, = work.rglob('00001.webp')
        assert client.post(f'/api/jobs/{job_id}/cancel').status_code == 200
        assert client.delete(f'/api/jobs/{job_id}').status_code == 200
        assert page.is_file()
    finally:
        finish.set()
        assert manager.stop(timeout=5)
    assert not work.exists()
