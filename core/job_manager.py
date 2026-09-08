"""
下载任务队列管理器
"""
import json
import threading
import uuid
from datetime import datetime
from typing import Optional

from . import database as db
from .logger import log
from .progress import progress_manager
from .jm_service import download_album_job


class JobManager:
    """下载任务管理器"""

    def __init__(self):
        self._lock = threading.Lock()
        self._running_jobs: dict[str, threading.Thread] = {}
        self._pause_events: dict[str, threading.Event] = {}  # job_id → Event
        self._stop_event = threading.Event()
        self._scheduler_thread: Optional[threading.Thread] = None

    def get_pause_event(self, job_id: str) -> threading.Event:
        """获取/创建暂停恢复通知事件"""
        with self._lock:
            if job_id not in self._pause_events:
                self._pause_events[job_id] = threading.Event()
            return self._pause_events[job_id]

    def clear_pause_event(self, job_id: str):
        with self._lock:
            self._pause_events.pop(job_id, None)

    def start(self):
        """启动调度器后台线程"""
        if self._scheduler_thread is not None:
            return
        self._stop_event.clear()
        self._scheduler_thread = threading.Thread(target=self._scheduler_loop, daemon=True)
        self._scheduler_thread.start()

    def stop(self):
        """停止调度器后台线程"""
        self._stop_event.set()
        if self._scheduler_thread:
            self._scheduler_thread.join(timeout=5)
            self._scheduler_thread = None

    def create_job(self, album_id: str, title: str, photo_ids: list[str]) -> str:
        """创建下载任务，返回 job_id"""
        job_id = f"job_{uuid.uuid4().hex[:12]}"
        log.info(f"创建任务 job_id={job_id} album_id={album_id} title={title} photo_count={len(photo_ids)}")
        db.insert_job(job_id, album_id, title, photo_ids)
        return job_id

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

        if not can_auto_schedule():
            return

        max_running = int(db.get_setting("max_running_jobs", "1") or 1)

        # 整个 critical section 保持在 self._lock 下，防止 schedule_next 并发导致
        # running_count 读、job claim、_running_jobs 写入三者之间出现 TOCTOU 窗口
        # （参见 THREAD_SAFETY_REVIEW.md P0-1）
        with self._lock:
            running_count = len(self._running_jobs)

            if running_count >= max_running:
                return

            # 原子地获取并锁定下一个 queued 任务（防止双重调度）
            next_job = db.claim_next_queued_job()
            if not next_job:
                return

            job_id = next_job["job_id"]
            album_id = next_job["album_id"]
            try:
                photo_ids = json.loads(next_job["selected_photo_ids"] or "[]")
            except Exception:
                log.warning(f"Job {job_id} 的 selected_photo_ids 解析失败，标记为 failed")
                db.transition_job_status(job_id, ["running"], "failed")
                return
            title = next_job.get("title", "")

            tracker = progress_manager.create_tracker(job_id)
            if not tracker:
                # tracker 创建失败（极罕见），标记为 failed 避免无限重试
                db.transition_job_status(job_id, ["running"], "failed")
                return

            thread = threading.Thread(
                target=self._run_job_wrapper,
                args=(job_id, album_id, photo_ids),
                daemon=True,
                name=f"dl-{job_id[:8]}",
            )

            self._running_jobs[job_id] = thread

        # 在线程启动前释放锁，避免 start() 持锁等待 OS 调度
        thread.start()
        log.info(f"开始执行任务 job_id={job_id} album_id={album_id} photo_count={len(photo_ids)}")

    def _run_job_wrapper(self, job_id: str, album_id: str, photo_ids: list[str]):
        """下载线程结束前才从 _running_jobs 移除"""
        try:
            download_album_job(job_id, album_id, photo_ids)
            log.info(f"任务完成 job_id={job_id} album_id={album_id}")
        except Exception as e:
            log.error(f"任务执行异常 job_id={job_id} error={e}")
            db.update_job(job_id, status="failed", error_message=str(e)[:1000])
            # 确保 wishlist 状态同步
            db.update_wishlist_download_status(album_id, "failed")
            tracker = progress_manager.get_tracker(job_id)
            if tracker:
                tracker.push("failed", {
                    "job_id": job_id, "status": "failed",
                    "error_message": str(e)[:500],
                })
                tracker.close()
        finally:
            with self._lock:
                self._running_jobs.pop(job_id, None)
            # 清理暂停事件，防止内存泄漏
            self.clear_pause_event(job_id)
            # 加固：确保 tracker 被清理
            tracker = progress_manager.get_tracker(job_id)
            if tracker:
                tracker.close()
                progress_manager.remove_tracker(job_id)

    def cancel_job(self, job_id: str) -> tuple[bool, str]:
        """
        取消任务。
        返回 (是否成功, 消息)
        - completed 任务不能取消
        - queued 立即取消
        - running/paused 标记取消，等待线程结束
        """
        job = db.get_job(job_id)
        if not job:
            return False, "任务不存在"

        status = job["status"]

        if status == "completed":
            return False, "任务已完成，不能取消"

        if status == "failed":
            return False, "任务已失败，无需取消"

        if status == "canceled":
            return True, "任务已取消"

        # 原子状态转换：仅在 queued/running/paused 时才更新为 canceled
        album_id = job.get("album_id")
        ok = db.transition_job_status(job_id, ["queued", "running", "paused"], "canceled")
        if not ok:
            # 状态已被其他操作（如下载完成）修改，重新读取确认
            job2 = db.get_job(job_id)
            if job2 and job2["status"] == "completed":
                return False, "任务已在取消前完成"
            if job2 and job2["status"] == "failed":
                return False, "任务已在取消前失败"
            return False, "任务状态已变更，请刷新后重试"

        # 同步更新收藏清单下载状态
        if album_id:
            db.update_wishlist_download_status(album_id, "none")
        log.info(f"取消任务 job_id={job_id}")

        return True, "已取消"


    def pause_job(self, job_id: str) -> tuple[bool, str]:
        """
        暂停任务。
        只有 running 状态的任务可以暂停。
        返回 (是否成功, 消息)
        """
        ok = db.transition_job_status(job_id, ["running"], "paused")
        if not ok:
            job = db.get_job(job_id)
            status = job["status"] if job else "不存在"
            return False, f"当前状态为「{status}」，无法暂停"
        # 清除 resume 事件（确保线程进入等待）
        ev = self._pause_events.get(job_id)
        if ev:
            ev.clear()
        log.info(f"暂停任务 job_id={job_id}")
        return True, "已暂停"

    def resume_job(self, job_id: str) -> tuple[bool, str]:
        """
        恢复暂停的任务。
        只有 paused 状态的任务可以恢复。
        返回 (是否成功, 消息)
        """
        ok = db.transition_job_status(job_id, ["paused"], "running")
        if not ok:
            job = db.get_job(job_id)
            status = job["status"] if job else "不存在"
            return False, f"当前状态为「{status}」，无法恢复"
        # 通知等待中的下载线程立即恢复
        ev = self._pause_events.get(job_id)
        if ev:
            ev.set()
        log.info(f"恢复任务 job_id={job_id}")
        return True, "已恢复"

    def retry_job(self, job_id: str) -> Optional[str]:
        """重试失败/已取消的任务，创建新 job"""
        job = db.get_job(job_id)
        if not job:
            return None

        if job["status"] not in ("failed", "canceled"):
            log.warning(f"重试任务状态非法 job_id={job_id} status={job['status']}")
            return None

        photo_ids = json.loads(job["selected_photo_ids"] or "[]")
        new_job_id = self.create_job(job["album_id"], job["title"] or "", photo_ids)
        log.info(f"重试任务 old_job_id={job_id} new_job_id={new_job_id}")

        if job.get("output_path"):
            db.update_job(new_job_id, output_path=job["output_path"])

        # 同步更新 wishlist 状态
        album_id = job.get("album_id")
        if album_id:
            db.update_wishlist_download_status(album_id, "queued")

        return new_job_id

    def schedule_next(self):
        """公共接口：尝试调度下一个 queued 任务"""
        return self._schedule_next()

    def get_running_count(self) -> int:
        """获取当前运行中的任务数量"""
        with self._lock:
            return len(self._running_jobs)


# 全局实例
job_manager = JobManager()
