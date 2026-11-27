"""Bounded ownership of tag lookups and background library writes."""
from concurrent.futures import Future
import threading
import time

from .background_calls import BackgroundCallsBusy, BackgroundCallsStopped
from .logger import bind_request_id


class BulkSyncActive(BackgroundCallsBusy):
    pass


class TagJobs:
    def __init__(self, capacity=4):
        self._capacity = capacity
        self._lock = threading.Lock()
        self._threads = {}
        self._stopped = threading.Event()

    def _reap(self):
        self._threads = {key: worker for key, worker in self._threads.items() if worker.is_alive()}

    def start(self):
        with self._lock:
            if self._stopped.is_set():
                self._reap()
                if self._threads:
                    raise RuntimeError("标签同步仍在退出")
                self._stopped = threading.Event()

    def require_accepting(self):
        if self._stopped.is_set():
            raise BackgroundCallsStopped("标签同步正在停止，请稍后重试")

    def submit(self, fn, *args, bulk=False):
        future = Future()
        key = "bulk" if bulk else future

        def run():
            if not future.set_running_or_notify_cancel():
                return
            try:
                self.require_accepting()
                result = fn(*args)
            except BaseException as exc:
                future.set_exception(exc)
            else:
                future.set_result(result)

        with self._lock:
            self.require_accepting()
            self._reap()
            if key in self._threads:
                raise BulkSyncActive("已有批量同步正在运行，请勿重复提交")
            if len(self._threads) >= self._capacity:
                raise BackgroundCallsBusy("标签同步繁忙，请稍后重试")
            worker = threading.Thread(target=bind_request_id(run), name="library-tag-sync", daemon=True)
            self._threads[key] = worker
            worker.start()
        return future

    def active_count(self) -> int:
        with self._lock:
            self._reap()
            return len(self._threads)

    def request_stop(self):
        with self._lock:
            self._stopped.set()

    def stop(self, timeout=5):
        deadline = time.monotonic() + max(0, timeout)
        self.request_stop()
        with self._lock:
            self._reap()
            workers = tuple(self._threads.values())
        for worker in workers:
            if worker is not threading.current_thread():
                worker.join(max(0, deadline - time.monotonic()))
        with self._lock:
            self._reap()
            return not self._threads


tag_jobs = TagJobs()
