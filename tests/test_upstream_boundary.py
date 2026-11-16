"""Failure contracts: no network, no dependency on private response content."""
import json
import threading
from types import SimpleNamespace

import pytest
from jmcomic import (
    JsonResolveFailException, MissingAlbumPhotoException,
    RequestRetryAllFailException, ResponseUnexpectedException,
)

from core.upstream import UpstreamFailure, UpstreamTimeout, classify_failure


def http_error(status):
    return ResponseUnexpectedException("private response and http://user:secret@proxy.invalid", {
        "resp": SimpleNamespace(status_code=status),
    })


@pytest.mark.parametrize("error,kind,status,upstream", [
    (http_error(403), "access_denied", 502, 403),
    (http_error(404), "http_error", 502, 404),
    (http_error(500), "http_error", 502, 500),
    (JsonResolveFailException("private HTML", {}), "invalid_response", 502, None),
    (json.JSONDecodeError("secret", "<html>", 0), "invalid_response", 502, None),
    (MissingAlbumPhotoException("secret", {}), "not_found", 404, None),
    (ConnectionError("secret"), "connection", 502, None),
    (TimeoutError("secret"), "timeout", 504, None),
])
def test_classification_preserves_failure_kind_without_exposing_content(error, kind, status, upstream):
    failure = classify_failure(error)
    assert (failure.kind, failure.http_status, failure.upstream_status) == (kind, status, upstream)
    payload = failure.payload()
    assert payload["error_kind"] == kind
    assert "secret" not in json.dumps(payload)
    assert "<html>" not in json.dumps(payload)
    assert isinstance(failure, TimeoutError) == (kind == "timeout")


@pytest.mark.parametrize("code,kind", [(28, "timeout"), (35, "tls"), (60, "tls"), (6, "connection")])
def test_transport_codes_do_not_require_sdk_message_parsing(code, kind):
    error = Exception("a private transport failure")
    error.code = code
    assert classify_failure(error).kind == kind


def test_mixed_retry_causes_are_not_disguised_as_a_timeout():
    error = RequestRetryAllFailException("all nodes failed", {"retry_errors": [
        {"error": TimeoutError()}, {"error": http_error(500)},
    ]})
    result = classify_failure(error)
    assert result.kind == "http_error"
    assert result.http_status == 502 and result.upstream_status == 500


def test_only_timeouts_across_nodes_remain_a_timeout():
    error = RequestRetryAllFailException("all nodes failed", {"retry_errors": [
        {"error": TimeoutError()}, {"error": TimeoutError()},
    ]})
    assert isinstance(classify_failure(error), UpstreamTimeout)


def test_retry_context_cycles_are_bounded():
    error = RequestRetryAllFailException("cycle", {"retry_errors": []})
    error.context["retry_errors"].append({"error": error})
    assert classify_failure(error).kind == "upstream_error"


def test_remote_call_keeps_the_original_failure_chain(client):
    from core import jm_service
    error = http_error(403)
    def fail():
        raise error
    with pytest.raises(UpstreamFailure) as caught:
        jm_service._call_with_timeout("test", 1, "timed out", fail)
    assert caught.value.kind == "access_denied"
    assert caught.value.__cause__ is error
    assert not isinstance(caught.value, TimeoutError)


def test_wait_budget_does_not_wait_for_the_worker_to_finish(client):
    from core import jm_service
    release, finished = threading.Event(), threading.Event()
    def slow():
        try:
            assert release.wait(3)
        finally:
            finished.set()
    try:
        with pytest.raises(UpstreamTimeout, match="bounded wait"):
            jm_service._call_with_timeout("test", 0.01, "bounded wait", slow)
        assert not finished.is_set()
    finally:
        release.set()
        assert finished.wait(3)


@pytest.mark.parametrize("error,expected_status,kind", [
    (http_error(403), 502, "access_denied"),
    (http_error(500), 502, "http_error"),
    (JsonResolveFailException("private HTML", {}), 502, "invalid_response"),
    (TimeoutError(), 504, "timeout"),
])
@pytest.mark.parametrize("endpoint", ["search", "detail", "online"])
def test_http_routes_use_structured_failure_contract(client, monkeypatch, error, expected_status, kind, endpoint):
    from routes import api_search, api_album, api_online
    failure = classify_failure(error)
    def fail(*args, **kwargs):
        raise failure
    if endpoint == "search":
        monkeypatch.setattr(api_search, "search_albums", fail)
        url = "/api/search?q=fixture"
    elif endpoint == "detail":
        monkeypatch.setattr(api_album, "get_album_detail_cached", fail)
        url = "/api/album/123"
    else:
        monkeypatch.setattr(api_online.online_reader, "album_pages", fail)
        url = "/api/online/123"
    response = client.get(url)
    assert response.status_code == expected_status
    # The legacy online timeout response still supplies its safe message/status.
    if not (endpoint == "online" and kind == "timeout"):
        assert response.json["error_kind"] == kind
    assert "secret" not in response.get_data(as_text=True)


def test_local_error_with_timeout_word_is_not_reported_as_upstream_timeout(client, monkeypatch):
    from routes import api_search
    def fail(*args, **kwargs):
        raise RuntimeError("local connection timeout handling failed")
    monkeypatch.setattr(api_search, "search_albums", fail)
    response = client.get("/api/search?q=fixture")
    assert response.status_code == 500
    assert "error_kind" not in response.json


def test_diagnostics_tolerate_removed_legacy_domain_without_discovery(client, monkeypatch):
    from jmcomic import JmModuleConfig
    from core.settings import update_settings
    from routes import api_system
    from unittest.mock import Mock
    update_settings({"client_type": "html"})
    monkeypatch.setattr(JmModuleConfig, "DOMAIN_HTML_LIST", None)
    monkeypatch.delattr(JmModuleConfig, "DOMAIN_HTML", raising=False)
    discovery = Mock(side_effect=AssertionError("diagnostics must not discover domains"))
    connect = Mock(side_effect=AssertionError("there is no known domain to probe"))
    monkeypatch.setattr(JmModuleConfig, "get_html_domain", discovery)
    monkeypatch.setattr(api_system.socket, "create_connection", connect)
    response = client.get("/api/system/diagnose")
    assert response.status_code == 200
    assert response.json["jmcomic"]["domain"] == ""
    assert response.json["jmcomic"]["reachable"] is False
    discovery.assert_not_called()
    connect.assert_not_called()
