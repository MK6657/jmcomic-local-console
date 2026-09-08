"""
定时下载调度器

每 60 秒检查一次，如果在设定的下载时间段内且有 queued 任务，
则启动下载。不干扰用户手动启动的任务。
"""
import threading
from datetime import datetime

from . import database as db
from .job_manager import job_manager
from .logger import log

_scheduler_thread: threading.Thread | None = None
# 每条调度线程持有自己的 stop event（线程创建时捕获），
# 避免并发 start/stop 交错时 stop() 误操作新线程的事件、留下孤儿线程
_stop_event = threading.Event()
_scheduler_lock = threading.Lock()


def is_schedule_time() -> bool:
    """检查当前是否在设定的下载时间段内"""
    start_str = db.get_setting("schedule_start", "23")
    end_str = db.get_setting("schedule_end", "7")

    try:
        start_hour = int(start_str)
        end_hour = int(end_str)
    except (ValueError, TypeError):
        return False

    current_hour = datetime.now().hour

    if start_hour <= end_hour:
        # 同一天内（例如 8:00 ~ 22:00）
        return start_hour <= current_hour < end_hour
    else:
        # 跨天（例如 23:00 ~ 7:00）
        return current_hour >= start_hour or current_hour < end_hour


def can_auto_schedule() -> bool:
    """供 job_manager 调用：判断是否允许自动调度 queued 任务"""
    enabled = db.get_setting("schedule_enabled", "false")
    if enabled != "true":
        # 未启用定时调度，不限制
        return True
    # 启用定时调度，只在时间段内允许自动调度
    return is_schedule_time()


def start():
    """启动定时调度器后台线程（幂等）"""
    global _scheduler_thread, _stop_event
    with _scheduler_lock:
        if _scheduler_thread is not None and _scheduler_thread.is_alive():
            return

        # 如果定时调度未启用，不启动线程以节省资源
        enabled = db.get_setting("schedule_enabled", "false")
        if enabled != "true":
            log.info("定时调度未启用，跳过启动调度器线程")
            return

        stop_ev = threading.Event()  # 本线程专属，stop() 只会操作到它
        _stop_event = stop_ev
        _scheduler_thread = threading.Thread(
            target=_loop, args=(stop_ev,), daemon=True, name="download-scheduler"
        )
        _scheduler_thread.start()
        log.info("定时下载调度器已启动")


def stop():
    """停止定时调度器（幂等）。set 与 join 都在锁内，与 start 串行化。"""
    global _scheduler_thread
    with _scheduler_lock:
        _stop_event.set()
        if _scheduler_thread:
            _scheduler_thread.join(timeout=5)
            _scheduler_thread = None
            log.info("定时下载调度器已停止")


def sync_with_settings():
    """根据当前 schedule_enabled 设置启停调度器线程。

    修复：旧版只在应用启动时决定是否启动调度器线程，
    运行中在设置页开启"定时下载"后线程不会启动（需重启应用才生效）。
    设置保存/导入后调用本函数即可即时生效。start()/stop() 均幂等。
    """
    enabled = db.get_setting("schedule_enabled", "false")
    if enabled == "true":
        start()
    else:
        stop()


def _loop(stop_ev: threading.Event):
    """调度器主循环，每 60 秒检查一次（stop_ev 为本线程专属停止事件）"""
    while not stop_ev.is_set():
        try:
            _tick()
        except Exception as e:
            log.error(f"定时调度器异常: {e}")
        stop_ev.wait(60)


def _tick():
    """每次心跳：直接尝试调度（减少一次不必要的 SQL 查询）"""
    if not is_schedule_time():
        return
    job_manager.schedule_next()
    # 定时 WAL checkpoint 防 WAL 膨胀
    try:
        from .database import get_db
        conn = get_db()
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        conn.close()
    except Exception:
        pass
