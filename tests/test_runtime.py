"""Offline lifecycle tests: no real listener, upstream requests or user task changes."""
import signal
import threading
import time
from types import SimpleNamespace

import pytest


def services(events, *, fail_start=None, fail_stop=None, pending=None):
    def component(name):
        def start():
            events.append("start:" + name)
            if fail_start == name:
                raise RuntimeError("synthetic start failure")
        def stop(timeout=5):
            events.append("stop:" + name)
            if fail_stop == name:
                raise RuntimeError("synthetic stop failure")
            return pending != name
        return SimpleNamespace(start=start, stop=stop)
    jobs, schedule, updates = (component(name) for name in ("jobs", "scheduler", "updates"))
    jobs.request_stop = lambda: events.append("request_stop")
    updates.request_stop = lambda: events.append("request_stop:updates")
    jobs.recover_interrupted = lambda: events.append("recover") or 2
    return jobs, schedule, updates


def test_runtime_starts_once_and_stops_in_reverse_order():
    from core.runtime import ApplicationRuntime
    events = []
    runtime = ApplicationRuntime(*services(events))
    runtime.start()
    runtime.start()
    assert events == ["recover", "start:jobs", "start:scheduler", "start:updates"]
    assert runtime.stop() is True
    assert events[-5:] == ["request_stop:updates", "request_stop", "stop:updates", "stop:scheduler", "stop:jobs"]
    snapshot = list(events)
    assert runtime.stop() is True
    assert events == snapshot
    with pytest.raises(RuntimeError):
        runtime.start()


@pytest.mark.parametrize("failure,cleanup", [
    ("jobs", ["stop:updates", "stop:jobs"]),
    ("scheduler", ["stop:updates", "stop:scheduler", "stop:jobs"]),
    ("updates", ["stop:updates", "stop:scheduler", "stop:jobs"]),
])
def test_partial_start_failure_rolls_back_including_the_failing_service(failure, cleanup):
    from core.runtime import ApplicationRuntime
    events = []
    runtime = ApplicationRuntime(*services(events, fail_start=failure))
    with pytest.raises(RuntimeError, match="synthetic start failure"):
        runtime.start()
    assert [event for event in events if event.startswith("stop:")] == cleanup
    gates = [events.index(name) for name in ("request_stop:updates", "request_stop")]
    assert max(gates) < min(events.index(name) for name in cleanup)
    assert events.count("stop:updates") == 1  # manual ownership predates updates.start()
    snapshot = list(events)
    assert runtime.stop() is True
    assert events == snapshot


def test_recovery_failure_starts_no_services():
    from core.runtime import ApplicationRuntime
    events = []
    jobs, scheduler, updates = services(events)
    def fail():
        raise RuntimeError("synthetic recovery failure")
    jobs.recover_interrupted = fail
    runtime = ApplicationRuntime(jobs, scheduler, updates)
    with pytest.raises(RuntimeError, match="recovery failure"):
        runtime.start()
    assert events == ["request_stop:updates", "request_stop", "stop:updates"]
    assert runtime._services == [] and runtime._state == "stopped"
    snapshot = list(events)
    assert runtime.stop() is True
    assert events == snapshot


@pytest.mark.parametrize("kind", ["failure", "pending"])
def test_stop_failure_is_reported_but_other_services_are_still_stopped(kind):
    from core.runtime import ApplicationRuntime
    events = []
    kwargs = {"fail_stop": "updates"} if kind == "failure" else {"pending": "updates"}
    runtime = ApplicationRuntime(*services(events, **kwargs))
    runtime.start()
    assert runtime.stop() is False
    assert events[-5:] == ["request_stop:updates", "request_stop", "stop:updates", "stop:scheduler", "stop:jobs"]
    assert len(runtime.stop_errors) == 1 and runtime.stop_errors[0].startswith("updates:")
    with pytest.raises(RuntimeError):
        runtime.start()
    # A later stop retries only the component whose ownership was retained.
    events.clear()
    assert runtime.stop(timeout=0) is False
    assert events == ["request_stop:updates", "request_stop", "stop:updates"]


def test_stop_uses_one_total_budget_not_one_budget_per_service(monkeypatch):
    import core.runtime as runtime_module
    events = []
    jobs, scheduler, updates = services(events)
    clock = [10.0]
    monkeypatch.setattr(runtime_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    budgets = []
    for name, component in (("jobs", jobs), ("scheduler", scheduler), ("updates", updates)):
        def stop(timeout, _name=name):
            budgets.append((_name, timeout))
            clock[0] += min(3, timeout)
            return timeout >= 3
        component.stop = stop
    runtime = runtime_module.ApplicationRuntime(jobs, scheduler, updates)
    runtime.start()
    assert runtime.stop(timeout=5) is False
    assert budgets == [("updates", 5), ("scheduler", 2), ("jobs", 0)]


@pytest.mark.parametrize("pending", ["jobs", "scheduler"])
def test_retry_drain_does_not_reacquire_an_already_drained_update_owner(pending):
    from core.runtime import ApplicationRuntime
    events = []
    runtime = ApplicationRuntime(*services(events, pending=pending))
    runtime.start()
    assert runtime.stop(timeout=0) is False
    assert [name for name, _ in runtime._services] == [pending]
    assert events.count("stop:updates") == 1
    events.clear()
    assert runtime.stop(timeout=0) is False
    assert events == ["request_stop:updates", "request_stop", "stop:" + pending]


def test_stop_during_recovery_prevents_later_service_start():
    from core.runtime import ApplicationRuntime
    events = []
    jobs, scheduler, updates = services(events)
    entered, finish = threading.Event(), threading.Event()
    errors = []
    def recovery():
        entered.set()
        assert finish.wait(2)
        return 1
    jobs.recover_interrupted = recovery
    runtime = ApplicationRuntime(jobs, scheduler, updates)
    def start():
        try:
            runtime.start()
        except RuntimeError as exc:
            errors.append(str(exc))
    thread = threading.Thread(target=start)
    thread.start()
    assert entered.wait(1)
    try:
        begin = time.monotonic()
        assert runtime.stop(timeout=0.02) is False
        assert time.monotonic() - begin < 0.5
        assert events == ["request_stop:updates", "request_stop"]
    finally:
        finish.set()
        thread.join(2)
    assert not thread.is_alive()
    assert errors and "停机请求" in errors[0]
    assert events == ["request_stop:updates", "request_stop", "request_stop:updates", "request_stop", "stop:updates"]
    assert not any(event.startswith("start:") for event in events)
    snapshot = list(events)
    assert runtime.stop() is True
    assert events == snapshot


def test_stop_reentrantly_requested_by_start_does_not_start_remaining_components():
    from core.runtime import ApplicationRuntime
    events = []
    jobs, scheduler, updates = services(events)
    runtime = ApplicationRuntime(jobs, scheduler, updates)
    jobs.start = lambda: runtime.stop()
    with pytest.raises(RuntimeError, match="停机请求"):
        runtime.start()
    assert "start:scheduler" not in events and "start:updates" not in events
    assert events.count("stop:jobs") == 1
    assert events.count("stop:updates") == 1
    assert events.index("request_stop:updates") < events.index("stop:updates") < events.index("stop:jobs")


def test_running_jobs_receive_stop_intent_before_contended_lifecycle_lock():
    from core.runtime import ApplicationRuntime
    events = []
    runtime = ApplicationRuntime(*services(events))
    runtime.start()
    entered, release = threading.Event(), threading.Event()
    def hold():
        with runtime._lock:
            entered.set()
            release.wait(2)
    holder = threading.Thread(target=hold)
    holder.start()
    assert entered.wait(1)
    try:
        assert runtime.stop(timeout=0.01) is False
        assert events[-2:] == ["request_stop:updates", "request_stop"]
        assert not any(event.startswith("stop:") for event in events)
    finally:
        release.set()
        holder.join(2)
    assert runtime.stop() is True


@pytest.mark.parametrize("module_name,thread_attr,lock_attr", [
    ("scheduler", "_scheduler_thread", "_scheduler_lock"),
    ("update_checker", "_thread", "_thread_lock"),
])
def test_component_stop_retains_live_thread_and_start_does_not_replace_it(
        monkeypatch, module_name, thread_attr, lock_attr):
    import importlib
    component = importlib.import_module("core." + module_name)
    release = threading.Event()
    thread = threading.Thread(target=lambda: release.wait(2), daemon=True)
    thread.start()
    event = threading.Event()
    monkeypatch.setattr(component, thread_attr, thread)
    monkeypatch.setattr(component, lock_attr, threading.Lock())
    monkeypatch.setattr(component, "_stop_event", event)
    if module_name == "update_checker":
        monkeypatch.setattr(component, "_armed", True)
    try:
        assert component.stop(timeout=0.01) is False
        assert getattr(component, thread_attr) is thread
        assert event.is_set()
        component.start()
        assert getattr(component, thread_attr) is thread
        assert event.is_set()
        if module_name == "update_checker":
            assert component._armed is False
    finally:
        release.set()
        thread.join(1)
    assert component.stop(timeout=0.1) is True
    assert getattr(component, thread_attr) is None


@pytest.mark.parametrize("module_name,lock_attr", [
    ("scheduler", "_scheduler_lock"), ("update_checker", "_thread_lock"),
])
def test_component_stop_lock_contention_is_bounded(monkeypatch, module_name, lock_attr):
    import importlib
    component = importlib.import_module("core." + module_name)
    lock, event = threading.Lock(), threading.Event()
    monkeypatch.setattr(component, lock_attr, lock)
    monkeypatch.setattr(component, "_stop_event", event)
    with lock:
        begin = time.monotonic()
        assert component.stop(timeout=0.01) is False
        assert time.monotonic() - begin < 0.5
    assert event.is_set()
    # A failed stop intentionally latches draining. Complete the lifecycle before
    # monkeypatch restores its event/lock, instead of leaking a stopped generation
    # into the following thread-creation test.
    assert component.stop(timeout=0.1) is True
    if module_name == "update_checker":
        assert component._draining is False and component._armed is False


@pytest.mark.parametrize("module_name,thread_attr,lock_attr", [
    ("scheduler", "_scheduler_thread", "_scheduler_lock"),
    ("update_checker", "_thread", "_thread_lock"),
])
def test_component_stop_can_roll_back_an_unstarted_thread(monkeypatch, module_name, thread_attr, lock_attr):
    import importlib
    component = importlib.import_module("core." + module_name)
    monkeypatch.setattr(component, thread_attr, None)
    monkeypatch.setattr(component, lock_attr, threading.Lock())
    monkeypatch.setattr(component, "_stop_event", threading.Event())
    if module_name == "scheduler":
        monkeypatch.setattr(component.db, "get_setting", lambda *args: "true")
    else:
        monkeypatch.setattr(component, "_armed", False)
        assert component._draining is False
    def fail():
        raise RuntimeError("synthetic thread creation failure")
    def broken_thread(*args, **kwargs):
        thread = threading.Thread(*args, **kwargs)
        thread.start = fail
        return thread
    monkeypatch.setattr(component, "threading", SimpleNamespace(
        Thread=broken_thread, Event=threading.Event, current_thread=threading.current_thread))
    with pytest.raises(RuntimeError, match="creation failure"):
        component.start()
    assert getattr(component, thread_attr) is not None
    assert component.stop(timeout=0.01) is True
    assert getattr(component, thread_attr) is None


@pytest.fixture
def entry(monkeypatch, tmp_path):
    import app
    import waitress
    events = []
    jobs, scheduler, updates = services(events)
    monkeypatch.setattr(app, "job_manager", jobs)
    monkeypatch.setattr(app, "scheduler", scheduler)
    monkeypatch.setattr(app, "update_checker", updates)
    monkeypatch.setattr(app, "create_app", lambda: events.append("create_app") or "application")
    monkeypatch.setattr(app, "install_process_hooks", lambda: None)
    monkeypatch.setattr(app, "create_shutdown_control", lambda token: None)
    monkeypatch.setattr(app, "background_services", ())
    monkeypatch.setattr(app, "start_log_maintenance", lambda: events.append("maintenance"))
    monkeypatch.setattr(app, "shutdown_logging", lambda: events.append("shutdown_logging"))
    monkeypatch.setattr(app, "_acquire_lock", lambda lease: events.append("lock"))
    marker = tmp_path / "flask.json"
    monkeypatch.setattr(app, "_PORT_FILE", marker)
    def release(*, keep_process_lock=False):
        assert keep_process_lock
        events.append("release")
        marker.unlink(missing_ok=True)
    monkeypatch.setattr(app, "_release_lock", release)
    monkeypatch.setattr(app.db, "get_setting", lambda *a: "downloads/")
    socket = SimpleNamespace(getsockname=lambda: ("127.0.0.1", 5001),
                             close=lambda: events.append("socket.close"))
    monkeypatch.setattr(app, "_bind_preemptive", lambda: events.append("bind") or socket)
    def serve(application, *, sockets, threads):
        assert application == "application" and sockets == [socket] and threads == 128
        assert marker.is_file()
        events.append("serve")
    monkeypatch.setattr(waitress, "serve", serve)
    class HTTP:
        def __init__(self, application, listener):
            self.application, self.listener = application, listener
        def run(self):
            waitress.serve(self.application, sockets=[self.listener], threads=128)
        def stop(self, timeout=3):
            return True
    monkeypatch.setattr(app, "ManagedHTTPServer", HTTP)
    handlers = {signal.SIGINT: "original-int", signal.SIGTERM: "original-term"}
    def set_handler(signum, handler):
        old = handlers[signum]
        handlers[signum] = handler
        return old
    monkeypatch.setattr(signal, "signal", set_handler)
    return SimpleNamespace(app=app, waitress=waitress, jobs=jobs, scheduler=scheduler, updates=updates,
                           events=events, marker=marker, handlers=handlers, socket=socket)


def test_entry_binds_before_recovery_and_cleans_all_owned_resources(entry):
    assert entry.app.main() == 0
    events = entry.events
    assert events.index("bind") < events.index("recover") < events.index("start:jobs") < events.index("serve")
    assert events[-8:] == ["request_stop:updates", "request_stop", "stop:updates", "stop:scheduler", "stop:jobs",
                           "socket.close", "release", "shutdown_logging"]
    assert not entry.marker.exists()
    assert entry.handlers == {signal.SIGINT: "original-int", signal.SIGTERM: "original-term"}


def test_binding_failure_does_not_recover_tasks_or_start_services(entry, monkeypatch):
    def fail_bind():
        entry.events.append("bind")
        raise OSError("synthetic port exhaustion")
    monkeypatch.setattr(entry.app, "_bind_preemptive", fail_bind)
    with pytest.raises(OSError, match="port exhaustion"):
        entry.app.main()
    assert entry.events == ["lock", "create_app", "bind", "request_stop:updates", "request_stop", "stop:updates", "release"]
    assert not any(event in entry.events for event in ("recover", "start:jobs", "start:scheduler", "start:updates"))
    assert not entry.marker.exists()


@pytest.mark.parametrize("component", ["jobs", "scheduler", "updates"])
def test_entry_partial_start_failure_releases_listener_and_record(entry, component):
    service = getattr(entry, component)
    def fail():
        entry.events.append("start:" + component)
        raise RuntimeError("synthetic partial startup")
    service.start = fail
    with pytest.raises(RuntimeError, match="partial startup"):
        entry.app.main()
    assert entry.events.count("socket.close") == entry.events.count("release") == 1
    assert not entry.marker.exists() and "serve" not in entry.events


def test_entry_serve_failure_preserves_original_exception_and_cleans_up(entry, monkeypatch):
    def fail(*a, **kw):
        raise ValueError("synthetic serving failure")
    monkeypatch.setattr(entry.waitress, "serve", fail)
    with pytest.raises(ValueError, match="serving failure"):
        entry.app.main()
    assert entry.events[-3:] == ["socket.close", "release", "shutdown_logging"]
    assert not entry.marker.exists()


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_entry_signal_uses_the_same_cleanup_path(entry, monkeypatch, signum):
    def signal_while_serving(*args, **kwargs):
        entry.handlers[signum](signum, None)
    monkeypatch.setattr(entry.waitress, "serve", signal_while_serving)
    assert entry.app.main() == 0
    assert entry.events.count("stop:jobs") == 1
    assert not entry.marker.exists()


def test_entry_repeated_signal_during_stop_does_not_interrupt_cleanup(entry, monkeypatch):
    def stop(timeout):
        entry.handlers[signal.SIGINT](signal.SIGINT, None)
        entry.events.append("stop:updates")
        return True
    entry.updates.stop = stop
    assert entry.app.main() == 0
    assert entry.events[-3:] == ["socket.close", "release", "shutdown_logging"]


def test_startup_signal_finishes_schema_work_without_logging_in_handler(entry, monkeypatch):
    from unittest.mock import Mock
    warning = Mock()
    monkeypatch.setattr(entry.app.log, "warning", warning)
    def create():
        entry.handlers[signal.SIGINT](signal.SIGINT, None)
        warning.assert_not_called()
        entry.events.append("schema ready")
        return "application"
    monkeypatch.setattr(entry.app, "create_app", create)
    assert entry.app.main() == 0
    assert "schema ready" in entry.events and "recover" not in entry.events
    assert "release" in entry.events and not entry.marker.exists()
    warning.assert_called_once()


def test_entry_closes_sse_before_runtime_drain(entry, monkeypatch):
    monkeypatch.setattr(entry.app.progress_manager, "close_all", lambda: entry.events.append("streams closed"))
    assert entry.app.main() == 0
    assert entry.events.index("streams closed") < entry.events.index("request_stop:updates")


def test_entry_incomplete_drain_returns_failure_and_keeps_logging_available(entry):
    entry.jobs.stop = lambda timeout: False
    assert entry.app.main() == 1
    assert entry.events[-1] == "socket.close"
    assert "release" not in entry.events
    assert "shutdown_logging" not in entry.events
    assert entry.marker.exists()  # identity remains protected until OS process exit


def test_entry_http_workers_still_running_keeps_instance_lock(entry, monkeypatch):
    class HTTP:
        def __init__(self, *args):
            pass
        def run(self):
            pass
        def stop(self, timeout=3):
            return False
    monkeypatch.setattr(entry.app, "ManagedHTTPServer", HTTP)
    assert entry.app.main() == 1
    assert "release" not in entry.events and entry.marker.exists()


def test_entry_stops_control_before_restoring_signal_handlers(entry, monkeypatch):
    from unittest.mock import Mock
    control = Mock(token="a" * 32)
    def stop():
        assert callable(entry.handlers[signal.SIGINT])
        entry.events.append("control.stop")
        return True
    control.stop.side_effect = stop
    monkeypatch.setattr(entry.app, "create_shutdown_control", lambda token: control)
    assert entry.app.main() == 0
    assert entry.events.index("control.stop") < entry.events.index("stop:jobs")
    assert entry.app._shutdown_control is None


def test_entry_confirms_last_http_lease_cleanup_before_declaring_success(entry, monkeypatch):
    pending, attempts = [False], []
    def stop(timeout):
        attempts.append(timeout)
        return not pending[0]
    requests = SimpleNamespace(start=lambda: None, request_stop=lambda: None, stop=stop)
    monkeypatch.setattr(entry.app, "background_services", (("requests", requests),))
    class HTTP:
        def __init__(self, *args):
            pass
        def run(self):
            pass
        def stop(self, timeout=3):
            # A finally block released its last retired lease but closing the
            # session failed: the second service drain must observe this.
            pending[0] = True
            return True
    monkeypatch.setattr(entry.app, "ManagedHTTPServer", HTTP)
    assert entry.app.main() == 1
    assert len(attempts) == 2
    assert "release" not in entry.events and entry.marker.exists()


def test_entry_listener_close_failure_still_releases_ownership(entry):
    def fail():
        raise OSError("synthetic close failure")
    entry.socket.close = fail
    assert entry.app.main() == 1
    assert entry.events[-1] == "release"
    assert not entry.marker.exists()


def test_create_app_never_recovers_running_or_paused_jobs(client, monkeypatch):
    import app
    from core import database as db
    for job_id, status in (("runtime-running", "running"), ("runtime-paused", "paused")):
        db.insert_job(job_id, "123", "fixture", [])
        db.transition_job_status(
            job_id, ["queued", "running", "paused", "completed", "failed", "canceled"], status,
        )
    monkeypatch.setattr(app.job_manager, "recover_interrupted", lambda: pytest.fail("factory recovered tasks"))
    monkeypatch.setattr(app.job_manager, "start", lambda: pytest.fail("factory started jobs"))
    monkeypatch.setattr(app.scheduler, "start", lambda: pytest.fail("factory started scheduler"))
    monkeypatch.setattr(app.update_checker, "start", lambda: pytest.fail("factory started checker"))
    app.create_app()
    assert db.get_job("runtime-running")["status"] == "running"
    assert db.get_job("runtime-paused")["status"] == "paused"
