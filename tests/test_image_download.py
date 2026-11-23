"""Domain fallback keeps SDK decoding and one operation deadline."""
from types import SimpleNamespace

import pytest


@pytest.fixture
def network(monkeypatch):
    from core import image_download, transfer
    from jmcomic import JmModuleConfig
    monkeypatch.setattr(image_download, "_good_host", None)
    monkeypatch.setattr(image_download, "_failed_hosts", {})
    monkeypatch.setattr(JmModuleConfig, "DOMAIN_IMAGE_LIST", ["first.invalid", "second.invalid"])
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(transfer, "_clock", lambda: clock.now)
    calls = []
    session = SimpleNamespace(curl_options={}, retry=object())
    def get(url, **kwargs):
        calls.append((url, kwargs["timeout"]))
        if url.endswith("/unavailable"):
            clock.now += 1
            raise ConnectionError("missing page")
        if "first.invalid" in url:
            clock.now += 2
            raise ConnectionError("bad node")
        clock.now += 1
        return b"content"
    root = SimpleNamespace(get=get, post=get, session=session)
    class Client:
        retry_times = 9
        def get_root_postman(self):
            return root
        def download_image(self, **kwargs):
            assert self.retry_times == 0
            return root.get(kwargs["img_url"])
    return Client(), calls, clock, root


def test_different_image_node_uses_remaining_budget_and_same_path_query(network):
    from core import image_download
    client, calls, _, root = network
    before = root.session.curl_options
    result = image_download.download_image(client, img_url="https://first.invalid/media/photos/7/00001.webp?v=2",
                                         img_save_path="unused", scramble_id=123, decode_image=True,
                                         timeout=5, attempts=3)
    assert result == b"content"
    assert calls == [("https://first.invalid/media/photos/7/00001.webp?v=2", 5),
                     ("https://second.invalid/media/photos/7/00001.webp?v=2", 3)]
    assert client.retry_times == 9 and root.session.curl_options is before


@pytest.mark.parametrize(("attempts", "expected_hosts"), [
    (1, ["first.invalid"]),
    (2, ["first.invalid", "second.invalid"]),
    (9, ["first.invalid", "second.invalid", "third.invalid"]),
])
def test_image_attempt_limit_never_repeats_a_node_within_a_pass(network, monkeypatch, attempts, expected_hosts):
    from core import image_download
    from jmcomic import JmModuleConfig
    client, calls, _, _ = network
    monkeypatch.setattr(JmModuleConfig, "DOMAIN_IMAGE_LIST", ["second.invalid", "first.invalid", "third.invalid"])
    with pytest.raises(ConnectionError, match="missing page"):
        image_download.download_image(client, img_url="https://first.invalid/unavailable", img_save_path="unused",
                                      scramble_id=None, decode_image=False, timeout=5, attempts=attempts)
    assert calls == [(f"https://{host}/unavailable", 5 - index) for index, host in enumerate(expected_hosts)]
    assert client.retry_times == 9


def test_next_page_prioritizes_a_node_that_recently_succeeded(network):
    from core import image_download
    client, calls, _, _ = network
    arguments = dict(img_url="https://first.invalid/image", img_save_path="unused", scramble_id=None,
                     decode_image=False, timeout=5, attempts=2)
    image_download.download_image(client, **arguments)
    image_download.download_image(client, **arguments)
    assert [url for url, _ in calls] == ["https://first.invalid/image", "https://second.invalid/image",
                                       "https://second.invalid/image"]


def test_full_disk_does_not_retry_or_demote_a_working_mirror(network, monkeypatch):
    import errno
    from core import image_download
    client, calls, _, _ = network
    def full(**kwargs):
        calls.append(kwargs['img_url'])
        raise OSError(errno.ENOSPC, 'fixture disk full')
    monkeypatch.setattr(client, 'download_image', full)
    with pytest.raises(OSError) as failed:
        image_download.download_image(client, img_url='https://first.invalid/page', img_save_path='unused',
                                     scramble_id=None, decode_image=True, timeout=5, attempts=3)
    assert calls == ['https://first.invalid/page'] and not image_download._failed_hosts
    assert '[local_file]' in image_download.failure_detail(failed.value)
    assert '[timeout]' in image_download.failure_detail(TimeoutError('fixture timeout'))


def test_recursion_error_does_not_retry_mirrors_or_demote_host(network, monkeypatch):
    from core import image_download
    client, calls, _, _ = network
    def recurse(**kwargs):
        calls.append(kwargs['img_url'])
        raise RecursionError('maximum recursion depth exceeded while calling a Python object')
    monkeypatch.setattr(client, 'download_image', recurse)
    with pytest.raises(RecursionError) as failed:
        image_download.download_image(client, img_url='https://first.invalid/page', img_save_path='unused',
                                     scramble_id=None, decode_image=True, timeout=5, attempts=3)
    assert calls == ['https://first.invalid/page'] and not image_download._failed_hosts
    assert image_download.non_retryable_error(failed.value) is True
    assert '[page_error]' in image_download.failure_detail(failed.value)


def test_pass_one_does_not_queue_non_retryable_error_for_pass_two(client, tmp_path, monkeypatch):
    import threading
    from core import jm_service, path_guard
    root = tmp_path / "downloads"
    folder = root / "book_123"
    folder.mkdir(parents=True)
    for module in (jm_service, path_guard):
        monkeypatch.setattr(module, "DOWNLOAD_ROOT", root)
    monkeypatch.setattr(
        jm_service.image_download,
        "download_image",
        lambda *args, **kwargs: (_ for _ in ()).throw(RecursionError("maximum recursion depth exceeded")),
    )
    img = SimpleNamespace(download_url="https://first.invalid/00001.webp", img_file_name="00001.webp", scramble_id=None)
    done, pending, failed = [0], [], []
    jm_service._download_image_single_attempt(
        None, "job_non_retryable", "123", None, "", [img], 0, img, img.download_url,
        folder / "00001.webp", None, done, 1, failed, pending, None, folder, 1, 5, threading.Lock(),
    )
    assert done == [0]
    assert not pending
    assert len(failed) == 1 and "[page_error]" in failed[0]


