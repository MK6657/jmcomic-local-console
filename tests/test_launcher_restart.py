"""start.bat reuses a server running current code and replaces one running outdated code. No real processes."""
import io
import os
import time
from unittest.mock import Mock

import pytest


@pytest.fixture
def launcher_env(monkeypatch):
    import launcher
    calls = {"opened": [], "stopped": [], "launched": 0}
    monkeypatch.setattr(launcher.webbrowser, "open", lambda url: calls["opened"].append(url))
    monkeypatch.setattr(launcher, "stop_outdated_server",
                        lambda pid, port=None: calls["stopped"].append(pid))

    def launch():
        calls["launched"] += 1
        return Mock(pid=777)
    monkeypatch.setattr(launcher, "launch_flask", launch)
    monkeypatch.setattr(launcher, "wait_until_ready", lambda proc: 5001)
    monkeypatch.setattr(launcher, "cleanup", lambda proc: None)

    def running(timestamp):
        record = {"port": 5000, "pid": 4242, "timestamp": timestamp}
        monkeypatch.setattr(launcher, "get_running_server", lambda *a, **k: (5000, record))
    return launcher, calls, running


def test_server_running_current_code_is_reused(launcher_env):
    launcher, calls, running = launcher_env
    running(time.time() + 3600)  # started after every program file was written
    assert launcher.main([], quiet=True) == 0
    assert calls == {"opened": ["http://127.0.0.1:5000"], "stopped": [], "launched": 0}


@pytest.mark.parametrize("timestamp", [0, "not-a-number", None])
def test_server_running_outdated_code_is_replaced(launcher_env, timestamp):
    launcher, calls, running = launcher_env
    running(timestamp)  # older than the program files, or unknown
    assert launcher.main([], quiet=True) == 0
    assert calls["stopped"] == [4242] and calls["launched"] == 1
    assert calls["opened"] == ["http://127.0.0.1:5001"]


def test_no_running_server_just_launches(launcher_env, monkeypatch):
    launcher, calls, _ = launcher_env
    monkeypatch.setattr(launcher, "get_running_server", lambda *a, **k: None)
    assert launcher.main([], quiet=True) == 0
    assert calls == {"opened": ["http://127.0.0.1:5001"], "stopped": [], "launched": 1}


def test_only_restart_relevant_files_count(tmp_path):
    import launcher
    (tmp_path / "static" / "js").mkdir(parents=True)
    (tmp_path / "static" / "js" / "page.js").write_text("", encoding="utf-8")
    assert not launcher.code_updated_since(0, root=tmp_path)  # static files are cache-busted, no restart
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "page.html").write_text("", encoding="utf-8")
    assert launcher.code_updated_since(time.time() - 60, root=tmp_path)
    assert not launcher.code_updated_since(time.time() + 60, root=tmp_path)


def test_update_copied_over_existing_files_is_detected(tmp_path):
    """Copying a release over existing files keeps their creation time and can set an older modification
    time; only the content fingerprint recorded by the server notices."""
    import launcher
    (tmp_path / "core").mkdir()
    module = tmp_path / "core" / "online_reader.py"
    module.write_text("SUFFIX = '.part'\n", encoding="utf-8")
    started = time.time() + 1
    record = {"timestamp": started, "code_fingerprint": launcher.code_fingerprint(tmp_path)}
    os.utime(module, (started + 60, started + 60))  # touched, same content: nothing to restart for
    assert not launcher.server_outdated(record, root=tmp_path)
    module.write_text("SUFFIX = '.webp'\n", encoding="utf-8")  # the update, built two days ago
    built = time.time() - 2 * 86400
    os.utime(module, (built, built))
    assert not launcher.code_updated_since(started, root=tmp_path)  # timestamps alone miss it
    assert launcher.server_outdated(record, root=tmp_path)
    module.write_text("SUFFIX = '.part'\n", encoding="utf-8")
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "new.html").write_text("", encoding="utf-8")  # an added file counts too
    assert launcher.server_outdated(record, root=tmp_path)


def test_record_without_fingerprint_falls_back_to_timestamps(tmp_path):
    import launcher
    (tmp_path / "app.py").write_text("", encoding="utf-8")
    assert launcher.server_outdated({"timestamp": time.time() - 60}, root=tmp_path)
    assert not launcher.server_outdated({"timestamp": time.time() + 60}, root=tmp_path)
    assert launcher.server_outdated({"timestamp": "not-a-number"}, root=tmp_path)


def test_server_records_its_code_fingerprint(app):
    import app as app_module
    import launcher
    record = app_module._startup_record(5001)
    assert record["port"] == 5001 and record["pid"] == os.getpid()
    assert record["code_fingerprint"] == launcher.code_fingerprint()
    assert not launcher.server_outdated(record)


@pytest.mark.skipif(os.name != "nt", reason="Windows process-tree stop")
def test_stopping_a_server_flushes_its_log_summaries_first(monkeypatch):
    """taskkill /F skips the server's atexit flush of pending "N similar messages merged" summaries."""
    import launcher
    events = []
    monkeypatch.setattr(launcher, "flush_server_logs", lambda port: events.append(("flush", port)))
    monkeypatch.setattr(launcher.subprocess, "run",
                        lambda args, **kwargs: events.append(("kill", args[2])) or Mock(returncode=0))
    monkeypatch.setattr(launcher, "process_alive", lambda pid: False)
    launcher.stop_outdated_server(4242, port=5000)
    proc = Mock(pid=77, jm_port=5001)
    proc.poll.return_value = None
    launcher.cleanup(proc)
    assert events == [("flush", 5000), ("kill", "4242"), ("flush", 5001), ("kill", "77")]


@pytest.mark.skipif(os.name != "nt", reason="Windows process-tree stop")
@pytest.mark.parametrize("failure", [KeyboardInterrupt, RuntimeError])
def test_server_is_stopped_even_when_the_flush_fails(monkeypatch, failure):
    """A second Ctrl+C during the flush wait (or any unexpected error) must not skip the taskkill: the
    launcher would exit and leave the server holding its port and mutex."""
    import launcher
    events = []

    def flush(port):
        events.append(("flush", port))
        raise failure("interrupted while waiting for the server")
    monkeypatch.setattr(launcher, "flush_server_logs", flush)
    monkeypatch.setattr(launcher.subprocess, "run",
                        lambda args, **kwargs: events.append(("kill", args[2])) or Mock(returncode=0))
    monkeypatch.setattr(launcher, "process_alive", lambda pid: False)
    with pytest.raises(failure):
        launcher.stop_outdated_server(4242, port=5000)
    proc = Mock(pid=77, jm_port=5001)
    proc.poll.return_value = None
    with pytest.raises(failure):
        launcher.cleanup(proc)
    assert events == [("flush", 5000), ("kill", "4242"), ("flush", 5001), ("kill", "77")]
    proc.wait.assert_called_once_with(timeout=5)


class _ReplySocket:
    """What http.client reads a reply from; lets the real response parser see arbitrary bytes offline."""

    def __init__(self, data):
        self.data = data

    def makefile(self, mode):
        return io.BytesIO(self.data)


@pytest.mark.parametrize("reply", [
    b"SSH-2.0-OpenSSH_9.6\r\n",                                      # http.client.BadStatusLine
    b"HTTP/1.1 200 OK\r\nX-Big: " + b"a" * 70000 + b"\r\n\r\n",      # http.client.LineTooLong
    b"",                                                             # RemoteDisconnected
], ids=["non-http", "header-too-long", "empty"])
def test_flush_server_logs_ignores_non_http_replies(monkeypatch, reply):
    """Something else answering on the verified port must not make the best-effort flush raise."""
    import http.client
    import launcher

    class Opener:
        def open(self, request, timeout):
            response = http.client.HTTPResponse(_ReplySocket(reply), method=request.get_method())
            response.begin()  # the parse urllib runs on every reply
            return response
    monkeypatch.setattr(launcher, "build_opener", lambda *args: Opener())
    with pytest.raises(http.client.HTTPException):
        Opener().open(launcher.Request("http://127.0.0.1:5000/", method="POST"), timeout=1)
    assert launcher.flush_server_logs(5000) is False


def test_flush_server_logs_posts_locally_without_proxy(monkeypatch):
    import io
    import launcher
    seen, handlers = {}, []

    class Opener:
        def open(self, request, timeout):
            seen.update(url=request.full_url, method=request.get_method(), timeout=timeout)
            return io.BytesIO(b'{"status": "ok", "flushed": 1}')
    monkeypatch.setattr(launcher, "build_opener", lambda *args: handlers.extend(args) or Opener())
    assert launcher.flush_server_logs(5002) is True
    assert seen == {"url": "http://127.0.0.1:5002/api/system/logs/flush", "method": "POST", "timeout": 3}
    assert handlers[0].proxies == {}
    assert launcher.flush_server_logs(None) is False and launcher.flush_server_logs(Mock()) is False

    def refused(*args):
        raise ConnectionRefusedError("server already gone")
    monkeypatch.setattr(launcher, "build_opener", refused)
    assert launcher.flush_server_logs(5002) is False


def test_running_server_record_is_returned_with_port(tmp_path, monkeypatch):
    import io
    import json
    import launcher
    marker = tmp_path / "flask.json"
    marker.write_text(json.dumps({"port": 5002, "pid": 42, "timestamp": 123.5}), encoding="utf-8")
    monkeypatch.setattr(launcher, "FLASK_JSON", marker)
    opener = Mock()
    opener.open.return_value = io.BytesIO(json.dumps(
        {"application": "jmcomic-local-console", "pid": 42, "status": "ok"}).encode())
    monkeypatch.setattr(launcher, "build_opener", lambda *args: opener)
    port, record = launcher.get_running_server()
    assert port == 5002 and record["timestamp"] == 123.5
