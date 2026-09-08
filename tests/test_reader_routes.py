"""Local reader entry points; no upstream requests or jobs are created."""
import pytest


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
def test_reader_rejects_non_numeric_id(client, album_id):
    assert client.get("/read/" + album_id).status_code == 400


def test_paged_preview_still_available(client):
    html = client.get("/preview/123").get_data(as_text=True)
    assert 'id="preview-image"' in html
    assert 'js/preview.js' in html
