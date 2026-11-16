"""Own detached SDK calls until their transport lease has actually been released.

The default permits create_app/CLI use without a runtime. After request_stop(),
only an explicit start() on an idle service reopens admission. A Python future's
result timeout never removes its still-running call from this registry.
"""
from dataclasses import dataclass
import threading
import time


class BackgroundCallsStopped(RuntimeError):
    pass


@dataclass(eq=False)
class _Call:
    release: object
    future: object = None
    submitting: bool = True
    acquired: bool = False
    cancel_requested: bool = False
    cleanup_attempt: int = -1
    error: object = None


class BackgroundCalls:
    def __init__(self):
        self._condition = threading.Condition()
        self._accepting = True
        self._calls = set()
        self._worker = None
        self._cleanup_round = 0
        self._worker_error = None
        self._shutdown_cleanup = None
        self._cleanup_requested = False
        self._cleanup_call = None

    def start(self):
        with self._condition:
            if self._accepting:
                return
            if (self._calls or self._cleanup_requested or self._worker_error is not None
                    or (self._worker is not None and self._worker.is_alive())):
                raise RuntimeError("后台调用仍在退出，不能重新接收请求")
            self._worker = None
            self._accepting = True

    def require_accepting(self):
        with self._condition:
            if not self._accepting:
                raise BackgroundCallsStopped("后台调用正在停止，请稍后重试")

    def set_shutdown_cleanup(self, callback):
        """Bind the single client-cache cleanup hook, without importing the SDK."""
        with self._condition:
            if self._shutdown_cleanup is not None and self._shutdown_cleanup is not callback:
                raise RuntimeError("后台调用的关闭清理已绑定")
            self._shutdown_cleanup = callback

    def request_cleanup(self):
        """Own a late client-close retry, including after an earlier stop drained."""
        with self._condition:
            if self._shutdown_cleanup is None:
                return
            self._cleanup_requested = True
            self._ensure_worker()
            self._condition.notify_all()

    def _ensure_worker(self):
        # The condition is held; no business/client lock is acquired here.
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = threading.Thread(target=self._clean, name="sdk-call-cleanup", daemon=True)
        self._worker.start()

    def submit(self, pool, fn, *args, acquire=None, release=None, **kwargs):
        """Register before submission; own exactly one optional acquire/release pair.

        Completion only notifies the owned cleanup worker. Cancellation can run
        arbitrary Future callbacks synchronously, so it also runs on that worker,
        never in request_stop()/stop(). Caller-specific callbacks (e.g. reader
        result publication) remain owned by their own service.
        """
        call = _Call(release=release)
        with self._condition:
            if not self._accepting:
                raise BackgroundCallsStopped("后台调用正在停止，请稍后重试")
            self._ensure_worker()
            self._calls.add(call)
        try:
            if acquire is not None:
                acquire()
            call.acquired = True
            with self._condition:
                if not self._accepting:
                    raise BackgroundCallsStopped("后台调用正在停止，请稍后重试")
            future = pool.submit(fn, *args, **kwargs)
        except BaseException:
            with self._condition:
                call.submitting = False
                self._condition.notify_all()
            raise
        with self._condition:
            call.future = future
            call.submitting = False
            self._condition.notify_all()
        future.add_done_callback(lambda _future: self._notify())
        return future

    def _notify(self):
        with self._condition:
            self._condition.notify_all()

    def request_stop(self):
        with self._condition:
            if self._accepting and self._shutdown_cleanup is not None:
                self._cleanup_requested = True
            self._accepting = False
            self._condition.notify_all()

    def stop(self, timeout=5) -> bool:
        deadline = time.monotonic() + max(0, timeout)
        with self._condition:
            if self._accepting and self._shutdown_cleanup is not None:
                self._cleanup_requested = True
            self._accepting = False
            self._cleanup_round += 1  # A later stop retries failed lease release.
            if self._calls or self._cleanup_requested:
                self._ensure_worker()
            self._condition.notify_all()
            while self._calls or self._cleanup_requested:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self._worker_error is not None:
                    return False
                # A late retry can arrive after the old worker decided to exit
                # but before is_alive() becomes false. Keep that ownership and
                # hand it to a replacement after the old thread actually exits.
                self._ensure_worker()
                self._condition.wait(min(0.05, remaining))
            worker = self._worker
        # Empty registry alone is insufficient: the cleanup callback/thread must
        # have returned too. Never join with the condition or SDK lock held.
        if worker is not None:
            if worker is threading.current_thread():
                return False
            worker.join(max(0, deadline - time.monotonic()))
        with self._condition:
            return (not self._calls and not self._cleanup_requested and self._worker_error is None
                    and (worker is None or not worker.is_alive()))

    def runtime_status(self):
        with self._condition:
            errors = [type(call.error).__name__ for call in self._calls if call.error is not None]
            if self._worker_error is not None:
                errors.append(type(self._worker_error).__name__)
            return {"accepting": self._accepting,
                    "pending": len(self._calls) + int(self._cleanup_requested and self._cleanup_call is None),
                    "cleanup_running": self._worker is not None and self._worker.is_alive(),
                    "errors": tuple(errors)}

    def _clean(self):
        try:
            self._clean_loop()
        except BaseException as error:
            with self._condition:
                self._worker_error = error
                self._accepting = False
                self._condition.notify_all()

    def _clean_loop(self):
        while True:
            with self._condition:
                if self._cleanup_requested and self._cleanup_call is None:
                    self._cleanup_call = _Call(release=self._shutdown_cleanup, submitting=False, acquired=True)
                    self._calls.add(self._cleanup_call)
                    self._cleanup_requested = False
                selected = None
                # Release completed call leases before retrying cache retirement.
                ordered = [call for call in self._calls if call is not self._cleanup_call]
                if self._cleanup_call is not None:
                    ordered.append(self._cleanup_call)
                for call in ordered:
                    if call.submitting:
                        continue
                    if call.future is None or call.future.done():
                        if call.cleanup_attempt != self._cleanup_round:
                            call.cleanup_attempt = self._cleanup_round
                            selected = ("release", call)
                            break
                    elif not self._accepting and not call.cancel_requested:
                        call.cancel_requested = True
                        selected = ("cancel", call)
                        break
                if selected is None:
                    if not self._calls and not self._accepting:
                        self._condition.notify_all()
                        return
                    self._condition.wait()
                    continue
            operation, call = selected
            if operation == "cancel":
                call.future.cancel()
                continue
            try:
                if call.acquired and call.release is not None and call.release() is False:
                    raise RuntimeError("后台调用的客户端释放失败")
            except BaseException as error:
                with self._condition:
                    call.error = error
                    self._condition.notify_all()
            else:
                with self._condition:
                    self._calls.discard(call)
                    if call is self._cleanup_call:
                        self._cleanup_call = None
                    self._condition.notify_all()


_service = BackgroundCalls()
start = _service.start
request_stop = _service.request_stop
stop = _service.stop
submit = _service.submit
runtime_status = _service.runtime_status
require_accepting = _service.require_accepting
set_shutdown_cleanup = _service.set_shutdown_cleanup
request_cleanup = _service.request_cleanup
