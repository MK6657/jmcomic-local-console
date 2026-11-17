"""SDK-facing failure boundary, independent of Flask and response bodies.

Keep SDK/transport exception details in the exception chain and logs.  Callers
receive stable failure kinds instead of depending on SDK messages or HTML.
"""
from __future__ import annotations

from concurrent.futures import TimeoutError as FuturesTimeout
import json
import ssl

from jmcomic import (
    JmcomicException, JsonResolveFailException, MissingAlbumPhotoException,
    RegularNotMatchException,
)
from requests.exceptions import (
    ConnectionError as RequestsConnectionError, HTTPError, SSLError, Timeout,
)


class UpstreamFailure(Exception):
    def __init__(self, kind: str, message: str, *, http_status: int = 502,
                 upstream_status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.http_status = http_status
        self.upstream_status = upstream_status

    def payload(self) -> dict:
        result = {"status": "error", "message": str(self), "error_kind": self.kind}
        if self.upstream_status is not None:
            result["upstream_status"] = self.upstream_status
        return result


class UpstreamTimeout(UpstreamFailure, TimeoutError):
    def __init__(self, message: str = "上游请求超时，请稍后重试"):
        super().__init__("timeout", message, http_status=504)


_PRIORITY = {
    "not_found": 0, "access_denied": 1, "http_error": 2,
    "invalid_response": 3, "tls": 4, "connection": 5,
    "upstream_error": 6, "timeout": 7,
}


def _response_status(error: Exception) -> int | None:
    context = getattr(error, "context", None)
    response = context.get("resp") if isinstance(context, dict) else None
    if response is None:
        response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) and not isinstance(status, bool) and 400 <= status <= 599 else None


def classify_failure(error: Exception, *, timeout_message: str | None = None,
                     _seen: frozenset[int] = frozenset()) -> UpstreamFailure:
    """Normalize known SDK failures, including per-domain retry causes.

    Mixed retry failures are not reported as a timeout merely because one node
    timed out.  The SDK may aggregate HTTP, TLS and parse failures in one error.
    Bound traversal to avoid malformed exception/context cycles.
    """
    if isinstance(error, UpstreamFailure):
        return error
    from .background_calls import BackgroundCallsBusy, BackgroundCallsStopped
    from .metadata_budget import MetadataStopped
    if isinstance(error, (BackgroundCallsBusy, BackgroundCallsStopped, MetadataStopped)):
        kind = "busy" if isinstance(error, BackgroundCallsBusy) else "stopping"
        return UpstreamFailure(kind, str(error), http_status=503)
    if id(error) in _seen or len(_seen) >= 8:
        return UpstreamFailure("upstream_error", "上游请求失败，请稍后重试")
    seen = _seen | {id(error)}
    context = getattr(error, "context", None)
    retries = context.get("retry_errors", []) if isinstance(context, dict) else []
    causes = [entry.get("error") for entry in retries[:32] if isinstance(entry, dict)] if isinstance(retries, list) else []
    failures = [classify_failure(cause, timeout_message=timeout_message, _seen=seen)
                for cause in causes if isinstance(cause, Exception)]
    if failures:
        return min(failures, key=lambda failure: _PRIORITY.get(failure.kind, 6))
    if isinstance(error, MissingAlbumPhotoException):
        return UpstreamFailure("not_found", "上游未找到该漫画或章节", http_status=404)
    status = _response_status(error)
    if status is not None:
        kind = "access_denied" if status in (401, 403) else "http_error"
        message = ("上游拒绝访问，请检查网络设置或稍后重试" if kind == "access_denied"
                   else f"上游服务返回异常（HTTP {status}），请稍后重试")
        return UpstreamFailure(kind, message, upstream_status=status)
    code = getattr(error, "code", None)
    if isinstance(error, (TimeoutError, FuturesTimeout, Timeout)) or code == 28:
        return UpstreamTimeout(timeout_message or "上游请求超时，请稍后重试")
    if isinstance(error, (ssl.SSLError, SSLError)) or code in (35, 51, 53, 54, 58, 59, 60, 64, 66, 77, 80, 82, 83, 90, 91):
        return UpstreamFailure("tls", "与上游建立安全连接失败，请检查网络设置或稍后重试")
    if isinstance(error, (ConnectionError, RequestsConnectionError)) or code in (5, 6, 7, 18, 52, 55, 56, 97):
        return UpstreamFailure("connection", "无法连接上游服务，请检查网络设置或稍后重试")
    if isinstance(error, (json.JSONDecodeError, JsonResolveFailException, RegularNotMatchException)):
        return UpstreamFailure("invalid_response", "上游返回了无法解析的内容，请稍后重试")
    return UpstreamFailure("upstream_error", "上游请求失败，请稍后重试")


def known_failure(error: Exception) -> UpstreamFailure | None:
    """HTTP entry points must not disguise local programming/DB errors as upstream."""
    from .background_calls import BackgroundCallsBusy, BackgroundCallsStopped
    from .metadata_budget import MetadataStopped
    if isinstance(error, (UpstreamFailure, BackgroundCallsBusy, BackgroundCallsStopped, MetadataStopped,
                          JmcomicException, TimeoutError, FuturesTimeout,
                          Timeout, ConnectionError, RequestsConnectionError, HTTPError, ssl.SSLError)):
        return classify_failure(error)
    # curl_cffi does not derive from requests.exceptions.RequestException.
    if type(error).__module__.startswith("curl_cffi."):
        return classify_failure(error)
    return None
