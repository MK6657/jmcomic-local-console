"""Own the background services for one application process.

Constructing a Flask application does not construct or start this runtime.  The
entry point acquires its listener first, then starts recovery and background work.
Dependencies are explicit so lifecycle failures can be exercised without sockets
or upstream requests.
"""
import threading
import time


class ApplicationRuntime:
    def __init__(self, job_manager, scheduler, update_checker, *, logger=None, shutdown_timeout=10,
                 additional_services=()):
        self.job_manager = job_manager
        self.scheduler = scheduler
        self.update_checker = update_checker
        self.logger = logger
        self.shutdown_timeout = shutdown_timeout
        self.additional_services = tuple(additional_services)
        self._services = list(self.additional_services)
        self._updates_registered = False  # owns pre-start manual checks too
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
                components = (*self.additional_services, ("jobs", self.job_manager),
                              ("scheduler", self.scheduler), ("updates", self.update_checker))
                for name, service in components:
                    self._check_start_allowed()
                    # A start() that raises may already own a thread; include it
                    # in rollback before invoking it, not only on success.
                    if (name, service) not in self._services:
                        self._services.append((name, service))
                    if name == "updates":
                        self._updates_registered = True
                    service.start()
                    self._check_start_allowed()
                self._state = "running"
            except BaseException:
                self.stop()
                raise

    def _check_start_allowed(self):
        if self._stop_requested.is_set():
            raise RuntimeError("后台运行时启动已被停机请求中止")

    def _close_admission(self, errors):
        # Producers can be waiting on shared request work. Close every admission
        # gate before draining any service, without holding a service's lock.
        for name, service in (("updates", self.update_checker),
                              *reversed(self.additional_services), ("jobs", self.job_manager)):
            if hasattr(service, "request_stop"):
                try:
                    service.request_stop()
                except Exception as exc:
                    errors.append(f"{name}.request_stop: {type(exc).__name__}")

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
        if self._state != "stopped":
            # Running executors must stop admission immediately, even if another
            # lifecycle call currently owns the lock. This includes manual update
            # checks during recovery and before the background loop has started.
            self._close_admission(errors)
        if not self._lock.acquire(timeout=max(0, deadline - time.monotonic())):
            self.stop_errors = tuple(errors + ["runtime: lifecycle lock timeout"])
            return False
        try:
            # Manual update requests are admitted even before updates.start().
            # Recovery/partial-start rollback must therefore drain this owner too.
            if self._state != "stopped" and not self._updates_registered:
                self._services.append(("updates", self.update_checker))
                self._updates_registered = True
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
