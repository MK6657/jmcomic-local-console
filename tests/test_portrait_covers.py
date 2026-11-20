from io import BytesIO
from urllib.parse import urlsplit

from PIL import Image

from core import database as db, path_guard


def test_uncached_cover_redirects_to_portrait_then_square(client):
    base = "/api/cover/1466640"
    for endpoint, suffix in (
        (base, "_3x4"), (base + "/portrait", "_3x4"),
        (base + "/portrait?fallback=square", ""),
    ):
        response = client.get(endpoint)
        assert response.status_code == 302
        assert urlsplit(response.location).path == f"/media/albums/1466640{suffix}.jpg"
        assert response.cache_control.no_store


def test_cover_prefers_existing_local_image_even_with_no_portrait_variant(client, tmp_path, monkeypatch):
    monkeypatch.setattr(path_guard, "DOWNLOAD_ROOT", tmp_path)
    album = tmp_path / "Book_123"
    album.mkdir()
    Image.new("RGB", (40, 80), "blue").save(album / "00001.webp")
    db.insert_job("job_test_cover", "123", "Book", [])
    db.transition_job_status("job_test_cover", ["queued"], "completed", output_path=str(album))

    for query in ("", "?fallback=square"):
        response = client.get("/api/cover/123/portrait" + query)
        assert response.status_code == 200
        with Image.open(BytesIO(response.data)) as image:
            assert image.size == (40, 80)


def test_portrait_cover_downscales_large_local_page_without_cropping(client, tmp_path, monkeypatch):
    from core import thumb_cache
    monkeypatch.setattr(path_guard, "DOWNLOAD_ROOT", tmp_path / "downloads")
    monkeypatch.setattr(thumb_cache, "get_cache_dir", lambda: tmp_path / "cache")
    album = tmp_path / "downloads" / "LargeBook_456"
    album.mkdir(parents=True)
    # 1200x1600 (exact 3:4 portrait)
    Image.new("RGB", (1200, 1600), (42, 118, 96)).save(album / "00001.png")
    db.insert_job("job_test_large_cover", "456", "LargeBook", [])
    db.transition_job_status("job_test_large_cover", ["queued"], "completed", output_path=str(album))

    # Default /portrait downscales to 480px width preserving 3:4 ratio (480x640), zero cropping
    resp = client.get("/api/cover/456/portrait")
    assert resp.status_code == 200
    assert resp.mimetype == "image/webp"
    with Image.open(BytesIO(resp.data)) as thumb:
        assert thumb.size == (480, 640)

    # Compact bucket ?w=180 produces exact 180x240 WebP
    small = client.get("/api/cover/456/portrait?w=180")
    assert small.status_code == 200
    with Image.open(BytesIO(small.data)) as thumb:
        assert thumb.size == (180, 240)

    # ?original=1 bypasses thumbnail cache and returns original full-resolution PNG
    orig = client.get("/api/cover/456/portrait?original=1")
    assert orig.status_code == 200
    assert orig.mimetype == "image/png"
    with Image.open(BytesIO(orig.data)) as full:
        assert full.size == (1200, 1600)
