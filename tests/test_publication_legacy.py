"""v4 resources and v5 snapshots must compose into one *readable* committed view."""
import io
import json
import os
from contextlib import contextmanager
from pathlib import Path
import zipfile

import pytest
from PIL import Image

from core import database as db
from core.download_layout import CHAPTER_MARKER
from test_publication_reader_export import finish_publication, prepare_publication, root


def resource(job, folder, selection, status='completed'):
    db.insert_job(job, '123', 'Album', selection)
    assert db.transition_job_status(job, ['queued'], status, output_path=str(folder), total_pages=3)


def image(color):
    out = io.BytesIO()
    with Image.new('RGB', (8, 8), color) as im:
        im.save(out, 'PNG')
    return out.getvalue()


def chapter(folder, name, marker_id, data=None):
    directory = folder / name
    directory.mkdir(parents=True)
    (directory / CHAPTER_MARKER).write_text(json.dumps({'photo_id': marker_id}), encoding='utf-8')
    if data:
        (directory / '00001.png').write_bytes(data)
    return directory


def reader(client):
    response = client.get('/api/preview/123')
    assert response.status_code == 200, response.json
    pages = []
    for page in response.json['pages']:
        with client.get(page['url']) as content:
            assert content.status_code == 200, page
            pages.append((page['photo_id'], content.data))
    return response.json['source'], pages


def publish(root):
    workspace, target, evidence = prepare_publication(root, 'v5')
    finish_publication('v5', target, evidence)
    return [('71', p.read_bytes()) for p in sorted(workspace.snapshot.rglob('*.webp'))]


@pytest.mark.parametrize('fmt', ['zip', 'cbz'])
def test_old_whole_album_archive_plus_new_loose_chapter(client, root, fmt):
    old = root / 'Former title'
    old.mkdir()
    packed = old / f'{old.name}.{fmt}'
    red, blue = image('red'), image('blue')
    with zipfile.ZipFile(packed, 'w') as zf:
        zf.writestr('A__68/00001.png', red)
        zf.writestr('B__69/00001.png', blue)
    original = packed.read_bytes()
    resource('v4', old, [])
    # The current album title is different; an unrelated ZIP must not become its own pack.
    with zipfile.ZipFile(old / 'Album.zip', 'w') as zf:
        zf.writestr('wrong.png', image('black'))
    assert reader(client) == ('archive', [('68', red), ('69', blue)])
    new = publish(root)
    assert reader(client) == ('mixed', [('68', red), ('69', blue), *new])
    assert packed.read_bytes() == original


@pytest.mark.parametrize('source', ['loose', 'pack', 'flat-pack'])
def test_legacy_pages_overlap_by_chapter_id_and_page_number(client, root, source):
    old = root / 'Album'
    stale, retained = image('red'), image('blue')
    folder = chapter(old, 'Former__71', '71')
    if source == 'loose':
        (folder / '00001.png').write_bytes(stale)
        (folder / '00003.png').write_bytes(retained)
    else:
        names = (('Former__71/00001.png', 'Former__71/00003.png') if source == 'pack'
                 else ('Former__71_p00001.png', 'Former__71_p00003.png'))
        with zipfile.ZipFile(old / 'Album.cbz', 'w') as archive:
            archive.writestr(names[0], stale)
            archive.writestr(names[1], retained)
    resource('v4', old, [])
    original = {page: page.read_bytes() for page in old.rglob('*') if page.is_file()}
    new = publish(root)
    assert reader(client)[1] == [*new, ('71', retained)]
    assert {page: page.read_bytes() for page in original} == original


def test_shared_v4_tree_only_admits_certified_chapters_including_packed_pages(client, root):
    old = root / 'Album'
    red, blue, dirty = image('red'), image('blue'), image('black')
    chapter(old, 'A__68_2', '68', red)
    chapter(old, 'B__69', '69')  # The packer removed originals, but kept their marker.
    chapter(old, 'Overlap__70', '70', dirty)
    chapter(old, 'NotSelected__73', '73', dirty)
    chapter(old, 'WrongMarker__74', '68', dirty)
    for name in ('Ambiguous__75', 'Ambiguous__75_2'):
        chapter(old, name, '75', dirty)
    (old / 'Flat__68_p00002.png').write_bytes(dirty)
    (old / 'A__68_2' / 'manual.png').write_bytes(dirty)
    with zipfile.ZipFile(old / 'Album.cbz', 'w') as zf:
        zf.writestr('B__69/00001.png', blue)
        zf.writestr('A__68_2/00001.png', dirty)  # loose wins
        zf.writestr('Overlap__70/00001.png', dirty)
        zf.writestr('NotSelected__73/00002.png', dirty)
        zf.writestr('B__69/nested/00001.png', dirty)
    resource('done1', old, ['68', '70', '74', '75'])
    resource('done2', old, ['69'])
    resource('different-folder', root / 'Elsewhere', ['73'])
    # Only evidence for the currently selected legacy tree is relevant.
    resource('latest', old, ['69'])
    assert client.post('/api/jobs/clear/completed').status_code == 200
    resource('paused', old, ['70'], 'paused')
    resource('private', root / '.jmcomic-work' / 'v5-paused' / 'pages', [], 'paused')
    original = {p: p.read_bytes() for p in old.rglob('*') if p.is_file()}
    new = publish(root)
    assert reader(client) == ('mixed', [('68', red), ('69', blue), *new])
    assert {p: p.read_bytes() for p in original} == original
    view = Path(db.get_completed_job_by_album_id('123')['output_path'])
    with zipfile.ZipFile(view / 'Album.cbz') as zf:
        assert set(zf.namelist()) == {'A__68_2/00001.png', 'B__69/00001.png'}


@pytest.mark.parametrize(('done', 'pending'), [(['68'], '[]'), ([], '["70"]'), (['68'], '{')])
def test_unknown_v4_ownership_does_not_guess(client, root, done, pending):
    old = root / 'Album'
    chapter(old, 'A__68', '68', image('red'))
    resource('v4', old, done)
    resource('active', old, ['70'], 'running')
    db.update_job('active', selected_photo_ids=pending)
    new = publish(root)
    assert reader(client) == ('files', new)
    assert (old / 'A__68' / '00001.png').is_file()


@pytest.mark.parametrize('shared', [False, True], ids=['whole-tree', 'marker-chapter'])
@pytest.mark.parametrize('fault', ['listing', 'page-stat'])
def test_legacy_scan_error_aborts_without_switching_shelf(client, root, monkeypatch, shared, fault):
    old = root / 'Album'
    red = image('red')
    folder = chapter(old, 'A__68', '68', red)
    resource('v4', old, ['68'])
    if shared:
        resource('paused', old, ['70'], 'paused')
    scan = os.scandir

    class LockedEntry:
        def __init__(self, entry):
            self.entry = entry

        def __getattr__(self, key):
            return getattr(self.entry, key)

        def stat(self, **kwargs):
            raise PermissionError('legacy page stat denied')

    @contextmanager
    def denied_stat(path):
        with scan(path) as entries:
            yield (LockedEntry(e) if e.name == '00001.png' else e for e in entries)

    def denied(path):
        if not isinstance(path, int) and Path(path) == folder:
            if fault == 'page-stat':
                return denied_stat(path)
            raise PermissionError('legacy listing denied')
        return scan(path)

    with monkeypatch.context() as patch:
        patch.setattr(os, 'scandir', denied)
        with pytest.raises(PermissionError, match='legacy .* denied'):
            prepare_publication(root, 'v5')
    assert not list((root / '.jmcomic-views').rglob('.jm-view.json'))
    assert db.get_completed_job_by_album_id('123')['job_id'] == 'v4'
    assert reader(client) == ('files', [('68', red)])


def test_disjoint_writers_and_fallback_cbz_keep_original_reader_priority(client, root):
    old = root / 'Old album'
    old.mkdir()
    red = image('red')
    for name, data in [('1.cbz', red), ('Album.cbz', image('blue'))]:
        with zipfile.ZipFile(old / name, 'w') as zf:
            zf.writestr('A__68/00001.png', data)
    resource('old', old, [])
    # Neither a different v4 folder nor a private v5 job ever wrote into this album tree.
    for job, path in [('other', root / 'Other'), ('private', root / '.jmcomic-work' / 'pages')]:
        resource(job, path, [], 'paused')
    assert reader(client) == ('archive', [('68', red)])
    new = publish(root)
    assert reader(client) == ('mixed', [('68', red), *new])


def test_unreadable_later_member_aborts_filtered_pack(client, root):
    from core import archive_pages

    old = root / 'Album'
    chapter(old, 'A__68', '68')
    resource('old', old, ['68'])
    resource('queued-v4', old, ['70'], 'queued')
    pack = old / 'Album.cbz'
    with zipfile.ZipFile(pack, 'w', compression=zipfile.ZIP_STORED) as zf:
        zf.writestr('A__68/00001.png', image('red'))
        zf.writestr('A__68/00002.png', image('blue'))
    # Central directory and first page remain valid; only a real read of page 2 fails CRC.
    original = pack.read_bytes().replace(image('blue'), image('blue')[:-1] + b'!', 1)
    pack.write_bytes(original)
    assert archive_pages.read_index(pack).status == 'ok'
    with pytest.raises(archive_pages.ArchiveError, match='CRC'):
        prepare_publication(root, 'v5')
    assert db.get_completed_job_by_album_id('123')['job_id'] == 'old'
    assert not list((root / '.jmcomic-views').rglob('.jm-view.json'))
    assert pack.read_bytes() == original


def test_paused_writer_through_a_directory_alias_still_contaminates_that_chapter(client, root):
    from test_storage import _directory_link, _remove_directory_link

    old, alias = root / 'Album', root / 'Former path'
    red = image('red')
    chapter(old, 'A__68', '68', red)
    chapter(old, 'B__69', '69', image('blue'))
    resource('old', old, ['68', '69'])
    _directory_link(old, alias)
    try:
        resource('paused', alias, ['69'], 'paused')
        new = publish(root)
        assert reader(client) == ('files', [('68', red), *new])
    finally:
        _remove_directory_link(alias)
