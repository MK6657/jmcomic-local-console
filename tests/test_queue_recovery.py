"""Recover interrupted work behind the queue, without moving user history or ownership."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


def _job(job_id, album_id, status="queued", *, created_at="2026-01-01T00:00:00", queued_at=None):
    from core import database as db
    with db.transaction() as conn:
        conn.execute("INSERT INTO jobs (job_id, album_id, title, selected_photo_ids, status, "
                     "created_at, updated_at, queued_at) VALUES (?, ?, 'Queue fixture', '[\"71\"]', ?, ?, ?, ?)",
                     (job_id, album_id, status, created_at, created_at, queued_at))


def _claim_all():
    from core import database as db
    result = []
    while row := db.claim_next_queued_job():
        result.append(row["job_id"])
    return result


def test_recovered_old_task_waits_after_existing_fifo_without_changing_creation_history(client):
    from core import database as db
    from core.job_manager import JobManager
    _job("job_old_blocked", "100", "running", created_at="2026-01-01T00:00:00")
    _job("job_waiting_first", "200", created_at="2026-02-01T00:00:00")
    _job("job_waiting_second", "300", created_at="2026-02-02T00:00:00")
    _job("job_user_paused", "400", "paused", created_at="2025-01-01T00:00:00")
    manager = JobManager()
    assert manager.recover_interrupted() == 1
    recovered = db.get_job("job_old_blocked")
    assert recovered["created_at"] == "2026-01-01T00:00:00"
    assert recovered["queued_at"] > "2026-02-02T00:00:00"
    assert manager.recover_interrupted() == 0
    assert db.get_job("job_old_blocked")["queued_at"] == recovered["queued_at"]
    assert _claim_all() == ["job_waiting_first", "job_waiting_second", "job_old_blocked"]
    assert db.get_job("job_user_paused")["status"] == "paused"


def test_recovery_appends_multiple_tasks_in_their_original_fifo_order_even_after_clock_rollback(client):
    from core import database as db
    from core.job_manager import JobManager
    _job("job_running_later", "300", "running", created_at="2026-02-02T00:00:00")
    _job("job_running_earlier", "200", "running", created_at="2026-02-01T00:00:00")
    _job("job_already_waiting", "100", created_at="2026-01-01T00:00:00",
         queued_at="2099-01-01T00:00:00")
    assert JobManager().recover_interrupted() == 2
    first = db.get_job("job_running_earlier")["queued_at"]
    second = db.get_job("job_running_later")["queued_at"]
    assert "2099-01-01T00:00:00" < first < second
    assert _claim_all() == ["job_already_waiting", "job_running_earlier", "job_running_later"]


def test_queued_metadata_updates_do_not_move_explicit_queue_position(client):
    from core import database as db
    _job("job_first", "100", queued_at="2026-01-01T00:00:00")
    _job("job_second", "200", created_at="2026-01-02T00:00:00", queued_at="2026-01-02T00:00:00")
    db.update_job("job_first", output_path="not-used-in-this-queue-test")
    assert db.get_job("job_first")["updated_at"] > db.get_job("job_second")["updated_at"]
    assert _claim_all() == ["job_first", "job_second"]


def test_shutdown_requeue_and_orphan_resume_append_after_waiting_without_duplicate_jobs(client):
    from core import database as db, task_commands
    _job("job_old_running", "100", "running")
    _job("job_old_paused", "200", "paused")
    _job("job_waiting", "300", created_at="2026-02-01T00:00:00")
    assert task_commands.requeue_interrupted("job_old_running")
    assert not task_commands.requeue_interrupted("job_old_running")
    assert task_commands.resume_download("job_old_paused", has_executor=False)[0]
    assert not task_commands.resume_download("job_old_paused", has_executor=False)[0]
    assert len(db.get_all_jobs()) == 3
    assert _claim_all() == ["job_waiting", "job_old_running", "job_old_paused"]


def test_recovery_never_releases_or_requeues_an_owned_executor(client):
    from core import database as db
    from core.job_manager import JobManager
    manager = JobManager()
    _job("job_owned", "100", "running")
    _job("job_stranded", "200", "running")
    _job("job_waiting", "300", created_at="2026-02-01T00:00:00")
    owner = Mock()
    manager._running_jobs["job_owned"] = owner
    manager._running_albums["job_owned"] = "100"
    assert manager.recover_interrupted() == 1
    assert manager._running_jobs["job_owned"] is owner
    assert db.get_job("job_owned")["status"] == "running"
    assert db.get_job("job_owned")["queued_at"] is None
    assert _claim_all() == ["job_waiting", "job_stranded"]


def test_concurrent_recovery_is_atomic_and_preserves_one_stable_order(client):
    from core import database as db
    from core.task_commands import recover_interrupted
    _job("job_interrupted", "100", "running")
    _job("job_waiting", "200", created_at="2026-02-01T00:00:00")
    barrier = threading.Barrier(4)
    def recover(_):
        barrier.wait(timeout=5)
        return recover_interrupted()
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(recover, range(4))) == 1
    assert len(db.get_all_jobs()) == 2
    assert _claim_all() == ["job_waiting", "job_interrupted"]


def test_recovery_queue_position_and_status_roll_back_together(client, monkeypatch):
    from core import database as db, task_commands
    _job("job_interrupted", "100", "running")
    _job("job_waiting", "200", created_at="2026-02-01T00:00:00")
    monkeypatch.setattr(task_commands, "_sync_wishlist", Mock(side_effect=RuntimeError("write failed")))
    with pytest.raises(RuntimeError, match="write failed"):
        task_commands.recover_interrupted()
    job = db.get_job("job_interrupted")
    assert job["status"] == "running" and job["queued_at"] is None
    assert _claim_all() == ["job_waiting"]


def test_repeated_restarts_let_each_waiting_task_advance_before_the_old_task(client):
    from core import database as db
    from core.job_manager import JobManager
    _job("job_bad_old", "100", "running")
    _job("job_next", "200", created_at="2026-02-01T00:00:00")
    _job("job_later", "300", created_at="2026-02-02T00:00:00")
    assert JobManager().recover_interrupted() == 1
    first = db.claim_next_queued_job()
    assert first["job_id"] == "job_next"
    # A second process restart requeues the work interrupted during that run at
    # the tail too; already waiting work must keep advancing before either retry.
    assert JobManager().recover_interrupted() == 1
    assert db.claim_next_queued_job()["job_id"] == "job_later"
    assert db.get_job("job_bad_old")["created_at"] == "2026-01-01T00:00:00"


@pytest.fixture
def real_download_pipeline(client, tmp_path, monkeypatch):
    """Keep the actual task/chapter/page/storage pipeline; fake only SDK transport."""
    from core import image_download, jm_service, job_manager as jobs, path_guard, transfer
    from core.settings import update_settings
    from PIL import Image
    from curl_cffi import CurlOpt

    downloads = tmp_path / "downloads"
    downloads.mkdir()
    monkeypatch.setattr(path_guard, "DOWNLOAD_ROOT", downloads)
    monkeypatch.setattr(jm_service, "DOWNLOAD_ROOT", downloads)
    manager = jobs.JobManager()
    monkeypatch.setattr(jobs, "job_manager", manager)
    update_settings({"timeout": "5", "retry_times": "0", "max_running_jobs": "1",
                     "image_threads": "1", "photo_threads": "1", "auto_pack": "false"})
    state = SimpleNamespace(now=1000.0, active=0, clients=[], requests=[], order=[],
                            entered=threading.Event(), release=threading.Event(), lock=threading.Lock())
    monkeypatch.setattr(transfer, "_clock", lambda: state.now)
    monkeypatch.setattr(image_download, "_good_host", None)
    monkeypatch.setattr(image_download, "_failed_hosts", {})

    class Session:
        def __init__(self, owner):
            self.owner = owner
            self.curl_options = {}
            self.retry = None
        def close(self):
            assert not self.owner.active, "Transport may close only after its request actually returns"
            self.owner.closed = True

    class Transport:
        def __init__(self):
            self.active = False
            self.closed = False
            self.session = Session(self)
        def post(self, url, **kwargs):
            return self.get(url, **kwargs)
        def get(self, url, **kwargs):
            assert not self.closed
            timeout = kwargs.get("timeout")
            assert isinstance(timeout, (int, float)) and 0 < timeout <= 5
            assert kwargs.get("stream") is False
            assert 0 < self.session.curl_options[CurlOpt.TIMEOUT_MS] <= 5000
            self.active = True
            with state.lock:
                state.active += 1
                state.requests.append((url, timeout))
            try:
                if "/images/100/" in url:
                    state.entered.set()
                    assert state.release.wait(5)
                    # Consume the *transport's provided remaining timeout*. The
                    # budget gate, rather than a mock worker, must terminate it.
                    state.now += timeout + 0.01
                    raise TimeoutError("fixture exhausted request deadline")
                return SimpleNamespace(status_code=200)
            finally:
                self.active = False
                with state.lock:
                    state.active -= 1

    class Photo(list):
        def __init__(self, album_id):
            super().__init__([SimpleNamespace(download_url=f"https://images.invalid/images/{album_id}/00001.webp",
                                               scramble_id=None, is_gif=False)])
            self.photo_id = "71"
            self.name = "Chapter"
            self.page_arr = ["00001.webp"]

    class Album(list):
        def __init__(self, album_id):
            photo = Photo(album_id)
            super().__init__([photo])
            self.album_id = album_id
            self.name = "Blocked" if album_id == "100" else "Normal"
            self.author = "Fixture"
            self.tags = []
            self.episode_list = [(photo.photo_id, 1, photo.name)]

    class Client:
        def __init__(self):
            self.root = Transport()
            self.retry_times = 7  # Controlled image scope must not multiply SDK retry loops.
        def get_root_postman(self):
            return self.root
        def get_album_detail(self, album_id):
            self.root.get(f"https://api.invalid/album/{album_id}")
            with state.lock:
                state.order.append(album_id)
            return Album(album_id)
        def check_photo(self, photo):
            self.root.get(f"https://api.invalid/chapter/{photo.photo_id}")
        def download_image(self, *, img_url, img_save_path, scramble_id, decode_image):
            assert self.retry_times == 0
            self.root.get(img_url)
            Image.new("RGB", (4, 4), "white").save(img_save_path, "WEBP")

    def construct(shared=True):
        assert not shared
        instance = Client()
        state.clients.append(instance)
        return instance, None
    monkeypatch.setattr(jm_service, "get_client", construct)
    try:
        yield manager, state, downloads
    finally:
        state.release.set()
        assert manager.stop(timeout=5)


def _await_status(job_id, expected, timeout=5):
    from core import database as db
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = db.get_job(job_id)
        if job["status"] == expected:
            return job
        threading.Event().wait(0.01)
    pytest.fail(f"{job_id} did not reach {expected}; last status: {job['status']}")


def test_bounded_real_download_failure_releases_capacity_only_after_transport_exits(real_download_pipeline):
    from core import database as db
    manager, state, downloads = real_download_pipeline
    _job("job_bad", "100")
    _job("job_good", "200", created_at="2026-02-01T00:00:00")
    manager.start()
    assert state.entered.wait(3)
    assert state.active == 1
    manager.schedule_next()
    assert db.get_job("job_good")["status"] == "queued"
    assert manager.get_running_count() == 1  # A status timeout never detaches a live writer.
    state.release.set()
    _await_status("job_bad", "failed")
    _await_status("job_good", "completed")
    assert manager.stop(timeout=3)
    assert state.active == 0 and manager.get_running_count() == 0
    assert all(instance.root.closed for instance in state.clients)
    image_attempts = [(url, timeout) for url, timeout in state.requests if "/images/100/" in url]
    assert len(image_attempts) == 2  # first pass plus bounded final retry; no hidden SDK retries
    assert all(0 < timeout <= 5 for _, timeout in image_attempts)
    assert db.resource_records(["100"]) == []
    assert len(db.resource_records(["200"])) == 1
    assert len(list((downloads / "Normal_200").rglob("00001.webp"))) == 1


def test_recovered_task_does_not_block_real_normal_download_after_restart(real_download_pipeline):
    from core import database as db
    manager, state, downloads = real_download_pipeline
    _job("job_old_bad", "100", "running")
    _job("job_waiting_good", "200", created_at="2026-02-01T00:00:00")
    assert manager.recover_interrupted() == 1
    manager.start()
    completed = _await_status("job_waiting_good", "completed")
    assert completed["done_pages"] == completed["total_pages"] == 1
    assert state.order[0] == "200"
    assert state.entered.wait(4)  # recovered work is attempted after the waiting work
    state.release.set()
    _await_status("job_old_bad", "failed")
    assert manager.stop(timeout=3)
    assert state.active == 0 and all(instance.root.closed for instance in state.clients)
    assert db.get_job("job_old_bad")["created_at"] == "2026-01-01T00:00:00"
