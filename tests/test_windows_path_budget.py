"""Offline downloads through the real workspace, rename and archive boundaries."""
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from core import jm_service, path_guard, publication, validation
from core.download_layout import CHAPTER_MARKER, album_output_dir, chapter_output_dir
from core.publication import VIEWS, Workspace
from core.settings import update_settings
from test_album_maintenance import queue, root, tick
from test_download_kernel import Book, Photo, pipeline


@pytest.fixture(autouse=True)
def legacy(monkeypatch):
    monkeypatch.setattr(validation, 'legacy_windows_paths', lambda: True)


def test_staged_chapter_collision_and_versioned_archive(pipeline, monkeypatch, record_property):
    album_id, pid = '12345678901234567890', '98765432109876543210'
    photo = Photo(pid, '章📚' * 80, 1)
    book = pipeline.books[album_id] = Book([photo])
    book.name = '書📚' * 100
    update_settings({'auto_pack': True, 'delete_originals': False, 'organize_mode': 'none'})
    shelf = album_output_dir(pipeline.root, 'long', book.name, album_id, book, pack=True)
    shelf.mkdir()
    (shelf / 'notes').write_text('keep')
    ws = Workspace(pipeline.root, 'long', album_id, shelf, SimpleNamespace(directory=None))
    occupied = chapter_output_dir(ws.work, photo)
    (occupied / CHAPTER_MARKER).unlink()  # not owned by the worker
    (occupied / 'notes').write_text('keep')

    attempts = []
    copies = []
    fetch = jm_service.image_download.download_image
    copy = publication._copy

    def observe_copy(source, dest):
        copies.append(dest)
        return copy(source, dest)

    def observe(*args, img_save_path, **kwargs):
        attempts.append(Path(img_save_path))
        return fetch(*args, img_save_path=img_save_path, **kwargs)

    monkeypatch.setattr(jm_service.image_download, 'download_image', observe)
    monkeypatch.setattr(publication, '_copy', observe_copy)
    row = pipeline.run('long', album_id)
    assert row['status'] == 'completed', row['error_message']
    view = Path(row['output_path'])
    assert view.is_relative_to(pipeline.root / VIEWS) and view.name.endswith('_' + album_id)
    chapter = chapter_output_dir(view, photo, create=False)
    snapshot_page = ws.snapshot / chapter.name / '00001.webp'
    assert chapter.name.endswith(f'__{pid}_2') and snapshot_page.is_file()
    archive = view / (view.name + '.cbz')
    with ZipFile(archive) as packed:
        assert packed.read(chapter.name + '/00001.webp') == (chapter / '00001.webp').read_bytes()
    # Observe *actual* temp names, not copies of the constants used to budget them.
    prepares = {p.parent.parent for p in copies if p.name == '00001.webp' and p.parent.name == chapter.name}
    assert len(prepares) == 2  # one seal, one public projection
    assert all(p.parent == ws.base / '.jmcomic-staging' and p.name.startswith('pub-') for p in prepares)
    lengths = dict(page_temp=max(map(validation.path_units, attempts)),
                   snapshot=validation.path_units(snapshot_page.with_name(CHAPTER_MARKER)),
                   publication_temp=max(map(validation.path_units, copies)),
                   view=validation.path_units(chapter / CHAPTER_MARKER), archive=validation.path_units(archive))
    for kind, units in lengths.items():
        record_property(kind, units)
    assert max(lengths.values()) <= 259
    assert (occupied / 'notes').read_text() == (shelf / 'notes').read_text() == 'keep'


def test_author_move_keeps_deep_user_files_addressable(root):
    from core import album_maintenance
    source = root / 'Book_123'
    extra = Path('user' * 10, 'notes' * 8 + '.txt')
    (source / extra).parent.mkdir(parents=True)
    (source / extra).write_bytes(b'keep')
    (source / '00001.webp').write_bytes(b'page')
    queue(author='作者📚' * 90, organize_mode='by_author', delete_originals='false')
    assert tick() == 'completed'
    target = album_maintenance.public_directory('123')
    assert target.parent.parent == root and target.name == source.name
    assert (target / extra).read_bytes() == b'keep' and validation.path_units(target / extra) <= 259
    with ZipFile(target / 'Book_123.cbz') as packed:
        assert packed.read('00001.webp') == b'page'


def test_existing_legacy_shelf_and_marked_chapter_keep_their_names(root):
    title = 'b' * min(120, 175 - validation.path_units(root))
    photo = Photo('456', 'Old' * 12, 1)
    shelf = root / (title + '_123')
    assert album_output_dir(root, 'job', title, '123', [photo]) != shelf
    old = shelf / (photo.name + '__456')
    old.mkdir(parents=True)
    marker = old / CHAPTER_MARKER
    marker.write_bytes(b'{"photo_id":"456","format":1}')
    assert album_output_dir(root, 'job', title, '123', [photo]) == shelf
    assert chapter_output_dir(shelf, photo, create=False) == old
    assert marker.read_bytes() == b'{"photo_id":"456","format":1}'


def test_impossible_root_fails_job_before_first_transfer(pipeline, monkeypatch):
    deep = pipeline.root / ('long' * 44)
    for module in (jm_service, path_guard):
        monkeypatch.setattr(module, 'DOWNLOAD_ROOT', deep)
    row = pipeline.run('too-deep')
    assert row['status'] == 'failed' and 'JMCONSOLE_DOWNLOAD_DIR' in row['error_message']
    assert not pipeline.calls and not deep.exists()


def test_opted_in_process_keeps_long_names(tmp_path, monkeypatch):
    monkeypatch.setattr(validation, 'legacy_windows_paths', lambda: False)
    root = tmp_path / ('long' * 28)
    target = album_output_dir(root, 'job', 'a' * 190, '123', [Photo('456', 'chapter', 1)])
    assert target == root / ('a' * 190 + '_123')
    assert validation.path_units(target) > 259
