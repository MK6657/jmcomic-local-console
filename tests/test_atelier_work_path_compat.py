"""Offline workspace upgrades and SDK writes at a deep Windows download root."""
import json
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from jmcomic import JmImageClient, JmImageResp

from core import database as db, jm_service, path_guard, validation
from core.download_checkpoint import DownloadCheckpoint
from core.download_layout import CHAPTER_MARKER, album_output_dir, chapter_output_dir
from core.image_download import download_image
from core.progress import progress_manager
from core.publication import WORK, VIEWS, Workspace, _key
from core.settings import update_settings
from core.storage import STAGING_DIRECTORY
from fake_transport import owned_client
from test_download_kernel import Book, Photo, image_bytes, pipeline


@pytest.fixture
def deep_root(pipeline, monkeypatch):
    parent = pipeline.root
    root = parent / ('深' + 'x' * (162 - validation.path_units(parent)))
    assert validation.path_units(root) == 164
    monkeypatch.setattr(validation, 'legacy_windows_paths', lambda: True)
    for module in (jm_service, path_guard):
        monkeypatch.setattr(module, 'DOWNLOAD_ROOT', root)
    return root


def test_long_titles_reach_sdk_and_versioned_view_at_164_units(deep_root, pipeline, monkeypatch):
    album_id, photo_id = '123456', '7654321'
    title = '合成📚' * 90
    photo = Photo(photo_id, title, 1)
    book = pipeline.books[album_id] = Book([photo])
    book.name = title
    update_settings({'auto_pack': True, 'delete_originals': False, 'organize_mode': 'none'})
    shelf = album_output_dir(deep_root, 'deep', title, album_id, book, pack=True)
    shelf.mkdir(parents=True)
    (shelf / 'notes').write_bytes(b'keep')
    payload = image_bytes('red')
    writes = []

    class LocalClient(JmImageClient):
        def get_album_detail(self, key):
            return pipeline.books[key]

        def check_photo(self, photo):
            pass

        def get_jm_image(self, img_url):
            return JmImageResp(SimpleNamespace(status_code=200, content=payload, url=img_url))

        def save_image_resp(self, decode_image, img_save_path, *args):
            writes.append(Path(img_save_path))
            return super().save_image_resp(decode_image, img_save_path, *args)

    monkeypatch.setattr(jm_service, 'get_client', lambda **kwargs: (owned_client(LocalClient()), None))
    monkeypatch.setattr(jm_service.image_download, 'download_image', download_image)
    row = pipeline.run('deep', album_id)
    assert row['status'] == 'completed', row['error_message']
    assert len(writes) == 1 and writes[0].parents[1] == deep_root / STAGING_DIRECTORY
    view = Path(row['output_path'])
    assert view.is_relative_to(deep_root / VIEWS)
    chapter = chapter_output_dir(view, photo, create=False)
    archive = view / (view.name + '.cbz')
    with ZipFile(archive) as packed:
        assert packed.read(f'{chapter.name}/00001.webp') == payload
    snapshot = deep_root / WORK / _key('deep') / 'snapshot'
    assert (snapshot / chapter.name / '00001.webp').read_bytes() == payload
    for path in [*writes, archive, chapter / CHAPTER_MARKER, snapshot / chapter.name / CHAPTER_MARKER]:
        assert validation.path_units(path) <= 259 and validation.path_units(path.parent) <= 248
    assert (shelf / 'notes').read_bytes() == b'keep'


def test_upgrade_keeps_old_receipt_with_renamed_chapter_and_another_job(deep_root, pipeline):
    album_id, job_id = '123456', 'paused'
    photo = Photo('765432', '新书名📚' * 40, 2)
    deep_root.mkdir()
    target = deep_root / ('旧_' + album_id)
    old = Workspace(deep_root, job_id, album_id, target, SimpleNamespace(directory=None))
    prior = old.work / '旧章__765432'
    prior.mkdir()
    (prior / CHAPTER_MARKER).write_text(json.dumps({'photo_id': photo.photo_id, 'format': 2}))
    page = prior / '00001.webp'
    page.write_bytes(image_bytes('blue'))
    db.insert_job(job_id, album_id, '合成书目', [])
    receipt = DownloadCheckpoint(job_id, {})
    receipt.own(old.work)
    receipt.confirm(page)
    receipt.retire('paused')
    db.insert_job('other', album_id, '合成书目', [])
    other = DownloadCheckpoint('other', {})
    sibling = Workspace(deep_root, 'other', album_id, target, other)
    other.own(sibling.work)
    occupied = old.work / chapter_output_dir(sibling.work, photo).name
    occupied.mkdir()
    (occupied / 'notes').write_bytes(b'keep')
    other_page = sibling.work / prior.name / page.name
    other_page.parent.mkdir()
    other_page.write_bytes(page.read_bytes())
    assert sibling.work != old.work and not other.contains(other_page)

    pipeline.books[album_id] = Book([photo])
    db.update_job(job_id, total_pages=2, done_pages=1, output_path=str(old.work))
    assert db.transition_job_status(job_id, ['queued'], 'running')
    progress_manager.create_tracker(job_id)
    try:
        jm_service.download_album_job(job_id, album_id, [])
    finally:
        progress_manager.remove_tracker(job_id)
    row = db.get_job(job_id)
    assert row['status'] == 'completed', row['error_message']
    assert pipeline.calls == {photo[1].download_url: 1}
    assert Path(row['output_path']) == target
    assert (target / prior.name / page.name).read_bytes() == other_page.read_bytes()
    assert (occupied / 'notes').read_bytes() == b'keep'


def test_unusable_deep_root_fails_before_any_files_or_transfer(deep_root, pipeline, monkeypatch):
    impossible = deep_root / ('x' * 95)
    for module in (jm_service, path_guard):
        monkeypatch.setattr(module, 'DOWNLOAD_ROOT', impossible)
    row = pipeline.run('no-space')
    assert row['status'] == 'failed' and 'JMCONSOLE_DOWNLOAD_DIR' in row['error_message']
    assert not pipeline.calls and not deep_root.exists()


def test_new_job_reuses_shelf_shortened_by_previous_staging_budget(pipeline, monkeypatch):
    monkeypatch.setattr(validation, 'legacy_windows_paths', lambda: True)
    root = pipeline.root / ('x' * (139 - validation.path_units(pipeline.root)))
    assert validation.path_units(root) == 140
    title = '书' * 100
    photo = Photo('765432', '章', 1)
    previous = root / ('书' * 11 + '_123456')
    previous.mkdir(parents=True)
    assert album_output_dir(root, 'new', title, '123456', [photo], pack=True) == previous
