"""An older download may publish last without replacing the newer book pages."""
import io
import json
import zipfile
from pathlib import PurePosixPath

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
