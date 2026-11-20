import os
import socket
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.mark.skipif(os.name != "nt", reason="Winsock exclusive address ownership")
def test_listener_falls_back_exclusively_and_rebinds_after_close(monkeypatch):
    import app
    with socket.socket() as occupied:
        occupied.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        monkeypatch.setattr(app, "_DEFAULT_PORT", occupied.getsockname()[1])
        monkeypatch.setattr(app, "_FALLBACK_PORTS", [0])
        with app._bind_preemptive() as listener:
            address = listener.getsockname()
            assert address != occupied.getsockname()
            assert listener.getsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE)
        monkeypatch.setattr(app, "_FALLBACK_PORTS", [address[1]])
        with app._bind_preemptive() as restarted:
            assert restarted.getsockname() == address


def test_runtime_drains_tag_job_accepted_before_start():
    from core.runtime import ApplicationRuntime
    from core.tag_jobs import TagJobs
    tags = TagJobs()
    entered, finish = threading.Event(), threading.Event()
    def work():
        entered.set()
        finish.wait(3)
    tags.submit(work)
    job_manager = SimpleNamespace(request_stop=lambda: None)
    updates = SimpleNamespace(request_stop=lambda: None, stop=lambda timeout: True)
    runtime = ApplicationRuntime(job_manager, None, updates, additional_services=(("tags", tags),))
    try:
        assert entered.wait(1)
        assert not runtime.stop(0)
        assert runtime.stop_errors == ("tags: drain timeout",)
    finally:
        finish.set()
        assert runtime.stop(2)


def test_failed_release_returns_budget_for_other_services_and_is_retryable():
    from core.background_calls import BackgroundCalls
    calls = BackgroundCalls()
    released = threading.Event()
    with ThreadPoolExecutor(1) as executor:
        calls.submit(executor, lambda: 1, release=released.is_set).result(1)
        try:
            assert executor.submit(calls.stop, 10).result(timeout=2) is False
            assert calls.runtime_status()["pending"] == 1
        finally:
            released.set()
            assert calls.stop(2)


@pytest.mark.skipif(os.name != "nt", reason="Windows source launcher")
@pytest.mark.parametrize("scenario", ["idle", "busy", "unverified", "failed-install", "failed-check"])
def test_source_setup_checks_instance_before_pip_and_stamps_only_success(tmp_path, monkeypatch, scenario):
    import launcher
    from core.instance_lock import InstanceBusyError
    (tmp_path / "requirements.txt").write_text("example==1\n")
    executable = tmp_path / ".venv/Scripts/python.exe"
    executable.parent.mkdir(parents=True)
    executable.touch()
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    record = dict(pid=777, shutdown_token="a" * 32, launch_token="a" * 32,
                  shutdown_protocol=launcher.SHUTDOWN_PROTOCOL)
    server = (5000, record)
    monkeypatch.setattr(launcher, "get_running_server", lambda **kw: None if scenario == "unverified" else server)
    events = []
    def stop(pid, *, port, record, only_if_idle):
        assert only_if_idle
        events.append("verify idle")
        if scenario == "busy":
            return "busy"
        events.append("observed exit")
    def lock():
        events.append("lock")
        if scenario == "unverified":
            raise InstanceBusyError("occupied")
        return SimpleNamespace(close=lambda: events.append("unlock"))
    def run(command, *, cwd, check):
        assert cwd == tmp_path and check and command[:3] == [str(executable), "-m", "pip"]
        assert "lock" in events and "unlock" not in events
        events.append(command[3])
        if scenario == "failed-" + command[3]:
            raise subprocess.CalledProcessError(1, command)
    monkeypatch.setattr(launcher, "stop_outdated_server", stop)
    monkeypatch.setattr(launcher, "acquire_instance_lock", lock)
    monkeypatch.setattr(launcher, "_source_python_startable", lambda path: path == executable)
    monkeypatch.setattr(launcher.subprocess, "run", run)
    monkeypatch.setattr(launcher, "launch_flask", lambda: events.append("launch") or Mock(pid=888))
    monkeypatch.setattr(launcher, "wait_until_ready", lambda proc: 5000)
    monkeypatch.setattr(launcher.webbrowser, "open", lambda *args: events.append("open") or True)
    if scenario.startswith("failed-"):
        with pytest.raises(subprocess.CalledProcessError):
            launcher.main(["--source-setup"], quiet=True)
        assert events[-1] == "unlock" and "launch" not in events
    elif scenario == "unverified":
        with pytest.raises(InstanceBusyError):
            launcher.main(["--source-setup"], quiet=True)
        assert events == ["lock"]
    else:
        assert launcher.main(["--source-setup"], quiet=True) == 0
        if scenario == "idle":
            assert events == ["verify idle", "observed exit", "lock", "install", "check", "unlock", "launch", "open"]
            events.clear()
            monkeypatch.setattr(launcher, "server_outdated", lambda *args: False)
            assert launcher.main(["--source-setup"], quiet=True) == 0
            assert events == ["open"]
        else:
            assert events == ["verify idle", "open"]
    assert launcher._setup_needed(tmp_path) == (scenario != "idle")
