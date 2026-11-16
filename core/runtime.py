"""Own the background services for one application process.

Constructing a Flask application does not construct or start this runtime.  The
entry point acquires its listener first, then starts recovery and background work.
Dependencies are explicit so lifecycle failures can be exercised without sockets
or upstream requests.
"""
import threading
import time


class ApplicationRuntime:
    def __init__(self, job_manager, scheduler, update_checker, *, logger=None, shutdown_timeout=10):
        self.job_manager = job_manager
        self.scheduler = scheduler
        self.update_checker = update_checker
        self.logger = logger
        self.shutdown_timeout = shutdown_timeout
        self._services = []
        self._lock = threading.RLock()
        self._stop_requested = threading.Event()
        self._state = "new"
        self.stop_errors = ()

    def start(self) -> None:
        """Recover once and start consumers before producers; roll back partial starts."""
        with self._lock:
            if self._state == "running":
                return
            if self._state != "new":
                raise RuntimeError("后台运行时已经关闭，不能重复启动")
            self._state = "starting"
            try:
                self._check_start_allowed()
                recovered = self.job_manager.recover_interrupted()
                if recovered and self.logger:
                    self.logger.info(f"已恢复 {recovered} 个中断的下载任务，暂停任务保持暂停")
                for name, service in (("jobs", self.job_manager), ("scheduler", self.scheduler),
                                      ("updates", self.update_checker)):
                    self._check_start_allowed()
                    # A start() that raises may already own a thread; include it
                    # in rollback before invoking it, not only on success.
                    self._services.append((name, service))
                    service.start()
                    self._check_start_allowed()
                self._state = "running"
            except BaseException:
                self.stop()
                raise

    def _check_start_allowed(self):
        if self._stop_requested.is_set():
            raise RuntimeError("后台运行时启动已被停机请求中止")

    def stop(self, timeout=None) -> bool:
        """Request no more downloads, then stop producers and drain jobs in one budget.

        A false return means at least one service is still active or its stop failed.
        Its ownership remains here so a later stop may retry; start never revives a
        partially drained runtime. Normal repeated stops are no-ops.
        """
        budget = self.shutdown_timeout if timeout is None else max(0, timeout)
        deadline = time.monotonic() + budget
        self._stop_requested.set()
        errors = []
        requested = False
        if self._state in ("running", "stopping"):
            # Running executors must stop admission immediately, even if another
            # lifecycle call currently owns the lock. During startup the separate
            # event instead prevents start() from reviving admission afterwards.
            requested = True
            try:
                self.job_manager.request_stop()
            except Exception as exc:
                errors.append(f"jobs.request_stop: {type(exc).__name__}")
        if not self._lock.acquire(timeout=max(0, deadline - time.monotonic())):
            self.stop_errors = tuple(errors + ["runtime: lifecycle lock timeout"])
            return False
        try:
            if self._services and not requested:
                try:
                    self.job_manager.request_stop()
                except Exception as exc:
                    errors.append(f"jobs.request_stop: {type(exc).__name__}")
            remaining = []
            for name, service in reversed(self._services):
                try:
                    stopped = service.stop(timeout=max(0, deadline - time.monotonic()))
                    if stopped is not True:
                        errors.append(f"{name}: drain timeout")
                        remaining.append((name, service))
                except Exception as exc:
                    errors.append(f"{name}: {type(exc).__name__}")
                    remaining.append((name, service))
            self._services = list(reversed(remaining))
            self.stop_errors = tuple(errors)
            safe = not errors and not self._services
            self._state = "stopped" if safe else "stopping"
            if errors and self.logger:
                self.logger.error("后台服务未完全排空，不能确认安全关闭", extra={"services": errors})
            return safe
        finally:
            self._lock.release()
