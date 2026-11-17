"""
下载任务队列管理器
"""
import json
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Optional

from . import database as db
from .logger import log, set_request_id, clear_request_id
from .progress import progress_manager
from .jm_service import download_album_job
from .task_commands import (
    RETRYABLE_STATUSES, EnqueueResult, TaskCommandError, enqueue_download, retry_download,
    recover_interrupted, requeue_interrupted, resume_download, finish_download, cancel_download,
)


@dataclass
class _JobControl:
    lock: threading.Lock = field(default_factory=threading.Lock)
    users: int = 0


class JobManager:
    """下载任务管理器"""

    def __init__(self):
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        # Admission/recovery may wait for SQLite, never while holding _lock.
        # One admission owns the available slot and album snapshot until it has
        # either registered a worker or finished undoing its claim.
        # Lock order: admission -> job control -> _lock. Commands release job
        # control before scheduling; retirement never acquires admission.
        self._admission_lock = threading.Lock()
        self._pending_operations = 0
        self._stop_waiters = 0
        self._job_controls: dict[str, _JobControl] = {}
        self._running_jobs: dict[str, threading.Thread] = {}
        self._running_albums: dict[str, str] = {}
        self._pause_events: dict[str, threading.Event] = {}  # job_id → Event
        self._stop_event = threading.Event()
        self._scheduler_thread: Optional[threading.Thread] = None

    @contextmanager
    def _operation(self):
        """Keep synchronous, in-flight DB work visible to stop(), including claims."""
        with self._lock:
            accepted = not self._stop_event.is_set()
            if accepted:
                self._pending_operations += 1
        try:
            yield accepted
        finally:
            if accepted:
                with self._changed:
                    self._pending_operations -= 1
                    self._changed.notify_all()

    @contextmanager
    def _job_control(self, job_id):
        """Order commands, notifications and executor retirement for one job.

        References include waiters, so removing an idle lock cannot split two
        commands for the same job across different locks. No wait owns _lock.
        """
        with self._lock:
            control = self._job_controls.setdefault(job_id, _JobControl())
            control.users += 1
        try:
            with control.lock:
                yield
        finally:
            with self._lock:
                control.users -= 1
                if not control.users:
                    del self._job_controls[job_id]

    def get_pause_event(self, job_id: str) -> threading.Event:
        """获取/创建暂停恢复通知事件"""
        with self._lock:
            if job_id not in self._pause_events:
                self._pause_events[job_id] = threading.Event()
            if self._stop_event.is_set():
                self._pause_events[job_id].set()
            return self._pause_events[job_id]

    def clear_pause_event(self, job_id: str):
        with self._lock:
            self._pause_events.pop(job_id, None)

    def start(self):
        """Idempotently start one scheduler; never revive an undrained generation."""
        with self._lock:
            if self._stop_waiters:
                raise RuntimeError("下载执行器仍在停止中，请等待退出后再启动")
            if self._scheduler_thread is not None and self._scheduler_thread.is_alive():
                if self._stop_event.is_set():
                    raise RuntimeError("下载执行器仍在停止中，请等待退出后再启动")
                return
            if self._stop_event.is_set() and (self._running_jobs or self._pending_operations):
                raise RuntimeError("下载执行器仍在停止中，请等待退出后再启动")
            self._stop_event.clear()
            thread = threading.Thread(target=self._scheduler_loop, daemon=True, name="download-queue")
            self._scheduler_thread = thread
            try:
                thread.start()
            except BaseException:
                self._scheduler_thread = None
                self._stop_event.set()
                raise

    def request_stop(self) -> None:
        """Stop admission immediately and wake paused executors without waiting."""
        self._stop_event.set()
        for event in self._pause_events.copy().values():
            event.set()

    def stop(self, timeout: float = 5) -> bool:
        """Join scheduler and workers within one budget; retain undrained ownership."""
        deadline = time.monotonic() + max(0.0, timeout)
        self.request_stop()
        if not self._lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            return False
        try:
            # A concurrent start may have completed between the nonblocking
            # request and acquiring this lock; stop that generation as well.
            self._stop_event.set()
            self._stop_waiters += 1
            scheduler = self._scheduler_thread
            workers = tuple(self._running_jobs.values())
            for event in self._pause_events.values():
                event.set()
        finally:
            self._lock.release()
        try:
            current = threading.current_thread()
            for thread in ((scheduler,) if scheduler is not None else ()) + workers:
                if thread is not current and thread.is_alive():
                    thread.join(timeout=max(0.0, deadline - time.monotonic()))
            with self._changed:
                while self._pending_operations or self._running_jobs:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    self._changed.wait(remaining)
                scheduler_done = scheduler is None or not scheduler.is_alive()
                if scheduler_done and self._scheduler_thread is scheduler:
                    self._scheduler_thread = None
                return scheduler_done
        finally:
            with self._changed:
                self._stop_waiters -= 1
                self._changed.notify_all()

    def recover_interrupted(self) -> int:
        """Recover stranded running rows while preserving every owned executor."""
        with self._operation() as accepted:
            if not accepted:
                return 0
            with self._admission_lock:
                # No new claim can commit between this snapshot and recovery.
                # Exits only shrink ownership; excluding them remains safe.
                with self._lock:
                    owned = tuple(self._running_jobs)
                return recover_interrupted(owned)

    def create_job(self, album_id: str, title: str, photo_ids: list[str]) -> str:
        """创建或复用相同章节的活跃任务；保留调用方使用的 job_id 返回契约。"""
        return self.create_job_result(album_id, title, photo_ids).job_id

    def create_job_result(self, album_id: str, title: str, photo_ids: list[str]) -> EnqueueResult:
        result = enqueue_download(album_id, title, photo_ids)
        log.info(f"创建任务 job_id={result.job_id} album_id={album_id} "
                 f"created={result.created} photo_count={len(photo_ids)}")
        return result

    def _scheduler_loop(self):
        """调度器后台主循环：每 2 秒尝试调度一次"""
        while not self._stop_event.is_set():
            try:
                self._schedule_next()
            except Exception as e:
                log.error(f"调度器异常: {e}")
            self._stop_event.wait(2)

    def _schedule_next(self):
        """尝试从队列中原子地取出下一个 queued 任务并启动下载线程"""
        # 如果启用了定时调度且不在时间段内，不自动调度
        # 局部导入避免与 scheduler 模块的循环导入
        from .scheduler import can_auto_schedule

        with self._operation() as accepted:
            if not accepted:
                return
            with self._admission_lock:
                if self._stop_event.is_set():
                    return
                if not can_auto_schedule():
                    return
                max_running = int(db.get_setting("max_running_jobs", "1") or 1)
                with self._lock:
                    if self._stop_event.is_set() or len(self._running_jobs) >= max_running:
                        return
                    excluded_albums = tuple(self._running_albums.values())

                next_job = db.claim_next_queued_job(excluded_albums)
                if not next_job:
                    return
                with self._job_control(next_job["job_id"]):
                    self._start_claimed_job(next_job)

    def _start_claimed_job(self, next_job):
        """Called with the tracked admission and per-job gates, never _lock."""
        job_id = next_job["job_id"]
        album_id = next_job["album_id"]
        started = False
        try:
            if self._stop_event.is_set():
                return
            # A command can commit after claim, before its job ID is known to
            # this gate. Never start a claim that was canceled or requeued.
            current = db.get_job(job_id)
            if current is None or current["status"] not in ("running", "paused"):
                return
            try:
                photo_ids = json.loads(next_job["selected_photo_ids"] or "[]")
            except Exception:
                log.warning(f"selected_photo_ids 解析失败，标记为 failed job_id={job_id} album_id={album_id}")
                finish_download(job_id, "failed")
                return
            tracker = progress_manager.create_tracker(job_id)
            if not tracker:
                finish_download(job_id, "failed")
                return
            thread = threading.Thread(
                target=self._run_job_wrapper,
                args=(job_id, album_id, photo_ids),
                daemon=True,
                name=f"dl-{job_id[:8]}",
            )
            # This section contains no storage calls. The admission gate keeps
            # another claim from consuming this slot or album during DB waits.
            with self._lock:
                if self._stop_event.is_set():
                    return
                self._running_jobs[job_id] = thread
                self._running_albums[job_id] = album_id
                try:
                    thread.start()
                    started = True
                except BaseException:
                    self._running_jobs.pop(job_id, None)
                    self._running_albums.pop(job_id, None)
                    raise
        except BaseException:
            finish_download(job_id, "failed", error_message="下载线程启动失败")
            raise
        finally:
            if not started:
                # Still part of the tracked admission: stop cannot report
                # drained while claim rollback waits for SQLite.
                try:
                    requeue_interrupted(job_id)
                finally:
                    progress_manager.remove_tracker(job_id)
        log.info(f"开始执行任务 job_id={job_id} album_id={album_id} photo_count={len(photo_ids)}")

    def _run_job_wrapper(self, job_id: str, album_id: str, photo_ids: list[str]):
        """下载线程结束前才从 _running_jobs 移除"""
        # 本任务的记录（含章节/图片工作线程与 jmcomic 库的重试/失败记录）共用一个 request_id，
        # 日志查看器的“只看此请求”可以串起整个任务的经过
        set_request_id()
        try:
            if not self._stop_event.is_set():
                download_album_job(job_id, album_id, photo_ids, stop_event=self._stop_event)
            log.info(f"下载执行器退出 job_id={job_id} album_id={album_id}")
        except Exception as e:
            log.error(f"任务执行异常 job_id={job_id} error={e}")
            if self._stop_event.is_set():
                return
            changed = finish_download(job_id, "failed", error_message=str(e)[:1000])
            tracker = progress_manager.get_tracker(job_id)
            if tracker and changed:
                tracker.push("failed", {
                    "job_id": job_id, "status": "failed",
                    "error_message": str(e)[:500],
                })
                tracker.close()
        finally:
            with self._job_control(job_id):
                try:
                    # Resume may have committed while this executor was already
                    # returning. Retire under the same per-job gate and release
                    # any unfinished running row; pause/cancel/results survive.
                    requeue_interrupted(job_id)
                except Exception as error:
                    # Leave a running row recoverable on next startup if storage
                    # failed; executor ownership must still be released below.
                    log.error(f"保存中断任务状态失败 job_id={job_id} error={error}")
                try:
                    progress_manager.remove_tracker(job_id)
                finally:
                    clear_request_id()
                    with self._changed:
                        self._pause_events.pop(job_id, None)
                        self._running_jobs.pop(job_id, None)
                        self._running_albums.pop(job_id, None)
                        self._changed.notify_all()

    def cancel_job(self, job_id: str) -> tuple[bool, str]:
        """
        取消任务。
        返回 (是否成功, 消息)
        - completed 任务不能取消
        - queued 立即取消
        - running/paused 标记取消，等待线程结束
        """
        result = cancel_download(job_id)
        if result == (True, "已取消"):
            log.info(f"取消任务 job_id={job_id}")
        return result


    def pause_job(self, job_id: str) -> tuple[bool, str]:
        """
        暂停任务。
        只有 running 状态的任务可以暂停。
        返回 (是否成功, 消息)
        """
        with self._operation() as accepted:
            if not accepted:
                return False, "下载执行器正在停止，无法暂停"
            with self._job_control(job_id):
                ok = db.transition_job_status(job_id, ["running"], "paused")
                if not ok:
                    job = db.get_job(job_id)
                    status = job["status"] if job else "不存在"
                    return False, f"当前状态为「{status}」，无法暂停"
                # Serialize commit and notification with resume/retirement.
                with self._lock:
                    ev = self._pause_events.get(job_id)
                    if ev:
                        ev.clear()
                        # Check *after* clear: request_stop() does not acquire
                        # _lock, and its wakeup must never be erased.
                        if self._stop_event.is_set():
                            ev.set()
        log.info(f"暂停任务 job_id={job_id}")
        return True, "已暂停"

    def resume_job(self, job_id: str) -> tuple[bool, str]:
        """
        恢复暂停的任务。
        只有 paused 状态的任务可以恢复。
        返回 (是否成功, 消息)
        """
        with self._operation() as accepted:
            if not accepted:
                return False, "下载执行器正在停止，无法恢复"
            with self._job_control(job_id):
                with self._lock:
                    thread = self._running_jobs.get(job_id)
                    has_executor = bool(thread is not None and thread.is_alive() and not self._stop_event.is_set())
                ok, message = resume_download(job_id, has_executor=has_executor)
                if not ok:
                    return ok, message
                with self._lock:
                    ev = self._pause_events.get(job_id)
                    if ev:
                        ev.set()
            if not has_executor:
                try:
                    self.schedule_next()
                except Exception as error:
                    # The resume command committed; transient admission failures
                    # do not turn success into a misleading HTTP error.
                    log.warning(f"恢复后调度未启动 job_id={job_id} error={error}")
        log.info(f"恢复任务 job_id={job_id}")
        return True, "已恢复"

    def retry_job(self, job_id: str) -> tuple[Optional[str], Optional[str]]:
        """重新下载已结束的任务（失败 / 已取消 / 已完成）：用同一部漫画、同样的章节创建新 job，原记录保留。

        返回 (新 job_id, None)；不能重试时返回 (None, 原因)：
        "not_found" 原任务不存在；"status" 任务还没结束（排队中 / 下载中 / 已暂停）。
        "invalid_source" 原任务数据损坏。相同章节已经在队列中时返回已有的 job_id。
        """
        try:
            return self.retry_job_result(job_id).job_id, None
        except TaskCommandError as error:
            return None, error.reason

    def retry_job_result(self, job_id: str) -> EnqueueResult:
        result = retry_download(job_id)
        log.info(f"重试任务 old_job_id={job_id} new_job_id={result.job_id} created={result.created}")
        return result

    def schedule_next(self):
        """公共接口：尝试调度下一个 queued 任务"""
        return self._schedule_next()

    def get_running_count(self) -> int:
        """获取当前运行中的任务数量"""
        with self._lock:
            return len(self._running_jobs)


# 全局实例
job_manager = JobManager()
