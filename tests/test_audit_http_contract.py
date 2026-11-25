"""HTTP audit contracts, using only the disposable offline app fixtures."""
import io
from unittest.mock import Mock

import pytest
from flask import abort
from werkzeug.exceptions import default_exceptions


def assert_security_headers(response):
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Content-Security-Policy"] == "frame-ancestors 'none'"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def assert_json_error(response, status):
    assert response.status_code == status
    assert response.mimetype == "application/json"
    payload = response.get_json()
    assert payload["status"] == "error"
    assert isinstance(payload["message"], str) and payload["message"]


@pytest.mark.parametrize("route", [
    "/api/jobs", "/api/settings", "/api/wishlist", "/api/wishlist/check",
])
@pytest.mark.parametrize("content_type", [None, "application/json"])
def test_empty_post_returns_json_400(client, route, content_type):
    response = client.post(route, data=b"", content_type=content_type)
    assert_json_error(response, 400)


@pytest.mark.parametrize("body", ["{bad", "null", "[]"])
def test_invalid_json_returns_json_400(client, body):
    assert_json_error(client.post("/api/jobs", data=body, content_type="application/json"), 400)


def test_unsupported_request_media_returns_json_415(client):
    response = client.post("/api/jobs", data='{"album_id":"123"}', content_type="text/plain")
    assert_json_error(response, 415)


@pytest.mark.parametrize("multipart", [False, True])
def test_oversize_json_and_upload_return_json_413(client, monkeypatch, multipart):
    monkeypatch.setitem(client.application.config, "MAX_CONTENT_LENGTH", 64)
    if multipart:
        response = client.post("/api/settings/import", data={
            "file": (io.BytesIO(b"x" * 65), "settings.json"),
        })
    else:
        response = client.post("/api/jobs", json={"title": "x" * 65})
    assert_json_error(response, 413)
    assert_security_headers(response)


@pytest.mark.parametrize("status", [400, 413, 415])
def test_framework_errors_are_json_only_on_api_paths(client, monkeypatch, status):
    def fail():
        abort(status, description="private parser detail")

    monkeypatch.setitem(client.application.view_functions, "api_jobs.create_job", fail)
    response = client.post("/api/jobs")
    assert_json_error(response, status)
    assert "private parser detail" not in response.get_data(as_text=True)
    assert_security_headers(response)

    monkeypatch.setitem(client.application.view_functions, "page.index", fail)
    response = client.get("/")
    expected = default_exceptions[status](description="private parser detail").get_response()
    assert response.status_code == status
    assert response.mimetype == "text/html"
    assert response.data == expected.data
    assert_security_headers(response)


@pytest.mark.parametrize("path,status", [
    ("/", 200), ("/api/system/health", 200),
    ("/missing-audit-page", 404), ("/api/missing-audit-route", 404),
    ("/preview/not-an-id", 400),
])
def test_security_headers_cover_pages_api_and_errors(client, path, status):
    response = client.get(path)
    assert response.status_code == status
    assert_security_headers(response)


def test_static_success_and_conditional_response_have_security_headers(client):
    response = client.get("/static/css/style.css")
    try:
        assert response.status_code == 200
        assert_security_headers(response)
        etag = response.headers["ETag"]
    finally:
        response.close()
    response = client.get("/static/css/style.css", headers={"If-None-Match": etag})
    try:
        assert response.status_code == 304
        assert_security_headers(response)
    finally:
        response.close()


def test_shutdown_early_response_has_security_headers(client, monkeypatch):
    monkeypatch.setitem(client.application.config, "SHUTTING_DOWN", True)
    response = client.get("/api/system/health")
    assert_json_error(response, 503)
    assert response.headers["Retry-After"] == "1"
    assert_security_headers(response)


@pytest.mark.parametrize("headers", [
    {"Origin": "https://foreign.example"}, {"Origin": "null"},
    {"Host": "foreign.example"}, {"Sec-Fetch-Site": "cross-site"},
])
@pytest.mark.parametrize("path", ["/api/settings", "/", "/static/css/style.css"])
def test_cross_site_requests_remain_denied_with_security_headers(client, headers, path):
    response = client.get(path, headers=headers)
    assert_json_error(response, 403)
    assert_security_headers(response)


class IndependentFutureTimeout(Exception):
    """Model Python versions where futures.TimeoutError is not the builtin."""


@pytest.fixture
def independent_future_timeout(monkeypatch):
    from core import upstream

    assert not issubclass(IndependentFutureTimeout, TimeoutError)
    monkeypatch.setattr(upstream, "FuturesTimeout", IndependentFutureTimeout, raising=False)
    return IndependentFutureTimeout("private executor detail")


@pytest.mark.parametrize("classifier", ["classify_failure", "known_failure"])
def test_independent_future_timeout_preserves_classification(independent_future_timeout, classifier):
    from core import upstream

    failure = getattr(upstream, classifier)(independent_future_timeout)
    assert isinstance(failure, upstream.UpstreamTimeout)
    assert (failure.kind, failure.http_status) == ("timeout", 504)
    assert "private executor detail" not in str(failure.payload())
    assert upstream.known_failure(RuntimeError("local timeout bug")) is None


def test_independent_future_timeout_uses_tag_sync_failure_contract(
        client, monkeypatch, independent_future_timeout):
    from core import upstream
    from routes import api_library

    future = Mock()
    future.result.side_effect = independent_future_timeout
    submit = Mock(return_value=future)
    monkeypatch.setattr(api_library.tag_jobs, "submit", submit)
    lookup = Mock(side_effect=AssertionError("fake executor must not perform a lookup"))
    monkeypatch.setattr(api_library, "get_album_detail_cached", lookup)

    response = client.post("/api/library/123/tags/sync")
    assert_json_error(response, 504)
    assert response.get_json() == upstream.classify_failure(independent_future_timeout).payload()
    future.result.assert_called_once_with(timeout=20)
    submit.assert_called_once_with(lookup, "123")
    lookup.assert_not_called()


@pytest.mark.parametrize("route", [
    "/api/wishlist",
    "/api/wishlist/batch-delete",
    "/api/wishlist/import",
    "/api/wishlist/download",
])
@pytest.mark.parametrize("body", ["[]", '"scalar"', "123", "{bad"])
def test_wishlist_json_routes_reject_non_object_or_malformed_json_with_400(client, route, body):
    response = client.post(route, data=body, content_type="application/json")
    assert_json_error(response, 400)


def test_deeply_nested_json_returns_400_without_500_recursion_error(client):
    nested = "[" * 2000 + "]" * 2000
    response = client.post("/api/jobs", data=nested, content_type="application/json")
    assert_json_error(response, 400)


@pytest.mark.parametrize("route", ["/api/jobs", "/api/settings", "/api/wishlist"])
def test_multipart_form_data_rejected_on_non_upload_api_routes(client, route):
    response = client.post(route, data={"file": (io.BytesIO(b"{}"), "x.json")})
    assert_json_error(response, 415)


def test_tag_cloud_huge_min_count_clamps_without_sqlite_overflow(client):
    response = client.get("/api/library/tags?min_count=99999999999999999999999999")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "ok" and payload["tags"] == []


def test_export_safe_name_clamps_length_and_reserved_windows_names():
    from routes.api_export import _safe_export_name

    long_name = _safe_export_name("漫" * 300)
    assert len(long_name) <= 200 and not long_name.endswith((" ", "."))
    assert _safe_export_name("CON").upper() != "CON"
    assert _safe_export_name("...") == "export"

