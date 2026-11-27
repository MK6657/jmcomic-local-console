"""
SSE 进度推送模块 — 每个 SSE 客户端独立队列，互不干扰
"""
import json
import queue
import threading
import time

from .logger import sanitize_fields

_DONE_SENTINEL = object()


class ProgressTracker:
    """单个任务的进度跟踪器（支持多客户端独立消费）"""

    def __init__(self, job_id: str):
        self.job_id = job_id
        self._subscribers: list[queue.Queue] = []
        self._lock = threading.Lock()
        self._done = False

    def subscribe(self) -> queue.Queue:
        """SSE 客户端订阅进度事件，返回独立专属队列"""
        q = queue.Queue(maxsize=500)
        with self._lock:
            if self._done:
                q.put_nowait((_DONE_SENTINEL, None))
            else:
                self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue):
        """客户端断开时取消订阅"""
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    def push(self, event: str, data: dict):
        """广播进度事件给所有订阅客户端（每个客户端独立队列，互不消费）"""
        data = sanitize_fields(data)
        with self._lock:
            for q in self._subscribers:
                try:
                    q.put_nowait((event, data))
                except queue.Full:
                    # 单个客户端消费慢时丢弃其最旧事件，不影响其他客户端
                    try:
                        for _ in range(50):
                            q.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        q.put_nowait((event, data))
                    except queue.Full:
                        pass  # 实在满就丢弃，不影响后续队列

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def is_done(self) -> bool:
        return self._done

    def mark_done(self):
        self._done = True

    def iter_events(self, client_queue: queue.Queue | None = None,
                    max_duration: float | None = None):
        """SSE 事件生成器；可限制连接寿命，不传 max_duration 时保持兼容行为。"""
        legacy_queue = client_queue is None
        if legacy_queue:
            with self._lock:
                q = self._subscribers[0] if self._subscribers else None
        else:
            q = client_queue
        if q is None:
            return

        deadline = None if max_duration is None else time.monotonic() + max_duration
        try:
            while not self._done or not q.empty():
                timeout = 2
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    timeout = min(timeout, remaining)
                try:
                    event, data = q.get(timeout=timeout)
                    if event is _DONE_SENTINEL:
                        continue
                    if deadline is not None and time.monotonic() >= deadline:
                        break
                    yield f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                except queue.Empty:
                    if deadline is not None and time.monotonic() >= deadline:
                        break
                    # 发送心跳保持连接
                    yield f": heartbeat\n\n"
        finally:
            if legacy_queue:
                self.unsubscribe(q)

    def close(self):
        with self._lock:
            self._done = True
            for q in self._subscribers:
                try:
                    q.put_nowait((_DONE_SENTINEL, None))
                except queue.Full:
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        q.put_nowait((_DONE_SENTINEL, None))
                    except queue.Full:
                        pass
            self._subscribers.clear()


class StreamCapacityError(RuntimeError):
    """HTTP SSE 连接已达到全局或单任务容量上限。"""


class StreamLease:
    """HTTP 响应持有的订阅；生成器和响应均可安全地重复关闭。"""

    def __init__(self, manager: "ProgressManager", tracker: ProgressTracker,
                 client_queue: queue.Queue):
        self.queue = client_queue
        self._manager = manager
        self._tracker = tracker
        self._job_id = tracker.job_id
        self._lock = threading.Lock()
        self._closed = False

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            self._tracker.unsubscribe(self.queue)
        finally:
            self._manager._release_stream(self._job_id)


class ProgressManager:
    """全局进度管理器，HTTP 流租约独立于 tracker 生命周期计数。"""

    def __init__(self, max_streams: int = 32, max_streams_per_job: int = 8):
        if max_streams < 1 or max_streams_per_job < 1:
            raise ValueError("Stream limits must be positive")
        self.max_streams = max_streams
        self.max_streams_per_job = max_streams_per_job
        self._lock = threading.Lock()
        self._trackers: dict[str, ProgressTracker] = {}
        self._active_streams = 0
        self._streams_by_job: dict[str, int] = {}

    def acquire_stream(self, tracker: ProgressTracker) -> StreamLease:
        """在创建 HTTP 响应前预留容量；内部 subscribe 不受此限制。"""
        job_id = tracker.job_id
        with self._lock:
            job_streams = self._streams_by_job.get(job_id, 0)
            if self._active_streams >= self.max_streams:
                raise StreamCapacityError("Global SSE stream capacity reached")
            if job_streams >= self.max_streams_per_job:
                raise StreamCapacityError("SSE stream capacity for this job reached")
            self._active_streams += 1
            self._streams_by_job[job_id] = job_streams + 1

        # 不同时持有 manager 与 tracker 锁；订阅失败必须归还预留容量。
        try:
            client_queue = tracker.subscribe()
        except BaseException:
            self._release_stream(job_id)
            raise
        return StreamLease(self, tracker, client_queue)

    def _release_stream(self, job_id: str):
        with self._lock:
            self._active_streams -= 1
            remaining = self._streams_by_job[job_id] - 1
            if remaining:
                self._streams_by_job[job_id] = remaining
            else:
                del self._streams_by_job[job_id]

    def active_stream_count(self, job_id: str | None = None) -> int:
        """返回全部或指定任务仍由 HTTP 响应持有的槽位数。"""
        with self._lock:
            if job_id is None:
                return self._active_streams
            return self._streams_by_job.get(job_id, 0)

    def stream_status(self) -> dict:
        """返回容量快照，不依赖已清理的 tracker 订阅列表。"""
        with self._lock:
            return {
                "active_streams": self._active_streams,
                "max_streams": self.max_streams,
                "max_streams_per_job": self.max_streams_per_job,
                "streams_by_job": dict(self._streams_by_job),
            }

    def create_tracker(self, job_id: str) -> ProgressTracker:
        with self._lock:
            if job_id in self._trackers:
                return self._trackers[job_id]
            tracker = ProgressTracker(job_id)
            self._trackers[job_id] = tracker
            return tracker

    def get_tracker(self, job_id: str) -> ProgressTracker | None:
        with self._lock:
            return self._trackers.get(job_id)

    def remove_tracker(self, job_id: str):
        with self._lock:
            tracker = self._trackers.pop(job_id, None)
        if tracker:
            tracker.close()

    def close_all(self):
        """End idle SSE generators during process shutdown, preserving job rows."""
        with self._lock:
            trackers = list(self._trackers.values())
            self._trackers.clear()
        for tracker in trackers:
            tracker.close()


# 全局实例
progress_manager = ProgressManager()
