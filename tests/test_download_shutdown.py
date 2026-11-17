"""Runtime shutdown is recoverable; it must not become cancellation or completion."""
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image
from fake_transport import owned_client


@pytest.fixture
def job(client, tmp_path, monkeypatch):
    from core import database as db, jm_service, path_guard
    from core.progress import progress_manager
    folder = tmp_path / "downloads"
    folder.mkdir()
    monkeypatch.setattr(path_guard, "DOWNLOAD_ROOT", folder)
    monkeypatch.setattr(jm_service, "DOWNLOAD_ROOT", folder)
    db.insert_job("job_shutdown_test", "123", "Shutdown fixture", [])
    db.transition_job_status(
        "job_shutdown_test", ["queued", "running", "paused", "completed", "failed", "canceled"], "running",
    )
    tracker = progress_manager.create_tracker("job_shutdown_test")
    yield folder, tracker
    progress_manager.remove_tracker("job_shutdown_test")


def test_already_stopping_job_does_not_construct_an_upstream_client(job, monkeypatch):
    from core import database as db, jm_service
    stop = threading.Event()
    stop.set()
    construct = Mock(side_effect=AssertionError("no requests after stop"))
    monkeypatch.setattr(jm_service, "get_client", construct)
    jm_service.download_album_job("job_shutdown_test", "123", [], stop_event=stop)
    construct.assert_not_called()
    assert db.get_job("job_shutdown_test")["status"] == "running"  # The manager owns requeueing.
    assert not db.resource_records(["123"])


@pytest.mark.parametrize("raises", [False, True])
def test_stop_during_metadata_does_not_request_chapters_or_mark_failed(job, monkeypatch, raises):
    from core import database as db, jm_service
    stop = threading.Event()
    upstream = owned_client(Mock())
    def detail(_):
        stop.set()
        if raises:
            raise ConnectionError("interrupted transport")
        return [SimpleNamespace(photo_id="456")]
    upstream.get_album_detail.side_effect = detail
    monkeypatch.setattr(jm_service, "get_client", lambda **kwargs: (upstream, None))
    close = Mock()
    monkeypatch.setattr(jm_service, "close_client", close)
    jm_service.download_album_job("job_shutdown_test", "123", [], stop_event=stop)
    upstream.check_photo.assert_not_called()
    assert db.get_job("job_shutdown_test")["status"] == "running"
    assert db.get_job("job_shutdown_test")["error_message"] is None
    assert not db.resource_records(["123"])
    assert any(call.args == (upstream,) for call in close.call_args_list)


@pytest.mark.parametrize("raises", [False, True])
def test_stop_during_image_preserves_old_page_without_retry_or_failure(job, raises):
    from core import database as db, jm_service
    from core.settings import update_settings
    folder, tracker = job
    update_settings({"skip_existing": False})
    page = folder / "00001.webp"
    Image.new("RGB", (8, 8), "red").save(page, "WEBP")
    original = page.read_bytes()
    stop = threading.Event()
    def download(**kwargs):
        Image.new("RGB", (8, 8), "blue").save(kwargs["img_save_path"], "WEBP")
        stop.set()
        if raises:
            raise ConnectionError("connection stopped")
    upstream = owned_client(Mock(download_image=Mock(side_effect=download)))
    failed, retry, done = [], [], [0]
    jm_service._download_image_single_attempt(
        upstream, "job_shutdown_test", "123", None, "", [1], 0,
        SimpleNamespace(is_gif=False), "https://fixture.invalid/page", page, None,
        done, 1, failed, retry, tracker, folder, 1, 10, threading.Lock(), stop_event=stop,
    )
    assert page.read_bytes() == original
    assert sorted(path.name for path in folder.iterdir()) == [page.name]
    assert done == [0] and failed == [] and retry == []
    assert db.get_job("job_shutdown_test")["status"] == "running"


@pytest.mark.parametrize("status", ["paused", "queued"])
def test_paused_or_requeued_executor_yields_without_canceled_event(job, status):
    from core import database as db, jm_service
    _, tracker = job
    db.transition_job_status(
        "job_shutdown_test", ["running"], status,
    )
    tracker.push = Mock()
    assert jm_service._should_stop("job_shutdown_test", tracker, threading.Event())
    assert db.get_job("job_shutdown_test")["status"] == status
    tracker.push.assert_not_called()


@pytest.mark.parametrize("paused", [False, True])
def test_manager_waits_for_real_inflight_call_and_recovers_only_after_it_exits(job, monkeypatch, paused):
    from core import database as db, jm_service, job_manager as jobs
    manager = jobs.JobManager()
    entered, release = threading.Event(), threading.Event()
    upstream = owned_client(Mock())
    def detail(_):
        entered.set()
        assert release.wait(3)
        raise ConnectionError("request ended after stop")
    upstream.get_album_detail.side_effect = detail
    monkeypatch.setattr(jm_service, "get_client", lambda **kwargs: (upstream, None))
    monkeypatch.setattr(jm_service, "close_client", Mock())
    thread = threading.Thread(target=manager._run_job_wrapper,
                              args=("job_shutdown_test", "123", []))
    manager._running_jobs["job_shutdown_test"] = thread
    manager._running_albums["job_shutdown_test"] = "123"
    thread.start()
    try:
        assert entered.wait(2)
        if paused:
            assert manager.pause_job("job_shutdown_test")[0]
        assert manager.stop(timeout=0.01) is False
        assert "job_shutdown_test" in manager._running_jobs
        assert db.get_job("job_shutdown_test")["status"] == ("paused" if paused else "running")
        with pytest.raises(RuntimeError, match="停止中"):
            manager.start()
    finally:
        release.set()
        thread.join(3)
    assert manager.stop(timeout=2) is True
    assert db.get_job("job_shutdown_test")["status"] == ("paused" if paused else "queued")
    assert not db.resource_records(["123"])
