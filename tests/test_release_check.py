"""Offline coverage of the explicit release-check boundary and failure cleanup.

The actual HTTP/curl check is an opt-in fresh-process command, never part of
ordinary pytest: tests/conftest.py must keep socket and libcurl blocked here.
"""
import os
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core import release_check as check


@pytest.mark.parametrize("loaded", ["app", "routes.api_settings", "core.path_utils"])
def test_self_check_rejects_application_imports_before_creating_files(monkeypatch, loaded):
    monkeypatch.setattr(check, "sys", SimpleNamespace(modules={check.__name__: check, loaded: object()}))
    create = Mock(side_effect=AssertionError("must reject before creating a directory"))
    monkeypatch.setattr(check.tempfile, "TemporaryDirectory", create)
    result = check.run_self_check()
    assert result["status"] == "error" and result["exit_code"] == 1
    assert "fresh process" in result["error"]
    create.assert_not_called()


def test_release_check_import_alone_is_not_an_application_import(monkeypatch):
    monkeypatch.setattr(check, "sys", SimpleNamespace(modules={"core": object(), check.__name__: check}))
    assert not check._application_already_loaded()


def test_environment_isolates_all_roots_and_restores_after_failure(tmp_path, monkeypatch):
    previous = dict(zip(check._PATH_KEYS[:2], ("personal-home", "personal-downloads")))
    for key in check._PATH_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in previous.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ValueError):
        with check._temporary_environment(tmp_path) as configured:
            assert set(configured) == set(check._PATH_KEYS)
            assert len(set(configured.values())) == 4
            assert all(Path(os.environ[key]).is_relative_to(tmp_path) for key in check._PATH_KEYS)
            raise ValueError("synthetic failure")
    assert {key: os.environ.get(key) for key in check._PATH_KEYS} == {
        key: previous.get(key) for key in check._PATH_KEYS}


@pytest.mark.parametrize("url", [
    "https://127.0.0.1:43123/image.png", "http://127.0.0.1:43124/image.png",
    "http://upstream.invalid:43123/image.png", "http://127.0.0.1.evil.invalid:43123/image.png",
    "http://user:password@127.0.0.1:43123/image.png", "file:///tmp/image.png",
    "/image.png", "http://127.0.0.1:not-a-port/image.png",
])
def test_sdk_guard_rejects_any_other_destination(url):
    with pytest.raises((RuntimeError, ValueError)):
        check._loopback_url(url, 43123)


@pytest.mark.parametrize("method", ["get", "post"])
def test_sdk_guard_preserves_arguments_but_disables_redirects(method):
    original = Mock(return_value="response")
    transport = SimpleNamespace(get=Mock(), post=Mock())
    setattr(transport, method, original)
    check._guard_transport(transport, 43123)
    guarded = getattr(transport, method)
    assert guarded("http://127.0.0.1:43123/image.png", "arg", timeout=2,
                   allow_redirects=True) == "response"
    original.assert_called_once_with("http://127.0.0.1:43123/image.png", "arg", timeout=2,
                                     allow_redirects=False)
    with pytest.raises(RuntimeError):
        guarded("http://upstream.invalid:43123/image.png")
    assert original.call_count == 1


@pytest.mark.parametrize("failure", [None, ValueError("must not expose private path or credentials")])
def test_report_requires_success_and_removes_temporary_data(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(check, "_application_already_loaded", lambda: False)
    monkeypatch.setenv("JMCONSOLE_HOME", "original-home")
    created = []
    def local_checks(root, checks):
        created.append(root)
        assert root.is_dir() and os.environ["JMCONSOLE_HOME"] == str(root)
        for key in check._PATH_KEYS:
            assert Path(os.environ[key]).is_relative_to(root)
        (root / "placeholder.txt").write_text("synthetic", encoding="utf-8")
        checks["synthetic"] = True
        if failure is not None:
            raise failure
    monkeypatch.setattr(check, "_run_checks", local_checks)
    result = check.run_self_check()
    assert created and not created[0].exists()
    assert os.environ["JMCONSOLE_HOME"] == "original-home"
    if failure is None:
        assert result == {"status": "ok", "exit_code": 0,
                          "checks": {"synthetic": True, "cleanup": True}, "upstream_requests": 0}
    else:
        assert result["status"] == "error" and result["exit_code"] == 1
        assert result["error"] == "local_checks: ValueError"
        assert "cleanup" not in result["checks"]


def test_temporary_directory_cleanup_failure_cannot_report_success(monkeypatch):
    monkeypatch.setattr(check, "_application_already_loaded", lambda: False)
    class CleanupFailure:
        def __enter__(self):
            return "synthetic-root"
        def __exit__(self, *args):
            raise PermissionError("temporary directory remained open")
    monkeypatch.setattr(check.tempfile, "TemporaryDirectory", lambda **kwargs: CleanupFailure())
    monkeypatch.setattr(check, "_run_checks", lambda root, checks: None)
    result = check.run_self_check()
    assert result["exit_code"] == 1 and result["error"] == "temporary_directory_cleanup: PermissionError"
    assert "cleanup" not in result["checks"]


def test_cleanup_attempts_every_release_even_when_session_and_workers_fail(monkeypatch):
    calls = []
    def action(name, fail=False, result=None):
        def run(*args, **kwargs):
            calls.append(name)
            if fail:
                raise OSError("synthetic close failure")
            return result
        return run
    session = SimpleNamespace(close=action("session", fail=True))
    client = SimpleNamespace(get_root_postman=lambda: SimpleNamespace(session=session))
    dispatcher = SimpleNamespace(shutdown=action("workers", fail=True), threads=set())
    server = SimpleNamespace(task_dispatcher=dispatcher)
    modules = {
        "core.jm_service": SimpleNamespace(invalidate_option_cache=action("shared_client", result=True)),
        "core.online_reader": SimpleNamespace(clear_cache=action("reader")),
    }
    monkeypatch.setattr(check, "sys", SimpleNamespace(modules=modules))
    monkeypatch.setattr(check, "_close_channels", action("channels"))
    monkeypatch.setattr(check, "_close_application_logging", action("logging"))
    with pytest.raises(check._CheckError, match="http_worker_shutdown: OSError"):
        check._cleanup_runtime(client, server, None, {}, threading.Event(), [])
    assert calls == ["channels", "workers", "session", "shared_client", "reader", "logging"]


@pytest.mark.parametrize("scenario,diagnostic", [
    ("http_loop", "http_loop: OSError"),
    ("serving", "http_thread_join: TimeoutError"),
    ("worker", "http_worker_shutdown: TimeoutError"),
])
def test_cleanup_checks_background_failures_and_remaining_threads(monkeypatch, scenario, diagnostic):
    monkeypatch.setattr(check, "sys", SimpleNamespace(modules={}))
    logger_cleanup = Mock()
    monkeypatch.setattr(check, "_close_application_logging", logger_cleanup)
    stop = threading.Event()
    errors = [check._CheckError("http_loop", OSError())] if scenario == "http_loop" else []
    thread = None if scenario == "worker" else SimpleNamespace(join=Mock(), is_alive=lambda: scenario == "serving")
    server = SimpleNamespace(task_dispatcher=SimpleNamespace(shutdown=Mock(), threads={1}))
    with pytest.raises(check._CheckError, match=diagnostic):
        check._cleanup_runtime(None, server, thread, {}, stop, errors)
    assert stop.is_set()
    if thread is not None:
        thread.join.assert_called_once_with(timeout=5)
        server.task_dispatcher.shutdown.assert_not_called()
    else:
        server.task_dispatcher.shutdown.assert_called_once_with(timeout=3)
    logger_cleanup.assert_called_once()


def test_all_channels_are_closed_after_one_close_failure():
    sockets = {}
    def close_good():
        sockets.pop("good")
    failed = Mock(side_effect=OSError("cannot close socket"))
    good = Mock(side_effect=close_good)
    sockets.update(bad=SimpleNamespace(close=failed), good=SimpleNamespace(close=good))
    with pytest.raises(check._CheckError):
        check._close_channels(sockets)
    failed.assert_called_once()
    good.assert_called_once()
    assert set(sockets) == {"bad"}


def test_self_check_stops_polling_before_its_http_thread_closes_channels():
    entered, stop = threading.Event(), threading.Event()
    owner = []
    sockets, failures = {}, []

    def poll(*, timeout, map, use_poll, count):
        assert (timeout, map, use_poll, count) == (0.1, sockets, False, 1)
        owner.append(("poll", threading.get_ident()))
        entered.set()
        assert stop.wait(3)
        assert map
        owner.append(("poll returned", threading.get_ident()))

    def close_channel():
        owner.append(("close channel", threading.get_ident()))
        sockets.clear()

    sockets[123] = SimpleNamespace(outbuf_lock=object(), handle_close=close_channel)
    dispatcher = SimpleNamespace(threads={1})

    def stop_workers(*, timeout):
        assert timeout == 3
        owner.append(("stop workers", threading.get_ident()))
        dispatcher.threads.clear()

    dispatcher.shutdown = stop_workers
    server = SimpleNamespace(asyncore=SimpleNamespace(loop=poll), task_dispatcher=dispatcher,
                             adj=SimpleNamespace(asyncore_loop_timeout=0.1, asyncore_use_poll=False))
    thread = threading.Thread(target=check._serve_http, args=(server, sockets, stop, failures))
    thread.start()
    try:
        assert entered.wait(3)
        check._stop_http(server, thread, sockets, stop, failures)
    finally:
        stop.set()
        thread.join(timeout=3)
    assert owner == [(action, thread.ident) for action in ("poll", "poll returned", "close channel", "stop workers")]
    assert not failures and not sockets and not dispatcher.threads


def test_report_names_failed_http_operation_and_exception_type(monkeypatch):
    monkeypatch.setattr(check, "_application_already_loaded", lambda: False)
    dispatcher = SimpleNamespace(shutdown=Mock(), threads=set())
    server = SimpleNamespace(asyncore=SimpleNamespace(loop=Mock(side_effect=OSError("private detail"))),
                             adj=SimpleNamespace(asyncore_loop_timeout=0.1, asyncore_use_poll=False),
                             task_dispatcher=dispatcher)

    def local_checks(root, checks):
        socket_map, stop, errors = {}, threading.Event(), []
        socket_map[10] = SimpleNamespace(close=socket_map.clear)
        thread = threading.Thread(target=check._serve_http, args=(server, socket_map, stop, errors))
        thread.start()
        thread.join(timeout=3)
        check._stop_http(server, thread, socket_map, stop, errors)

    monkeypatch.setattr(check, "_run_checks", local_checks)
    result = check.run_self_check()
    assert result["exit_code"] == 1 and result["error"] == "http_loop: OSError"
    dispatcher.shutdown.assert_called_once_with(timeout=3)


def test_logging_closes_every_handler_even_if_stop_or_flush_fails(monkeypatch):
    first = SimpleNamespace(flush=Mock(side_effect=OSError("flush failed")), close=Mock())
    second = SimpleNamespace(flush=Mock(), close=Mock())
    shutdown = Mock(side_effect=RuntimeError("stop failed"))
    logger = SimpleNamespace(shutdown_logging=shutdown, _shared_handlers=[first, second])
    monkeypatch.setattr(check, "sys", SimpleNamespace(modules={"core.logger": logger}))
    with pytest.raises(check._CheckError):
        check._close_application_logging()
    shutdown.assert_called_once()
    for handler in (first, second):
        handler.flush.assert_called_once()
        handler.close.assert_called_once()


def test_partial_application_import_failure_still_runs_runtime_cleanup(tmp_path, monkeypatch):
    import builtins
    original_import = builtins.__import__
    def fail_app(name, *args, **kwargs):
        if name == "app":
            raise ImportError("bundled dependency missing")
        return original_import(name, *args, **kwargs)
    cleanup = Mock()
    monkeypatch.setattr(check, "_cleanup_runtime", cleanup)
    monkeypatch.setattr(builtins, "__import__", fail_app)
    with pytest.raises(check._CheckError, match="application_import: ImportError"):
        check._run_checks(tmp_path, {})
    cleanup.assert_called_once()
    client, server, thread, socket_map, stop, errors = cleanup.call_args.args
    assert client is server is thread is None
    assert socket_map == {} and not stop.is_set() and errors == []
