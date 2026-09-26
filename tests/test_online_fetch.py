"""Online page fetching: original bytes vs descrambling, host fallback, the client pool and prefetch.
Fake clients, a fake CDN and a fake clock; no network."""
import io
import threading
import time
from pathlib import Path

import pytest
from PIL import Image

HOSTS = ["img-a.test", "img-b.test", "img-c.test"]   # stands in for JmModuleConfig.DOMAIN_IMAGE_LIST
OWN = "img-own.test"                                  # the page's own image host (not in the list)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Response:
    def __init__(self, content, status_code=200):
        self.content = content
        self.status_code = status_code


class Cdn:
    """Answers every page-image request. answer(url, timeout) returns bytes / a Response, or raises."""
    def __init__(self):
        self.answer = lambda url, timeout: page_bytes(url)
        self.calls = []       # (client, url, timeout)
        self.lock = threading.Lock()

    def urls(self):
        with self.lock:
            return [url for _, url, _ in self.calls]

    def hosts(self):
        return [url.split("/")[2] for url in self.urls()]


class ImageClient:
    """What jm_service.new_image_client() returns: a postman plus jmcomic's per-client image-header rule."""
    def __init__(self, cdn):
        client = self

        class Postman:
            def get(self, url, headers=None, timeout=None):
                assert headers == {"X-Requested-With": "com.JMComic3.app"}  # jmcomic's image headers
                with cdn.lock:
                    cdn.calls.append((client, url, timeout))
                result = cdn.answer(url, timeout)
                return result if isinstance(result, Response) else Response(result)

        self.postman = Postman()

    def update_request_with_specify_domain(self, kwargs, domain, is_image=False):
        if is_image:
            kwargs["headers"] = {"X-Requested-With": "com.JMComic3.app"}


class Page:
    def __init__(self, url, scramble_id):
        self.download_url = url
        self.scramble_id = scramble_id


class Photo:
    def __init__(self, photo_id, pages, scramble_id="220980", host=OWN, ext="webp"):
        self.photo_id = photo_id
        self.page_arr = [f"{n:05d}.{ext}" for n in range(1, pages + 1)]
        self.scramble_id = scramble_id
        self.host = host
        self.ext = ext

    def __len__(self):
        return len(self.page_arr)

    def __getitem__(self, index):
        return Page(f"https://{self.host}/media/photos/{self.photo_id}/{index + 1:05d}.{self.ext}?v=1",
                    self.scramble_id)


def _pattern(size=(24, 48), mode="RGB", seed=0):
    """Rows of distinct colours, so a wrongly ordered slice is visible pixel for pixel."""
    width, height = size
    data = bytes((x * 7 + y * 13 + seed * 31 + c * 50) % 256 for y in range(height) for x in range(width)
                 for c in range(3))
    return Image.frombytes("RGB", size, data).convert(mode)


def _encode(image, fmt, **kwargs):
    buffer = io.BytesIO()
    image.save(buffer, fmt, **kwargs)
    return buffer.getvalue()


_PAGE_CACHE = {}


def page_bytes(url):
    """A distinct small WebP per URL path (host and query do not matter, like the real CDN)."""
    path = url.split("/", 3)[3].split("?")[0]
    if path not in _PAGE_CACHE:
        _PAGE_CACHE[path] = _encode(_pattern(seed=len(_PAGE_CACHE)), "WEBP")
    return _PAGE_CACHE[path]


def refuse(url, timeout):
    raise ConnectionError("curl: (35) Connection closed abruptly")


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class Api:
    """The shared jmcomic client, asked for a chapter whose page list is not in memory (get_photo_detail).
    answer(photo_id) returns the chapter or raises."""
    def __init__(self, upstream):
        self.upstream = upstream
        self.answer = lambda photo_id: self.upstream[photo_id]
        self.calls = []
        self.lock = threading.Lock()

    def get_photo_detail(self, photo_id, fetch_album=True):
        with self.lock:
            self.calls.append(photo_id)
        return self.answer(photo_id)


class Chapters(dict):
    """Chapters of the album the reader opened: album_pages() remembered their page lists."""
    def __init__(self, reader, upstream):
        super().__init__()
        self.reader, self.upstream = reader, upstream

    def __setitem__(self, photo_id, photo):
        super().__setitem__(photo_id, photo)
        self.upstream[photo_id] = photo
        self.reader._remember_photos([photo])


@pytest.fixture
def env(client, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from jmcomic import JmModuleConfig
    from core import jm_service, online_reader
    monkeypatch.setattr(online_reader, "CACHE_ROOT", tmp_path / "online-cache")
    monkeypatch.setattr(JmModuleConfig, "DOMAIN_IMAGE_LIST", list(HOSTS))
    monkeypatch.setattr(online_reader, "_PREFETCH_AHEAD", 0)  # the prefetch tests turn it on
    monkeypatch.setattr(online_reader, "_PREFETCH_IDLE", 0.2)
    clock = Clock()
    monkeypatch.setattr(online_reader, "_clock", clock)
    cdn = Cdn()
    created, closed = [], []

    def new_image_client():
        created.append(ImageClient(cdn))
        return created[-1]
    monkeypatch.setattr(jm_service, "new_image_client", new_image_client)
    monkeypatch.setattr(jm_service, "close_client",
                        lambda client: closed.append(client) if isinstance(client, ImageClient) else None)
    upstream = {}              # every chapter the API knows
    api = Api(upstream)
    monkeypatch.setattr(jm_service, "get_client", lambda shared=True: (api, None))
    online_reader.clear_cache()
    closed.clear()
    photos = Chapters(online_reader, upstream)
    state = SimpleNamespace(reader=online_reader, cdn=cdn, clock=clock, photos=photos, upstream=upstream, api=api,
                            created=created, closed=closed)
    yield state
    online_reader.clear_cache()
    assert wait_until(lambda: not online_reader._prefetch_active and online_reader._prefetch_threads == 0)
    assert wait_until(lambda: not online_reader._photo_lookups)


def _read(env, photo_id, index):
    return env.reader.image_file(photo_id, index).read_bytes()


def _log_text(kind="app"):
    from core import logger
    path = logger.LOG_FILE if kind == "app" else logger.ERROR_LOG_FILE
    return path.read_text(encoding="utf-8") if path.exists() else ""


# ── original bytes vs descrambling ─────────────────────

def _animated_gif():
    frames = [_pattern(seed=seed) for seed in range(3)]
    buffer = io.BytesIO()
    frames[0].save(buffer, "GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
    return buffer.getvalue()


@pytest.mark.parametrize("fmt,photo_id,scramble_id,ext", [
    ("WEBP", "100000", "220980", "webp"),  # album older than the scramble id: num == 0
    ("PNG", "300000", None, "webp"),       # no scramble id
    ("JPEG", "300000", None, "jpg"),
    ("GIF", "300000", "220980", "gif"),    # GIF pages are never scrambled
], ids=["num0-webp", "no-scramble-png", "no-scramble-jpeg", "gif"])
def test_page_that_needs_no_descrambling_keeps_the_upstream_bytes(env, monkeypatch, fmt, photo_id, scramble_id, ext):
    payload = _animated_gif() if fmt == "GIF" else _encode(_pattern(), fmt)
    env.photos[photo_id] = Photo(photo_id, 2, scramble_id, ext=ext)
    env.cdn.answer = lambda url, timeout: payload

    def no_encoding(*args, **kwargs):
        raise AssertionError("the page was re-encoded")
    monkeypatch.setattr(Image.Image, "save", no_encoding)
    assert _read(env, photo_id, 0) == payload
    path = env.reader.CACHE_ROOT / photo_id / "00001.webp"
    assert env.reader.image_mime(path) == f"image/{fmt.lower()}"
    assert [p.name for p in path.parent.iterdir()] == ["00001.webp"]


@pytest.mark.parametrize("num", [2, 4, 6, 8, 10, 12, 14, 16, 18, 20])
@pytest.mark.parametrize("mode", ["RGB", "RGBA", "L"])
def test_unscramble_matches_jmcomic_pixel_for_pixel(app, tmp_path, num, mode):
    """_unscramble must produce exactly what jmcomic's decode_and_save produces (compared via lossless PNG),
    including the leftover rows when the height is not a multiple of num."""
    from jmcomic import JmImageTool
    from core import online_reader
    source = _pattern((31, 203), mode, seed=num)
    expected_path = tmp_path / "jmcomic.png"
    JmImageTool.decode_and_save(num, source, str(expected_path))
    with Image.open(expected_path) as expected:
        restored = online_reader._unscramble(source, num)
        assert restored.mode == expected.mode == "RGB"
        assert restored.tobytes() == expected.tobytes()


def test_scrambled_page_is_restored_like_jmcomic_and_stored_as_jpeg(env, tmp_path):
    """End to end on a scrambled album: the slice count is jmcomic's, the stored page is a fast JPEG whose
    pixels match jmcomic's own decode_and_save output of the same upstream bytes."""
    from jmcomic import JmImageTool
    photo = Photo("300000", 6)
    env.photos["300000"] = photo
    width, height = 40, 211
    source = Image.new("RGB", (width, height))  # a smooth gradient: JPEG keeps it at ~40 dB, a misplaced slice
    source.putdata([(y * 255 // height, y * 255 // height, x * 255 // width)  # drops it below 30 dB
                    for y in range(height) for x in range(width)])
    payload = _encode(source, "WEBP", lossless=True)
    env.cdn.answer = lambda url, timeout: payload
    nums = []
    for index in range(6):
        url = photo[index].download_url
        num = JmImageTool.get_num_by_url(220980, url.split("?")[0])
        assert env.reader._scramble_num(photo[index], url) == num
        nums.append(num)
        stored = env.reader.CACHE_ROOT / "300000" / f"{index + 1:05d}.webp"
        env.reader.image_file("300000", index)
        assert env.reader.image_mime(stored) == "image/jpeg"
        expected_path = tmp_path / f"expected-{index}.png"
        JmImageTool.decode_and_save(num, JmImageTool.open_image(payload), str(expected_path))
        with Image.open(expected_path) as expected, Image.open(stored) as served:
            assert served.size == expected.size
            assert _psnr(expected, served) > 35
    assert len(set(nums)) >= 4 and 0 not in nums


def _psnr(a, b):
    import math
    from PIL import ImageChops, ImageStat
    diff = ImageChops.difference(a.convert("RGB"), b.convert("RGB"))
    mse = sum(ImageStat.Stat(diff.point(lambda x: x * x)).mean) / 3
    return 99.0 if mse == 0 else 10 * math.log10(255 * 255 / mse)


def test_truncated_upstream_image_is_not_cached(env):
    payload = _encode(_pattern((64, 96)), "JPEG")
    env.photos["100001"] = Photo("100001", 1)
    env.cdn.answer = lambda url, timeout: payload[:len(payload) // 2]
    with pytest.raises(TimeoutError):
        env.reader.image_file("100001", 0)
    assert not (env.reader.CACHE_ROOT / "100001").exists()


# ── host fallback ──────────────────────────────────────

def test_host_fallback_order_quick_retry_and_failure_memory(env):
    env.photos["100002"] = Photo("100002", 4)

    def answer(url, timeout):
        host = url.split("/")[2]
        if host in (OWN, "img-a.test"):
            raise ConnectionError(f"curl: (35) Connection closed abruptly in connection to {host}")
        return page_bytes(url)
    env.cdn.answer = answer
    _read(env, "100002", 0)
    # own host, its one quick retry, then the list in order until one works
    assert env.cdn.hosts() == [OWN, OWN, "img-a.test", "img-b.test"]
    assert all(url.endswith("/media/photos/100002/00001.webp?v=1") for url in env.cdn.urls())
    assert ("换用图片域名后取图成功 photo_id=100002 page=1 host=img-b.test failed=img-own.test,img-a.test"
            in _log_text())

    env.cdn.calls.clear()
    _read(env, "100002", 1)
    assert env.cdn.hosts() == ["img-b.test"]  # failed hosts wait 60 s; the last host that worked goes first

    env.cdn.calls.clear()
    env.clock.advance(61)
    env.cdn.answer = lambda url, timeout: page_bytes(url)
    _read(env, "100002", 2)
    assert env.cdn.hosts() == [OWN]  # remembered failures expire


def test_recently_failed_hosts_are_still_tried_when_nothing_else_is_left(env):
    env.photos["100003"] = Photo("100003", 2, host="img-a.test")
    env.cdn.answer = refuse
    with pytest.raises(TimeoutError):
        env.reader.image_file("100003", 0)
    assert sorted(set(env.cdn.hosts())) == HOSTS
    env.cdn.calls.clear()
    env.cdn.answer = lambda url, timeout: page_bytes(url) if "img-c" in url else refuse(url, timeout)
    _read(env, "100003", 1)  # every host is in its cool-down: all are tried again rather than none
    assert env.cdn.hosts()[-1] == "img-c.test"


def test_page_budget_bounds_the_total_time_and_logs_one_warning(env):
    """Every host hangs until its connect timeout. With the Settings timeout at its minimum (5 s) the page budget
    is _PAGE_BUDGET (15 s): the page gives up after it, each attempt gets the time that is left, a slow failure is
    never retried on the same host, and one WARNING names the page, the hosts tried and the last error."""
    from core.settings import update_settings
    update_settings({"timeout": "5"})
    env.photos["100004"] = Photo("100004", 1)

    def hang(url, timeout):
        env.clock.advance(timeout[0])
        raise TimeoutError(f"curl: (28) Connection timed out after {int(timeout[0] * 1000)} milliseconds")
    env.cdn.answer = hang
    start = env.clock()
    with pytest.raises(TimeoutError):
        env.reader.image_file("100004", 0)
    assert env.clock() - start <= env.reader._PAGE_BUDGET
    assert env.cdn.hosts() == [OWN, "img-a.test", "img-b.test", "img-c.test"]
    assert [timeout for _, _, timeout in env.cdn.calls] == [(4.0, 11.0), (4.0, 7.0), (4.0, 3.0), (3.0, 0.0)]
    errors = _log_text("error")
    warning = ("在线阅读 取图失败 photo_id=100004 page=1 hosts=img-own.test,img-a.test,img-b.test,img-c.test "
               "error=TimeoutError: curl: (28) Connection timed out after 3000 milliseconds")
    assert errors.count(warning) == 1


def test_missing_page_is_not_held_against_the_host(env):
    env.photos["100005"] = Photo("100005", 2)
    env.cdn.answer = lambda url, timeout: Response(b"", 404) if OWN in url else page_bytes(url)
    _read(env, "100005", 0)
    assert env.cdn.hosts() == [OWN, "img-a.test"]  # no quick retry for a 404
    env.cdn.calls.clear()
    env.cdn.answer = lambda url, timeout: page_bytes(url)
    _read(env, "100005", 1)
    assert env.cdn.hosts() == [OWN]  # a 404 does not put the host in its cool-down


def test_page_missing_on_every_host_does_not_put_the_hosts_in_cool_down(env, monkeypatch):
    """The real CDN answers HTTP 502 (not 404) for a page it does not have, from every host. Two hosts giving
    the same status for the page means the page is broken: no more hosts are tried, no host is put in its
    cool-down, and prefetch carries on with the following pages."""
    env.photos["100015"] = Photo("100015", 8)

    def answer(url, timeout):
        if "/00003.webp" in url:
            return Response(b"<html>502 Bad Gateway</html>", 502)
        return page_bytes(url)
    env.cdn.answer = answer
    _read(env, "100015", 1)
    env.cdn.calls.clear()
    with pytest.raises(TimeoutError):
        env.reader.image_file("100015", 2)
    assert env.cdn.hosts() == [OWN, OWN, "img-a.test"]  # the page's own host (one quick retry), then one more
    assert not env.reader._host_failed_at and not env.reader._all_hosts_down(OWN)
    assert env.reader._last_good_host == OWN
    assert ("在线阅读 取图失败 photo_id=100015 page=3 hosts=img-own.test,img-a.test error=_FetchError: HTTP 502"
            in _log_text("error"))
    monkeypatch.setattr(env.reader, "_PREFETCH_AHEAD", 4)
    env.reader.image_file("100015", 1)  # the reader is back on page 2 (cached): prefetch pages 3-6
    assert wait_until(lambda: all(_cached(env, "100015", i) for i in (3, 4, 5)))


def test_host_answering_5xx_for_a_page_another_host_serves_goes_to_the_back(env):
    env.photos["100016"] = Photo("100016", 3)
    env.cdn.answer = lambda url, timeout: Response(b"", 522) if OWN in url else page_bytes(url)
    _read(env, "100016", 0)
    assert env.cdn.hosts() == [OWN, OWN, "img-a.test"]
    env.cdn.calls.clear()
    _read(env, "100016", 1)
    assert env.cdn.hosts() == ["img-a.test"]  # the own host failed a page that exists: it waits its cool-down


def _slow_link(env, seconds):
    """A link that needs `seconds` for the whole page: an attempt whose total timeout is shorter is cut off
    with the data received so far (fake clock)."""
    def answer(url, timeout):
        total = timeout[0] + timeout[1]
        if total < seconds:
            env.clock.advance(total)
            raise TimeoutError(f"curl: (28) Operation timed out after {int(total * 1000)} milliseconds with "
                               f"{int(961605 * total / seconds)} out of 961605 bytes received")
        env.clock.advance(seconds)
        return page_bytes(url)
    return answer


def test_slow_but_steady_page_loads_within_the_settings_timeout(env):
    """A user on a slow link raised Settings -> timeout to 60; a large page needs 16 s at that speed (downloads
    fetch it fine). The online reader gives the page the Settings timeout instead of fixed 10 s attempts."""
    from core.settings import update_settings
    update_settings({"timeout": "60"})
    env.photos["100017"] = Photo("100017", 2)
    env.cdn.answer = _slow_link(env, 16.1)
    _read(env, "100017", 0)
    assert env.cdn.hosts() == [OWN]
    assert env.cdn.calls[0][2] == (4.0, 56.0)


def test_attempt_cut_short_by_the_page_budget_is_not_held_against_the_host(env):
    env.photos["100018"] = Photo("100018", 2)
    env.cdn.answer = _slow_link(env, 45)  # longer than the default Settings timeout (30 s)
    start = env.clock()
    with pytest.raises(TimeoutError):
        env.reader.image_file("100018", 0)
    assert env.clock() - start == 30
    assert env.cdn.hosts() == [OWN]  # data kept arriving: never restarted from zero on another host
    assert not env.reader._host_failed_at


def test_image_clients_abandon_a_stalled_transfer(env, monkeypatch):
    """A host that accepts the request and then sends (almost) nothing is abandoned by libcurl itself after 8 s
    (LOW_SPEED_LIMIT / LOW_SPEED_TIME on the pooled client's session), so the next host gets its turn."""
    from types import SimpleNamespace
    from curl_cffi import CurlOpt, requests as curl_requests
    from core import jm_service
    session = curl_requests.Session()
    image_client = SimpleNamespace(get_root_postman=lambda: SimpleNamespace(session=session))
    monkeypatch.setattr(jm_service, "new_image_client", lambda: image_client)
    try:
        with env.reader._pooled_client() as pooled:
            assert pooled is image_client
        assert session.curl_options[CurlOpt.LOW_SPEED_LIMIT] == 1024
        assert session.curl_options[CurlOpt.LOW_SPEED_TIME] == 8
    finally:
        env.reader.clear_cache()
        session.close()


# ── client pool ────────────────────────────────────────

def test_one_pooled_client_serves_sequential_pages_and_is_rebuilt_after_settings_change(env):
    from core import jm_service
    env.photos["100006"] = Photo("100006", 6)
    for index in range(4):
        _read(env, "100006", index)
    assert len(env.created) == 1 and {client for client, _, _ in env.cdn.calls} == {env.created[0]}
    assert env.closed == []
    jm_service.invalidate_option_cache()  # settings saved
    _read(env, "100006", 4)
    assert len(env.created) == 2 and env.closed == [env.created[0]]
    assert env.cdn.calls[-1][0] is env.created[1]
    env.reader.clear_cache()
    assert env.closed == env.created


def test_concurrent_fetches_never_share_a_client(env):
    env.photos["100007"] = Photo("100007", 4)
    barrier = threading.Barrier(3, timeout=5)

    def together(url, timeout):
        barrier.wait()  # all three requests are in flight at the same time
        return page_bytes(url)
    env.cdn.answer = together
    threads = [threading.Thread(target=env.reader.image_file, args=("100007", index)) for index in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert len({client for client, _, _ in env.cdn.calls}) == 3
    env.cdn.answer = lambda url, timeout: page_bytes(url)
    _read(env, "100007", 3)
    assert len(env.created) == 3  # the idle clients are reused


# ── prefetch ───────────────────────────────────────────

def _cached(env, photo_id, index):
    return (env.reader.CACHE_ROOT / photo_id / f"{index + 1:05d}.webp").is_file()


def test_prefetch_fetches_the_next_pages_once_with_at_most_two_at_a_time(env, monkeypatch):
    monkeypatch.setattr(env.reader, "_PREFETCH_AHEAD", 4)
    env.photos["100008"] = Photo("100008", 8)
    running, peak = [0], [0]
    lock = threading.Lock()

    def slow(url, timeout):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.05)
        with lock:
            running[0] -= 1
        return page_bytes(url)
    env.cdn.answer = slow
    _read(env, "100008", 0)
    assert wait_until(lambda: all(_cached(env, "100008", i) for i in range(1, 5)))
    _read(env, "100008", 1)  # a cached page still moves the prefetch window
    assert wait_until(lambda: _cached(env, "100008", 5))
    assert wait_until(lambda: not env.reader._prefetch_active)
    urls = env.cdn.urls()
    assert sorted(urls) == [f"https://{OWN}/media/photos/100008/{n:05d}.webp?v=1" for n in range(1, 7)]
    assert peak[0] <= 2
    assert not _cached(env, "100008", 6)


def test_prefetch_stops_at_the_end_of_the_chapter(env, monkeypatch):
    monkeypatch.setattr(env.reader, "_PREFETCH_AHEAD", 4)
    env.photos["100009"] = Photo("100009", 3)
    _read(env, "100009", 1)
    assert wait_until(lambda: _cached(env, "100009", 2))
    assert wait_until(lambda: not env.reader._prefetch_active and not env.reader._prefetch_queue)
    assert len(env.cdn.urls()) == 2


def test_foreground_waits_for_a_prefetch_of_the_same_page_instead_of_fetching_it_again(env, monkeypatch):
    monkeypatch.setattr(env.reader, "_PREFETCH_AHEAD", 4)
    env.photos["100010"] = Photo("100010", 8)
    entered, release = threading.Event(), threading.Event()

    def answer(url, timeout):
        if url.endswith("/00003.webp?v=1"):
            entered.set()
            assert release.wait(5)
        return page_bytes(url)
    env.cdn.answer = answer
    try:
        _read(env, "100010", 0)
        assert entered.wait(5)  # the prefetch of page 3 is in flight
        result = {}
        reader = threading.Thread(target=lambda: result.update(data=_read(env, "100010", 2)))
        reader.start()
        time.sleep(0.1)
        assert reader.is_alive()  # waiting on the page lock, not fetching
    finally:
        release.set()
    reader.join(5)
    assert result["data"] == page_bytes(f"https://{OWN}/media/photos/100010/00003.webp")
    assert wait_until(lambda: not env.reader._prefetch_active)
    urls = env.cdn.urls()
    assert len(urls) == len(set(urls))
    assert urls.count(f"https://{OWN}/media/photos/100010/00003.webp?v=1") == 1


def test_prefetch_skips_a_page_the_foreground_is_already_fetching(env, monkeypatch):
    env.photos["100011"] = Photo("100011", 8)
    entered, release = threading.Event(), threading.Event()

    def answer(url, timeout):
        if url.endswith("/00004.webp?v=1"):
            entered.set()
            assert release.wait(5)
        return page_bytes(url)
    env.cdn.answer = answer
    reader = threading.Thread(target=env.reader.image_file, args=("100011", 3))
    try:
        reader.start()
        assert entered.wait(5)  # the foreground request for page 4 holds its lock
        monkeypatch.setattr(env.reader, "_PREFETCH_AHEAD", 4)
        env.reader._schedule_prefetch("100011", 2)  # e.g. page 3 was read from the cache meanwhile
        assert wait_until(lambda: all(_cached(env, "100011", i) for i in (4, 5, 6)))
        monkeypatch.setattr(env.reader, "_PREFETCH_AHEAD", 0)  # nothing more once page 4 arrives
    finally:
        release.set()
    reader.join(5)
    assert wait_until(lambda: not env.reader._prefetch_active)
    assert env.cdn.urls().count(f"https://{OWN}/media/photos/100011/00004.webp?v=1") == 1
    assert len(env.cdn.urls()) == 4


def test_clear_cache_stops_prefetch_and_closes_clients(env, monkeypatch):
    monkeypatch.setattr(env.reader, "_PREFETCH_AHEAD", 4)
    env.photos["100012"] = Photo("100012", 8)
    started, release = threading.Semaphore(0), threading.Event()

    def answer(url, timeout):
        if not url.endswith("/00001.webp?v=1"):
            started.release()
            assert release.wait(5)
        return page_bytes(url)
    env.cdn.answer = answer
    try:
        _read(env, "100012", 0)
        assert started.acquire(timeout=5) and started.acquire(timeout=5)  # two prefetches in flight
        with env.cdn.lock:
            in_flight = {id(client) for client, url, _ in env.cdn.calls if not url.endswith("/00001.webp?v=1")}
        env.reader.clear_cache()
        assert not env.reader._prefetch_queue
        assert len(in_flight) == 2 and not in_flight & set(map(id, env.closed))  # never closed mid-request
    finally:
        release.set()
    assert wait_until(lambda: not env.reader._prefetch_active)
    assert not env.reader.CACHE_ROOT.exists() or not any(env.reader.CACHE_ROOT.rglob("*.*"))
    assert len(env.cdn.urls()) == 3  # pages 4 and 5 were still queued: never fetched
    assert sorted(map(id, env.closed)) == sorted(map(id, env.created))  # in-flight clients closed on return
    assert env.reader._idle_clients == []


def test_failed_prefetch_leaves_no_files(env, monkeypatch):
    import os
    monkeypatch.setattr(env.reader, "_PREFETCH_AHEAD", 2)
    env.photos["100013"] = Photo("100013", 4)
    _read(env, "100013", 0)
    assert wait_until(lambda: all(_cached(env, "100013", i) for i in (1, 2)))
    real_replace = os.replace

    def refuse(src, dst):
        if ".tmp-" in str(src):
            raise PermissionError(13, "in use")
        return real_replace(src, dst)
    monkeypatch.setattr(os, "replace", refuse)
    _read(env, "100013", 2)  # prefetches page 4, whose write fails
    assert wait_until(lambda: len(env.cdn.urls()) == 4 and not env.reader._prefetch_active)
    names = sorted(p.name for p in (env.reader.CACHE_ROOT / "100013").iterdir())
    assert names == ["00001.webp", "00002.webp", "00003.webp"]


def test_prefetch_waits_while_every_image_host_is_failing(env, monkeypatch):
    env.photos["100014"] = Photo("100014", 6)
    env.cdn.answer = refuse
    with pytest.raises(TimeoutError):
        env.reader.image_file("100014", 0)
    folder = env.reader.CACHE_ROOT / "100014"
    folder.mkdir(parents=True)
    (folder / "00002.webp").write_bytes(page_bytes("https://x/media/photos/100014/00002.webp"))
    env.cdn.calls.clear()
    monkeypatch.setattr(env.reader, "_PREFETCH_AHEAD", 4)
    env.reader.image_file("100014", 1)  # cached page: schedules prefetch, but every host just failed
    assert wait_until(lambda: not env.reader._prefetch_queue and not env.reader._prefetch_active)
    assert env.cdn.urls() == []


# ── chapter page lists ─────────────────────────────────

def _cache_page(env, photo_id, index):
    """A page already on disk, e.g. read before the server restarted."""
    folder = env.reader.CACHE_ROOT / photo_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{index + 1:05d}.webp").write_bytes(page_bytes(f"https://x/media/photos/{photo_id}/{index + 1:05d}.webp"))


def test_requests_for_an_unknown_chapter_share_one_lookup_and_prefetch_never_looks_up(env, monkeypatch):
    """The server restarted (or the cache was cleared) while a reader stayed open, so the chapter's page list
    is not in memory. The reader shows a page still on disk (which schedules prefetch) while the browser asks
    for the next two pages: one get_photo_detail call serves them all, and prefetch never makes one itself."""
    monkeypatch.setattr(env.reader, "_PREFETCH_AHEAD", 4)
    env.upstream["200001"] = Photo("200001", 12)
    _cache_page(env, "200001", 4)
    release = threading.Event()

    def slow(photo_id):
        assert release.wait(5)
        return env.upstream[photo_id]
    env.api.answer = slow
    results, readers = {}, []
    try:
        env.reader.image_file("200001", 4)  # on disk: served at once, the following pages are scheduled
        readers = [threading.Thread(target=lambda i=i: results.update({i: _read(env, "200001", i)})) for i in (5, 6)]
        for reader in readers:
            reader.start()
        assert wait_until(lambda: env.api.calls)
        time.sleep(0.2)  # time for a second lookup of the same chapter to start
    finally:
        release.set()
    for reader in readers:
        reader.join(5)
    assert sorted(results) == [5, 6]
    assert wait_until(lambda: all(_cached(env, "200001", i) for i in range(7, 11)))  # prefetch once it is known
    assert env.api.calls == ["200001"]


def test_failed_chapter_lookup_is_shared_logged_once_and_not_repeated_at_once(env):
    env.upstream["200002"] = Photo("200002", 8)
    release = threading.Event()

    def outage(photo_id):
        assert release.wait(5)
        raise RuntimeError("Could not connect to mysql!")
    env.api.answer = outage
    outcomes, readers = {}, []

    def read(index):
        try:
            env.reader.image_file("200002", index)
            outcomes[index] = "ok"
        except TimeoutError:
            outcomes[index] = "timeout"
    try:
        readers = [threading.Thread(target=read, args=(index,)) for index in range(3)]
        for reader in readers:
            reader.start()
        assert wait_until(lambda: env.api.calls)
        time.sleep(0.2)
    finally:
        release.set()
    for reader in readers:
        reader.join(5)
    assert outcomes == {0: "timeout", 1: "timeout", 2: "timeout"}
    assert env.api.calls == ["200002"]
    errors = _log_text("error")
    assert "获取章节 photo_id=200002" in errors and "Could not connect to mysql!" in errors
    with pytest.raises(TimeoutError):
        env.reader.image_file("200002", 3)  # right after the failure: not asked again
    assert env.api.calls == ["200002"]
    env.clock.advance(3)  # the reader's own retry comes 3 s later
    env.api.answer = lambda photo_id: env.upstream[photo_id]
    _read(env, "200002", 3)
    assert env.api.calls == ["200002", "200002"]
    assert env.cdn.urls() == [f"https://{OWN}/media/photos/200002/00004.webp?v=1"]


def test_stuck_chapter_lookup_is_replaced_after_its_age_limit(env, monkeypatch):
    """An API request that connects but never answers must not hold every later request for the chapter
    (e.g. after a restart while a reader tab stays open): past the age limit the next request asks again,
    and the stuck request's late failure does not undo the fresh answer."""
    normal_budget = env.reader._page_budget
    monkeypatch.setattr(env.reader, "_page_budget", lambda: 0.3)  # keep the waits on the stuck lookup short
    env.upstream["200004"] = Photo("200004", 4)
    release, stuck = threading.Event(), [True]

    def answer(photo_id):
        if stuck[0]:
            stuck[0] = False
            assert release.wait(5)
            raise RuntimeError("stuck request finally gave up")
        return env.upstream[photo_id]
    env.api.answer = answer
    try:
        with pytest.raises(TimeoutError):
            env.reader.image_file("200004", 0)  # waits its budget on the stuck lookup
        with pytest.raises(TimeoutError):
            env.reader.image_file("200004", 0)  # still young: waits on the same lookup, no second call
        assert env.api.calls == ["200004"]
        env.clock.advance(env.reader._LOOKUP_MAX_AGE + 1)
        monkeypatch.setattr(env.reader, "_page_budget", normal_budget)
        _read(env, "200004", 0)  # too old now: asked again and served
        assert env.api.calls == ["200004", "200004"]
        assert "超过 20 秒仍未完成，重新获取" in _log_text("error")
    finally:
        release.set()
    assert wait_until(lambda: not env.reader._photo_lookups)
    _read(env, "200004", 1)  # the late failure of the stuck lookup did not drop the chapter
    assert env.api.calls == ["200004", "200004"]


def test_remembered_chapter_is_not_looked_up_again_after_thirty_minutes(env, monkeypatch):
    """A 234-page album read at about 8 s a page takes more than 30 minutes; the image URLs do not change."""
    from types import SimpleNamespace
    env.photos["200003"] = Photo("200003", 4)  # album_pages() remembered it
    later = SimpleNamespace(monotonic=lambda: time.monotonic() + 31 * 60, time=time.time, sleep=time.sleep)
    monkeypatch.setattr(env.reader, "time", later)
    _read(env, "200003", 0)
    assert env.api.calls == []


# ── one deadline per page request ──────────────────────

def _scaled_limits(env, monkeypatch, budget=1.5):
    """Real time, with the limits scaled down 10x: a 1.5 s page budget and a 0.4 s connect timeout."""
    monkeypatch.setattr(env.reader, "_clock", time.monotonic)
    monkeypatch.setattr(env.reader, "_page_budget", lambda: budget)
    monkeypatch.setattr(env.reader, "_CONNECT_TIMEOUT", 0.4)
    monkeypatch.setattr(env.reader, "_MIN_ATTEMPT", 0.1)
    monkeypatch.setattr(env.reader, "_QUICK_FAILURE", 0.2)
    monkeypatch.setattr(env.reader, "_CUT_SLACK", 0.025)
    return budget


def _unreachable(url, timeout):
    """Nothing answers: the request fails when its connect timeout runs out (real time)."""
    time.sleep(timeout[0])
    raise TimeoutError(f"curl: (28) Connection timed out after {int(timeout[0] * 1000)} milliseconds")


def test_reader_waiting_behind_a_failing_prefetch_of_its_page_gets_one_budget_in_total(env, monkeypatch):
    budget = _scaled_limits(env, monkeypatch)
    monkeypatch.setattr(env.reader, "_PREFETCH_AHEAD", 1)
    env.photos["200004"] = Photo("200004", 4)
    prefetching = threading.Event()

    def answer(url, timeout):
        if "/00002.webp" in url:
            prefetching.set()
            return _unreachable(url, timeout)
        return page_bytes(url)
    env.cdn.answer = answer
    _read(env, "200004", 0)  # page 1 -> page 2 is prefetched
    assert prefetching.wait(5)
    time.sleep(0.1)
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        env.reader.image_file("200004", 1)  # the reader scrolls to page 2 while its prefetch is failing
    assert time.monotonic() - start < budget + 0.5


def test_requests_queued_for_a_fetch_slot_still_fail_within_one_budget(env, monkeypatch):
    """Eight pages at once (resume or a jump into a chapter), four foreground slots, nothing answers."""
    budget = _scaled_limits(env, monkeypatch)
    env.photos["200005"] = Photo("200005", 8)
    env.cdn.answer = _unreachable
    waited = {}

    def read(index):
        start = time.monotonic()
        try:
            env.reader.image_file("200005", index)
        except TimeoutError:
            waited[index] = time.monotonic() - start
    readers = [threading.Thread(target=read, args=(index,)) for index in range(8)]
    for reader in readers:
        reader.start()
    for reader in readers:
        reader.join(10)
    assert sorted(waited) == list(range(8))
    assert max(waited.values()) < budget + 0.5


# ── reader.js (online mode) ────────────────────────────

def test_reader_retries_a_failed_online_page_once_by_itself():
    script = (Path(__file__).resolve().parent.parent / "static" / "js" / "reader.js").read_text(encoding="utf-8")
    assert "var AUTO_RETRY_MS = 3000;" in script
    handler = script[script.index("img.onerror = function () {"):]
    handler = handler[:handler.index("};") + 2]
    # online only, once, after the pause; otherwise the error block with its manual retry and log link
    assert "if (online && src && !autoRetried) {" in handler
    assert "autoRetried = true;" in handler and "setTimeout(reload, AUTO_RETRY_MS);" in handler
    assert handler.index("return;") < handler.index("error.classList.remove('d-none')")
    assert "retry.addEventListener('click'" in script and "reload();" in script
    assert "'/settings#logs'" in script and "查看日志" in script
    assert "url.searchParams.set('retry', Date.now());" in script  # a retry is never served from the cache
