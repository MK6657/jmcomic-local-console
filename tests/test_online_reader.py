"""Online reading: page lists, on-demand images and the page cache. Fake clients only; no network."""
import io
import os
import threading
import time
from pathlib import Path

import pytest
from PIL import Image


class FakeImage:
    def __init__(self, url, scramble_id="220980"):
        self.download_url = url
        self.scramble_id = scramble_id


class FakePhoto:
    def __init__(self, photo_id, pages, name="Chapter"):
        self.photo_id = photo_id
        self.name = name
        self.page_arr = [f"{n:05d}.webp" for n in range(1, pages + 1)] or None
        self.from_album = None

    def __len__(self):
        return len(self.page_arr or ())

    def __getitem__(self, index):
        # jmcomic's real CDN URL shape; photo ids below the scramble id are never sliced (num == 0)
        return FakeImage(f"https://cdn.invalid/media/photos/{self.photo_id}/{index + 1:05d}.webp?v=1")


class FakeAlbum(list):
    album_id = "123"
    name = "Online album"


class FakeResponse:
    def __init__(self, content, status_code=200):
        self.content = content
        self.status_code = status_code


class FakePostman:
    """Stands in for the client's curl_cffi session. answer(url) returns the body, a FakeResponse, or raises."""
    def __init__(self, answer):
        self.answer = answer
        self.requests = []

    def get(self, url, headers=None, timeout=None):
        self.requests.append(url)
        result = self.answer(url)
        return result if isinstance(result, FakeResponse) else FakeResponse(result)


def _png(size=(12, 16)):
    buffer = io.BytesIO()
    Image.new("RGB", size, "white").save(buffer, "PNG")
    return buffer.getvalue()


class FakeClient:
    """Metadata client (album / chapter lists) and page-image client in one."""
    def __init__(self, album=None, fail_download=False):
        self.album = album
        self.fail_download = fail_download
        self.postman = FakePostman(self._image)

    @property
    def downloads(self):
        return self.postman.requests

    def get_album_detail(self, album_id):
        return self.album

    def check_photo(self, photo):
        raise OSError("chapter unavailable")

    def get_photo_detail(self, photo_id, fetch_album=True):
        return FakePhoto(photo_id, 2)

    def update_request_with_specify_domain(self, kwargs, domain, is_image=False):
        kwargs["headers"] = {"Accept": "image/webp"}

    def _image(self, url):
        if self.fail_download:
            raise OSError("upstream refused")
        return _png()


def use_client(monkeypatch, fake):
    from core import jm_service
    monkeypatch.setattr(jm_service, "get_client", lambda shared=True: (fake, None))
    monkeypatch.setattr(jm_service, "new_image_client", lambda: fake)


class RealUrlPhoto(FakePhoto):
    """Pages with jmcomic's real CDN URL shape: .../media/photos/<photo_id>/00001.webp?v=..."""
    def __init__(self, photo_id, pages, scramble_id, ext="webp"):
        super().__init__(photo_id, pages)
        self.scramble_id = scramble_id
        self.ext = ext

    def __getitem__(self, index):
        return FakeImage(f"https://cdn.invalid/media/photos/{self.photo_id}/{index + 1:05d}.{self.ext}?v=1",
                         self.scramble_id)


def payload_client(photo, payload):
    """Serves `photo` and answers every page-image request with `payload` (the upstream bytes)."""
    fake = FakeClient()
    fake.get_photo_detail = lambda photo_id, fetch_album=True: photo
    fake.postman = FakePostman(lambda url: payload)
    return fake


def _encoded(fmt, size=(24, 48)):
    image = Image.new("RGB", size, "white")
    for y in range(0, size[1], 8):  # stripes so the scrambled slices differ
        image.paste((40 * (y // 8) % 256, 90, 160), (0, y, size[0], y + 4))
    buffer = io.BytesIO()
    image.save(buffer, fmt)
    return buffer.getvalue()


@pytest.fixture
def online(client, tmp_path, monkeypatch):
    """Isolated cache directory and a fake jmcomic client for every call. Prefetch is off here (its own tests
    in test_online_fetch.py turn it on) so each test sees exactly the requests it made."""
    from core import jm_service, online_reader
    monkeypatch.setattr(online_reader, "CACHE_ROOT", tmp_path / "online-cache")
    monkeypatch.setattr(online_reader, "_PREFETCH_AHEAD", 0)
    online_reader.clear_cache()
    fake = FakeClient(FakeAlbum([FakePhoto("123", 3, "第1话"), FakePhoto("124", 0, "第2话"), FakePhoto("125", 2, "第3话")]))
    use_client(monkeypatch, fake)
    monkeypatch.setattr(jm_service, "close_client", lambda _client: None)
    yield online_reader, fake
    online_reader.clear_cache()


def test_online_page_uses_reader_in_online_mode(client, monkeypatch):
    from core import local_availability
    monkeypatch.setattr(local_availability, "is_readable", lambda album_id: True)  # /read: album counts as downloaded
    online_html = client.get("/online/123").get_data(as_text=True)
    assert 'data-source="online"' in online_html and "js/reader.js" in online_html
    assert 'id="reader-paged"' not in online_html  # the paged view only reads local files
    assert 'href="/album/123" id="reader-back"' in online_html
    local_html = client.get("/read/123").get_data(as_text=True)
    assert 'data-source="local"' in local_html and 'id="reader-paged"' in local_html


def test_online_page_error_points_to_the_log_viewer():
    script = (Path(__file__).resolve().parent.parent / "static" / "js" / "reader.js").read_text(encoding="utf-8")
    assert "在线获取这张图片失败，可以重试。" in script and "'/settings#logs'" in script
    assert "这张图片加载失败，可以重试。" in script  # local mode keeps its wording
    assert "innerHTML" not in script


@pytest.mark.parametrize("path,status", [
    ("/online/bad", 400), ("/api/online/bad", 400), ("/api/online-img/bad/0", 400),
    ("/api/online-img/123/-1", 404),
])
def test_online_routes_reject_bad_ids(client, path, status):
    assert client.get(path).status_code == status


def test_album_pages_lists_every_readable_chapter(client, online):
    from core import database as db
    jobs_before = len(db.get_all_jobs())
    data = client.get("/api/online/123").get_json()
    assert data["status"] == "ok" and data["title"] == "Online album"
    assert data["total_pages"] == 5 and data["skipped_chapters"] == 1
    assert [p["page"] for p in data["pages"]] == [1, 2, 3, 4, 5]
    assert data["pages"][0] == {"page": 1, "url": "/api/online-img/123/0", "chapter": "第1话", "photo_id": "123"}
    assert data["pages"][3]["url"] == "/api/online-img/125/0"
    assert len(db.get_all_jobs()) == jobs_before  # reading online never creates a download job


def test_single_chapter_album_whose_page_list_fails_is_retryable(client, online):
    """Most albums have one chapter whose page list comes from check_photo. A network failure there is a
    retryable load failure (504), not a permanent "nothing to read online" (404), and the log says why."""
    from core import logger as lm
    _, fake = online
    fake.album = FakeAlbum([FakePhoto("500", 0, "第1话")])  # FakeClient.check_photo raises OSError
    response = client.get("/api/online/500")
    assert response.status_code == 504
    assert response.get_json()["message"] == "在线加载失败或超时，请稍后重试"
    assert "获取 photo 详情失败 photo_id=500 error=OSError: chapter unavailable" in lm.ERROR_LOG_FILE.read_text(
        encoding="utf-8")


def test_album_whose_chapters_really_have_no_pages_is_404(client, online, monkeypatch):
    _, fake = online
    fake.album = FakeAlbum([FakePhoto("501", 0)])
    monkeypatch.setattr(fake, "check_photo", lambda photo: None)  # fetched fine, still no pages
    assert client.get("/api/online/501").status_code == 404


def test_online_album_errors(client, online, monkeypatch):
    online_reader, _ = online
    def slow(_album_id):
        raise TimeoutError("在线加载失败或超时，请稍后重试")
    monkeypatch.setattr(online_reader, "album_pages", slow)
    assert client.get("/api/online/123").status_code == 504
    monkeypatch.setattr(online_reader, "album_pages", lambda _a: {"title": "x", "pages": [], "skipped_chapters": 2})
    assert client.get("/api/online/123").status_code == 404


def test_image_is_fetched_once_then_served_from_cache(client, online):
    online_reader, fake = online
    client.get("/api/online/123")
    first = client.get("/api/online-img/123/1")
    assert first.status_code == 200
    assert first.mimetype == "image/png"  # unscrambled originals keep their real format
    assert first.headers["Cache-Control"] == "public, max-age=86400"
    first.close()
    again = client.get("/api/online-img/123/1")
    assert again.status_code == 200
    again.close()
    assert fake.downloads == ["https://cdn.invalid/media/photos/123/00002.webp?v=1"]
    assert (online_reader.CACHE_ROOT / "123" / "00002.webp").is_file()


def test_image_for_unlisted_chapter_fetches_its_page_list(client, online):
    _, fake = online
    response = client.get("/api/online-img/999/1")  # e.g. the server restarted while a reader stayed open
    assert response.status_code == 200
    response.close()
    assert fake.downloads == ["https://cdn.invalid/media/photos/999/00002.webp?v=1"]


@pytest.mark.parametrize("photo_id,scramble_id,ext,fmt,mime", [
    ("300000", "220980", "webp", "WEBP", "image/jpeg"),  # scrambled: sliced back, then encoded as JPEG
    ("100000", "220980", "webp", "WEBP", "image/webp"),  # album older than the scramble id: upstream bytes
    ("300000", None, "webp", "WEBP", "image/webp"),      # no scramble id: upstream bytes
    ("300000", None, "jpg", "JPEG", "image/jpeg"),       # no scramble id, JPEG upstream: still the upstream bytes
], ids=["scrambled", "unscrambled", "raw-copy", "jpeg-original"])
def test_page_is_cached_under_the_fixed_name_and_served_as_its_real_type(client, online, monkeypatch, photo_id,
                                                                          scramble_id, ext, fmt, mime):
    """The cache name is always <nnnnn>.webp; image_mime reports what the file really is."""
    online_reader, _ = online
    payload = _encoded(fmt)
    use_client(monkeypatch, payload_client(RealUrlPhoto(photo_id, 2, scramble_id, ext), payload))
    response = client.get(f"/api/online-img/{photo_id}/1")
    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.mimetype == mime
    with Image.open(io.BytesIO(response.get_data())) as served:
        assert served.size == (24, 48)
    if mime != "image/jpeg" or scramble_id is None:
        assert response.get_data() == payload  # not scrambled: never decoded or re-encoded
    response.close()
    assert [p.name for p in (online_reader.CACHE_ROOT / photo_id).iterdir()] == ["00002.webp"]  # no temp files


def _animated_gif():
    frames = []
    for shade in (40, 120, 200):
        frame = Image.new("RGB", (24, 48), "white")
        for y in range(0, 48, 8):
            frame.paste((shade, 90, (y * 5) % 256), (0, y, 24, y + 4))
        frames.append(frame)
    buffer = io.BytesIO()
    frames[0].save(buffer, "GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
    return buffer.getvalue()


@pytest.mark.parametrize("photo_id", ["300000", "250000", "100000"])
def test_gif_page_is_served_unsliced_and_animated(client, online, monkeypatch, photo_id):
    """GIF pages are never scrambled (jmcomic's own downloader does not decode them). Decoding sliced them
    into shuffled bands for albums newer than the scramble id and always dropped the animation."""
    online_reader, _ = online
    payload = _animated_gif()
    use_client(monkeypatch, payload_client(RealUrlPhoto(photo_id, 2, "220980", "gif"), payload))
    response = client.get(f"/api/online-img/{photo_id}/0")
    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.mimetype == "image/gif"
    assert response.get_data() == payload  # the original bytes: no slicing, no re-encoding
    with Image.open(io.BytesIO(response.get_data())) as served:
        assert served.n_frames == 3
    response.close()
    assert [p.name for p in (online_reader.CACHE_ROOT / photo_id).iterdir()] == ["00001.webp"]


def test_download_job_does_not_unscramble_gif_pages(client, monkeypatch, tmp_path):
    from core import jm_service
    from jmcomic import JmImageDetail
    seen = []
    monkeypatch.setattr(jm_service, "get_client", lambda shared=True: (object(), None))
    monkeypatch.setattr(jm_service, "close_client", lambda _client: None)
    monkeypatch.setattr(jm_service, "_should_stop", lambda *args: False)
    monkeypatch.setattr(jm_service, "_download_image_single_attempt",
                        lambda client, job_id, album_id, album, output, photo, idx, img, url, path, scramble_id,
                        *rest, **kwargs: seen.append(scramble_id))
    for ext in ("gif", "webp"):
        image = JmImageDetail.of("300000", "220980", f"https://cdn.invalid/media/photos/300000/00001.{ext}")
        assert jm_service.image_is_gif(image) is (ext == "gif")
        jm_service._download_chapter_image(None, "job_x", "1", None, None, tmp_path, 0, image, 1, [0], [], [],
                                           None, None, threading.Lock(), 1, 10)
    assert seen == [None, 220980]


class _Page:
    def __init__(self, url, scramble_id):
        self.download_url = url
        self.scramble_id = scramble_id


class DownloadPhoto(list):
    """A chapter for the download job: iterable pages with jmcomic's real CDN URL shape."""
    def __init__(self, photo_id, exts, scramble_id="220980"):
        super().__init__(_Page(f"https://cdn.invalid/media/photos/{photo_id}/{n:05d}.{ext}?v=1", scramble_id)
                         for n, ext in enumerate(exts, 1))
        self.photo_id = photo_id
        self.name = "第1话"
        self.page_arr = [f"{n:05d}.{ext}" for n, ext in enumerate(exts, 1)]


class DownloadAlbum(list):
    name = "GIF album"
    author = "someone"
    tags = []


def jm_download_client(album, payloads):
    """Saves pages with jmcomic's OWN JmImageClient.download_image -> JmImageResp.transfer_to; the upstream
    bytes are chosen by the page URL's extension. Records every upstream image request."""
    from types import SimpleNamespace
    from jmcomic.jm_client_interface import JmImageClient, JmImageResp

    class JmDownloadClient(JmImageClient):
        def __init__(self):
            self.fetched = []

        def get_album_detail(self, album_id):
            return album

        def check_photo(self, photo):
            pass

        def get_jm_image(self, img_url):
            self.fetched.append(img_url)
            ext = img_url.split("?", 1)[0].rsplit(".", 1)[1]
            return JmImageResp(SimpleNamespace(status_code=200, content=payloads[ext], url=img_url, text=""))

    return JmDownloadClient()


@pytest.fixture
def download_job(client, tmp_path, monkeypatch):
    """Runs the real download_album_job into a temp downloads root with the given fake client."""
    from core import database as db, jm_service, path_guard
    from core.progress import progress_manager
    root = tmp_path / "downloads"
    root.mkdir()
    monkeypatch.setattr(path_guard, "DOWNLOAD_ROOT", root)
    monkeypatch.setattr(jm_service, "DOWNLOAD_ROOT", root)
    monkeypatch.setattr(jm_service, "close_client", lambda _client: None)
    runs = []

    def run(album_id, fake):
        job_id = f"job_{len(runs) + 1:012x}"
        runs.append(job_id)
        monkeypatch.setattr(jm_service, "get_client", lambda shared=True: (fake, None))
        db.insert_job(job_id, album_id, "GIF album", [])
        db.update_job(job_id, status="running")
        progress_manager.create_tracker(job_id)
        try:
            jm_service.download_album_job(job_id, album_id, [])
        finally:
            progress_manager.remove_tracker(job_id)
        return db.get_job(job_id)
    return root, run


def _frame_difference(expected, actual):
    from PIL import ImageChops, ImageStat
    return sum(ImageStat.Stat(ImageChops.difference(expected.convert("RGB"), actual.convert("RGB"))).mean) / 3


@pytest.mark.parametrize("photo_id", ["300000", "100000"])
def test_downloaded_gif_page_keeps_every_frame(download_job, photo_id):
    """jmcomic converts a GIF saved under a .webp name with a plain Image.save(): only frame 0 survived.
    The download now keeps the animation (animated WebP, same file name) and never slices the GIF."""
    root, run = download_job
    source = _animated_gif()
    fake = jm_download_client(DownloadAlbum([DownloadPhoto(photo_id, ["gif", "webp"])]),
                              {"gif": source, "webp": _encoded("WEBP")})
    job = run(photo_id, fake)
    assert job["status"] == "completed", job["error_message"]
    (chapter,) = [path for path in Path(job["output_path"]).iterdir() if path.is_dir()]
    assert sorted(p.name for p in chapter.iterdir()) == [".jm-chapter.json", "00001.webp", "00002.webp"]
    with Image.open(io.BytesIO(source)) as original, Image.open(chapter / "00001.webp") as page:
        assert page.format == "WEBP" and page.n_frames == original.n_frames == 3
        for index in range(3):
            original.seek(index)
            page.seek(index)
            assert _frame_difference(original, page) < 8  # the right frame, not sliced into shuffled bands
            assert page.info.get("duration") == 100      # known once the frame is decoded
    with Image.open(chapter / "00002.webp") as other:
        assert other.format == "WEBP" and getattr(other, "n_frames", 1) == 1


def test_redownload_refetches_gif_pages_saved_by_older_versions(download_job):
    """Chapters written before this fix hold GIF pages sliced or flattened to one frame; they pass the
    skip_existing check, so a re-download kept them. Their GIF pages are fetched again (other pages are still
    skipped) and, once the job succeeds, the chapter marker records the new format so it happens only once."""
    import json
    from core import jm_service
    from core.settings import get_settings
    assert get_settings()["skip_existing"] == "true"
    root, run = download_job
    album = DownloadAlbum([DownloadPhoto("300000", ["gif", "webp"])])
    chapter = root / "GIF album_300000" / "第1话__300000"
    chapter.mkdir(parents=True)
    (chapter / ".jm-chapter.json").write_text(json.dumps({"photo_id": "300000"}), encoding="utf-8")
    Image.new("RGB", (24, 48), "red").save(chapter / "00001.webp", "WEBP")   # sliced/flattened old GIF page
    Image.new("RGB", (24, 48), "blue").save(chapter / "00002.webp", "WEBP")  # a correct old page
    kept = (chapter / "00002.webp").read_bytes()
    fake = jm_download_client(album, {"gif": _animated_gif(), "webp": _encoded("WEBP")})
    job = run("300000", fake)
    assert job["status"] == "completed", job["error_message"]
    assert [url.split("?")[0].rsplit("/", 1)[1] for url in fake.fetched] == ["00001.gif"]
    with Image.open(chapter / "00001.webp") as page:
        assert page.n_frames == 3
    assert (chapter / "00002.webp").read_bytes() == kept
    assert json.loads((chapter / ".jm-chapter.json").read_text(encoding="utf-8")) == {
        "photo_id": "300000", "format": jm_service._CHAPTER_FORMAT}
    again = jm_download_client(album, {"gif": _animated_gif(), "webp": _encoded("WEBP")})
    assert run("300000", again)["status"] == "completed"
    assert again.fetched == []  # up to date now: every page skipped


def test_failed_redownload_keeps_the_old_chapter_marker(download_job):
    """If the job does not complete, the GIF pages must be fetched again next time."""
    import json
    root, run = download_job
    album = DownloadAlbum([DownloadPhoto("300000", ["gif"])])
    chapter = root / "GIF album_300000" / "第1话__300000"
    chapter.mkdir(parents=True)
    (chapter / ".jm-chapter.json").write_text(json.dumps({"photo_id": "300000"}), encoding="utf-8")
    Image.new("RGB", (24, 48), "red").save(chapter / "00001.webp", "WEBP")
    old = (chapter / "00001.webp").read_bytes()
    job = run("300000", jm_download_client(album, {"gif": b"GIF89a truncated"}))
    assert job["status"] == "failed"
    assert (chapter / "00001.webp").read_bytes() == old  # never replaced by a broken download
    assert json.loads((chapter / ".jm-chapter.json").read_text(encoding="utf-8")) == {"photo_id": "300000"}
    assert sorted(p.name for p in chapter.iterdir()) == [".jm-chapter.json", "00001.webp"]  # no temp files


def test_locked_temp_file_does_not_turn_a_timeout_into_502(client, online, monkeypatch):
    """A fetch that timed out may still be writing its temp file (Windows refuses to delete it). The cleanup
    must not replace the TimeoutError: the reader should get 504 "try again", not 502."""
    online_reader, fake = online
    fake.fail_download = True
    real_unlink = Path.unlink

    def locked(self, *args, **kwargs):
        if ".tmp-" in self.name:
            raise PermissionError(13, "The process cannot access the file because it is being used by another process")
        return real_unlink(self, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", locked)
    client.get("/api/online/123")
    response = client.get("/api/online-img/123/0")
    assert response.status_code == 504
    assert response.get_json()["message"] == "在线加载失败或超时，请稍后重试"


def test_invalid_download_is_rejected_without_leftovers(client, online, monkeypatch):
    """A host answering 200 with an HTML page is a failed host: the other image hosts are tried (each once,
    plus the one quick retry), nothing is written, and the reader gets a retryable 504."""
    from urllib.parse import urlsplit
    from jmcomic import JmModuleConfig
    online_reader, _ = online
    fake = payload_client(RealUrlPhoto("300000", 2, None), b"<html>blocked</html>")
    use_client(monkeypatch, fake)
    assert client.get("/api/online-img/300000/0").status_code == 504
    folder = online_reader.CACHE_ROOT / "300000"
    assert not folder.exists() or list(folder.iterdir()) == []
    hosts = [urlsplit(url).netloc for url in fake.downloads]
    assert set(hosts) == {"cdn.invalid", *JmModuleConfig.DOMAIN_IMAGE_LIST}
    assert len(hosts) == len(set(hosts)) + 1  # the page's own host got its one quick retry


def test_image_out_of_range_is_404(client, online):
    client.get("/api/online/123")
    assert client.get("/api/online-img/123/3").status_code == 404


def test_failed_download_leaves_no_partial_file(client, online):
    online_reader, fake = online
    fake.fail_download = True
    client.get("/api/online/123")
    assert client.get("/api/online-img/123/0").status_code == 504
    leftovers = list(online_reader.CACHE_ROOT.rglob("*.*")) if online_reader.CACHE_ROOT.exists() else []
    assert leftovers == []


def test_cache_trim_evicts_least_recently_read_pages(client, online, monkeypatch):
    online_reader, _ = online
    folder = online_reader.CACHE_ROOT / "123"
    folder.mkdir(parents=True)
    now = time.time()
    for n in range(10):
        page = folder / f"{n + 1:05d}.webp"
        page.write_bytes(b"x" * 100)
        os.utime(page, (now - 100 + n, now - 100 + n))
    # temp files left by timed-out fetches: the legacy "*.part" name and the current "*.tmp-*.webp" one
    for name in ("00011.abc.part", "00012.tmp-abc12345.webp"):
        (folder / name).write_bytes(b"x")
        os.utime(folder / name, (now - 3600, now - 3600))
    in_flight = folder / "00013.tmp-def67890.webp"  # still being written: neither deleted nor evicted
    in_flight.write_bytes(b"x" * 100)
    os.utime(in_flight, (now - 300, now - 300))
    monkeypatch.setattr(online_reader, "CACHE_LIMIT_BYTES", 500)
    online_reader._trim_cache(force=True)
    remaining = sorted(p.name for p in folder.iterdir())
    # oldest pages removed down to 80% of the limit, stale temp files removed
    assert remaining == [f"{n:05d}.webp" for n in range(7, 11)] + [in_flight.name]


def test_clear_cache_endpoint_also_clears_online_pages(client, online):
    online_reader, _ = online
    client.get("/api/online/123")
    client.get("/api/online-img/123/0").close()
    assert online_reader.CACHE_ROOT.exists()
    assert client.post("/api/system/clear-cache").status_code == 200
    assert not online_reader.CACHE_ROOT.exists()


def test_check_album_photos_keeps_order_when_a_chapter_fails(client):
    from core import jm_service
    album = FakeAlbum([FakePhoto("1", 2), FakePhoto("2", 0), FakePhoto("3", 1)])
    failed = []
    photos = jm_service.check_album_photos(FakeClient(album), album, failed=failed)
    assert [p.photo_id for p in photos] == ["1", "2", "3"]
    assert [len(p) for p in photos] == [2, 0, 1]
    assert all(p.from_album is album for p in photos)
    assert [p.photo_id for p in failed] == ["2"]
