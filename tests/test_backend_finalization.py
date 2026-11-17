"""Runtime admission/drain boundaries for reader and shared request ownership."""
from types import SimpleNamespace

import pytest


def _services(events):
    def component(name):
        return SimpleNamespace(
            start=lambda: events.append("start:" + name),
            request_stop=lambda: events.append("gate:" + name),
            stop=lambda timeout: events.append("stop:" + name) or True,
        )
    jobs, scheduler, updates, requests, reader = map(component, ("jobs", "scheduler", "updates", "requests", "reader"))
    jobs.recover_interrupted = lambda: 0
    return jobs, scheduler, updates, requests, reader


def test_shared_requests_start_before_consumers_and_all_gates_close_before_drain():
    from core.runtime import ApplicationRuntime
    events = []
    jobs, scheduler, updates, requests, reader = _services(events)
    runtime = ApplicationRuntime(jobs, scheduler, updates,
                                 additional_services=(("requests", requests), ("reader", reader)))
    runtime.start()
    assert events == ["start:requests", "start:reader", "start:jobs", "start:scheduler", "start:updates"]
    assert runtime.stop()
    assert events[5:] == ["gate:updates", "gate:reader", "gate:requests", "gate:jobs", "stop:updates", "stop:scheduler",
                          "stop:jobs", "stop:reader", "stop:requests"]


def test_pending_reader_retains_ownership_and_retries_with_no_resurrection():
    from core.runtime import ApplicationRuntime
    events = []
    jobs, scheduler, updates, requests, reader = _services(events)
    pending = [True]
    reader.stop = lambda timeout: not pending[0]
    runtime = ApplicationRuntime(jobs, scheduler, updates,
                                 additional_services=(("requests", requests), ("reader", reader)))
    runtime.start()
    assert not runtime.stop(timeout=0)
    assert runtime._services == [("reader", reader)]
    with pytest.raises(RuntimeError):
        runtime.start()
    pending[0] = False
    assert runtime.stop(timeout=0)


def test_reader_start_failure_rolls_back_it_and_shared_request_service():
    from core.runtime import ApplicationRuntime
    events = []
    jobs, scheduler, updates, requests, reader = _services(events)
    def fail():
        raise OSError("synthetic startup failure")
    reader.start = fail
    runtime = ApplicationRuntime(jobs, scheduler, updates,
                                 additional_services=(("requests", requests), ("reader", reader)))
    with pytest.raises(OSError):
        runtime.start()
    assert "start:jobs" not in events
    assert events[-2:] == ["stop:reader", "stop:requests"]


def test_background_services_share_the_existing_shutdown_budget(monkeypatch):
    from core import runtime as module
    events, now, budgets = [], [0.0], []
    jobs, scheduler, updates, requests, reader = _services(events)
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    for name, service in (("requests", requests), ("reader", reader), ("jobs", jobs),
                          ("scheduler", scheduler), ("updates", updates)):
        def stop(timeout, name=name):
            budgets.append((name, timeout))
            now[0] += min(1, timeout)
            return True
        service.stop = stop
    runtime = module.ApplicationRuntime(jobs, scheduler, updates,
                                       additional_services=(("requests", requests), ("reader", reader)))
    runtime.start()
    assert runtime.stop(timeout=3)
    assert budgets == [("updates", 3), ("scheduler", 2), ("jobs", 1), ("reader", 0), ("requests", 0)]


def test_http_admission_returns_503_before_database_or_mutating_route(client, monkeypatch):
    from routes import api_settings
    app = client.application
    monkeypatch.setitem(app.config, "SHUTTING_DOWN", True)
    mutate = lambda *a, **kw: pytest.fail("shutdown must reject before mutating the database")
    monkeypatch.setattr(api_settings, "update_settings", mutate)
    response = client.post("/api/settings", json={"timeout": "47"})
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert response.json["status"] == "error"
