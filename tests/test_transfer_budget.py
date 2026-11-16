"""One real transport deadline across SDK retries; no upstream or sockets."""
from types import SimpleNamespace
import threading

import pytest


class Clock:
    now = 0.0
    def __call__(self):
        return self.now
    def advance(self, seconds):
        self.now += seconds


class Transport:
    def __init__(self):
        from curl_cffi.requests import RetryStrategy
        self.session = SimpleNamespace(curl_options={12345: "keep"}, retry=RetryStrategy(count=2),
            proxies={"https": "http://private-user:private-password@proxy.invalid"}, impersonate="chrome")
        self.calls = []
        self.answer = lambda **kwargs: "response"
    def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs, dict(self.session.curl_options)))
        return self.answer(**kwargs)
    def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs, dict(self.session.curl_options)))
        return self.answer(**kwargs)


@pytest.fixture
def transfer(monkeypatch):
    from core import transfer
    clock, root = Clock(), Transport()
    monkeypatch.setattr(transfer, "_clock", clock)
    client = SimpleNamespace(get_root_postman=lambda: root)
    return transfer, clock, root, client


def test_each_attempt_receives_remaining_total_time_and_real_curl_limits(transfer):
    module, clock, root, client = transfer
    from curl_cffi import CurlOpt
    with module.download_budget(client, 10):
        assert root.get("https://images.invalid/one", timeout=600, headers={"X-Test": "preserved"}) == "response"
        clock.advance(3)
        assert root.post("https://images.invalid/two", timeout=None) == "response"
        clock.advance(5)
        root.get("https://images.invalid/three", timeout=(200, 400))
    assert [call[2]["timeout"] for call in root.calls] == [10, 7, 2]
    assert all(call[2]["stream"] is False for call in root.calls)
    assert root.calls[0][2]["headers"] == {"X-Test": "preserved"}
    assert [call[3][CurlOpt.TIMEOUT_MS] for call in root.calls] == [10000, 7000, 2000]
    assert [call[3][CurlOpt.CONNECTTIMEOUT_MS] for call in root.calls] == [4000, 4000, 2000]
    assert [call[3][CurlOpt.LOW_SPEED_TIME] for call in root.calls] == [8, 7, 2]
    assert all(call[3][CurlOpt.LOW_SPEED_LIMIT] == 1024 for call in root.calls)


def test_expired_budget_never_starts_another_attempt(transfer):
    module, clock, root, client = transfer
    with pytest.raises(module.TransferTimeout):
        with module.download_budget(client, 5):
            root.get("https://images.invalid/first")
            clock.advance(5)
            root.get("https://images.invalid/second")
    assert len(root.calls) == 1


def test_sub_millisecond_budget_cannot_disable_curl_timeout(transfer):
    module, clock, root, client = transfer
    with pytest.raises(module.TransferTimeout):
        with module.download_budget(client, 0.0005):
            root.get("https://images.invalid/never")
    assert root.calls == []


@pytest.mark.parametrize("on_exit", ["success", "error", "stop", "timeout"])
def test_all_methods_and_session_configuration_restore_exactly(transfer, on_exit):
    module, clock, root, client = transfer
    original_options, original_retry = root.session.curl_options, root.session.retry
    original_get, original_post = root.get, root.post
    try:
        with module.download_budget(client, 5):
            root.get("https://images.invalid/page")
            assert root.session.retry.count == 0
            if on_exit == "error":
                raise RuntimeError("fixture error")
            if on_exit == "timeout":
                clock.advance(6)
            if on_exit == "stop":
                raise module.DownloadInterrupted()
    except (RuntimeError, module.TransferTimeout, module.DownloadInterrupted):
        pass
    assert root.get == original_get and root.post == original_post
    assert "get" not in root.__dict__ and "post" not in root.__dict__
    assert root.session.curl_options is original_options
    assert root.session.retry is original_retry
    assert root.session.impersonate == "chrome"
    assert root.session.proxies["https"].endswith("@proxy.invalid")


@pytest.mark.parametrize("control", ["stop", "cancel"])
@pytest.mark.parametrize("when", ["before", "during"])
def test_explicit_stop_or_cancel_never_becomes_transfer_failure(transfer, control, when):
    module, _, root, client = transfer
    stop = threading.Event()
    canceled = [False]
    def set_control():
        stop.set() if control == "stop" else canceled.__setitem__(0, True)
    if when == "before":
        set_control()
    else:
        def answer(**kwargs):
            set_control()
            return "response"
        root.answer = answer
    with pytest.raises(module.DownloadInterrupted) as failure:
        with module.download_budget(client, 5, stop_event=stop, should_cancel=lambda: canceled[0]):
            root.get("https://images.invalid/page")
    assert failure.value.reason == ("stopped" if control == "stop" else "canceled")
    assert len(root.calls) == (0 if when == "before" else 1)


def test_deadline_is_checked_when_a_transport_exception_is_wrapped(transfer):
    module, clock, root, client = transfer
    def answer(**kwargs):
        clock.advance(kwargs["timeout"])
        raise RuntimeError("private-password")
    root.answer = answer
    with pytest.raises(module.TransferTimeout) as failure:
        with module.download_budget(client, 5):
            root.get("https://images.invalid/page")
    assert "private-password" not in str(failure.value)


def test_typed_transport_timeout_remains_safe_before_overall_deadline(transfer):
    module, _, root, client = transfer
    def answer(**kwargs):
        error = RuntimeError("private-password")
        error.code = 28
        raise error
    root.answer = answer
    with pytest.raises(module.TransferTimeout) as failure:
        with module.download_budget(client, 5):
            root.get("https://images.invalid/page")
    assert failure.value.reason == "transport"
    assert "private-password" not in str(failure.value)


def test_other_clients_and_option_configuration_are_untouched(transfer):
    module, _, root, client = transfer
    other = Transport()
    before = other.session.curl_options
    with module.download_budget(client, 5):
        root.get("https://images.invalid/page")
        other.get("https://images.invalid/unrelated")
    assert other.calls[0][2] == {} and other.session.curl_options is before
    assert other.session.retry.count == 2


def test_shared_and_nested_clients_are_rejected(transfer):
    module, _, root, client = transfer
    client._jm_downloader_shared = True
    with pytest.raises(module.TransferConfigurationError):
        with module.download_budget(client, 5):
            pass
    client._jm_downloader_shared = False
    with module.download_budget(client, 5):
        with pytest.raises(module.TransferConfigurationError):
            with module.download_budget(client, 5):
                pass
        assert root.get("https://images.invalid/page") == "response"


def test_control_callback_errors_do_not_leak_private_details(transfer):
    module, _, _, client = transfer
    def fail():
        raise RuntimeError("private-password")
    with pytest.raises(module.TransferConfigurationError) as failure:
        with module.download_budget(client, 5, should_cancel=fail):
            pass
    assert "private-password" not in str(failure.value)


@pytest.mark.parametrize("timeout", [None, True, 0, -1, 121, float("nan"), float("inf"), "30"])
def test_invalid_budget_is_rejected_before_any_request(transfer, timeout):
    module, _, root, client = transfer
    with pytest.raises(ValueError):
        with module.download_budget(client, timeout):
            pass
    assert not root.calls


def test_actual_sdk_retries_share_one_deadline_and_restore_transport(client, monkeypatch):
    from core import transfer, sdk_adapter
    from jmcomic import JmModuleConfig
    clock = Clock()
    monkeypatch.setattr(transfer, "_clock", clock)
    monkeypatch.setattr(JmModuleConfig, "DOMAIN_API_LIST", ["api-one.invalid", "api-two.invalid"])
    instance = sdk_adapter.new_client(sdk_adapter.build_option())
    root = instance.get_root_postman()
    original_options = root.session.curl_options
    requests = []
    def get(url, **kwargs):
        requests.append((url, kwargs["timeout"], dict(root.session.curl_options)))
        clock.advance(min(2, kwargs["timeout"]))
        raise ConnectionError("fixture transport failure")
    monkeypatch.setattr(root.session, "get", get)
    try:
        with pytest.raises(transfer.TransferTimeout):
            with transfer.download_budget(instance, 5):
                instance.download_image("https://image.invalid/media/photos/123/00001.webp?v=keep", "unused.webp")
        assert [attempt[1] for attempt in requests] == [5, 3, 1]
        assert all(attempt[0].endswith("/media/photos/123/00001.webp?v=keep") for attempt in requests)
        assert root.session.curl_options is original_options
    finally:
        from core.jm_service import close_client
        close_client(instance)
