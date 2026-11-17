"""Exercise the real health contract and upgrade decision without sockets or service processes."""
import json
import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture
def running_service(client, tmp_path, monkeypatch):
    import launcher
    from core.path_utils import get_path_config
    from routes import api_system

    paths = get_path_config({}, default_home=tmp_path / "实例")
    token = "a" * 32
    monkeypatch.setenv("JMCONSOLE_LAUNCH_TOKEN", token)
    marker = tmp_path / "flask.json"
    record = dict(pid=os.getpid(), port=5002, launch_token=token, shutdown_token=token,
                  shutdown_protocol=launcher.SHUTDOWN_PROTOCOL, code_fingerprint="old")
    marker.write_text(json.dumps(record))
    monkeypatch.setattr(launcher, "FLASK_JSON", marker)
    monkeypatch.setattr(launcher, "get_path_config", lambda: paths)
    monkeypatch.setattr(api_system, "get_path_config", lambda: paths)
    monkeypatch.setattr(api_system, "get_active_client_count", lambda: 0)
    monkeypatch.setattr(launcher, "process_alive", lambda pid: False)
    notify, browser, spawn, cleanup = Mock(return_value=True), Mock(), Mock(), Mock()
    monkeypatch.setattr(launcher, "request_shutdown", notify)
    monkeypatch.setattr(launcher.os, "kill", notify)
    monkeypatch.setattr(launcher.webbrowser, "open", browser)
    monkeypatch.setattr(launcher, "launch_flask", spawn)
    monkeypatch.setattr(launcher, "cleanup", cleanup)
    monkeypatch.setattr(launcher, "wait_until_ready", lambda child: 5003)

    def local(port, endpoint):
        assert port == 5002 and endpoint.split("?")[0] == "/api/system/health"
        response = client.get(endpoint)
        assert response.status_code == 200
        return response.get_json()

    monkeypatch.setattr(launcher, "_local_json", local)
    return SimpleNamespace(paths=paths, local=local, record=record, notify=notify, browser=browser,
                           spawn=spawn, cleanup=cleanup)


@pytest.mark.parametrize("busy", ["running", "queued", "paused", "executors", "clients"])
def test_update_opens_original_service_without_interrupting_work(running_service, monkeypatch, busy, capsys):
    import launcher
    from core import database
    from routes import api_system
    if busy == "executors":
        monkeypatch.setattr(api_system.job_manager, "get_running_count", lambda: 1)
    elif busy == "clients":
        monkeypatch.setattr(api_system, "get_active_client_count", lambda: 1)
    else:
        database.insert_job("fixture", "123", "private-title", [])
        if busy != "queued":
            assert database.transition_job_status("fixture", ["queued"], busy)
    service = running_service
    assert launcher.main(["--wait"]) == 0
    assert "已暂缓升级" in capsys.readouterr().out
    service.browser.assert_called_once_with("http://127.0.0.1:5002")
    service.notify.assert_not_called()
    service.spawn.assert_not_called()
    service.cleanup.assert_not_called()


def test_idle_verified_update_signals_original_then_opens_replacement(running_service):
    import launcher
    assert launcher.main([]) == 0
    service = running_service
    if os.name == "nt":
        service.notify.assert_called_once_with(service.record["shutdown_token"])
    else:
        service.notify.assert_called_once_with(service.record["pid"], 15)
    service.spawn.assert_called_once()
    service.browser.assert_called_once_with("http://127.0.0.1:5003")


@pytest.mark.parametrize("failure", ["timeout", "missing", "negative", "bool", "string"])
def test_unknown_activity_preserves_verified_service(running_service, monkeypatch, capsys, failure):
    import launcher
    service = running_service

    def local(port, endpoint):
        health = service.local(port, endpoint)
        if "?" in endpoint:
            if failure == "timeout":
                raise TimeoutError
            if failure == "missing":
                del health["jobs"]
            else:
                health["jobs"]["running"] = {"negative": -1, "bool": False, "string": "0"}[failure]
        return health

    monkeypatch.setattr(launcher, "_local_json", local)
    assert launcher.main([]) == 0
    assert "无法确认任务是否空闲" in capsys.readouterr().out
    service.browser.assert_called_once_with("http://127.0.0.1:5002")
    service.notify.assert_not_called()
    service.spawn.assert_not_called()


@pytest.mark.parametrize("options", [[], ["--attach"]])
@pytest.mark.parametrize("difference", ["JMCONSOLE_HOME", "JMCONSOLE_DOWNLOAD_DIR", "JMCONSOLE_CACHE_DIR",
                                         "JMCONSOLE_BACKUP_DIR", "nonce", "legacy"])
def test_copied_record_storage_mismatch_or_legacy_cannot_attach_or_stop(running_service, tmp_path, monkeypatch,
                                                                       options, difference):
    import launcher
    from core.path_utils import get_path_config
    service = running_service

    def local(port, endpoint):
        health = service.local(port, endpoint)
        if difference == "legacy":
            del health["instance_id"]
        elif difference == "nonce":
            health["instance_id"] = service.paths.instance_id("b" * 32)
        else:
            other = service.paths.as_environment()
            other[difference] = str(tmp_path / "另一个")
            health["instance_id"] = get_path_config(other).instance_id(service.record["launch_token"])
        return health

    monkeypatch.setattr(launcher, "_local_json", local)
    with pytest.raises(RuntimeError, match="原启动窗口"):
        launcher.main(options)
    service.browser.assert_not_called()
    service.notify.assert_not_called()
    service.spawn.assert_not_called()


def test_attach_only_opens_verified_service_without_preflight_or_ownership(running_service, monkeypatch):
    import launcher
    from core import database
    service = running_service
    monkeypatch.setattr(database, "_existing_schema", Mock(side_effect=AssertionError("must not inspect a newer DB")))
    monkeypatch.setattr(launcher, "server_outdated", Mock(side_effect=AssertionError("must not upgrade")))
    assert launcher.main(["--attach"]) == 0
    service.browser.assert_called_once_with("http://127.0.0.1:5002")
    launcher.FLASK_JSON.unlink()
    with pytest.raises(RuntimeError, match="没有可核实"):
        launcher.main(["--attach"])
    service.notify.assert_not_called()
    service.spawn.assert_not_called()
    service.cleanup.assert_not_called()


@pytest.mark.skipif(os.name != "nt", reason="Windows shutdown events")
def test_different_shutdown_event_cannot_receive_the_signal(running_service, capsys):
    import launcher
    service = running_service
    launcher.FLASK_JSON.write_text(json.dumps(dict(service.record, shutdown_token="b" * 32)))
    assert launcher.main([]) == 0
    assert "无法核实停机通道" in capsys.readouterr().out
    service.browser.assert_called_once_with("http://127.0.0.1:5002")
    service.notify.assert_not_called()
    service.spawn.assert_not_called()


def test_health_identity_and_optional_counts_never_disclose_paths_jobs_or_tokens(running_service):
    service = running_service
    health = service.local(5002, "/api/system/health")
    assert health["instance_id"] == service.paths.instance_id(service.record["launch_token"])
    assert "jobs" not in health
    detail = service.local(5002, "/api/system/health?activity=1")
    assert detail["jobs"] == dict(queued=0, running=0, paused=0, executors=0)
    for value in (service.record["launch_token"], str(service.paths.home)):
        assert value not in str(health) and value not in str(detail)
