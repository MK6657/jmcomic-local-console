"""An upgrade cannot bypass ownership, future schema or an unfinished shutdown."""
from contextlib import closing
import sqlite3
from unittest.mock import Mock

import pytest


@pytest.fixture
def service(monkeypatch):
    import launcher
    record = {"pid": 1234, "launch_token": "b" * 32, "shutdown_token": "a" * 32,
              "shutdown_protocol": launcher.SHUTDOWN_PROTOCOL}
    monkeypatch.setattr(launcher, "get_running_server", lambda **kw: (5001, dict(record)))
    notify = Mock(return_value=True)
    monkeypatch.setattr(launcher, "request_shutdown", notify)
    monkeypatch.setattr(launcher.subprocess, "run", Mock(side_effect=AssertionError("must not kill")))
    monkeypatch.setattr(launcher, "process_alive", lambda pid: True)
    return launcher, record, notify


def test_shutdown_timeout_preserves_running_process(service):
    launcher, record, notify = service
    with pytest.raises(RuntimeError, match="已取消重启"):
        launcher.stop_outdated_server(1234, timeout=0, port=5001, record=record)
    notify.assert_called_once_with("a" * 32)
    launcher.subprocess.run.assert_not_called()


def test_legacy_service_requires_original_launcher_to_close(service):
    launcher, record, notify = service
    record.pop("shutdown_protocol")
    with pytest.raises(RuntimeError, match="原启动窗口"):
        launcher.stop_outdated_server(1234, port=5001, record=record)
    notify.assert_not_called()


def test_identity_change_prevents_shutdown_signal(service):
    launcher, record, notify = service
    stale = dict(record, shutdown_token="c" * 32)
    with pytest.raises(RuntimeError, match="身份已变化"):
        launcher.stop_outdated_server(1234, port=5001, record=stale)
    notify.assert_not_called()


def test_missing_event_does_not_force_upgrade(service):
    launcher, record, notify = service
    notify.return_value = False
    with pytest.raises(RuntimeError, match="停机通道"):
        launcher.stop_outdated_server(1234, port=5001, record=record)
    launcher.subprocess.run.assert_not_called()


def test_owned_child_still_draining_is_not_killed(service):
    launcher, _, _ = service
    process = Mock(jm_launch_token="a" * 32)
    process.poll.return_value = None
    process.wait.side_effect = launcher.subprocess.TimeoutExpired("JMComic", 20)
    with pytest.raises(RuntimeError, match="进程已保留"):
        launcher.cleanup(process)
    launcher.subprocess.run.assert_not_called()
    process.kill.assert_not_called()


def test_missing_event_during_startup_cleanup_does_not_imply_safe_to_kill(service):
    launcher, _, notify = service
    notify.return_value = False
    process = Mock(jm_launch_token="a" * 32, jm_port=None)
    process.poll.return_value = None
    process.wait.side_effect = launcher.subprocess.TimeoutExpired("JMComic", 1)
    with pytest.raises(RuntimeError, match="进程仍在运行"):
        launcher.cleanup(process)
    launcher.subprocess.run.assert_not_called()
    process.kill.assert_not_called()
    process.terminate.assert_not_called()


def test_clean_identity_removal_can_retain_os_mutex_until_process_exit(tmp_path, monkeypatch):
    import app
    import os
    import json
    marker, pid_file = tmp_path / "flask.json", tmp_path / "flask.pid"
    marker.write_text(json.dumps({"pid": os.getpid()}))
    pid_file.write_text(str(os.getpid()))
    mutex = Mock()
    monkeypatch.setattr(app, "_PORT_FILE", marker)
    monkeypatch.setattr(app, "_PID_FILE", pid_file)
    monkeypatch.setattr(app, "_mutex_handle", mutex)
    app._release_lock(keep_process_lock=True)
    assert not marker.exists() and not pid_file.exists()
    assert app._mutex_handle is mutex
    mutex.close.assert_not_called()
    app._release_lock()
    mutex.close.assert_called_once()


def test_future_schema_rejected_before_running_newer_service_is_stopped(tmp_path, monkeypatch):
    import launcher
    from core import database
    database_path = tmp_path / "future.db"
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute(f"PRAGMA user_version={database.CURRENT_SCHEMA_VERSION + 1}")
    before = database_path.read_bytes()
    monkeypatch.setattr(database, "DB_PATH", database_path)
    running = Mock()
    monkeypatch.setattr(launcher, "get_running_server", running)
    stop, launch = Mock(), Mock()
    monkeypatch.setattr(launcher, "stop_outdated_server", stop)
    monkeypatch.setattr(launcher, "launch_flask", launch)
    with pytest.raises(RuntimeError, match="数据库版本高于"):
        launcher.main([], quiet=True)
    running.assert_not_called()
    stop.assert_not_called()
    launch.assert_not_called()
    assert database_path.read_bytes() == before


def test_http_worker_timeout_is_not_a_successful_shutdown(monkeypatch):
    from core.http_server import ManagedHTTPServer
    from types import SimpleNamespace
    server = object.__new__(ManagedHTTPServer)
    server.channels = {}
    dispatcher = SimpleNamespace(shutdown=Mock(return_value=True), threads={7})
    server.server = SimpleNamespace(task_dispatcher=dispatcher)
    assert server.stop(timeout=0) is False
    dispatcher.threads.clear()
    assert server.stop(timeout=0) is True


def test_progress_shutdown_ends_subscribers_without_database_mutation():
    from core.progress import ProgressManager
    manager = ProgressManager()
    tracker = manager.create_tracker("fixture")
    queue = tracker.subscribe()
    manager.close_all()
    assert tracker.is_done() and tracker.subscriber_count() == 0
    assert list(tracker.iter_events(queue)) == []
    assert manager.get_tracker("fixture") is None


def test_http_shutdown_wakes_blocked_output_workers():
    import threading
    from core.http_server import ManagedHTTPServer
    from types import SimpleNamespace
    from waitress.channel import HTTPChannel
    server = object.__new__(ManagedHTTPServer)
    entered = threading.Event()
    dispatcher = SimpleNamespace(threads={7})
    channel = object.__new__(HTTPChannel)
    channel.outbuf_lock = threading.Condition()
    channel.outbufs = []
    channel.total_outbufs_len = 10
    channel.connected = True
    channel.adj = SimpleNamespace(outbuf_high_watermark=1)
    channel.server = SimpleNamespace(pull_trigger=entered.set, active_channels={})
    channel._flush_exception = lambda *a, **kw: (False, None)
    channel.socket = None
    channel._fileno = 7
    server.channels = {7: channel}
    channel._map = server.channels
    def write():
        try:
            channel._flush_outbufs_below_high_watermark()
        finally:
            dispatcher.threads.clear()
    writer = threading.Thread(target=write, daemon=True)
    dispatcher.shutdown = lambda timeout: writer.join(timeout)
    server.server = SimpleNamespace(task_dispatcher=dispatcher)
    writer.start()
    try:
        assert entered.wait(1)
        assert server.stop(timeout=1) is True
        assert not writer.is_alive()
    finally:
        with channel.outbuf_lock:
            channel.connected = False
            channel.outbuf_lock.notify_all()
        writer.join(1)
