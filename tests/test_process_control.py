"""Named-event shutdown: native loopback IPC only; no HTTP or content requests."""
import ctypes
import os
import threading
import uuid
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core import process_control as control


class _FakeKernel:
    def __init__(self):
        self.event = threading.Event()
        self.waited = threading.Event()
        self.closed = []
        self.opened_names = []
        self.created_names = []
        self.create_error = 0
        self.open_error = 0
        self.close_error = 0
        self.set_error = 0
        self.wait_result = None
        self.create_handle = 41
        self.open_handle = 42

    def CreateEventW(self, security, manual, initial, name):
        assert security is None and manual is True and initial is False
        self.created_names.append(name)
        ctypes.set_last_error(self.create_error)
        return self.create_handle

    def OpenEventW(self, access, inherit, name):
        assert access == control._EVENT_MODIFY_STATE and inherit is False
        self.opened_names.append(name)
        ctypes.set_last_error(self.open_error)
        return self.open_handle

    def SetEvent(self, handle):
        assert handle == self.open_handle
        if self.set_error:
            ctypes.set_last_error(self.set_error)
            return False
        self.event.set()
        return True

    def WaitForSingleObject(self, handle, milliseconds):
        assert handle == self.create_handle and 100 <= milliseconds <= 200
        self.waited.set()
        if self.wait_result is not None:
            ctypes.set_last_error(6)
            return self.wait_result
        return control._WAIT_OBJECT_0 if self.event.wait(milliseconds / 1000) else control._WAIT_TIMEOUT

    def CloseHandle(self, handle):
        self.closed.append(handle)
        if self.close_error:
            ctypes.set_last_error(self.close_error)
            return False
        return True


@pytest.fixture
def kernel(monkeypatch):
    fake = _FakeKernel()
    monkeypatch.setattr(control, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(control, "_kernel32", lambda: fake)
    monkeypatch.setattr(control._thread, "interrupt_main", Mock())
    return fake


@pytest.mark.parametrize("token", [None, "", "a" * 31, "a" * 33, "g" * 32,
                                  "a" * 32 + "\n", "../" + "a" * 29, b"a" * 32, 123])
def test_request_strictly_rejects_invalid_token_without_api(token, monkeypatch):
    kernel = Mock(side_effect=AssertionError("invalid token must not reach WinAPI"))
    monkeypatch.setattr(control, "_kernel32", kernel)
    with pytest.raises(ValueError):
        control.request_shutdown(token)
    kernel.assert_not_called()


def test_non_windows_has_no_control_or_api(monkeypatch):
    monkeypatch.setattr(control, "os", SimpleNamespace(name="posix"))
    kernel = Mock(side_effect=AssertionError("non-Windows must not load kernel32"))
    monkeypatch.setattr(control, "_kernel32", kernel)
    assert control.create_shutdown_control() is None
    assert control.request_shutdown("a" * 32) is False
    kernel.assert_not_called()


@pytest.mark.parametrize("token", [None, "malformed"])
def test_constructor_generates_uuid_for_missing_or_invalid_token(kernel, token, monkeypatch):
    generated = "b" * 32
    monkeypatch.setattr(control.uuid, "uuid4", lambda: SimpleNamespace(hex=generated))
    owner = control.create_shutdown_control(token)
    try:
        assert owner.token == generated
        assert kernel.created_names == ["Local\\JMConsoleShutdown_" + generated]
    finally:
        assert owner.stop()


def test_private_event_invokes_standard_interrupt_once_and_closes_after_watcher(kernel):
    token = "A1" * 16
    called = threading.Event()
    callback = control._thread.interrupt_main
    callback.side_effect = called.set
    owner = control.create_shutdown_control(token)
    try:
        assert owner.token == token
        assert kernel.waited.wait(1)
        assert control.request_shutdown(token)
        assert control.request_shutdown(token)
        assert called.wait(1)
        owner._watcher.join(1)
        callback.assert_called_once_with()
        assert owner.error is None
        assert owner.stop()
        assert owner.stop()
        assert kernel.closed.count(kernel.create_handle) == 1
        assert kernel.opened_names == ["Local\\JMConsoleShutdown_" + token] * 2
    finally:
        owner.stop()


def test_existing_named_event_is_never_reused(kernel):
    kernel.create_error = control._ERROR_ALREADY_EXISTS
    with pytest.raises(FileExistsError):
        control.create_shutdown_control("c" * 32)
    assert kernel.closed == [kernel.create_handle]
    assert not kernel.waited.is_set()


def test_create_failure_does_not_close_invalid_handle(kernel):
    kernel.create_handle, kernel.create_error = None, 5
    with pytest.raises(OSError) as error:
        control.create_shutdown_control()
    assert error.value.winerror == 5
    assert not kernel.closed


def test_failed_watcher_start_closes_constructor_handle(kernel, monkeypatch):
    def fail_start(self):
        raise RuntimeError("cannot start watcher")
    monkeypatch.setattr(control.threading.Thread, "start", fail_start)
    with pytest.raises(RuntimeError, match="cannot start watcher"):
        control.create_shutdown_control()
    assert kernel.closed == [kernel.create_handle]


def test_request_missing_event_returns_false(kernel):
    kernel.open_handle, kernel.open_error = None, 2
    assert control.request_shutdown("d" * 32) is False
    assert not kernel.closed


def test_request_other_open_error_raises(kernel):
    kernel.open_handle, kernel.open_error = None, 5
    with pytest.raises(OSError) as error:
        control.request_shutdown("d" * 32)
    assert error.value.winerror == 5


@pytest.mark.parametrize("operation", ["set", "close"])
def test_request_signal_and_close_errors_raise(kernel, operation):
    setattr(kernel, operation + "_error", 6)
    with pytest.raises(OSError) as error:
        control.request_shutdown("e" * 32)
    assert error.value.winerror == 6
    assert kernel.closed == [kernel.open_handle]


def test_stop_timeout_retains_handle_until_inflight_callback_returns(kernel):
    entered, release = threading.Event(), threading.Event()
    def callback():
        entered.set()
        assert release.wait(2)
    control._thread.interrupt_main.side_effect = callback
    owner = control.create_shutdown_control()
    try:
        kernel.event.set()
        assert entered.wait(1)
        assert owner.stop(timeout=0.01) is False
        assert kernel.create_handle not in kernel.closed
        assert owner.error is None
        release.set()
        assert owner.stop(timeout=1) is True
        assert kernel.closed.count(kernel.create_handle) == 1
    finally:
        release.set()
        owner.stop()


def test_stop_while_waiting_suppresses_new_callback(kernel):
    owner = control.create_shutdown_control()
    try:
        assert kernel.waited.wait(1)
        assert owner.stop(timeout=0) is False
        assert kernel.create_handle not in kernel.closed
        kernel.event.set()
        assert owner.stop()
        control._thread.interrupt_main.assert_not_called()
    finally:
        owner.stop()


def test_callback_error_is_preserved_and_stop_never_reports_success(kernel):
    failure = ValueError("callback failed")
    control._thread.interrupt_main.side_effect = failure
    owner = control.create_shutdown_control()
    try:
        kernel.event.set()
        owner._watcher.join(1)
        assert owner.error is failure
        assert owner.stop() is False
        assert owner.stop() is False
        assert kernel.closed.count(kernel.create_handle) == 1
    finally:
        owner.stop()


@pytest.mark.parametrize("result", [control._WAIT_FAILED, 17])
def test_wait_error_preserved_and_handle_released(kernel, result):
    kernel.wait_result = result
    owner = control.create_shutdown_control()
    try:
        owner._watcher.join(1)
        assert isinstance(owner.error, OSError)
        assert owner.stop() is False
        assert kernel.closed == [kernel.create_handle]
    finally:
        owner.stop()


def test_close_error_retains_handle_and_error_for_retry(kernel):
    owner = control.create_shutdown_control()
    try:
        kernel.close_error = 6
        assert owner.stop() is False
        first_error = owner.error
        assert isinstance(first_error, OSError)
        assert owner._handle == kernel.create_handle
        kernel.close_error = 0
        assert owner.stop() is False
        assert owner._handle is None and owner.error is first_error
        assert kernel.closed == [kernel.create_handle, kernel.create_handle]
    finally:
        kernel.close_error = 0
        owner.stop()


def test_stop_called_by_callback_defers_close_until_joined(kernel):
    holder, callback_stop = {}, []
    def callback():
        callback_stop.append(holder["control"].stop())
    control._thread.interrupt_main.side_effect = callback
    owner = holder["control"] = control.create_shutdown_control()
    try:
        kernel.event.set()
        owner._watcher.join(1)
        assert callback_stop == [False]
        assert kernel.create_handle not in kernel.closed
        assert owner.stop()
    finally:
        owner.stop()


@pytest.mark.parametrize("timeout", [-1, float("nan"), float("inf")])
def test_stop_rejects_invalid_timeout(kernel, timeout):
    owner = control.create_shutdown_control()
    try:
        with pytest.raises(ValueError):
            owner.stop(timeout)
    finally:
        assert owner.stop()


@pytest.mark.skipif(os.name != "nt", reason="real Windows named event")
def test_real_windows_event_delivers_harmless_callback_and_disappears(monkeypatch):
    token, notified = uuid.uuid4().hex, threading.Event()
    callback = Mock(side_effect=notified.set)
    monkeypatch.setattr(control._thread, "interrupt_main", callback)
    assert control.request_shutdown(token) is False
    owner = control.create_shutdown_control(token)
    try:
        with pytest.raises(FileExistsError):
            control.create_shutdown_control(token)
        assert control.request_shutdown(token)
        assert notified.wait(2)
        owner._watcher.join(1)
        callback.assert_called_once_with()
        assert owner.stop()
        assert owner.error is None
    finally:
        owner.stop()
    assert control.request_shutdown(token) is False
