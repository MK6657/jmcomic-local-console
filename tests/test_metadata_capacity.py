"""Real shared transport wrappers and bounded submission, without network."""
from concurrent.futures import Future, ThreadPoolExecutor
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core import metadata_budget
from core.background_calls import BackgroundCalls, BackgroundCallsBusy
from test_transfer_budget import Clock, Transport


@pytest.fixture
def transport(monkeypatch):
    clock, root = Clock(), Transport()
    monkeypatch.setattr(metadata_budget, "_clock", clock)
    client = SimpleNamespace(get_root_postman=lambda: root)
    metadata_budget.install(client)
    return clock, root, client


def test_shared_requests_receive_remaining_time_without_mutating_session(transport):
    from curl_cffi import CurlOpt
    clock, root, _ = transport
    options = root.session.curl_options
    with metadata_budget.call_scope(10):
        root.get("https://api-a.invalid/album/1", timeout=60)
        clock.advance(4)
        root.post("https://api-b.invalid/album/1", timeout=60)
    assert [call[2]["timeout"] for call in root.calls] == [10, 6]
    assert root.session.curl_options is options
    assert options[CurlOpt.CONNECTTIMEOUT_MS] == 4000
    assert options[CurlOpt.LOW_SPEED_TIME] == 8
    assert CurlOpt.TIMEOUT_MS not in options
    assert root.session.retry.count == 0


def test_budget_exhaustion_and_stop_prevent_late_requests(transport):
    clock, root, _ = transport
    event = threading.Event()
    with pytest.raises(TimeoutError):
        with metadata_budget.call_scope(5):
            root.get("https://api.invalid/1")
            clock.advance(5)
            root.get("https://api.invalid/2")
    assert len(root.calls) == 1
    with pytest.raises(metadata_budget.MetadataStopped):
        with metadata_budget.call_scope(20, event):
            event.set()
            root.get("https://api.invalid/3")
    assert len(root.calls) == 1
    with pytest.raises(RuntimeError, match="预算"):
        root.get("https://api.invalid/no-scope")


def test_parallel_calls_on_one_client_keep_independent_deadlines(transport):
    _, root, _ = transport
    barrier = threading.Barrier(2)
    root.answer = lambda **kwargs: barrier.wait(2)
    def call(deadline):
        with metadata_budget.call_scope(deadline):
            root.get("https://api.invalid/fixture")
    options = root.session.curl_options
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(call, [5, 19]))
    assert sorted(call[2]["timeout"] for call in root.calls) == [5, 19]
    assert root.session.curl_options is options


def test_capacity_rejects_before_submit_and_keeps_unclosed_lease():
    service = BackgroundCalls(max_pending=1)
    future = Future()
    release_entered, finish_release = threading.Event(), threading.Event()
    def release():
        release_entered.set()
        assert finish_release.wait(2)
    service.submit(SimpleNamespace(submit=lambda *args: future), lambda: None, release=release)
    rejected = Mock()
    try:
        future.set_result("done")
        assert release_entered.wait(1)
        with pytest.raises(BackgroundCallsBusy):
            service.submit(SimpleNamespace(submit=rejected), lambda: None)
        rejected.assert_not_called()
        assert service.runtime_status()["pending"] == 1
        assert service.stop(0.02) is False
    finally:
        finish_release.set()
    assert service.stop(1) and service.runtime_status()["pending"] == 0


def test_page_timeout_does_not_restore_capacity_while_worker_is_running(client, monkeypatch):
    from core import jm_service
    from core.upstream import UpstreamFailure, UpstreamTimeout
    service = BackgroundCalls(max_pending=1)
    monkeypatch.setattr(jm_service, "background_calls", service)
    entered, finish = threading.Event(), threading.Event()
    def blocked():
        entered.set()
        assert finish.wait(3)
    try:
        with pytest.raises(UpstreamTimeout):
            jm_service._call_with_timeout("fixture", 0.1, "wait expired", blocked)
        assert entered.is_set() and service.runtime_status()["pending"] == 1
        before = time.monotonic()
        with pytest.raises(UpstreamFailure) as caught:
            jm_service._call_with_timeout("fixture", 1, "wait expired", lambda: None)
        assert caught.value.kind == "busy" and caught.value.http_status == 503
        assert time.monotonic() - before < 0.2
    finally:
        finish.set()
        assert service.stop(1)


def test_chapter_batch_never_submits_the_entire_book(client, monkeypatch):
    from core import jm_service
    from test_audit_correctness import Album, Photo
    service = BackgroundCalls(max_pending=16)
    monkeypatch.setattr(jm_service, "background_calls", service)
    finish = threading.Event()
    active, peak, entered = [], [0], threading.Event()
    lock = threading.Lock()
    def check(photo):
        with lock:
            active.append(photo)
            peak[0] = max(peak[0], len(active))
            if len(active) == 4:
                entered.set()
        try:
            assert finish.wait(3)
            photo.page_arr = ["1.webp"]
        finally:
            with lock:
                active.remove(photo)
    failed = []
    try:
        photos = jm_service.check_album_photos(SimpleNamespace(check_photo=check),
            Album([Photo(str(index)) for index in range(100)]), timeout=0.1, failed=failed)
        assert entered.is_set() and peak[0] == 4
        assert service.runtime_status()["pending"] == 4
        assert len(failed) == len(photos) == 100
        assert all(photo.page_arr is None for photo in photos)
    finally:
        finish.set()
        assert service.stop(1)


def test_actual_sdk_constructor_uses_its_own_domain_family_and_guard(client, monkeypatch):
    from jmcomic import JmModuleConfig
    from core import jm_service
    from core.settings import update_settings
    monkeypatch.setattr(JmModuleConfig, "DOMAIN_API_LIST", ["api-only.invalid"])
    monkeypatch.setattr(JmModuleConfig, "DOMAIN_HTML_LIST", ["html-only.invalid"])
    for implementation in ("api", "html"):
        update_settings({"client_type": implementation})
        jm_service.invalidate_option_cache()
        instance, _ = jm_service.get_client()
        try:
            assert instance.get_domain_list() == [f"{implementation}-only.invalid"]
            with pytest.raises(RuntimeError, match="预算"):
                instance.get_root_postman().get("https://no-network.invalid")
        finally:
            jm_service.close_client(instance)
    jm_service.invalidate_option_cache()


@pytest.mark.parametrize("url", ["/api/search?q=fixture", "/api/album/123", "/api/online/123"])
def test_http_busy_errors_are_explicit_503(client, monkeypatch, url):
    from routes import api_search, api_album, api_online
    def busy(*args, **kwargs):
        raise BackgroundCallsBusy("元数据请求过多，请稍后重试")
    monkeypatch.setattr(api_search, "search_albums", busy)
    monkeypatch.setattr(api_album, "get_album_detail_cached", busy)
    monkeypatch.setattr(api_online.online_reader, "album_pages", busy)
    response = client.get(url)
    assert response.status_code == 503
    assert response.json["error_kind"] == "busy"


def test_many_fast_chapters_replenish_within_window_instead_of_losing_pages(client, monkeypatch):
    from core import jm_service
    from test_audit_correctness import Album, Photo
    service = BackgroundCalls(max_pending=4)
    monkeypatch.setattr(jm_service, "background_calls", service)
    def check(photo):
        photo.page_arr = ["1.webp"]
    failed = []
    try:
        photos = jm_service.check_album_photos(SimpleNamespace(check_photo=check),
            Album([Photo(str(index)) for index in range(100)]), timeout=3, failed=failed)
        assert len(photos) == 100 and not failed
        assert all(photo.page_arr == ["1.webp"] for photo in photos)
    finally:
        assert service.stop(1)


def test_album_and_chapter_requests_share_one_saved_total_budget(client, transport, monkeypatch):
    from core import jm_service
    from test_audit_correctness import Album, Photo
    clock, root, upstream = transport
    service = BackgroundCalls()
    monkeypatch.setattr(jm_service, "background_calls", service)
    monkeypatch.setattr(jm_service, "get_settings", lambda: {"timeout": "5"})
    monkeypatch.setattr(jm_service, "get_client", lambda: (upstream, None))
    monkeypatch.setattr(jm_service, "close_client", lambda value: True)
    def album(_):
        root.get("https://api.invalid/album")
        clock.advance(3)
        return Album([Photo()])
    def photo(working):
        root.get("https://api.invalid/photo")
        clock.advance(3)
        working.page_arr = ["late.webp"]
    upstream.get_album_detail, upstream.check_photo = album, photo
    try:
        result = jm_service.get_album_detail("123")
        assert [call[2]["timeout"] for call in root.calls] == [5, 2]
        assert result["details_complete"] is False
        assert result["photos"][0]["page_count"] is None
    finally:
        assert service.stop(1)


def test_cold_detail_lock_wait_obeys_budget_and_does_not_start_another_fetch(client, monkeypatch):
    from core import jm_service
    lock = jm_service._detail_fetch_locks[hash((str(jm_service.db.DB_PATH), "123")) % len(jm_service._detail_fetch_locks)]
    fetch = Mock()
    monkeypatch.setattr(jm_service, "get_album_detail", fetch)
    monkeypatch.setattr(jm_service, "_operation_timeout", lambda cap: 0.1)
    lock.acquire()
    before = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            jm_service.get_album_detail_cached("123")
        assert time.monotonic() - before < 0.4
        fetch.assert_not_called()
    finally:
        lock.release()
