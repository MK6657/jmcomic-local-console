"""
SSE 进度推送模块 — 每个 SSE 客户端独立队列，互不干扰
"""
import json
import queue
import threading
from datetime import datetime


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

    def iter_events(self, client_queue: queue.Queue | None = None):
        """SSE 事件生成器（使用客户端专属队列，不传则走兼容模式）"""
        if client_queue is None:
            with self._lock:
                q = self._subscribers[0] if self._subscribers else None
        else:
            q = client_queue
        if q is None:
            return

        try:
            while not self._done or not q.empty():
                try:
                    event, data = q.get(timeout=2)
                    yield f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                except queue.Empty:
                    # 发送心跳保持连接
                    yield f": heartbeat\n\n"
        except GeneratorExit:
            self.unsubscribe(q)
            raise

    def close(self):
        self.mark_done()
        with self._lock:
            self._subscribers.clear()


class ProgressManager:
    """全局进度管理器"""

    def __init__(self):
        self._lock = threading.Lock()
        self._trackers: dict[str, ProgressTracker] = {}

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


# 全局实例
progress_manager = ProgressManager()
