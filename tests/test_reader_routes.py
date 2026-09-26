"""Local reader entry points; no upstream requests or jobs are created."""
import re
from pathlib import Path

import pytest
from PIL import Image

STATIC_JS = Path(__file__).resolve().parent.parent / "static" / "js"


def test_reader_renders_local_scrolling_controls(client):
    from core import database as db
    before = len(db.get_all_jobs())
    response = client.get("/read/123")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    for marker in ('data-album-id="123"', 'id="reader-pages"', 'id="reader-jump"', 'js/reader.js', 'css/reader.css'):
        assert marker in html
    assert len(db.get_all_jobs()) == before
    assert client.get("/api/preview/123").status_code == 404


@pytest.mark.parametrize("album_id", ["bad", "123x", "１２３", "1" * 21])
@pytest.mark.parametrize("prefix", ["/read/", "/preview/"])
def test_reading_pages_reject_non_numeric_id(client, prefix, album_id):
    assert client.get(prefix + album_id).status_code == 400


def test_paged_preview_still_available(client):
    html = client.get("/preview/123").get_data(as_text=True)
    assert 'id="preview-image"' in html
    assert 'js/preview.js' in html
    assert 'data-album-id="123"' in html


def test_reader_tools_are_hidden_but_accessible(client):
    html = client.get('/read/123').get_data(as_text=True)
    assert re.search(r'<section[^>]*id="reader-tools"[^>]*\bhidden\b', html)
    assert 'aria-controls="reader-tools" aria-expanded="false"' in html
    assert 'id="reader-tools-close"' in html
    assert '按 M' in html


def test_reader_has_accessible_edge_buttons(client):
    html = client.get('/read/123').get_data(as_text=True)
    assert 'id="reader-top" title="一键到顶" aria-label="一键到顶" disabled' in html
    assert 'id="reader-bottom" title="一键到底" aria-label="一键到底" disabled' in html


def test_reading_mode_links_and_thumbnail_controls(client):
    paged = client.get('/preview/123').get_data(as_text=True)
    continuous = client.get('/read/123').get_data(as_text=True)
    assert 'id="preview-continuous" href="/read/123"' in paged
    assert 'id="reader-paged" href="/preview/123"' in continuous
    assert 'id="thumb-prev"' in paged and 'id="thumb-next"' in paged
    assert 'css/preview.css' in paged


def test_both_reading_modes_share_navigation_helpers(client):
    for path in ('/preview/123', '/read/123'):
        html = client.get(path).get_data(as_text=True)
        assert html.index('js/reading-nav.js') < html.index('js/preview.js' if 'preview' in path else 'js/reader.js')
        assert 'class="reader-toolbar"' in html
    paged = client.get('/preview/123').get_data(as_text=True)
    # Back falls back to a real page instead of the old javascript:history.back() link
    assert 'id="preview-back"' in paged and 'javascript:' not in paged


def test_reading_and_search_pages_have_no_inline_handlers(client):
    for path in ('/preview/123', '/read/123'):
        assert not re.search(r'\son[a-z]+="', client.get(path).get_data(as_text=True))
    # Search history keywords are user-controlled: they must never be spliced into inline JS.
    for script in ('search.js', 'preview.js', 'reader.js', 'reading-nav.js'):
        assert 'onclick=' not in (STATIC_JS / script).read_text(encoding="utf-8")


@pytest.fixture
def preview_root(client, tmp_path, monkeypatch):
    from core import path_guard
    from routes import api_preview
    root = tmp_path / "downloads"
    (root / "Album").mkdir(parents=True)
    monkeypatch.setattr(path_guard, "DOWNLOAD_ROOT", root)
    monkeypatch.setattr(api_preview, "DOWNLOAD_ROOT", root)
    return root


def test_preview_image_serves_images_with_shared_mime_map(client, preview_root):
    Image.new("RGB", (8, 8), "white").save(preview_root / "Album" / "001.png")
    response = client.get("/api/preview-img/Album/001.png")
    assert response.status_code == 200
    assert response.mimetype == "image/png"
    response.close()


def test_preview_image_rejects_non_images_and_missing_files(client, preview_root):
    (preview_root / "Album" / "notes.txt").write_text("not an image", encoding="utf-8")
    assert client.get("/api/preview-img/Album/notes.txt").status_code == 403
    assert client.get("/api/preview-img/Album/missing.png").status_code == 404
