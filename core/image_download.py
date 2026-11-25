"""Bound one image operation across SDK requests and alternate SDK image hosts.

The SDK still owns headers, response validation, decryption and file conversion.
Only independently owned clients enter this scope; shared sessions are not edited.
"""
from __future__ import annotations

import errno
import threading
import time
from urllib.parse import urlsplit, urlunsplit

from .sdk_adapter import image_domains
from .transfer import download_budget, DownloadInterrupted

_host_lock = threading.Lock()
_failed_hosts = {}
_good_host = None
_COOLDOWN = 60.0


def local_write_error(error):
    """Only concrete filesystem failures, never a network OSError/timeout."""
    return isinstance(error, OSError) and (
        error.errno in (errno.ENOSPC, errno.EDQUOT, errno.EROFS, errno.EACCES, errno.EPERM,
                        errno.ENAMETOOLONG, errno.ENOTDIR, errno.EISDIR, errno.EFBIG)
        or getattr(error, 'winerror', None) in (5, 32, 33, 39, 112, 206))


def non_retryable_error(error):
    """Local write failures and fatal process errors will not recover by switching CDN."""
    return local_write_error(error) or isinstance(error, (RecursionError, MemoryError))


def failure_detail(error):
    from PIL import UnidentifiedImageError
    from .logger import sanitize
    from .upstream import known_failure

    if local_write_error(error):
        kind = 'local_file'
    elif isinstance(error, UnidentifiedImageError):
        kind = 'invalid_response'
    else:
        failure = known_failure(error)
        kind = failure.kind if failure else 'page_error'
    return f'[{kind}] {sanitize(str(error))}'


def _hosts(url):
    parts = urlsplit(url)
    # SDK metadata provides image URLs. Never turn malformed/relative paths into
    # a different endpoint; alternate only scheme+host and preserve path/query.
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("图片地址无效")
    candidates = list(dict.fromkeys([parts.netloc, *image_domains()]))
    now = time.monotonic()
    with _host_lock:
        if _good_host in candidates:
            candidates.remove(_good_host)
            candidates.insert(0, _good_host)
        failed = {host for host, at in _failed_hosts.items() if now - at < _COOLDOWN}
    return parts, [host for host in candidates if host not in failed] + [host for host in candidates if host in failed]


def reset_hosts() -> None:
    """Clear transient CDN failure cooldowns when network/proxy settings change."""
    global _good_host
    with _host_lock:
        _failed_hosts.clear()
        _good_host = None


def _result(host, success):
    global _good_host
    with _host_lock:
        now = time.monotonic()
        # Domain lists change across SDK upgrades; keep the cache bounded.
        for old in [host for host, at in _failed_hosts.items() if now - at >= _COOLDOWN]:
            _failed_hosts.pop(old, None)
        if success:
            _failed_hosts.pop(host, None)
            _good_host = host
        else:
            _failed_hosts[host] = now
            if _good_host == host:
                _good_host = None


def download_image(client, *, img_url, img_save_path, scramble_id, decode_image,
                   timeout, attempts, stop_event=None, should_cancel=None):
    """Use at most ``attempts`` nodes within one shared wall-clock network budget.

    Disable the old SDK's per-node retry multiplication only for this operation.
    Both this selection and transport overrides are restored before the owner
    closes its client. No background request is detached after a timeout.
    """
    parts, candidates = _hosts(img_url)
    original_retries = getattr(client, "retry_times", None)
    has_retries = hasattr(client, "retry_times")
    if has_retries:
        client.retry_times = 0
    last_error = None
    try:
        with download_budget(client, timeout, stop_event=stop_event, should_cancel=should_cancel) as budget:
            for host in candidates[:max(1, min(int(attempts), len(candidates)))]:
                budget.check()
                current = urlunsplit(parts._replace(netloc=host))
                try:
                    result = client.download_image(img_url=current, img_save_path=img_save_path,
                                                   scramble_id=scramble_id, decode_image=decode_image)
                    budget.check()
                except DownloadInterrupted:
                    raise
                except Exception as error:
                    if non_retryable_error(error):
                        raise  # Changing CDN cannot fix local disk, config, or recursion failures.
                    last_error = error
                    _result(host, False)
                    continue
                _result(host, True)
                return result
            budget.check()
            if last_error is not None:
                raise last_error
            raise IOError("没有可用的图片域名")
    finally:
        if has_retries:
            client.retry_times = original_retries
