"""Offline tests for the explicit lifecycle supervisor; no actual worker starts."""
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core import release_lifecycle as lifecycle


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1", "localhost", b"127.0.0.1"])
def test_only_explicit_loopback_addresses_are_allowed(host):
    assert lifecycle._loopback(host)


@pytest.mark.parametrize("url", ["https://example.invalid/page", "http://127.0.0.1.evil.invalid/",
                                 "http://user:pass@127.0.0.1/", "file:///tmp/page", "http://192.168.1.1/"])
def test_curl_guard_rejects_remote_targets_without_dns(url):
    with pytest.raises(RuntimeError):
        lifecycle._loopback_url(url)


def test_marker_verification_requires_direct_temporary_root_and_exact_nonce():
    token = "a" * 64
    with tempfile.TemporaryDirectory(prefix="jmcomic-lifecycle-") as folder:
        root = Path(folder).resolve()
        (root / lifecycle.MARKER).write_text(json.dumps({"format": 1, "nonce": token}))
        assert lifecycle._verify_root(root, token) == root
        with pytest.raises(RuntimeError):
            lifecycle._verify_root(root, "b" * 64)
        with pytest.raises(RuntimeError):
            lifecycle._verify_root(root, "not-a-token")


def test_worker_rejects_invalid_marker_before_application_import(tmp_path, monkeypatch):
    monkeypatch.setattr(lifecycle, "_fresh_process", lambda: True)
    guard = Mock()
    monkeypatch.setattr(lifecycle, "_guard_network", guard)
    with pytest.raises(RuntimeError):
        lifecycle.run_worker(tmp_path, "a" * 64)
    guard.assert_not_called()


def test_worker_requires_all_paths_inside_its_marked_root_before_network_guard(monkeypatch):
    token = "a" * 64
    with tempfile.TemporaryDirectory(prefix="jmcomic-lifecycle-") as folder:
        root = Path(folder).resolve()
        (root / lifecycle.MARKER).write_text(json.dumps({"format": 1, "nonce": token}))
        monkeypatch.setattr(lifecycle, "_fresh_process", lambda: True)
        monkeypatch.setenv("JMCONSOLE_HOME", str(root))
        monkeypatch.setenv("JMCONSOLE_DOWNLOAD_DIR", str(root.parent / "foreign"))
        guard = Mock()
        monkeypatch.setattr(lifecycle, "_guard_network", guard)
        with pytest.raises(RuntimeError, match="paths are not isolated"):
            lifecycle.run_worker(root, token)
        guard.assert_not_called()


def test_environment_is_restored_after_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("JMCONSOLE_HOME", "original-instance")
    monkeypatch.delenv("JMCONSOLE_CACHE_DIR", raising=False)
    with pytest.raises(RuntimeError):
        with lifecycle._environment(tmp_path):
            assert os.environ["JMCONSOLE_HOME"] == str(tmp_path)
            raise RuntimeError("synthetic")
    assert os.environ["JMCONSOLE_HOME"] == "original-instance"
    assert "JMCONSOLE_CACHE_DIR" not in os.environ


def test_source_and_frozen_worker_commands_do_not_start_launcher(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    source = lifecycle._worker_command(tmp_path, "a" * 64)
    assert source[:2] == [sys.executable, str(Path(lifecycle.__file__).resolve().parent.parent / "desktop.py")]
    assert source[2:] == ["_lifecycle-worker", str(tmp_path), "a" * 64]
    monkeypatch.setattr(sys, "frozen", True)
    assert lifecycle._worker_command(tmp_path, "a" * 64) == [sys.executable, *source[2:]]


def test_start_worker_pins_instance_paths_and_owns_only_its_process(tmp_path, monkeypatch):
    runner = Mock(return_value=SimpleNamespace(pid=123))
    monkeypatch.setattr(lifecycle.subprocess, "Popen", runner)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    worker = lifecycle._start_worker(tmp_path, "a" * 64, "b" * 32)
    assert worker.pid == 123
    command = runner.call_args.args[0]
    kwargs = runner.call_args.kwargs
    assert command == [sys.executable, "_lifecycle-worker", str(tmp_path), "a" * 64]
    assert kwargs["env"]["JMCONSOLE_LAUNCH_TOKEN"] == "b" * 32
    assert kwargs["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert all(kwargs["env"][key] == value for key, value in lifecycle._paths(tmp_path).items())
    assert kwargs["cwd"] == str(tmp_path) and kwargs["stdout"] != lifecycle.subprocess.PIPE


def test_wait_ready_accepts_redirected_pid_only_with_launch_nonce(monkeypatch):
    verified = (5002, {"pid": 321, "launch_token": "fixture"})
    getter = Mock(return_value=verified)
    monkeypatch.setitem(sys.modules, "launcher", SimpleNamespace(get_running_server=getter))
    process = SimpleNamespace(pid=320, poll=lambda: None)
    assert lifecycle._wait_ready(process, "fixture") == verified
    getter.assert_called_once_with(expected_token="fixture")


def test_early_worker_exit_never_contacts_another_service(monkeypatch):
    getter = Mock()
    monkeypatch.setitem(sys.modules, "launcher", SimpleNamespace(get_running_server=getter))
    with pytest.raises(RuntimeError, match="exited"):
        lifecycle._wait_ready(SimpleNamespace(pid=321, poll=lambda: 1), "fixture")
    getter.assert_not_called()


@pytest.mark.parametrize("payload", [None, {"timeout": "47"}])
def test_api_request_declares_json_only_for_an_actual_body(monkeypatch, payload):
    response = Mock(status=200, read=Mock(return_value=b'{"status":"ok"}'))
    context = Mock(__enter__=Mock(return_value=response), __exit__=Mock(return_value=False))
    opener = SimpleNamespace(open=Mock(return_value=context))
    monkeypatch.setattr(lifecycle, "build_opener", Mock(return_value=opener))
    assert lifecycle._request(5002, "/api/settings", payload) == {"status": "ok"}
    request = opener.open.call_args.args[0]
    assert request.has_header("Content-type") == (payload is not None)
    assert request.get_method() == ("GET" if payload is None else "POST")


def test_verified_stop_requires_worker_identity_and_checks_real_exit(monkeypatch, tmp_path):
    stop = Mock()
    monkeypatch.setitem(sys.modules, "launcher", SimpleNamespace(stop_outdated_server=stop))
    process = SimpleNamespace(pid=320, jm_launch_token="fixture", wait=Mock(return_value=0))
    record = {"pid": 321, "launch_token": "fixture", "shutdown_protocol": "windows-event-v1", "shutdown_token": "fixture"}
    lifecycle._stop_verified(process, 5002, record, tmp_path)
    stop.assert_called_once_with(321, timeout=15, port=5002, record=record)
    process.wait.assert_called_once_with(timeout=15)
    with pytest.raises(RuntimeError):
        lifecycle._stop_verified(process, 5002, dict(record, launch_token="another-launch"), tmp_path)
    assert stop.call_count == 1


def test_cleanup_targets_only_owned_process_and_waits_after_kill(monkeypatch):
    monkeypatch.setattr(lifecycle.os, "name", "posix")
    process = Mock()
    process.jm_launch_token = None
    process.poll.side_effect = [None, -1]
    process.wait.side_effect = [lifecycle.subprocess.TimeoutExpired("worker", 5), -1]
    lifecycle._cleanup_owned(process)
    process.terminate.assert_called_once()
    process.kill.assert_called_once()
    assert process.wait.call_count == 2


def test_failure_cleanup_first_notifies_its_own_event_and_waits(monkeypatch):
    request = Mock(return_value=True)
    monkeypatch.setitem(sys.modules, "core.process_control", SimpleNamespace(request_shutdown=request))
    process = SimpleNamespace(jm_launch_token="owned-nonce", poll=lambda: None, wait=Mock(return_value=0))
    lifecycle._cleanup_owned(process)
    request.assert_called_once_with("owned-nonce")
    process.wait.assert_called_once_with(timeout=lifecycle.STOP_TIMEOUT)


def test_supervisor_rejects_already_imported_application_without_side_effects(monkeypatch):
    monkeypatch.setattr(lifecycle, "_fresh_process", lambda: False)
    start = Mock()
    monkeypatch.setattr(lifecycle, "_start_worker", start)
    assert lifecycle.run_lifecycle_check()["exit_code"] == 1
    start.assert_not_called()


@pytest.mark.parametrize("failure", [False, True])
def test_supervisor_cleans_owned_workers_before_deleting_temporary_data(monkeypatch, failure):
    monkeypatch.setattr(lifecycle, "_fresh_process", lambda: True)
    roots, calls = [], []
    process = SimpleNamespace(poll=lambda: 0)
    def run(root, token, processes, checks):
        roots.append(root)
        assert lifecycle._verify_root(root, token) == root
        assert all(os.environ[key] == value for key, value in lifecycle._paths(root).items())
        processes.append(process)
        checks["synthetic"] = True
        if failure:
            raise RuntimeError("synthetic check failure")
    def cleanup(owned):
        assert owned is process and roots[0].exists()
        calls.append("cleanup")
    monkeypatch.setattr(lifecycle, "_run_supervisor", run)
    monkeypatch.setattr(lifecycle, "_cleanup_owned", cleanup)
    result = lifecycle.run_lifecycle_check()
    assert result["status"] == ("error" if failure else "ok")
    assert calls == (["cleanup"] if failure else [])
    assert not roots[0].exists()


def test_supervisor_never_deletes_files_while_owned_worker_may_still_be_alive(monkeypatch):
    monkeypatch.setattr(lifecycle, "_fresh_process", lambda: True)
    temporary = Mock()
    temporary.name = str(Path(tempfile.gettempdir()) / "jmcomic-lifecycle-synthetic")
    monkeypatch.setattr(lifecycle.tempfile, "TemporaryDirectory", lambda **kw: temporary)
    monkeypatch.setattr(lifecycle.Path, "write_text", lambda *args, **kwargs: None)
    monkeypatch.setattr(lifecycle, "_verify_root", lambda *args: None)
    def run(root, token, processes, checks):
        processes.append(object())
        raise RuntimeError("synthetic")
    monkeypatch.setattr(lifecycle, "_run_supervisor", run)
    monkeypatch.setattr(lifecycle, "_cleanup_owned", Mock(side_effect=RuntimeError("still alive")))
    result = lifecycle.run_lifecycle_check()
    assert result["error"] == "cleanup failed"
    temporary.cleanup.assert_not_called()
    temporary._finalizer.detach.assert_called_once()


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
def test_interruption_still_reaps_owned_worker_before_temporary_cleanup(monkeypatch, interruption):
    monkeypatch.setattr(lifecycle, "_fresh_process", lambda: True)
    roots, cleaned = [], []
    process = object()
    def run(root, token, processes, checks):
        roots.append(root)
        processes.append(process)
        raise interruption()
    def cleanup(owned):
        assert owned is process and roots[0].exists()
        cleaned.append(owned)
    monkeypatch.setattr(lifecycle, "_run_supervisor", run)
    monkeypatch.setattr(lifecycle, "_cleanup_owned", cleanup)
    result = lifecycle.run_lifecycle_check()
    assert result["status"] == "error" and result["error"] == interruption.__name__
    assert cleaned == [process] and not roots[0].exists()


def test_socket_and_curl_guards_refuse_nonlocal_without_overriding_pytest_blockers(monkeypatch):
    from curl_cffi import Curl, CurlOpt
    monkeypatch.setattr(lifecycle.socket, "socket", SimpleNamespace(connect=Mock(), connect_ex=Mock()))
    original_connect = lifecycle.socket.socket.connect
    monkeypatch.setattr(lifecycle.socket, "getaddrinfo", Mock(return_value=[]))
    original_setopt = Curl.setopt
    original_perform = Curl.perform
    original_resolve = lifecycle.socket.getaddrinfo
    try:
        lifecycle._guard_network()
        with pytest.raises(RuntimeError, match="socket forbidden"):
            lifecycle.socket.socket.connect(object(), ("203.0.113.1", 443))
        original_connect.assert_not_called()
        lifecycle.socket.socket.connect(object(), ("127.0.0.1", 5001))
        original_connect.assert_called_once()
        with pytest.raises(RuntimeError, match="DNS"):
            lifecycle.socket.getaddrinfo("example.invalid", 443)
        with pytest.raises(RuntimeError, match="DNS"):
            lifecycle.socket.getaddrinfo("0.0.0.0", 443)
        lifecycle.socket.getaddrinfo("0.0.0.0", 8080, flags=socket.AI_PASSIVE)
        assert original_resolve.call_args.args[-1] == socket.AI_PASSIVE | socket.AI_NUMERICHOST
        with pytest.raises(RuntimeError, match="DNS"):
            lifecycle.socket.getaddrinfo("example.invalid", 443, flags=socket.AI_PASSIVE)
        with pytest.raises(RuntimeError):
            Curl.setopt(object(), CurlOpt.URL, b"https://example.invalid/")
        with pytest.raises(RuntimeError):
            Curl.setopt(object(), CurlOpt.FOLLOWLOCATION, 1)
        with pytest.raises(RuntimeError):
            Curl.perform(object())
    finally:
        Curl.setopt, Curl.perform = original_setopt, original_perform
        lifecycle.socket.getaddrinfo = original_resolve
