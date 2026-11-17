"""
下载任务队列管理器
"""
import json
import threading
import time
from typing import Optional

from . import database as db
from .logger import log, set_request_id, clear_request_id
from .progress import progress_manager
from .jm_service import download_album_job
from .task_commands import (
    RETRYABLE_STATUSES, EnqueueResult, TaskCommandError, enqueue_download, retry_download,
    recover_interrupted, requeue_interrupted, resume_download, finish_download, cancel_download,
)


class JobManager:
    """下载任务管理器"""

    def __init__(self):
        self._lock = threading.RLock()
        self._running_jobs: dict[str, threading.Thread] = {}
        self._running_albums: dict[str, str] = {}
        self._pause_events: dict[str, threading.Event] = {}  # job_id → Event
        self._stop_event = threading.Event()
        self._scheduler_thread: Optional[threading.Thread] = None

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
            if self._scheduler_thread is not None and self._scheduler_thread.is_alive():
                if self._stop_event.is_set():
                    raise RuntimeError("下载执行器仍在停止中，请等待退出后再启动")
                return
            if self._stop_event.is_set() and self._running_jobs:
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
            scheduler = self._scheduler_thread
            workers = tuple(self._running_jobs.values())
            for event in self._pause_events.values():
                event.set()
        finally:
            self._lock.release()
        current = threading.current_thread()
        for thread in ((scheduler,) if scheduler is not None else ()) + workers:
            if thread is not current and thread.is_alive():
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
        if not self._lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            return False
        try:
            scheduler_done = scheduler is None or not scheduler.is_alive()
            if scheduler_done and self._scheduler_thread is scheduler:
                self._scheduler_thread = None
            return scheduler_done and not self._running_jobs
        finally:
            self._lock.release()

    def recover_interrupted(self) -> int:
        """Recover stranded running rows while preserving every owned executor."""
        with self._lock:
            return recover_interrupted(self._running_jobs)

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

        if self._stop_event.is_set() or not can_auto_schedule():
            return

        max_running = int(db.get_setting("max_running_jobs", "1") or 1)

        # 整个 critical section 保持在 self._lock 下，防止 schedule_next 并发导致
        # running_count 读、job claim、_running_jobs 写入三者之间出现 TOCTOU 窗口
        # （参见 THREAD_SAFETY_REVIEW.md P0-1）
        with self._lock:
            if self._stop_event.is_set():
                return
            running_count = len(self._running_jobs)

            if running_count >= max_running:
                return

            # 原子地获取并锁定下一个 queued 任务（防止双重调度）
            next_job = db.claim_next_queued_job(self._running_albums.values())
            if not next_job:
                return

            job_id = next_job["job_id"]
            album_id = next_job["album_id"]
            if self._stop_event.is_set():
                requeue_interrupted(job_id)
                return
            # Canceled workers can still be finishing an HTTP request.
            if album_id in self._running_albums.values():
                db.transition_job_status(job_id, ["running"], "queued")
                return
            try:
                photo_ids = json.loads(next_job["selected_photo_ids"] or "[]")
            except Exception:
                # 写成 job_id=…：去重只保留 *_id= 形式的编号，不同任务的这条警告才不会被合并掉
                log.warning(f"selected_photo_ids 解析失败，标记为 failed job_id={job_id} album_id={album_id}")
                finish_download(job_id, "failed")
                return
            title = next_job.get("title", "")

            tracker = progress_manager.create_tracker(job_id)
            if not tracker:
                # tracker 创建失败（极罕见），标记为 failed 避免无限重试
                finish_download(job_id, "failed")
                return

            thread = threading.Thread(
                target=self._run_job_wrapper,
                args=(job_id, album_id, photo_ids),
                daemon=True,
                name=f"dl-{job_id[:8]}",
            )

            self._running_jobs[job_id] = thread
            self._running_albums[job_id] = album_id
            # Registration and start are one admission critical section. The
            # target checks stop again before touching the download executor.
            try:
                if self._stop_event.is_set():
                    self._running_jobs.pop(job_id, None)
                    self._running_albums.pop(job_id, None)
                    progress_manager.remove_tracker(job_id)
                    requeue_interrupted(job_id)
                    return
                thread.start()
            except Exception:
                self._running_jobs.pop(job_id, None)
                self._running_albums.pop(job_id, None)
                finish_download(job_id, "failed", error_message="下载线程启动失败")
                progress_manager.remove_tracker(job_id)
                raise
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
            if self._stop_event.is_set():
                try:
                    requeue_interrupted(job_id)
                except Exception as error:
                    # Leave a running row recoverable on next startup if storage
                    # failed; executor ownership must still be released below.
                    log.error(f"保存中断任务状态失败 job_id={job_id} error={error}")
            # 清理暂停事件，防止内存泄漏
            self.clear_pause_event(job_id)
            # 加固：确保 tracker 被清理
            tracker = progress_manager.get_tracker(job_id)
            if tracker:
                tracker.close()
                progress_manager.remove_tracker(job_id)
            clear_request_id()
            with self._lock:
                self._running_jobs.pop(job_id, None)
                self._running_albums.pop(job_id, None)

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
        with self._lock:
            ok = db.transition_job_status(job_id, ["running"], "paused")
            if not ok:
                job = db.get_job(job_id)
                status = job["status"] if job else "不存在"
                return False, f"当前状态为「{status}」，无法暂停"
            # Serialize the DB transition and wakeup state with resume_job; an
            # older pause must never clear a newer resume notification.
            ev = self._pause_events.get(job_id)
            if ev:
                ev.set() if self._stop_event.is_set() else ev.clear()
        log.info(f"暂停任务 job_id={job_id}")
        return True, "已暂停"

    def resume_job(self, job_id: str) -> tuple[bool, str]:
        """
        恢复暂停的任务。
        只有 paused 状态的任务可以恢复。
        返回 (是否成功, 消息)
        """
        with self._lock:
            thread = self._running_jobs.get(job_id)
            has_executor = bool(thread is not None and thread.is_alive() and not self._stop_event.is_set())
            ok, message = resume_download(job_id, has_executor=has_executor)
            if not ok:
                return ok, message
            ev = self._pause_events.get(job_id)
            if ev:
                ev.set()
        if not has_executor:
            try:
                self.schedule_next()
            except Exception as error:
                # The resume command committed; transient admission failures do
                # not turn that successful command into a misleading HTTP error.
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
