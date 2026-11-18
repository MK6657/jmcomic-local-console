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
