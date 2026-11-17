"""Bound one image operation across SDK retries without detaching HTTP workers.

Only use this context with an exclusively owned client in its calling thread.
The actual synchronous curl request receives the remaining total budget; TCP/TLS
connect and stalled-transfer limits are applied through public session options.
Stop/cancel is checked between requests. A request already in progress may take
the remaining budget to return; this module never closes its handle concurrently.
"""
from contextlib import contextmanager
import math
import threading
import time

from .sdk_adapter import transport_and_session

_clock = time.monotonic
_MISSING = object()
_ACTIVE_ATTRIBUTE = "_jm_transfer_budget_active"
CONNECT_TIMEOUT = 4.0
STALL_SECONDS = 8
STALL_BYTES_PER_SECOND = 1024
MAX_IMAGE_BUDGET = 120


class TransferTimeout(TimeoutError):
    """The single image exhausted its transfer budget or stalled."""

    def __init__(self, reason="deadline"):
        super().__init__("图片传输超时或停滞，请稍后重试")
        self.reason = reason


class DownloadInterrupted(Exception):
    """Explicit shutdown/cancellation, distinct from a failed upstream image."""

    def __init__(self, reason="canceled"):
        super().__init__("下载已因停机或取消操作中断")
        self.reason = reason


class TransferConfigurationError(RuntimeError):
    def __init__(self):
        super().__init__("当前下载客户端不支持受控传输，请检查客户端配置")


class TransferBudget:
    def __init__(self, timeout, stop_event=None, should_cancel=None):
        self.deadline = _clock() + timeout
        self.stop_event = stop_event
        self.should_cancel = should_cancel
        self.thread = threading.get_ident()

    def check(self) -> float:
        if threading.get_ident() != self.thread:
            raise TransferConfigurationError()
        if self.stop_event is not None and self.stop_event.is_set():
            raise DownloadInterrupted("stopped")
        if self.should_cancel is not None:
            try:
                canceled = self.should_cancel()
            except Exception:
                raise TransferConfigurationError() from None
            if canceled:
                raise DownloadInterrupted("canceled")
        remaining = self.deadline - _clock()
        # A sub-millisecond curl timeout truncates to zero (unlimited). Never
        # start a request with so little time that it disables libcurl's limit.
        if remaining < 0.001:
            raise TransferTimeout()
        return remaining


def _restore_attribute(owner, name, previous):
    if previous is _MISSING:
        owner.__dict__.pop(name, None)
    else:
        setattr(owner, name, previous)


@contextmanager
def download_budget(client, timeout, *, stop_event=None, should_cancel=None):
    """Yield one deadline shared by all GET/POST attempts made inside this scope.

    This uses curl_cffi 0.17's public ``Session.curl_options``; synchronous
    ``Session.request`` does not accept a curl_options keyword. Its scalar timeout
    sets TIMEOUT_MS, and explicit options are applied after request defaults. The
    session option object, retry policy and transport methods are restored exactly.
    Session-level automatic retries are disabled here so SDK/application retry
    decisions always pass through the same remaining-budget gate.
    """
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or not 0 < timeout <= MAX_IMAGE_BUDGET):
        raise ValueError("图片传输预算必须大于零且不超过120秒")
    if client is None or getattr(client, "_jm_downloader_shared", False):
        raise TransferConfigurationError()
    try:
        root, session = transport_and_session(client)
        original_options = session.curl_options
        methods = {method: getattr(root, method) for method in ("get", "post")}
    except AttributeError:
        raise TransferConfigurationError() from None
    if not isinstance(getattr(root, "__dict__", None), dict) or getattr(root, _ACTIVE_ATTRIBUTE, False):
        raise TransferConfigurationError()
    from curl_cffi import CurlOpt
    from curl_cffi.requests import RetryStrategy
    budget = TransferBudget(float(timeout), stop_event, should_cancel)
    originals = {method: root.__dict__.get(method, _MISSING) for method in methods}
    old_retry = getattr(session, "retry", _MISSING)
    last_transport_timeout = False

    def bounded_request(method):
        def request(url, *args, **kwargs):
            nonlocal last_transport_timeout
            remaining = budget.check()
            kwargs = dict(kwargs)
            kwargs["timeout"] = remaining
            # SDK image methods consume the body synchronously. Streaming would
            # return before the body completes and move work to another executor.
            kwargs["stream"] = False
            options = dict(original_options or {})
            options.update({
                CurlOpt.TIMEOUT_MS: max(1, int(remaining * 1000)),
                CurlOpt.CONNECTTIMEOUT_MS: max(1, int(min(CONNECT_TIMEOUT, remaining) * 1000)),
                CurlOpt.LOW_SPEED_LIMIT: STALL_BYTES_PER_SECOND,
                CurlOpt.LOW_SPEED_TIME: min(STALL_SECONDS, max(1, math.ceil(remaining))),
            })
            session.curl_options = options
            last_transport_timeout = False
            try:
                result = methods[method](url, *args, **kwargs)
            except Exception as error:
                last_transport_timeout = isinstance(error, TimeoutError) or getattr(error, "code", None) == 28
                budget.check()  # Preserve explicit cancellation/deadline over SDK wrapping.
                raise
            budget.check()
            return result
        return request

    setattr(root, _ACTIVE_ATTRIBUTE, True)
    try:
        budget.check()
        if old_retry is not _MISSING:
            session.retry = RetryStrategy(count=0)
        for method in methods:
            setattr(root, method, bounded_request(method))
        try:
            yield budget
        except Exception as error:
            budget.check()
            if last_transport_timeout and not isinstance(error, DownloadInterrupted):
                raise TransferTimeout("transport") from error
            raise
        budget.check()
    finally:
        for method, previous in originals.items():
            _restore_attribute(root, method, previous)
        session.curl_options = original_options
        if old_retry is not _MISSING:
            session.retry = old_retry
        root.__dict__.pop(_ACTIVE_ATTRIBUTE, None)
