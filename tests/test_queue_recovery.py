"""Recover interrupted work behind the queue, without moving user history or ownership."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlsplit

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
    assert task_commands.resume_download("job_old_paused")[0]
    assert not task_commands.resume_download("job_old_paused")[0]
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
    from jmcomic import JmModuleConfig
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
    state = SimpleNamespace(now=1000.0, active=0, clients=[], order=[], faults=True, first_title="Blocked",
                            counts=Counter(), block_on_slow_attempt=1,
                            entered=threading.Event(), release=threading.Event(), lock=threading.Lock())
    monkeypatch.setattr(transfer, "_clock", lambda: state.now)
    monkeypatch.setattr(image_download, "_good_host", None)
    monkeypatch.setattr(image_download, "_failed_hosts", {})
    monkeypatch.setattr(JmModuleConfig, "DOMAIN_IMAGE_LIST", ["alternate.invalid", "spare.invalid"])

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
            path = urlsplit(url).path
            with state.lock:
                state.active += 1
                state.counts[path] += 1
                attempt = state.counts[path]
            try:
                if path == getattr(state, 'hold_page', None):
                    state.entered.set()
                    assert state.release.wait(10)
                if state.faults and path == "/images/100/00001.webp":
                    if attempt == state.block_on_slow_attempt:
                        state.entered.set()
                        assert state.release.wait(5)
                    # Consume the *transport's provided remaining timeout*. The
                    # budget gate, rather than a mock worker, must terminate it.
                    state.now += timeout + 0.01
                    raise TimeoutError("fixture exhausted request deadline")
                if state.faults and path == "/images/100/00003.webp":
                    raise ConnectionError("fixture unavailable on this node")
                return SimpleNamespace(status_code=200)
            finally:
                self.active = False
                with state.lock:
                    state.active -= 1

    class Photo(list):
        def __init__(self, album_id):
            page_count = 3 if album_id == "100" else 1
            self.page_arr = [f"{index:05d}.webp" for index in range(1, page_count + 1)]
            super().__init__(SimpleNamespace(download_url=f"https://images.invalid/images/{album_id}/{name}",
                                             scramble_id=None, is_gif=False) for name in self.page_arr)
            self.photo_id = "71"
            self.name = "Chapter"

    class Album(list):
        def __init__(self, album_id):
            photo = Photo(album_id)
            super().__init__([photo])
            if album_id == '100' and getattr(state, 'two_chapters', False):
                other = Photo('second')
                other.photo_id, other.name = '72', 'Second'
                self.append(other)
            self.album_id = album_id
            self.name = state.first_title if album_id == "100" else "Normal"
            self.author = "Fixture"
            self.tags = []
            self.episode_list = [(p.photo_id, n, p.name) for n, p in enumerate(self, 1)]

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


@pytest.mark.parametrize(("retries", "slow_attempts", "broken_attempts"), [(0, 1, 1), (1, 2, 4)])
def test_bad_pages_finish_and_scheduler_runs_next_album(real_download_pipeline, retries, slow_attempts, broken_attempts):
    from core import database as db
    from core.settings import update_settings
    from PIL import Image
    manager, state, downloads = real_download_pipeline
    update_settings({"retry_times": retries})
    _job("job_bad", "100")
    _job("job_good", "200", created_at="2026-02-01T00:00:00")
    manager.start()
    assert state.entered.wait(3)
    assert state.active == 1
    assert db.get_job("job_good")["status"] == "queued"
    assert manager.get_running_count() == 1  # A status timeout never detaches a live writer.
    state.release.set()
    bad = _await_status("job_bad", "failed")
    good = _await_status("job_good", "completed")
    assert manager.stop(timeout=3)
    assert state.active == 0 and manager.get_running_count() == 0
    assert all(instance.root.closed for instance in state.clients)
    assert state.order == ["100", "200"]
    assert {path: count for path, count in state.counts.items() if path.startswith("/images/")} == {
        "/images/100/00001.webp": slow_attempts, "/images/100/00002.webp": 1,
        "/images/100/00003.webp": broken_attempts, "/images/200/00001.webp": 1,
    }
    assert (bad["done_pages"], bad["total_pages"]) == (1, 3)
    assert all(f"第 {page} 页下载失败" in bad["error_message"] for page in (1, 3))
    assert good["done_pages"] == good["total_pages"] == 1
    assert db.resource_records(["100"]) == []
    assert len(db.resource_records(["200"])) == 1
    from pathlib import Path
    for directory, expected in ((bad['output_path'], "00002.webp"), (good['output_path'], "00001.webp")):
        pages = list(Path(directory).rglob("*.webp"))
        assert [path.name for path in pages] == [expected]
        with Image.open(pages[0]) as image:
            image.verify()


@pytest.mark.parametrize("interrupt", ["cancel", "stop"])
def test_interrupt_in_second_pass_preserves_good_page_and_stops_retrying(real_download_pipeline, interrupt):
    from core import database as db
    from core.settings import update_settings
    manager, state, downloads = real_download_pipeline
    update_settings({"retry_times": 1})
    state.block_on_slow_attempt = 2
    _job("job_interrupted", "100")
    manager.start()
    assert state.entered.wait(3)
    if interrupt == "cancel":
        assert manager.cancel_job("job_interrupted")[0]
    else:
        assert not manager.stop(timeout=0.01)
    assert manager.get_running_count() == state.active == 1
    state.release.set()
    if interrupt == "cancel":
        _job("job_after_cancel", "200")
        _await_status("job_after_cancel", "completed")
    assert manager.stop(timeout=3)
    job = db.get_job("job_interrupted")
    assert job["status"] == ("canceled" if interrupt == "cancel" else "queued")
    assert job["error_message"] is None and job["done_pages"] == 1
    assert state.counts["/images/100/00001.webp"] == state.counts["/images/100/00003.webp"] == 2
    from pathlib import Path
    assert [page.name for page in Path(job['output_path']).rglob("*.webp")] == ["00002.webp"]
    assert state.active == manager.get_running_count() == 0 and all(instance.root.closed for instance in state.clients)


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


@pytest.fixture
def paused_page(real_download_pipeline, monkeypatch):
    from core import database as db, jm_service, storage
    from core.settings import update_settings
    from PIL import Image

    manager, state, downloads = real_download_pipeline
    state.faults = False
    update_settings({"skip_existing": False})
    from core.publication import WORK, _key
    album = downloads / WORK / _key('job_first') / 'pages'
    album.mkdir(parents=True)
    chapter = jm_service._chapter_output_dir(album, SimpleNamespace(photo_id="71", name="Chapter"))
    for name in ("00002.webp", "00003.webp"):
        Image.new("RGB", (8, 8), "red").save(chapter / name, "WEBP")
    old = (chapter / "00002.webp").read_bytes()
    entered, release = threading.Event(), threading.Event()
    publish = storage.StagedFile.publish

    def gated(staged, validate, **kwargs):
        def validate_then_hold(path):
            valid = validate(path)
            if staged.target == chapter / "00002.webp" and not entered.is_set():
                entered.set()
                assert release.wait(10)
            return valid
        return publish(staged, validate_then_hold, **kwargs)

    monkeypatch.setattr(storage.StagedFile, "publish", gated)
    _job("job_first", "100")
    _job("job_next", "200", created_at="2026-02-01T00:00:00")
    manager.start()
    try:
        assert entered.wait(5)
        assert db.get_job("job_first")["done_pages"] == 1
        assert manager.pause_job("job_first") == (True, "已暂停")
        yield manager, state, chapter, old, release
    finally:
        release.set()
        assert manager.stop(timeout=5)


@pytest.mark.parametrize("quick", [False, True], ids=["resume-after-restart", "resume-during-write"])
def test_pause_drains_then_runs_next_before_resuming_same_pages(paused_page, quick):
    from core import database as db
    from core.job_manager import JobManager
    manager, state, chapter, old, release = paused_page
    first_page = chapter / "00001.webp"
    signature = first_page.stat().st_mtime_ns, first_page.read_bytes()
    if quick:
        assert manager.resume_job("job_first")[0]
        assert not manager.resume_job("job_first")[0]
    manager.schedule_next()
    assert db.get_job("job_first")["status"] == ("queued" if quick else "paused")
    assert db.get_job("job_next")["status"] == "queued" and state.order == ["100"]
    assert manager.get_running_count() == 1 and (chapter / "00002.webp").read_bytes() == old
    state.first_title = "New upstream title"
    release.set()
    _await_status("job_next", "completed")
    resumed = manager if quick else JobManager()
    try:
        if not quick:
            assert manager.stop(timeout=3)
            assert (db.get_job("job_first")["status"], db.get_job("job_first")["done_pages"]) == ("paused", 2)
            assert resumed.recover_interrupted() == 0
            assert resumed.resume_job("job_first")[0]
        completed = _await_status("job_first", "completed")
    finally:
        assert resumed.stop(timeout=3)
    assert state.order == ["100", "200", "100"]
    assert completed["done_pages"] == completed["total_pages"] == 3
    from pathlib import Path
    published = Path(completed['output_path']) / chapter.name
    assert published.parent.name == 'Blocked_100'
    assert (published / first_page.name).read_bytes() == signature[1]
    assert all((published / name).read_bytes() != old for name in ("00002.webp", "00003.webp"))
    assert {path: count for path, count in state.counts.items() if path.startswith("/images/")} == {
        "/images/100/00001.webp": 1, "/images/100/00002.webp": 1, "/images/100/00003.webp": 1,
        "/images/200/00001.webp": 1,
    }
    assert {row["job_id"] for row in db.get_all_jobs()} == {"job_first", "job_next"}
    assert [row["job_id"] for row in db.resource_records(["100"])] == ["job_first"]
    assert not list(db.DB_DIR.glob("resume-*.json"))
    assert all(instance.root.closed for instance in state.clients)


@pytest.mark.parametrize("quick", [False, True])
@pytest.mark.parametrize("cancel", [False, True])
def test_interrupt_before_page_replacement_preserves_old_file_and_owner(paused_page, quick, cancel):
    from core import database as db, storage
    manager, state, chapter, old, release = paused_page
    if quick:
        assert manager.resume_job("job_first")[0]
    if cancel:
        assert manager.cancel_job("job_first")[0]
        assert not manager.resume_job("job_first")[0]
    assert not manager.stop(timeout=0.02)
    with pytest.raises(RuntimeError, match="停止中"):
        manager.start()
    assert manager.get_running_count() == 1 and db.get_job("job_next")["status"] == "queued"
    release.set()
    assert manager.stop(timeout=3)
    job = db.get_job("job_first")
    assert job["status"] == ("canceled" if cancel else "queued" if quick else "paused")
    assert job["done_pages"] == 1 and not job['error_message']
    assert not db.resource_records(["100"])
    assert (chapter / "00002.webp").read_bytes() == old
    assert not (chapter / storage.STAGING_DIRECTORY).exists()
    assert state.counts['/images/100/00003.webp'] == 0 and state.order == ["100"]
    assert all(instance.root.closed for instance in state.clients) and manager.get_running_count() == 0


@pytest.fixture
def drained_pause(paused_page):
    from core import database as db
    manager, state, chapter, _, release = paused_page
    release.set()
    _await_status("job_next", "completed")
    assert manager.stop(timeout=3)
    assert db.get_job("job_first")["status"] == "paused"
    assert len(list(db.DB_DIR.glob("resume-*.json"))) == 1
    state.counts.clear()
    return state, chapter


@pytest.mark.parametrize("change", ["moved", "deleted", "occupied", "outside"])
def test_resume_rebuilds_only_a_safe_original_directory(drained_pause, tmp_path, change):
    from core import database as db
    from core.job_manager import JobManager
    state, chapter = drained_pause
    original = chapter.parent
    moved = tmp_path / "kept-original"
    assert original.resolve().is_relative_to(tmp_path.resolve()) and moved.parent == tmp_path
    page = chapter / "00001.webp"
    saved = page.read_bytes()
    if change == "deleted":
        for name in (".jm-chapter.json", "00001.webp", "00002.webp", "00003.webp"):
            (chapter / name).unlink()
        chapter.rmdir()
        original.rmdir()
    else:
        original.rename(moved)
    if change == "occupied":
        original.write_bytes(b"keep this file")
    elif change == "outside":
        db.update_job("job_first", output_path=str(moved))
    state.first_title = "Changed while paused"
    resumed = JobManager()
    try:
        assert resumed.resume_job("job_first")[0]
        job = _await_status("job_first", "completed" if change in ("moved", "deleted") else "failed")
    finally:
        assert resumed.stop(timeout=3)
    requests = {path: count for path, count in state.counts.items() if path.startswith("/images/")}
    if change in ("moved", "deleted"):
        from pathlib import Path
        assert Path(job['output_path']).name == 'Blocked_100' and job["done_pages"] == job["total_pages"] == 3
        assert requests == {f"/images/100/{page:05d}.webp": 1 for page in (1, 2, 3)}
        assert len(list(Path(job['output_path']).rglob("*.webp"))) == 3
    else:
        assert not requests and job['error_message']
        if change == "occupied":
            assert original.read_bytes() == b"keep this file"
        else:
            assert "下载根目录" in job["error_message"] and not original.exists()
    if change != "deleted":
        assert (moved / chapter.name / page.name).read_bytes() == saved
    assert not list(db.DB_DIR.glob("resume-*.json"))


@pytest.mark.parametrize("skip", [True, False], ids=["verify-existing", "explicit-refresh"])
def test_full_restore_without_resume_sidecar_obeys_skip_existing(drained_pause, tmp_path, monkeypatch, skip):
    from core import database as db, jm_service, path_guard
    from core.backup import create_backup
    from core.job_manager import JobManager
    from core.restore import restore_backup
    from core.settings import invalidate_settings_cache, update_settings
    from PIL import Image
    state, chapter = drained_pause
    update_settings({"skip_existing": skip})
    (chapter / "00002.webp").write_bytes(b"damaged page")
    (chapter / "00003.webp").unlink()
    source_downloads = path_guard.DOWNLOAD_ROOT
    archive = tmp_path / "paused-full.zip"
    create_backup(db.DB_PATH, source_downloads, archive, include_downloads=True, service_stopped=True)
    restored = restore_backup(archive, tmp_path / "restored")
    assert restored.resources_included and not list(restored.database.parent.glob("resume-*.json"))
    monkeypatch.setattr(db, "DB_DIR", restored.database.parent)
    monkeypatch.setattr(db, "DB_PATH", restored.database)
    monkeypatch.setattr(path_guard, "DOWNLOAD_ROOT", restored.downloads)
    monkeypatch.setattr(jm_service, "DOWNLOAD_ROOT", restored.downloads)
    invalidate_settings_cache()
    restored_chapter = restored.downloads / chapter.relative_to(source_downloads)
    saved = (restored_chapter / "00001.webp").read_bytes()
    resumed = JobManager()
    try:
        assert db.get_job("job_first")["status"] == "paused"
        assert resumed.resume_job("job_first")[0]
        job = _await_status("job_first", "completed")
    finally:
        assert resumed.stop(timeout=3)
    from pathlib import Path
    assert job['done_pages'] == job['total_pages'] == 3
    restored_chapter = Path(job['output_path']) / chapter.name
    assert restored_chapter.is_relative_to(restored.downloads)
    assert [state.counts[f"/images/100/{page:05d}.webp"] for page in (1, 2, 3)] == [0, 1, 1]
    assert (restored_chapter / "00001.webp").read_bytes() == saved
    for page in (1, 2, 3):
        with Image.open(restored_chapter / f"{page:05d}.webp") as image:
            image.load()
    assert not list(db.DB_DIR.glob("resume-*.json"))
    assert (chapter / "00002.webp").read_bytes() == b"damaged page" and not (chapter / "00003.webp").exists()


@pytest.mark.parametrize("keep_record", [False, True], ids=["lost-checkpoint", "known-pages"])
def test_refresh_resume_verifies_changed_pages_and_requires_confirmation(drained_pause, keep_record):
    from core import database as db
    from core.job_manager import JobManager
    state, chapter = drained_pause
    record, = db.DB_DIR.glob("resume-*.json")
    if not keep_record:
        record.unlink()
        with db.transaction() as conn:
            conn.execute('DELETE FROM task_page_receipts WHERE job_id=?', ('job_first',))
    (chapter / "00002.webp").write_bytes(b"damaged after pause")
    resumed = JobManager()
    try:
        assert resumed.resume_job("job_first")[0]
        assert _await_status("job_first", "completed")["done_pages"] == 3
    finally:
        assert resumed.stop(timeout=3)
    assert [state.counts[f"/images/100/{page:05d}.webp"] for page in (1, 2, 3)] == [0 if keep_record else 1, 1, 1]
    assert not record.exists()


@pytest.mark.parametrize("discard", ["cancel-paused", "delete-queued"])
def test_discard_resume_record_without_removing_published_pages(drained_pause, client, discard):
    from core import database as db
    from core.job_manager import JobManager
    _, chapter = drained_pause
    saved = (chapter / "00001.webp").read_bytes()
    if discard == "cancel-paused":
        assert JobManager().cancel_job("job_first")[0]
        assert db.get_job("job_first")["status"] == "canceled"
    else:
        db.set_setting("schedule_enabled", "true")
        db.set_setting("schedule_end", "0")
        db.set_setting("schedule_start", "0")
        assert JobManager().resume_job("job_first")[0]
        assert db.get_job("job_first")["status"] == "queued"
        assert client.delete('/api/jobs/job_first').status_code == 200
        assert db.get_job("job_first") is None
    assert not list(db.DB_DIR.glob("resume-*.json"))
    assert (chapter / "00001.webp").read_bytes() == saved


@pytest.mark.parametrize("mode", ["flat", "pack-delete", "pack-only"])
def test_completed_album_is_available_while_maintenance_runs(real_download_pipeline, monkeypatch, mode):
    from core import album_maintenance, database as db
    from core.settings import update_settings
    manager, state, downloads = real_download_pipeline
    state.faults = False
    update_settings({'skip_existing': False, 'organize_mode': 'flat' if mode == 'flat' else 'none',
                     'auto_pack': mode != 'flat', 'delete_originals': mode == 'pack-delete'})
    entered, release = threading.Event(), threading.Event()
    finalize = album_maintenance.finalize_published
    def hold(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return finalize(*args, **kwargs)
    monkeypatch.setattr(album_maintenance, 'finalize_published', hold)
    _job('job_files', '100')
    manager.start()
    try:
        assert entered.wait(7)
        assert db.get_job('job_files')['status'] == 'completed'
        assert db.resource_records(['100'])
        assert not manager.pause_job('job_files')[0]
        assert album_maintenance.statuses(['100'])['100']['state'] == 'running'
    finally:
        release.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and album_maintenance.statuses(['100'])['100']['state'] == 'running':
        threading.Event().wait(.01)
    assert album_maintenance.statuses(['100'])['100']['state'] == 'completed'
    assert manager.stop(timeout=3)
    assert [state.counts[f'/images/100/{p:05d}.webp'] for p in (1,2,3)] == [1,1,1]


def test_quick_resume_wins_before_flat_files_and_skips_confirmed_pages(real_download_pipeline, monkeypatch):
    from core import database as db
    from core.settings import update_settings
    manager, state, downloads = real_download_pipeline
    state.faults = False
    update_settings({"skip_existing": False, "organize_mode": "flat"})
    entering, release = threading.Event(), threading.Event()
    begin = manager.begin_file_finalization
    def before_files(job_id):
        if job_id == "job_writing" and not entering.is_set():
            entering.set()
            assert release.wait(10)
        return begin(job_id)
    monkeypatch.setattr(manager, "begin_file_finalization", before_files)
    _job("job_writing", "100")
    _job("job_waiting", "200")
    manager.start()
    try:
        assert entering.wait(5)
        assert db.get_job("job_writing")["done_pages"] == 3
        assert manager.pause_job("job_writing")[0] and manager.resume_job("job_writing")[0]
        assert not list((downloads / "Blocked_100").glob("*.webp"))
    finally:
        release.set()
    _await_status("job_writing", "completed")
    from core import album_maintenance
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline and album_maintenance.statuses(["100"])["100"]["state"] != "completed":
        threading.Event().wait(.02)
    assert manager.stop(timeout=3) and state.order == ["100", "200", "100"]
    assert len(list((downloads / "Blocked_100").glob("*.webp"))) == 3
    assert [state.counts[f"/images/100/{page:05d}.webp"] for page in (1, 2, 3)] == [1, 1, 1]


def test_same_album_pause_publishes_only_finished_chapters(real_download_pipeline, client, monkeypatch):
    """A pauses after one page; B may publish chapter 72 and unrelated C must run."""
    import io
    import zipfile
    import pikepdf
    from core import database as db
    from core.settings import update_settings
    from core.task_commands import enqueue_download
    from routes import api_export, api_preview
    manager, state, downloads = real_download_pipeline
    for module in (api_export, api_preview):
        monkeypatch.setattr(module, 'DOWNLOAD_ROOT', downloads)
    state.two_chapters, state.faults = True, False
    state.hold_page = '/images/100/00002.webp'
    update_settings({'skip_existing': False, 'auto_pack': True, 'delete_originals': True})
    a = enqueue_download('100', 'A', ['71']).job_id
    b = enqueue_download('100', 'B', ['72']).job_id
    c = enqueue_download('200', 'C', ['71']).job_id
    manager.start()
    try:
        assert state.entered.wait(5)
        assert db.get_job(a)['done_pages'] == 1
        assert manager.pause_job(a)[0]
    finally:
        state.release.set()
    _await_status(b, 'completed')
    _await_status(c, 'completed')
    assert db.get_job(a)['status'] == 'paused'
    preview = client.get('/api/preview/100')
    assert preview.status_code == 200
    assert [p['photo_id'] for p in preview.json['pages']] == ['72']
    for endpoint in (f'/api/export/{b}', '/api/library/100/export'):
        with client.post(endpoint + '/zip') as response:
            assert response.status_code == 200
            with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
                names = [n for n in archive.namelist() if n.lower().endswith('.webp')]
                assert len(names) == 1 and '__72/' in names[0]
        with client.post(endpoint + '/pdf') as response:
            assert response.status_code == 200
            with pikepdf.open(io.BytesIO(response.data)) as document:
                assert len(document.pages) == 1
    assert manager.resume_job(a)[0]
    _await_status(a, 'completed')
    assert state.counts['/images/100/00001.webp'] == 1
    assert sorted(p['photo_id'] for p in client.get('/api/preview/100').json['pages']) == ['71'] * 3 + ['72']


def test_restart_merges_the_book_published_while_interrupted_task_was_requeued(real_download_pipeline, monkeypatch, client):
    from core import database as db, jm_service, publication
    from core.progress import progress_manager
    from core.task_commands import enqueue_download, recover_interrupted
    from core.transfer import DownloadInterrupted
    _, state, root = real_download_pipeline
    from routes import api_preview
    monkeypatch.setattr(api_preview, 'DOWNLOAD_ROOT', root)
    state.faults, state.two_chapters = False, True
    first = enqueue_download('100', 'A', ['71']).job_id
    second = enqueue_download('100', 'B', ['72']).job_id
    def run(job, chapter):
        assert db.claim_next_queued_job()['job_id'] == job
        progress_manager.create_tracker(job)
        try:
            jm_service.download_album_job(job, '100', [chapter])
        finally:
            progress_manager.remove_tracker(job)
    project = publication.Workspace.project
    def exit_after_rename(*args):
        project(*args)
        raise DownloadInterrupted('stopped')
    with monkeypatch.context() as patch:
        patch.setattr(publication.Workspace, 'project', exit_after_rename)
        run(first, '71')
    assert recover_interrupted() == 1
    run(second, '72')
    run(first, '71')
    assert all(db.get_job(job)['status'] == 'completed' for job in (first, second))
    assert sorted(p['photo_id'] for p in client.get('/api/preview/100').json['pages']) == ['71'] * 3 + ['72']
    assert [state.counts[f'/images/100/{p:05d}.webp'] for p in (1, 2, 3)] == [1, 1, 1]


@pytest.mark.parametrize('boundary', ['seal', 'project', 'committed'])
def test_publication_restart_at_filesystem_database_boundary(real_download_pipeline, monkeypatch, boundary):
    from core import database as db, jm_service, publication
    from core.progress import progress_manager
    from core.settings import update_settings
    from core.task_commands import enqueue_download, recover_interrupted
    from core.transfer import DownloadInterrupted
    _, state, root = real_download_pipeline
    state.faults = False
    update_settings({'skip_existing': False})
    job = enqueue_download('100', 'Synthetic', ['71']).job_id
    assert db.claim_next_queued_job()['job_id'] == job

    def execute():
        progress_manager.create_tracker(job)
        try:
            jm_service.download_album_job(job, '100', ['71'])
        finally:
            progress_manager.remove_tracker(job)

    method = 'release_pages' if boundary == 'committed' else boundary
    original = getattr(publication.Workspace, method)
    hit = []
    def interrupted(work, *args):
        original(work, *args)
        hit.append(1)
        raise DownloadInterrupted('stopped')

    with monkeypatch.context() as patch:
        patch.setattr(publication.Workspace, method, interrupted)
        execute()
    assert hit == [1]
    if boundary != 'committed':
        assert not db.resource_records(['100'])
        assert publication.job_snapshot(job, root) is None
        assert recover_interrupted() == 1
        assert db.claim_next_queued_job()['job_id'] == job
        execute()
    else:
        assert recover_interrupted() == 0
    assert db.get_job(job)['status'] == 'completed'
    assert len(list(publication.job_snapshot(job, root).rglob('*.webp'))) == 3
    assert len(db.resource_records(['100'])) == 1
    assert [state.counts[f'/images/100/{p:05d}.webp'] for p in (1, 2, 3)] == [1, 1, 1]
