"""Admission for background image work, shared by every chapter and both download passes.

image_threads (1–50) limits downloads across the process; the reader has separate
slots for four foreground fetches and two prefetches. Lowering the setting drains
existing owners normally.
"""
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from .settings import get_settings

MAX_DOWNLOAD_IMAGES = 50
_available = threading.Condition()
_active = 0


def image_limit():
    return max(1, min(int(get_settings()["image_threads"]), MAX_DOWNLOAD_IMAGES))


def _acquire(should_stop):
    global _active
    while not should_stop():
        limit = image_limit()
        with _available:
            if _active < limit:
                _active += 1
                return True
            _available.wait(0.15)
    return False


def _release(_future=None):
    global _active
    with _available:
        _active -= 1
        _available.notify()


def run_images(items, work, *, workers, should_stop):
    """Acquire before submitting/creating a client; join every admitted writer on exit."""
    if workers == 1:
        for item in items:
            if not _acquire(should_stop):
                return
            try:
                work(item)
            finally:
                _release()
        return

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="download-image") as pool:
        pending = set()
        try:
            for item in items:
                if len(pending) == workers:
                    finished, pending = wait(pending, return_when=FIRST_COMPLETED)
                    for future in finished:
                        future.result()
                if not _acquire(should_stop):
                    return
                try:
                    future = pool.submit(work, item)
                except BaseException:
                    _release()
                    raise
                future.add_done_callback(_release)
                pending.add(future)
            for future in pending:
                future.result()
        finally:
            for future in pending:
                future.cancel()
