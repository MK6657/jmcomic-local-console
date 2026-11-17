"""Metadata lease, cache-wait and publication regressions; no network access."""
from collections import OrderedDict
import threading
from unittest.mock import Mock

import pytest

from core import metadata_budget
from core.background_calls import BackgroundCalls


@pytest.fixture
def lifetime(client, monkeypatch):
    """Use the offline app's temporary database and an independently drained owner."""
    from core import jm_service

    calls = BackgroundCalls()
    monkeypatch.setattr(jm_service, "background_calls", calls)
    monkeypatch.setattr(jm_service, "_global_client", None)
    monkeypatch.setattr(jm_service, "_global_option", None)
    monkeypatch.setattr(jm_service, "_retired_clients", {})
    monkeypatch.setattr(jm_service, "_detail_cache", OrderedDict())
    calls.set_shutdown_cleanup(jm_service._shutdown_client_cache)
    yield jm_service, calls
    assert calls.stop(2), calls.runtime_status()


@pytest.mark.parametrize("implementation", ["api", "html"])
def test_real_sdk_search_closure_keeps_lease_until_transport_finishes(
    lifetime, monkeypatch, implementation,
):
    from jmcomic import JmModuleConfig
    from core.settings import update_settings
    from core.upstream import UpstreamTimeout

    service, calls = lifetime
    monkeypatch.setattr(JmModuleConfig, "DOMAIN_API_LIST", ["api-lifetime.invalid"])
    monkeypatch.setattr(JmModuleConfig, "DOMAIN_HTML_LIST", ["html-lifetime.invalid"])
    update_settings({"client_type": implementation, "retry_times": 0})
    instance, _ = service.get_client()
    # Leave the real shared client idle; search_albums acquires its own lease.
    service.close_client(instance)
    search = instance.search
    assert getattr(search, "__self__", None) is None
    assert search.__closure__ is not None

    session = instance.get_root_postman().session
    entered, finish, transport_finished = (threading.Event() for _ in range(3))
    original_close = session.close

    def blocked_get(*args, **kwargs):
        entered.set()
        try:
            assert finish.wait(3)
            raise ConnectionError("local synthetic transport failure")
        finally:
            transport_finished.set()

    def close_after_transport():
        assert transport_finished.is_set(), "session closed during an active search"
        original_close()

    close = Mock(side_effect=close_after_transport)
    # Keep the SDK search/cache wrapper intact: only replace the transport.
    monkeypatch.setattr(session, "get", blocked_get)
    monkeypatch.setattr(session, "close", close)
    monkeypatch.setattr(service, "_operation_timeout", lambda cap: 0.2)
    try:
        with pytest.raises(UpstreamTimeout):
            service.search_albums("fixture")
        assert entered.is_set() and not transport_finished.is_set()
        assert instance.search is search
        assert calls.runtime_status()["pending"] == 1
        assert instance._jm_users == 1  # The request released its foreground lease.

        service.invalidate_option_cache()
        assert instance._jm_retired and instance._jm_users == 1
        assert calls.stop(0.02) is False
        close.assert_not_called()
    finally:
        finish.set()
        assert calls.stop(2), calls.runtime_status()

    close.assert_called_once_with()
    assert instance._jm_users == 0
    assert calls.runtime_status()["pending"] == 0
    assert not service._retired_clients


class _ObservedLock:
    """Expose contention without sleeps, also supporting the old with-lock path."""

    def __init__(self):
        self._lock = threading.Lock()
        self.waiting = threading.Event()

    def acquire(self, *args, **kwargs):
        if self._lock.locked():
            self.waiting.set()
        return self._lock.acquire(*args, **kwargs)

    def release(self):
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()


def _complete_detail():
    return {
        "album_id": "lifetime",
        "details_complete": True,
        "photos": [{"photo_id": "123", "page_count": 1, "page_count_known": True}],
    }


@pytest.mark.parametrize("phase", ["read", "invalidate", "publish"])
@pytest.mark.parametrize("interruption", ["timeout", "stop"])
def test_detail_cache_lock_waits_obey_budget_and_stop_before_publication(
    lifetime, monkeypatch, phase, interruption,
):
    service, calls = lifetime
    clock = [0.0]
    monkeypatch.setattr(metadata_budget, "_clock", lambda: clock[0])
    monkeypatch.setattr(service, "_operation_timeout", lambda cap: 1.0)
    lock = _ObservedLock()
    monkeypatch.setattr(service, "_detail_cache_lock", lock)
    stage_entered, finish_stage = threading.Event(), threading.Event()

    def pause_before_lock(value):
        stage_entered.set()
        assert finish_stage.wait(3)
        return value

    read = Mock(return_value=None)
    fetch = Mock(return_value=_complete_detail())
    if phase == "invalidate":
        read.side_effect = lambda *args, **kwargs: pause_before_lock(
            {"detail": {"details_complete": False}}
        )
    elif phase == "publish":
        fetch.side_effect = lambda *args: pause_before_lock(_complete_detail())
    delete, write = Mock(), Mock()
    monkeypatch.setattr(service.db, "get_cached_album_detail", read)
    monkeypatch.setattr(service.db, "delete_cached_album_detail", delete)
    monkeypatch.setattr(service.db, "set_cached_album_detail", write)
    monkeypatch.setattr(service, "get_album_detail", fetch)

    outcome = {}

    def request():
        try:
            outcome["result"] = service.get_album_detail_cached("lifetime")
        except BaseException as error:
            outcome["error"] = error

    held = False
    worker = threading.Thread(target=request, name="metadata-cache-wait-test")
    try:
        if phase == "read":
            lock.acquire()
            held = True
        worker.start()
        if phase != "read":
            assert stage_entered.wait(1)
            lock.acquire()
            held = True
            finish_stage.set()
        assert lock.waiting.wait(1)

        if interruption == "stop":
            calls.request_stop()
            expected = metadata_budget.MetadataStopped
        else:
            clock[0] = 2.0  # Expire the original deadline while the lock stays held.
            expected = TimeoutError
        worker.join(1)
        assert not worker.is_alive(), "cache waiter ignored its deadline or stop"
        assert isinstance(outcome.get("error"), expected), outcome
        assert "result" not in outcome
        delete.assert_not_called()
        write.assert_not_called()
        assert not service._detail_cache
        assert fetch.call_count == int(phase == "publish")
        if phase == "read":
            read.assert_not_called()
    finally:
        finish_stage.set()
        if held:
            lock.release()
        if worker.ident is not None:
            worker.join(2)
            assert not worker.is_alive()


@pytest.mark.parametrize("interruption", ["timeout", "stop"])
def test_late_detail_is_not_published_even_when_cache_lock_is_available(
    lifetime, monkeypatch, interruption,
):
    service, calls = lifetime
    clock = [0.0]
    monkeypatch.setattr(metadata_budget, "_clock", lambda: clock[0])
    monkeypatch.setattr(service, "_operation_timeout", lambda cap: 1.0)
    monkeypatch.setattr(service.db, "get_cached_album_detail", lambda *args, **kwargs: None)
    write = Mock()
    monkeypatch.setattr(service.db, "set_cached_album_detail", write)

    def fetch(_):
        if interruption == "stop":
            calls.request_stop()
        else:
            clock[0] = 2.0
        return _complete_detail()

    monkeypatch.setattr(service, "get_album_detail", fetch)
    expected = metadata_budget.MetadataStopped if interruption == "stop" else TimeoutError
    with pytest.raises(expected):
        service.get_album_detail_cached("lifetime")
    write.assert_not_called()
    assert not service._detail_cache


def test_stop_after_publication_permit_allows_existing_transaction_to_finish(
    lifetime, monkeypatch,
):
    service, calls = lifetime
    data = _complete_detail()
    monkeypatch.setattr(service, "get_album_detail", lambda _: data)
    original_write = service.db.set_cached_album_detail

    def stop_during_admitted_write(*args, **kwargs):
        # The lock and publication permit are already held when DB writing begins.
        calls.request_stop()
        return original_write(*args, **kwargs)

    write = Mock(side_effect=stop_during_admitted_write)
    monkeypatch.setattr(service.db, "set_cached_album_detail", write)
    with pytest.raises(metadata_budget.MetadataStopped):
        service.get_album_detail_cached("lifetime")
    write.assert_called_once()
    assert service.db.get_cached_album_detail("lifetime")["detail"] == data
    key = (str(service.db.DB_PATH), "lifetime")
    assert service._detail_cache[key]["data"] == data
