"""start.bat reuses a server running current code and replaces one running outdated code. No real processes."""
import hashlib
import io
import os
import subprocess
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture
def launcher_env(monkeypatch):
    import launcher
    calls = {"opened": [], "stopped": [], "launched": 0}
    monkeypatch.setattr(launcher.webbrowser, "open", lambda url: calls["opened"].append(url) or True)
    def stop(pid, port, record, *, only_if_idle):
        assert only_if_idle
        calls["stopped"].append(pid)
    monkeypatch.setattr(launcher, "stop_outdated_server", stop)

    def launch():
        calls["launched"] += 1
        return Mock(pid=777)
    monkeypatch.setattr(launcher, "launch_flask", launch)
    monkeypatch.setattr(launcher, "wait_until_ready", lambda proc: 5001)
    monkeypatch.setattr(launcher, "cleanup", lambda proc: None)

    def running(timestamp):
        record = {"port": 5000, "pid": 4242, "timestamp": timestamp, "shutdown_protocol": launcher.SHUTDOWN_PROTOCOL,
                  "launch_token": "a" * 32, "shutdown_token": "a" * 32}
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


@pytest.mark.parametrize("directory", ["templates/partials", "templates/classic"])
def test_nested_template_creation_edit_and_deletion_trigger_restart(tmp_path, directory):
    import launcher
    nested = tmp_path / directory / "page.html"
    nested.parent.mkdir(parents=True)
    started = time.time() + 1
    empty = launcher.code_fingerprint(tmp_path)
    nested.write_text("old")
    original = launcher.code_fingerprint(tmp_path)
    assert original != empty
    assert launcher.code_updated_since(started - 60, tmp_path)
    nested.write_text("new")
    os.utime(nested, (started - 3600, started - 3600))
    assert not launcher.code_updated_since(started, tmp_path)
    assert launcher.server_outdated({"code_fingerprint": original}, tmp_path)
    nested.unlink()
    assert launcher.code_fingerprint(tmp_path) == empty


@pytest.mark.parametrize("scenario", ["good", "empty", "bad", "timeout", "wrong_program"])
def test_source_setup_stamp_requires_startable_python(tmp_path, monkeypatch, scenario):
    import launcher
    venv = tmp_path / ".venv"
    executable = venv / "Scripts/python.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"" if scenario == "empty" else b"fake binary")
    requirements = b"example==1\n"
    (tmp_path / "requirements.txt").write_bytes(requirements)
    (venv / ".req.sha256").write_text(hashlib.sha256(requirements).hexdigest(), encoding="ascii")
    probe = Mock(return_value=SimpleNamespace(returncode=0, stdout=b"jmconsole-python\n"))
    if scenario == "bad":
        probe.side_effect = OSError("synthetic invalid executable")
    elif scenario == "timeout":
        probe.side_effect = subprocess.TimeoutExpired(str(executable), 5)
    elif scenario == "wrong_program":
        probe.return_value.stdout = b"not the Python probe"
    monkeypatch.setattr(launcher.subprocess, "run", probe)
    assert launcher._setup_needed(tmp_path) == (scenario != "good")
    if scenario == "empty":
        probe.assert_not_called()
    else:
        assert probe.call_args.args[0][0] == str(executable)


def test_source_setup_rebuilds_a_broken_interpreter_before_pip(tmp_path, monkeypatch):
    import launcher
    venv = tmp_path / ".venv"
    executable = venv / "Scripts/python.exe"
    executable.parent.mkdir(parents=True)
    executable.touch()
    requirements = b"example==1\n"
    (tmp_path / "requirements.txt").write_bytes(requirements)
    stamp = venv / ".req.sha256"
    expected = hashlib.sha256(requirements).hexdigest()
    stamp.write_text(expected, encoding="ascii")
    lease = Mock()
    monkeypatch.setattr(launcher, "acquire_instance_lock", lambda: lease)
    startup = Mock(side_effect=lambda path: path.stat().st_size > 0)
    monkeypatch.setattr(launcher, "_source_python_startable", startup)
    commands = []

    def run(command, *, cwd, check):
        assert cwd == tmp_path and check and not stamp.exists()
        lease.close.assert_not_called()
        commands.append(command)
        if command[2] == "venv":
            executable.write_bytes(b"repaired interpreter")
        else:
            assert command[0] == str(executable) and executable.stat().st_size > 0

    monkeypatch.setattr(launcher.subprocess, "run", run)
    launcher._prepare_source(tmp_path)
    assert [command[2:4] for command in commands] == [
        ["venv", str(venv)], ["pip", "install"], ["pip", "check"]]
    assert stamp.read_text(encoding="ascii") == expected
    lease.close.assert_called_once()


@pytest.mark.parametrize("exit_code", [0, 7])
def test_wait_propagates_exit_failure_after_readiness(monkeypatch, exit_code):
    import launcher
    child = Mock(pid=4321)
    child.poll.return_value = exit_code
    monkeypatch.setattr(launcher, "get_running_server", lambda: None)
    monkeypatch.setattr(launcher, "launch_flask", lambda: child)
    monkeypatch.setattr(launcher, "wait_until_ready", lambda proc: 5001)
    monkeypatch.setattr(launcher.webbrowser, "open", Mock(return_value=True))
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    notify = Mock()
    monkeypatch.setattr(launcher, "request_shutdown", notify)
    if exit_code:
        with pytest.raises(RuntimeError, match="退出码 7"):
            launcher.main(["--wait"])
    else:
        assert launcher.main(["--wait"]) == 0
    notify.assert_not_called()
    child.wait.assert_not_called()


def test_launch_displays_the_local_url_if_browser_fails(launcher_env, monkeypatch, capsys):
    launcher, calls, _ = launcher_env
    monkeypatch.setattr(launcher, "get_running_server", lambda: None)
    monkeypatch.setattr(launcher.webbrowser, "open", Mock(side_effect=OSError("no browser")))
    assert launcher.main([], quiet=True) == 0
    assert calls["launched"] == 1
    output = capsys.readouterr().out
    assert "浏览器未打开" in output and "http://127.0.0.1:5001" in output


def test_record_without_fingerprint_falls_back_to_timestamps(tmp_path):
    import launcher
    (tmp_path / "app.py").write_text("", encoding="utf-8")
    assert launcher.server_outdated({"timestamp": time.time() - 60}, root=tmp_path)
    assert not launcher.server_outdated({"timestamp": time.time() + 60}, root=tmp_path)
    assert launcher.server_outdated({"timestamp": "not-a-number"}, root=tmp_path)


def test_server_records_its_code_fingerprint(app, tmp_path, monkeypatch):
    import runpy
    import app as app_module
    import launcher
    source = tmp_path / "app.py"
    source.write_text("version = 1")
    fingerprint = launcher.code_fingerprint
    monkeypatch.setattr(launcher, "code_fingerprint", lambda root=tmp_path: fingerprint(root))
    startup = runpy.run_path(app_module.__file__)["_startup_record"]
    record = startup(5001)
    assert record["port"] == 5001 and record["pid"] == os.getpid()
    assert record["code_fingerprint"] == fingerprint(tmp_path)
    assert not launcher.server_outdated(record, tmp_path)
    source.write_text("version = 2")
    assert startup(5001)["code_fingerprint"] == record["code_fingerprint"]
    assert launcher.server_outdated(record, tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="Windows process-tree stop")
def test_stopping_a_verified_server_notifies_and_waits_without_force(monkeypatch):
    import launcher
    events = []
    record = {"pid": 4242, "shutdown_protocol": launcher.SHUTDOWN_PROTOCOL, "shutdown_token": "a" * 32}
    monkeypatch.setattr(launcher, "get_running_server", lambda **kw: (5000, record))
    monkeypatch.setattr(launcher, "request_shutdown", lambda token: events.append(("notify", token)) or True)
    monkeypatch.setattr(launcher.subprocess, "run",
                        lambda *a, **kw: pytest.fail("must not force terminate a verified service"))
    monkeypatch.setattr(launcher, "process_alive", lambda pid: False)
    launcher.stop_outdated_server(4242, port=5000, record=record)
    proc = Mock(pid=77, jm_port=5001, jm_launch_token="b" * 32, returncode=0)
    proc.poll.return_value = None
    launcher.cleanup(proc)
    assert events == [("notify", "a" * 32), ("notify", "b" * 32)]
    proc.wait.assert_called_once_with(timeout=20)


@pytest.mark.skipif(os.name != "nt", reason="Windows process-tree stop")
@pytest.mark.parametrize("failure", [KeyboardInterrupt, RuntimeError])
def test_notification_failure_never_force_kills_running_service(monkeypatch, failure):
    import launcher
    events = []
    record = {"pid": 4242, "shutdown_protocol": launcher.SHUTDOWN_PROTOCOL, "shutdown_token": "a" * 32}
    monkeypatch.setattr(launcher, "get_running_server", lambda **kw: (5000, record))
    def notify(token):
        events.append(token)
        raise failure("notification failed")
    monkeypatch.setattr(launcher, "request_shutdown", notify)
    monkeypatch.setattr(launcher.subprocess, "run",
                        lambda *a, **kw: pytest.fail("must preserve the process"))
    with pytest.raises(failure):
        launcher.stop_outdated_server(4242, port=5000)
    proc = Mock(pid=77, jm_port=5001, jm_launch_token="b" * 32)
    proc.poll.return_value = None
    with pytest.raises(failure):
        launcher.cleanup(proc)
    assert events == ["a" * 32, "b" * 32]
    proc.wait.assert_not_called()


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
        {"application": "jmcomic-local-console", "pid": 42, "status": "ok",
         "instance_id": launcher.get_path_config().instance_id()}).encode())
    monkeypatch.setattr(launcher, "build_opener", lambda *args: opener)
    port, record = launcher.get_running_server()
    assert port == 5002 and record["timestamp"] == 123.5
