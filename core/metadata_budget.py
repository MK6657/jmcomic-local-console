"""Per-call deadlines on a shared SDK transport without mutable call options.

Transport methods and fixed session limits are installed before a shared client
is published. Each worker supplies only its own thread-local deadline; parallel
requests never replace each other's curl_options, timeout or retry policy.
"""
from contextlib import contextmanager
import math
import threading
import time

from .sdk_adapter import transport_and_session

_clock = time.monotonic
_context = threading.local()


class MetadataStopped(RuntimeError):
    pass


def deadline_after(timeout):
    now = _clock()
    value = now + timeout
    # Adding a fractional limit to a large Windows uptime can round upward.
    # A first request on the same clock tick must still fit its configured limit.
    if value - now > timeout:
        value = math.nextafter(value, now)
    operation = getattr(_context, "operation", None)
    return min(value, operation[0]) if operation is not None else value


def check_current():
    operation = getattr(_context, "operation", None)
    if operation is None:
        raise RuntimeError("元数据操作缺少调用预算")
    return remaining(operation[0], *operation[1])


@contextmanager
def lock_scope(lock):
    """Wait for local cache locks within the same request budget and stop gate."""
    while not lock.acquire(timeout=min(0.05, check_current())):
        pass
    try:
        check_current()
        yield
    finally:
        lock.release()


def remaining(deadline, *events):
    if any(event is not None and event.is_set() for event in events):
        raise MetadataStopped("元数据服务正在停止，请稍后重试")
    value = deadline - _clock()
    if value < 0.001:
        raise TimeoutError("元数据请求超过总时间限制")
    return value


@contextmanager
def call_scope(deadline, *events):
    previous = getattr(_context, "operation", None)
    if previous is not None:
        deadline = min(deadline, previous[0])
        events = (*events, *previous[1])
    _context.operation = (deadline, events)
    try:
        remaining(deadline, *events)
        try:
            yield
        except Exception:
            remaining(deadline, *events)
            raise
        remaining(deadline, *events)
    finally:
        _context.operation = previous


def install(client):
    """Configure a newly constructed, unpublished metadata-only shared client."""
    from curl_cffi import CurlOpt
    from curl_cffi.requests import RetryStrategy
    root, session = transport_and_session(client)
    options = dict(session.curl_options or {})
    # TIMEOUT_MS must come from each request's remaining deadline, not a
    # process-wide session value that would override the scalar timeout.
    options.pop(CurlOpt.TIMEOUT, None)
    options.pop(CurlOpt.TIMEOUT_MS, None)
    options.update({CurlOpt.CONNECTTIMEOUT_MS: 4000, CurlOpt.LOW_SPEED_LIMIT: 1024,
                    CurlOpt.LOW_SPEED_TIME: 8})
    session.curl_options = options
    session.retry = RetryStrategy(count=0)

    def bounded(method):
        def request(url, *args, **kwargs):
            operation = getattr(_context, "operation", None)
            if operation is None:
                raise RuntimeError("共享元数据请求缺少调用预算")
            deadline, events = operation
            left = remaining(deadline, *events)
            requested = kwargs.get("timeout")
            if isinstance(requested, (int, float)) and not isinstance(requested, bool):
                if math.isfinite(requested) and requested > 0:
                    left = min(left, requested)
            if left < 0.001:
                raise TimeoutError("元数据请求超过总时间限制")
            kwargs = dict(kwargs, timeout=left, stream=False)
            try:
                result = method(url, *args, **kwargs)
            except Exception:
                remaining(deadline, *events)
                raise
            remaining(deadline, *events)
            return result
        return request

    for name in ("get", "post"):
        setattr(root, name, bounded(getattr(root, name)))
