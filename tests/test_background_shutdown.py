"""SDK future lifetime tests: every callable and transport is local and fake."""
from concurrent.futures import Future, ThreadPoolExecutor
import threading
import time
from types import MethodType, SimpleNamespace
from unittest.mock import Mock

import pytest

from core.background_calls import BackgroundCalls, BackgroundCallsStopped


@pytest.fixture
def calls(monkeypatch):
    from core import jm_service
    service = BackgroundCalls()
    monkeypatch.setattr(jm_service, "background_calls", service)
    monkeypatch.setattr(jm_service, "_global_client", None)
    monkeypatch.setattr(jm_service, "_global_option", None)
    monkeypatch.setattr(jm_service, "_retired_clients", {})
    service.set_shutdown_cleanup(jm_service._shutdown_client_cache)
    yield service
    assert service.stop(timeout=2), service.runtime_status()


def shared_client():
    session = SimpleNamespace(close=Mock())
    return SimpleNamespace(_jm_downloader_shared=True, _jm_users=1, _jm_retired=False,
                           get_root_postman=lambda: SimpleNamespace(session=session)), session


def test_timed_out_request_keeps_call_and_lease_until_real_completion(calls):
    from core import jm_service
    entered, release = threading.Event(), threading.Event()
    client, session = shared_client()
    def slow(self):
        entered.set()
        assert release.wait(2)
        return "result"
    try:
        with pytest.raises(TimeoutError):
            jm_service._call_with_timeout("local", 0.1, "bounded wait", MethodType(slow, client))
        assert entered.is_set() and calls.runtime_status()["pending"] == 1
        jm_service.close_client(client)
        client._jm_retired = True
        assert client._jm_users == 1
        assert calls.stop(timeout=0.01) is False
        session.close.assert_not_called()
        with pytest.raises(RuntimeError):
            calls.start()
    finally:
        release.set()
    assert calls.stop(timeout=1)
    session.close.assert_called_once()
    assert client._jm_users == 0 and calls.runtime_status()["pending"] == 0


def test_retired_generations_are_both_tracked_without_early_transport_close(calls, monkeypatch):
    from core import jm_service
    first, first_session = shared_client()
    second, second_session = shared_client()
    gates = [threading.Event(), threading.Event()]
    entered = [threading.Event(), threading.Event()]
    def work(index):
        entered[index].set()
        assert gates[index].wait(2)
        return index
    monkeypatch.setattr(jm_service, "_global_client", first)
    monkeypatch.setattr(jm_service, "_global_option", object())
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            old = jm_service._submit_client_call(pool, first, work, 0)
            assert entered[0].wait(1)
            jm_service.invalidate_option_cache()
            jm_service.close_client(first)
            monkeypatch.setattr(jm_service, "_global_client", second)
            new = jm_service._submit_client_call(pool, second, work, 1)
            assert entered[1].wait(1)
            jm_service.invalidate_option_cache()
            jm_service.close_client(second)
            assert calls.stop(0.01) is False
            first_session.close.assert_not_called()
            second_session.close.assert_not_called()
        finally:
            for gate in gates:
                gate.set()
        from core.metadata_budget import MetadataStopped
        for future in (old, new):
            with pytest.raises(MetadataStopped):
                future.result(1)
    assert calls.stop(1)
    first_session.close.assert_called_once()
    second_session.close.assert_called_once()


def test_stop_cancels_queued_future_and_waits_running_call(calls):
    from core import jm_service
    gate, entered = threading.Event(), threading.Event()
    client, _ = shared_client()
    queued_call = Mock()
    with ThreadPoolExecutor(max_workers=1) as pool:
        def work():
            entered.set()
            assert gate.wait(2)
        try:
            running = jm_service._submit_client_call(pool, client, work)
            assert entered.wait(1)
            queued = jm_service._submit_client_call(pool, client, queued_call)
            assert calls.stop(0.03) is False
            assert queued.cancelled() and not running.done()
            queued_call.assert_not_called()
        finally:
            gate.set()
    assert calls.stop(1)
    assert client._jm_users == 1  # Only the foreground owner's original lease remains.


def test_submit_failure_releases_its_lease_and_rolls_back_registry(calls):
    from core import jm_service
    client, session = shared_client()
    pool = SimpleNamespace(submit=Mock(side_effect=RuntimeError("executor closed")))
    with pytest.raises(RuntimeError, match="executor closed"):
        jm_service._submit_client_call(pool, client, lambda: None)
    assert calls.stop(1)
    assert client._jm_users == 1
    session.close.assert_not_called()
    assert calls.runtime_status()["pending"] == 0


def test_rejected_submission_never_acquires_a_lease(calls):
    from core import jm_service
    client, _ = shared_client()
    calls.request_stop()
    pool = SimpleNamespace(submit=Mock())
    with pytest.raises(BackgroundCallsStopped):
        jm_service._submit_client_call(pool, client, lambda: None)
    pool.submit.assert_not_called()
    assert client._jm_users == 1


def test_stop_submit_race_tracks_registration_while_executor_is_still_submitting(calls):
    from core import jm_service
    submitting, complete = threading.Event(), threading.Event()
    future, client = Future(), shared_client()[0]
    def submit(*args, **kwargs):
        submitting.set()
        assert complete.wait(2)
        return future
    failures = []
    def request():
        try:
            jm_service._submit_client_call(SimpleNamespace(submit=submit), client, lambda: None)
        except BaseException as error:
            failures.append(error)
    thread = threading.Thread(target=request)
    thread.start()
    try:
        assert submitting.wait(1)
        assert calls.stop(0.01) is False
        assert calls.runtime_status()["pending"] >= 1
    finally:
        complete.set()
        thread.join(1)
    assert not failures and not thread.is_alive()
    assert calls.stop(1) and future.cancelled()
    assert client._jm_users == 1


def test_stop_does_not_hold_option_lock_while_waiting_for_lease_release(calls):
    from core import jm_service
    future, client = Future(), shared_client()[0]
    pool = SimpleNamespace(submit=lambda *args: future)
    jm_service._submit_client_call(pool, client, lambda: None)
    with jm_service._option_lock:
        future.set_result("done")
        before = time.monotonic()
        assert calls.stop(0.02) is False
        assert time.monotonic() - before < 0.3
        assert calls.runtime_status()["pending"] >= 1
    assert calls.stop(1)
    assert client._jm_users == 1


@pytest.mark.parametrize("failure", [False, ValueError("release failed")])
def test_release_failure_is_retained_and_retried_without_repeating_business_call(calls, failure):
    future = Future()
    result = [failure]
    released = threading.Event()
    def release():
        released.set()
        if isinstance(result[0], Exception):
            raise result[0]
        return result[0]
    business = Mock()
    calls.submit(SimpleNamespace(submit=lambda *args: future), business, release=release)
    future.set_result("done")
    assert released.wait(1)
    assert calls.stop(0.02) is False
    assert calls.runtime_status()["pending"] == 1 and calls.runtime_status()["errors"]
    result[0] = True
    assert calls.stop(1)
    business.assert_not_called()  # Fake executor owns its invocation; cleanup never invokes it.
    assert not calls.runtime_status()["errors"]


def test_cancel_callback_cannot_exceed_the_stopping_threads_budget(calls):
    future, entered, unblock = Future(), threading.Event(), threading.Event()
    released = Mock()
    calls.submit(SimpleNamespace(submit=lambda *args: future), lambda: None, release=released)
    def callback(_future):
        entered.set()
        assert unblock.wait(2)
    future.add_done_callback(callback)
    try:
        before = time.monotonic()
        assert calls.stop(0.02) is False
        assert time.monotonic() - before < 0.3 and entered.wait(1)
        assert calls.runtime_status()["pending"] >= 1
        released.assert_not_called()
        with pytest.raises(RuntimeError):
            calls.start()
    finally:
        unblock.set()
    assert calls.stop(1)
    released.assert_called_once_with()


def test_completed_future_is_retained_until_release_callback_actually_returns(calls):
    entered, unblock = threading.Event(), threading.Event()
    def release():
        entered.set()
        assert unblock.wait(2)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = calls.submit(pool, lambda: 42, release=release)
        try:
            assert future.result(1) == 42 and entered.wait(1)
            assert calls.stop(0.02) is False
            assert calls.runtime_status()["pending"] >= 1
        finally:
            unblock.set()
    assert calls.stop(1)
    assert not calls.runtime_status()["cleanup_running"]


def test_idle_stop_and_explicit_restart_are_idempotent(calls):
    assert calls.stop(1) and calls.stop(0)
    assert calls.runtime_status()["accepting"] is False
    calls.start()
    calls.start()
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert calls.submit(pool, lambda: 17).result(1) == 17
    assert calls.stop(1)


def test_sdk_failure_mapping_survives_lifecycle_tracking(calls):
    from core import jm_service
    from core.upstream import UpstreamFailure
    def fail():
        raise ValueError("bad local response")
    with pytest.raises(UpstreamFailure) as error:
        jm_service._call_with_timeout("local", 1, "bounded wait", fail)
    assert error.value.kind == "upstream_error"
    assert calls.stop(1)


def test_client_close_failure_keeps_handle_and_active_count_for_retry(monkeypatch):
    from core import jm_service
    failure = [True]
    def close():
        if failure[0]:
            raise OSError("close failed")
    client = SimpleNamespace(_jm_counted=True,
                             get_root_postman=lambda: SimpleNamespace(session=SimpleNamespace(close=close)))
    monkeypatch.setattr(jm_service, "_active_independent_clients", 1)
    assert jm_service.close_client(client) is False
    assert not getattr(client, jm_service._JM_CLOSED_ATTR, False)
    assert jm_service.get_active_client_count() == 1
    failure[0] = False
    assert jm_service.close_client(client) is True
    assert jm_service.close_client(client) is True
    assert jm_service.get_active_client_count() == 0


def test_concurrent_client_close_only_closes_once_and_decrements_count_once(monkeypatch):
    from core import jm_service
    close = Mock()
    client = SimpleNamespace(_jm_counted=True,
                             get_root_postman=lambda: SimpleNamespace(session=SimpleNamespace(close=close)))
    monkeypatch.setattr(jm_service, "_active_independent_clients", 1)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: jm_service.close_client(client), range(8)))
    assert all(results) and jm_service.get_active_client_count() == 0
    close.assert_called_once_with()


def test_retired_client_close_failure_is_preserved_by_registered_call_and_retry(calls, monkeypatch):
    from core import jm_service
    client, session = shared_client()
    client._jm_users = 0
    client._jm_retired = True
    session.close.side_effect = OSError("close failed")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = jm_service._submit_client_call(pool, client, lambda: "done")
        assert future.result(1) == "done"
    assert calls.stop(0.03) is False
    assert not getattr(client, jm_service._JM_CLOSED_ATTR, False)
    assert client._jm_users == 0 and calls.runtime_status()["pending"] >= 1
    session.close.side_effect = None
    assert calls.stop(1)
    assert getattr(client, jm_service._JM_CLOSED_ATTR)


def test_shutdown_hook_retires_cache_without_closing_http_owners_lease(calls, monkeypatch):
    from core import jm_service
    client, session = shared_client()
    monkeypatch.setattr(jm_service, "_global_client", client)
    assert calls.stop(1)
    assert jm_service._global_client is None and client._jm_retired
    assert jm_service._retired_clients[id(client)] is client
    session.close.assert_not_called()
    assert jm_service.close_client(client)
    assert jm_service.close_client(client)
    session.close.assert_called_once()
    assert not jm_service._retired_clients


def test_after_retirement_get_client_cannot_build_a_new_shared_transport(calls, monkeypatch):
    from core import jm_service
    assert calls.stop(1)
    build = Mock(side_effect=AssertionError("must reject before constructing options or SDK"))
    monkeypatch.setattr(jm_service, "_get_or_create_option", build)
    with pytest.raises(BackgroundCallsStopped):
        jm_service.get_client()
    build.assert_not_called()


def test_late_http_close_failure_restarts_owned_cleanup_and_final_stop_retries(calls, monkeypatch):
    from core import jm_service
    client, session = shared_client()
    monkeypatch.setattr(jm_service, "_global_client", client)
    assert calls.stop(1)
    session.close.side_effect = OSError("late HTTP close failure")
    assert jm_service.close_client(client) is False
    assert calls.stop(0.02) is False
    assert jm_service._retired_clients[id(client)] is client
    session.close.side_effect = None
    assert calls.stop(1)
    assert getattr(client, jm_service._JM_CLOSED_ATTR)
    assert not jm_service._retired_clients


def test_idle_cache_close_failure_is_not_forgotten_when_cache_is_invalidated(calls, monkeypatch):
    from core import jm_service
    client, session = shared_client()
    client._jm_users = 0
    monkeypatch.setattr(jm_service, "_global_client", client)
    session.close.side_effect = OSError("retired close failure")
    assert jm_service.invalidate_option_cache() is False
    assert jm_service._global_client is None
    assert jm_service._retired_clients[id(client)] is client
    assert calls.stop(0.02) is False
    with pytest.raises(RuntimeError):
        calls.start()
    session.close.side_effect = None
    assert calls.stop(1) and not jm_service._retired_clients


def test_blocking_cache_close_runs_only_on_owned_worker_with_stop_budget(calls, monkeypatch):
    from core import jm_service
    client, session = shared_client()
    entered, unblock = threading.Event(), threading.Event()
    client._jm_users = 0
    monkeypatch.setattr(jm_service, "_global_client", client)
    def close():
        entered.set()
        assert unblock.wait(2)
    session.close.side_effect = close
    try:
        before = time.monotonic()
        calls.request_stop()
        assert calls.stop(0.02) is False
        assert time.monotonic() - before < 0.3 and entered.wait(1)
        assert not getattr(client, jm_service._JM_CLOSED_ATTR, False)
    finally:
        unblock.set()
    assert calls.stop(1)
    session.close.assert_called_once()


def test_stop_during_acquire_never_submits_and_releases_acquired_lease(calls):
    entered, unblock = threading.Event(), threading.Event()
    released, submitted = Mock(), Mock()
    failures = []
    def acquire():
        entered.set()
        assert unblock.wait(2)
    def request():
        try:
            calls.submit(SimpleNamespace(submit=submitted), lambda: None,
                         acquire=acquire, release=released)
        except BaseException as error:
            failures.append(error)
    thread = threading.Thread(target=request)
    thread.start()
    try:
        assert entered.wait(1)
        assert calls.stop(0.02) is False
    finally:
        unblock.set()
        thread.join(1)
    assert len(failures) == 1 and isinstance(failures[0], BackgroundCallsStopped)
    assert calls.stop(1)
    submitted.assert_not_called()
    released.assert_called_once()


def test_failed_acquire_does_not_release_someone_elses_lease(calls):
    release = Mock()
    acquire = Mock(side_effect=ValueError("acquire failed"))
    with pytest.raises(ValueError, match="acquire failed"):
        calls.submit(SimpleNamespace(submit=Mock()), lambda: None, acquire=acquire, release=release)
    assert calls.stop(1)
    release.assert_not_called()


def test_late_cleanup_during_previous_worker_exit_is_handed_to_replacement(monkeypatch):
    service = BackgroundCalls()
    hook = Mock(return_value=True)
    service.set_shutdown_cleanup(hook)
    exiting, release_exit, stopping = threading.Event(), threading.Event(), threading.Event()
    original_clean, original_wait = service._clean, service._condition.wait
    runs = []
    def clean():
        runs.append(threading.get_ident())
        original_clean()
        if len(runs) == 1:
            exiting.set()
            assert release_exit.wait(2)
    monkeypatch.setattr(service, "_clean", clean)
    def wait(timeout=None):
        if threading.current_thread().name == "retry-stop":
            stopping.set()
        return original_wait(timeout)
    monkeypatch.setattr(service._condition, "wait", wait)
    result = []
    try:
        assert service.stop(0.02) is False and exiting.is_set()
        service.request_cleanup()
        assert service.runtime_status()["pending"] == 1
        thread = threading.Thread(target=lambda: result.append(service.stop(1)), name="retry-stop")
        thread.start()
        assert stopping.wait(1)
        release_exit.set()
        thread.join(1)
        assert not thread.is_alive() and result == [True]
        assert len(runs) == 2 and hook.call_count == 2
        assert not service.runtime_status()["cleanup_running"]
    finally:
        release_exit.set()
        assert service.stop(1)
