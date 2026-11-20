"""Reader shutdown retains real owners; all transport work here is synthetic."""
import threading
import time
from types import SimpleNamespace

import pytest
from core.upstream import UpstreamFailure


def wait_until(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def launch(call):
    result = {}
    def run():
        try:
            result["value"] = call()
        except BaseException as exc:
            result["error"] = exc
    thread = threading.Thread(target=run)
    thread.start()
    return thread, result


class Photo:
    photo_id = "900001"
    page_arr = ["1.webp", "2.webp", "3.webp"]
    def __getitem__(self, index):
        return SimpleNamespace(download_url=f"https://cdn.invalid/{index}.webp", scramble_id=None)


@pytest.fixture
def reader(tmp_path, monkeypatch):
    from core import online_reader
    online_reader.start()
    assert online_reader.stop(2)
    online_reader.start()
    monkeypatch.setattr(online_reader, "CACHE_ROOT", tmp_path / "online")
    monkeypatch.setattr(online_reader, "_PREFETCH_AHEAD", 0)
    monkeypatch.setattr(online_reader, "_clock", time.monotonic)
    monkeypatch.setattr(online_reader, "_page_budget", lambda: 2)
    monkeypatch.setattr(online_reader.jm_service, "get_settings", lambda: {"timeout": "30"})
    online_reader.clear_cache()
    yield online_reader
    assert online_reader.stop(2)
    online_reader.start()
    online_reader.clear_cache()


def test_stop_is_sticky_preserves_cache_and_blocks_all_new_reader_work(reader, monkeypatch):
    target = reader._page_path(Photo.photo_id, 0)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"valid cached page")
    client_calls = []
    monkeypatch.setattr(reader.jm_service, "get_client", lambda: client_calls.append("client"))
    reader.request_stop()
    for call in (lambda: reader.image_file(Photo.photo_id, 0),
                 lambda: reader.album_pages("900001"),
                 lambda: reader._get_photo(Photo.photo_id, time.monotonic() + 1)):
        with pytest.raises(UpstreamFailure, match="停止") as caught:
            call()
        assert caught.value.kind == "stopping" and caught.value.http_status == 503
    reader._schedule_prefetch(Photo.photo_id, 0)
    assert not reader._prefetch_queue and not client_calls
    assert reader.stop(0) and reader.stop(0)
    assert target.read_bytes() == b"valid cached page"
    with pytest.raises(UpstreamFailure, match="停止"):
        reader.image_file(Photo.photo_id, 0)
    reader.start()
    assert reader.image_file(Photo.photo_id, 0) == target


def test_active_foreground_transport_is_not_closed_or_published_during_stop(reader, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    client, closed = object(), []
    def request(*args):
        entered.set()
        assert release.wait(2)
        return b"synthetic page"
    monkeypatch.setattr(reader.jm_service, "new_image_client", lambda: client)
    monkeypatch.setattr(reader.jm_service, "close_client", lambda value: closed.append(value))
    monkeypatch.setattr(reader, "_limit_stalls", lambda value: None)
    monkeypatch.setattr(reader, "_request", request)
    monkeypatch.setattr(reader, "_host_order", lambda own: [own])
    monkeypatch.setattr(reader, "_page_content", lambda *args: args[-1])
    reader._remember_photos([Photo()])
    thread, result = launch(lambda: reader.image_file(Photo.photo_id, 0))
    try:
        assert entered.wait(1)
        started = time.monotonic()
        assert not reader.stop(0.03)
        assert time.monotonic() - started < 0.4
        assert thread.is_alive() and not closed
        with pytest.raises(RuntimeError):
            reader.start()
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive() and result["error"].kind == "stopping"
    assert reader.stop(1) and closed == [client]
    assert not reader._page_path(Photo.photo_id, 0).exists()


def test_album_listing_remains_owned_until_its_real_foreground_call_exits(reader, monkeypatch):
    entered, release, checked, closed = threading.Event(), threading.Event(), [], []
    client = SimpleNamespace(get_album_detail=lambda *args: None)
    def call(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return object()
    monkeypatch.setattr(reader.jm_service, "get_client", lambda: (client, None))
    monkeypatch.setattr(reader.jm_service, "_call_with_timeout", call)
    monkeypatch.setattr(reader.jm_service, "check_album_photos", lambda *args, **kwargs: checked.append(True))
    monkeypatch.setattr(reader.jm_service, "close_client", lambda value: closed.append(value))
    thread, result = launch(lambda: reader.album_pages("900001"))
    try:
        assert entered.wait(1)
        assert not reader.stop(0.02) and not closed
    finally:
        release.set()
        thread.join(2)
    assert result["error"].kind == "stopping"
    assert reader.stop(1) and closed == [client] and not checked


def test_direct_pool_lease_remains_owned_until_borrower_releases_and_closes(reader, monkeypatch):
    entered, release, closed = threading.Event(), threading.Event(), []
    client = object()
    monkeypatch.setattr(reader.jm_service, "new_image_client", lambda: client)
    monkeypatch.setattr(reader.jm_service, "close_client", lambda value: closed.append(value))
    monkeypatch.setattr(reader, "_limit_stalls", lambda value: None)
    def borrow():
        with reader._pooled_client() as actual:
            assert actual is client
            entered.set()
            assert release.wait(2)
    thread, result = launch(borrow)
    try:
        assert entered.wait(1)
        assert not reader.stop(0.02) and not closed
    finally:
        release.set()
        thread.join(2)
    assert "error" not in result
    assert reader.stop(1) and closed == [client]


def test_idle_prefetch_threads_wake_and_active_prefetch_keeps_shutdown_owned(reader, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(reader, "_PREFETCH_AHEAD", 2)
    monkeypatch.setattr(reader, "_PREFETCH_IDLE", 60)
    monkeypatch.setattr(reader, "_PREFETCH_WORKERS", 1)
    def prefetch(key, epoch):
        entered.set()
        assert release.wait(2)
    monkeypatch.setattr(reader, "_prefetch", prefetch)
    reader._remember_photos([Photo()])
    reader._schedule_prefetch(Photo.photo_id, 0)
    try:
        assert entered.wait(1)
        owners = list(reader._prefetch_threads)
        assert len(owners) == 1 and isinstance(owners[0], threading.Thread)
        assert not reader.stop(0.02)
        assert not reader._prefetch_queue and owners[0].is_alive()
    finally:
        release.set()
    assert reader.stop(1)
    assert not owners[0].is_alive() and not reader._prefetch_threads
    reader.start()
    monkeypatch.setattr(reader, "_prefetch", lambda key, epoch: None)
    reader._schedule_prefetch(Photo.photo_id, 0)
    assert wait_until(lambda: not reader._prefetch_queue and not reader._prefetch_active)
    assert reader.stop(0.5)


@pytest.mark.parametrize("waiting_for", ["page", "slot"])
def test_page_and_slot_waiters_exit_cooperatively_on_stop(reader, monkeypatch, waiting_for):
    reader._remember_photos([Photo()])
    if waiting_for == "slot":
        resource = threading.BoundedSemaphore(1)
        resource.acquire()
        monkeypatch.setattr(reader, "_fetch_slots", resource)
        context = None
    else:
        context = reader._page_lock(reader._page_path(Photo.photo_id, 0))
        assert context.__enter__()
    thread, result = launch(lambda: reader.image_file(Photo.photo_id, 0))
    try:
        assert wait_until(lambda: reader._active_operations > 0)
        reader.request_stop()
        thread.join(0.5)
        assert not thread.is_alive() and result["error"].kind == "stopping"
        assert reader.stop(0.2)
    finally:
        if context:
            context.__exit__(None, None, None)
        else:
            resource.release()
        thread.join(2)


def test_stopped_lookup_waiter_returns_before_sdk_and_late_callback_cannot_publish(reader, monkeypatch):
    from concurrent.futures import Future
    future = Future()
    client = SimpleNamespace(get_photo_detail=lambda *args: None)
    monkeypatch.setattr(reader.jm_service, "get_client", lambda: (client, None))
    monkeypatch.setattr(reader.jm_service, "close_client", lambda value: None)
    monkeypatch.setattr(reader.jm_service, "_submit_client_call", lambda *args, **kwargs: future)
    thread, result = launch(lambda: reader._get_photo(Photo.photo_id, time.monotonic() + 5))
    try:
        assert wait_until(lambda: reader._lookup_callbacks == 1)
        reader.request_stop()
        thread.join(0.5)
        assert not thread.is_alive() and result["error"].kind == "stopping"
        assert not reader.stop(0.02)
        with pytest.raises(RuntimeError):
            reader.start()
    finally:
        future.set_result(Photo())
        thread.join(2)
    assert reader.stop(1)
    assert reader._cached_photo(Photo.photo_id) is None
    assert not reader._photo_lookups and reader._lookup_callbacks == 0


def test_stop_waits_for_lookup_callback_itself_even_after_future_is_done(reader, monkeypatch):
    from concurrent.futures import Future
    future, callback_entered, release = Future(), threading.Event(), threading.Event()
    client = SimpleNamespace(get_photo_detail=lambda *args: None)
    monkeypatch.setattr(reader.jm_service, "get_client", lambda: (client, None))
    monkeypatch.setattr(reader.jm_service, "close_client", lambda value: None)
    monkeypatch.setattr(reader.jm_service, "_submit_client_call", lambda *args, **kwargs: future)
    original = reader._finish_lookup
    def finish(*args):
        callback_entered.set()
        assert release.wait(2)
        original(*args)
    monkeypatch.setattr(reader, "_finish_lookup", finish)
    waiter, _ = launch(lambda: reader._get_photo(Photo.photo_id, time.monotonic() + 5))
    assert wait_until(lambda: reader._lookup_callbacks == 1)
    completer, _ = launch(lambda: future.set_result(Photo()))
    try:
        assert callback_entered.wait(1) and future.done()
        assert not reader.stop(0.02)
    finally:
        release.set()
        completer.join(2)
        waiter.join(2)
    assert reader.stop(1) and reader._cached_photo(Photo.photo_id) is None


def test_stop_between_temporary_write_and_replace_never_publishes(reader, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = reader.os.fdopen
    class Writer:
        def __init__(self, handle):
            self.handle = handle
        def __enter__(self):
            return self
        def write(self, content):
            self.handle.write(content)
            entered.set()
            assert release.wait(2)
        def __exit__(self, *args):
            self.handle.close()
    monkeypatch.setattr(reader.os, "fdopen", lambda *args: Writer(original(*args)))
    target = reader._page_path(Photo.photo_id, 0)
    thread, result = launch(lambda: reader._store(target, b"synthetic page"))
    try:
        assert entered.wait(1)
        assert not reader.stop(0.02)
        assert not target.exists()
    finally:
        release.set()
        thread.join(2)
    assert result.get("value") is False
    assert reader.stop(1) and not list(reader.CACHE_ROOT.rglob("*.webp"))


def test_idle_client_close_obeys_budget_and_failure_is_retained_for_retry(reader, monkeypatch):
    client, entered, release, calls = object(), threading.Event(), threading.Event(), []
    def close(value):
        assert value is client
        calls.append(value)
        if len(calls) == 1:
            entered.set()
            assert release.wait(2)
            return False
        return True
    monkeypatch.setattr(reader.jm_service, "close_client", close)
    reader._idle_clients.append((reader.jm_service.option_generation(), client))
    try:
        started = time.monotonic()
        assert not reader.stop(0.03)
        assert entered.is_set() and time.monotonic() - started < 0.4
        with pytest.raises(RuntimeError):
            reader.start()
    finally:
        release.set()
    assert wait_until(lambda: id(client) in reader._failed_clients)
    assert reader.stop(1) and calls == [client, client]
    reader.start()


def test_failed_close_returns_false_and_cannot_reopen_until_a_later_retry(reader, monkeypatch):
    client = object()
    monkeypatch.setattr(reader.jm_service, "close_client", lambda value: False)
    reader._idle_clients.append((reader.jm_service.option_generation(), client))
    assert not reader.stop(1)
    assert reader._failed_clients == {id(client): client}
    with pytest.raises(RuntimeError):
        reader.start()
    monkeypatch.setattr(reader.jm_service, "close_client", lambda value: True)
    assert reader.stop(1)
    reader.start()


@pytest.mark.parametrize("kind", ["prefetch", "closer"])
def test_thread_that_starts_then_raises_is_kept_until_actual_exit(reader, monkeypatch, kind):
    entered, release = threading.Event(), threading.Event()
    def block(*args):
        entered.set()
        assert release.wait(2)
    if kind == "prefetch":
        monkeypatch.setattr(reader, "_PREFETCH_AHEAD", 1)
        monkeypatch.setattr(reader, "_prefetch", block)
        reader._remember_photos([Photo()])
        action = lambda: reader._schedule_prefetch(Photo.photo_id, 0)
        owners = reader._prefetch_threads
    else:
        monkeypatch.setattr(reader.jm_service, "close_client", block)
        reader._idle_clients.append((reader.jm_service.option_generation(), object()))
        action = lambda: reader.stop(0.02)
        owners = reader._client_closers
    original = threading.Thread.start
    def partial_start(thread):
        original(thread)
        raise RuntimeError("raised after actual Thread.start")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(threading.Thread, "start", partial_start)
            with pytest.raises(RuntimeError, match="actual Thread.start"):
                action()
        assert entered.wait(1)
        assert len(owners) == 1 and next(iter(owners)).is_alive()
        assert not reader.stop(0.02)
        with pytest.raises(RuntimeError):
            reader.start()
    finally:
        release.set()
    assert reader.stop(1)


def test_clear_cache_keeps_running_admission_open(reader):
    target = reader._page_path(Photo.photo_id, 0)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"page")
    reader.clear_cache()
    assert not target.exists() and not reader._stop_event.is_set()
    assert not reader._store(target, b"retired page")
    assert reader._store(reader._page_path(Photo.photo_id, 0), b"new page")
