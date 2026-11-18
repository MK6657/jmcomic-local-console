"""Offline output and filesystem contracts; all secrets and paths are fixtures."""
import json
import logging
from unittest.mock import Mock

import pytest


def test_clipped_transport_record_drops_partial_userinfo(monkeypatch):
    from core import logger
    prefix = "transfer failed http://fixture-user:123"
    monkeypatch.setattr(logger, "EXTERNAL_MESSAGE_MAX_CHARS", len(prefix))
    record = logging.makeLogRecord({"msg": prefix + "456@proxy.invalid/"})
    logger._clean_external(record)
    assert "transfer failed" in record.msg and "已截断" in record.msg
    assert "fixture-user" not in record.msg and "123" not in record.msg


def test_same_failure_is_safe_in_logs_database_jobs_and_sse(client):
    from core import database, logger
    from core.progress import progress_manager
    from core.task_commands import finish_download

    error = ("TLS failed via socks5h://fixture-user:fixture-pass@proxy.invalid:1080\n"
             "Authorization: Bearer fixture-auth\nCookie: pref=on; sid=fixture-cookie\n"
             "Set-Cookie: sid=fixture-set; Path=/\nBasic fixture-basic")
    fields = {"count": 3, "errors": [error, {"proxy-Authorization": "fixture-header"}, ("Set-Cookie", "sid=fixture-sid")],
              "token": {"opaque": "fixture-token"}}
    record = logging.makeLogRecord({"msg": error, "log_extra": fields, "exc_text": error, "stack_info": error})
    formatted = logger.StructuredFormatter().format(record)
    try:
        raise ValueError(error)
    except ValueError:
        logger.log.exception("telemetry-boundary-check", extra=fields)
    entries = client.get("/api/system/logs?q=telemetry-boundary-check").json["entries"]
    entry = next(item for item in entries if item["message"].startswith("telemetry-boundary-check"))
    assert entry["extra"]["count"] == 3 and "Traceback" in entry["message"]

    job = "job_telemetry"
    database.insert_job(job, "123", "Fixture", [])
    assert database.transition_job_status(job, ["queued"], "running")
    database.update_job(job, error_message=error)
    assert 'fixture-' not in database.get_job(job)['error_message']
    tracker = progress_manager.create_tracker(job)
    try:
        with client.get(f"/api/jobs/{job}/events", buffered=False) as response:
            assert response.status_code == 200
            assert finish_download(job, "failed", error_message=error)
            tracker.push("failed", {"job_id": job, "error_message": error, **fields})
            tracker.mark_done()
            stream = response.get_data(as_text=True)
    finally:
        progress_manager.remove_tracker(job)

    stored = database.get_job(job)["error_message"]
    detail = client.get(f"/api/jobs/{job}").json["job"]["error_message"]
    jobs = client.get("/api/jobs").get_data(as_text=True)
    event = json.loads(next(line[6:] for line in stream.splitlines() if line.startswith("data: ")))
    assert event["count"] == 3 and event["error_message"] == detail == stored
    assert fields["token"] == {"opaque": "fixture-token"}
    for output in (formatted, json.dumps(entry), stored, jobs, stream):
        assert "TLS failed" in output and "proxy.invalid" in output
        assert "fixture-" not in output


@pytest.mark.parametrize("domain,proxy,endpoint", [
    ("mirror.invalid:8443", None, ("mirror.invalid", 8443)),
    ("http://[::1]:8181/path", None, ("::1", 8181)),
    ("http://mirror.invalid/path", None, ("mirror.invalid", 80)),
    ("mirror.invalid:443", "socks5h://fixture-user:fixture-pass@proxy.invalid", ("proxy.invalid", 1080)),
    ("mirror.invalid", "http://fixture-user:fixture-pass@proxy.invalid:8123", ("proxy.invalid", 8123)),
    ("mirror.invalid", "http://proxy.invalid", ("proxy.invalid", 1080)),
])
def test_diagnostics_checks_only_the_selected_tcp_endpoint(client, monkeypatch, domain, proxy, endpoint):
    from routes import api_system
    config = {"impl": "api", "domain": {"api": [domain]}, "postman": {"meta_data": {}}}
    if proxy:
        config["postman"]["meta_data"]["proxies"] = {"https": proxy}
    monkeypatch.setattr(api_system, "build_jmcomic_option", lambda: {"client": config})
    connect = Mock()
    monkeypatch.setattr(api_system.socket, "create_connection", connect)
    for success in (True, False):
        connect.reset_mock()
        connect.side_effect = None if success else OSError("fixture-pass")
        response = client.get("/api/system/diagnose")
        assert response.status_code == 200
        connect.assert_called_once_with(endpoint, timeout=3)
        if success:
            connect.return_value.close.assert_called_once()
        data = response.json["jmcomic"]
        assert "fixture-" not in response.get_data(as_text=True)
        assert data["tcp_target"] == ("proxy" if proxy else "domain")
        assert data["reachable"] is (None if proxy else success)
        assert data["proxy_reachable"] is (success if proxy else None)


@pytest.mark.parametrize("name,limit,expected", [
    ("   CON.pdf ", 200, "_CON.pdf"), ("NUL   appendix", 5, "_NUL"),
    ("LPT³.zip", 200, "_LPT³.zip"), ("com¹ .cbz", 200, "_com¹ .cbz"),
    ("a\x00b\x1fc\x7fd", 200, "a_b_c_d"), (".hack", 200, ".hack"),
    ("正常📚标题", 5, "正常📚标"),
])
def test_final_windows_component_is_safe(tmp_path, name, limit, expected):
    from core.validation import safe_dirname, safe_filename
    assert safe_filename(name, limit) == safe_dirname(name, limit) == expected
    target = tmp_path / expected
    target.write_bytes(b"ok")
    assert target.read_bytes() == b"ok"


@pytest.mark.parametrize("job_id", [None, 123, "job_1\n", "", "x" * 65, "job-1"])
def test_job_validation_uses_the_entire_string(job_id):
    from core.validation import require_job_id, validate_job_id
    assert (require_job_id(job_id) is None) == validate_job_id(job_id) == (job_id == "job-1")
