"""Safe, offline release regressions; no live downloads or personal database."""
import io
import json
import os
import subprocess
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize("path,status", [
    ("/", 200), ("/search", 200), ("/downloads", 200), ("/wishlist", 200),
    ("/library", 200), ("/settings", 200), ("/album/123456", 200),
    ("/preview/123456", 200), ("/api/jobs", 200), ("/api/wishlist", 200),
    ("/api/library", 200), ("/api/settings", 200), ("/api/system/health", 200),
    ("/missing-audit-page", 404), ("/api/missing-audit-route", 404),
])
def test_local_routes(client, path, status):
    assert client.get(path).status_code == status


def test_health_identifies_process(client):
    data = client.get("/api/system/health").get_json()
    assert data["application"] == "jmcomic-local-console"
    assert data["pid"] == os.getpid()


@pytest.mark.parametrize("proxy", ["", "http://audit-user:audit-pass@127.0.0.1:9876"])
def test_proxy_option_constructs(client, proxy):
    from core.settings import update_settings, build_jmcomic_option
    from core.jm_service import get_client, close_client
    from jmcomic import JmOption
    update_settings({"proxy": proxy})
    config = build_jmcomic_option()
    assert "proxies" not in config
    option = JmOption.construct(config)
    metadata = option.client.postman.meta_data.src_dict
    if proxy:
        assert metadata["proxies"] == {"http": proxy, "https": proxy}
    instance, _ = get_client(shared=False)
    try:
        # Check the actual HTTP session too, not just the configuration shape.
        if proxy:
            assert instance.get_root_postman().session.proxies["https"] == proxy
    finally:
        close_client(instance)


def test_export_omits_private_fields_and_import_preserves_proxy(client):
    from core.settings import update_settings, get_settings
    from core.database import set_setting
    proxy = "http://audit-user:audit-pass@127.0.0.1:9876"
    update_settings({"proxy": proxy, "timeout": "41"})
    set_setting("unknown_private_field", "not-for-export")
    exported = client.get("/api/settings/export")
    payload = exported.get_json()
    assert "proxy" not in payload
    assert "download_root" not in payload
    assert "unknown_private_field" not in payload
    assert "audit-pass" not in exported.get_data(as_text=True)
    assert exported.headers["Cache-Control"] == "no-store"
    result = client.post("/api/settings/import", data={
        "file": (io.BytesIO(exported.data), "settings.json"),
    })
    assert result.status_code == 200
    assert get_settings()["proxy"] == proxy
    assert get_settings()["timeout"] == "41"


def test_database_is_outside_project(client):
    from pathlib import Path
    from core import database as db
    root = Path(__file__).resolve().parent.parent
    assert not db.DB_PATH.is_relative_to(root)


@pytest.mark.parametrize("record", ["invalid", "[]", "null", '{}', '{"port": 9999, "pid": 42}'])
def test_launcher_rejects_invalid_record(tmp_path, monkeypatch, record):
    import launcher
    marker = tmp_path / "flask.json"
    marker.write_text(record, encoding="utf-8")
    monkeypatch.setattr(launcher, "FLASK_JSON", marker)
    assert launcher.get_running_port() is None


@pytest.mark.parametrize("health,expected", [
    ({"application": "jmcomic-local-console", "pid": 42, "status": "ok"}, 5002),
    ({"application": "another-app", "pid": 42, "status": "ok"}, None),
    ({"application": "jmcomic-local-console", "pid": 99, "status": "ok"}, None),
    ({"application": "jmcomic-local-console", "pid": 42, "status": "error"}, None),
])
def test_launcher_checks_service_identity(tmp_path, monkeypatch, health, expected):
    import launcher
    marker = tmp_path / "flask.json"
    marker.write_text(json.dumps({"port": 5002, "pid": 42}), encoding="utf-8")
    monkeypatch.setattr(launcher, "FLASK_JSON", marker)
    opener = Mock()
    opener.open.return_value = io.BytesIO(json.dumps(health).encode())
    monkeypatch.setattr(launcher, "build_opener", lambda *args: opener)
    assert launcher.get_running_port(expected_pid=42) == expected
    assert launcher.get_running_port(expected_pid=99) is None


def test_launcher_writes_to_log_not_pipe(tmp_path, monkeypatch):
    import launcher
    popen = Mock()
    monkeypatch.setattr(launcher.subprocess, "Popen", popen)
    launcher.launch_flask(tmp_path)
    kwargs = popen.call_args.kwargs
    assert kwargs["stdout"] != subprocess.PIPE
    assert kwargs["stderr"] == subprocess.STDOUT
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert (tmp_path / "runtime/logs/launcher.log").is_file()


def test_launcher_cleans_child_after_startup_failure(monkeypatch):
    import launcher
    proc = Mock()
    cleanup = Mock()
    monkeypatch.setattr(launcher, "get_running_port", lambda: None)
    monkeypatch.setattr(launcher, "launch_flask", lambda: proc)
    monkeypatch.setattr(launcher, "cleanup", cleanup)
    monkeypatch.setattr(launcher, "wait_until_ready", Mock(side_effect=TimeoutError))
    with pytest.raises(TimeoutError):
        launcher.main([])
    cleanup.assert_called_once_with(proc)


def test_launcher_detects_early_child_exit():
    import launcher
    proc = Mock()
    proc.poll.return_value = 1
    with pytest.raises(RuntimeError, match="exited"):
        launcher.wait_until_ready(proc)


def test_launcher_timeout():
    import launcher
    with pytest.raises(TimeoutError):
        launcher.wait_until_ready(Mock(), timeout=0)


def test_launcher_opens_existing_instance_without_spawning(monkeypatch):
    import launcher
    spawn = Mock()
    browser = Mock()
    monkeypatch.setattr(launcher, "get_running_port", lambda: 5003)
    monkeypatch.setattr(launcher, "launch_flask", spawn)
    monkeypatch.setattr(launcher.webbrowser, "open", browser)
    assert launcher.main([]) == 0
    spawn.assert_not_called()
    browser.assert_called_once_with("http://127.0.0.1:5003")


def test_cleanup_does_not_terminate_finished_child():
    import launcher
    proc = Mock()
    proc.poll.return_value = 0
    launcher.cleanup(proc)
    proc.terminate.assert_not_called()


def test_startup_token_handles_windows_redirector_pid(tmp_path, monkeypatch):
    import launcher
    marker = tmp_path / "flask.json"
    marker.write_text(json.dumps({"port": 5001, "pid": 84, "launch_token": "this-launch"}), encoding="utf-8")
    monkeypatch.setattr(launcher, "FLASK_JSON", marker)
    health = {"application": "jmcomic-local-console", "pid": 84, "status": "ok"}
    opener = Mock()
    opener.open.return_value = io.BytesIO(json.dumps(health).encode())
    monkeypatch.setattr(launcher, "build_opener", lambda *args: opener)
    proc = Mock(pid=42, jm_launch_token="this-launch")
    proc.poll.return_value = None
    assert launcher.wait_until_ready(proc) == 5001
    assert launcher.get_running_port(expected_token="stale-launch") is None


@pytest.mark.skipif(os.name != "nt", reason="Windows process-tree cleanup")
def test_cleanup_targets_only_owned_process_tree(monkeypatch):
    import launcher
    proc = Mock(pid=4242)
    proc.poll.return_value = None
    run = Mock(return_value=Mock(returncode=0))
    monkeypatch.setattr(launcher.subprocess, "run", run)
    launcher.cleanup(proc)
    assert run.call_args.args[0] == ["taskkill", "/PID", "4242", "/T", "/F"]
    proc.wait.assert_called_once_with(timeout=5)


def test_launcher_wait_stops_its_own_process(monkeypatch):
    import launcher
    proc = Mock()
    cleanup = Mock()
    monkeypatch.setattr(launcher, "get_running_port", lambda: None)
    monkeypatch.setattr(launcher, "launch_flask", lambda: proc)
    monkeypatch.setattr(launcher, "wait_until_ready", lambda child: 5001)
    monkeypatch.setattr(launcher, "cleanup", cleanup)
    monkeypatch.setattr(launcher.webbrowser, "open", Mock())
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    assert launcher.main(["--wait"]) == 0
    cleanup.assert_called_once_with(proc)
