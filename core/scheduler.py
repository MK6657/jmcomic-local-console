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


def _schedule_hours() -> tuple[int, int] | None:
    """设定的开始 / 结束时（0-23）；不是整数或超出范围时为 None（这时永远不在时间段内）"""
    try:
        start_hour = int(db.get_setting("schedule_start", "23"))
        end_hour = int(db.get_setting("schedule_end", "7"))
    except (ValueError, TypeError):
        return None
    if not (0 <= start_hour <= 23 and 0 <= end_hour <= 23):
        return None
    return start_hour, end_hour


def is_schedule_time(now: datetime | None = None) -> bool:
    """检查当前（或 now）是否在设定的下载时间段内"""
    hours = _schedule_hours()
    if hours is None:
        return False
    start_hour, end_hour = hours

    current_hour = (now or datetime.now()).hour

    if start_hour <= end_hour:
        # 同一天内（例如 8:00 ~ 22:00）；开始 = 结束时永远不在时间段内
        return start_hour <= current_hour < end_hour
    else:
        # 跨天（例如 23:00 ~ 7:00）
        return current_hour >= start_hour or current_hour < end_hour


def window_state(now: datetime | None = None) -> dict:
    """定时下载时间段现在的状态（批量下载的确认窗口据此如实说明任务什么时候开始）：
    enabled 开了定时下载；open 现在排队的任务能开始（没开定时下载也算）；start / end 开始 / 结束时（设置无效时为 None）；
    invalid 时间段设置无效；never 开了定时下载但时间段永远不会到（无效或开始 = 结束）；
    opens_tomorrow 现在关着、下一次要到明天的 start 才开始。与 can_auto_schedule 同一个判断。"""
    now = now or datetime.now()
    enabled = db.get_setting("schedule_enabled", "false") == "true"
    hours = _schedule_hours()
    invalid = hours is None
    start, end = hours if hours else (None, None)
    never = enabled and (invalid or start == end)
    is_open = not enabled or (not invalid and is_schedule_time(now))
    return {
        "enabled": enabled, "open": is_open, "start": start, "end": end,
        "never": never, "invalid": invalid,
        "opens_tomorrow": enabled and not is_open and not never and now.hour >= start,
    }


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
