"""Atomic, offline task commands; no worker starts and all databases are temporary."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest


@pytest.fixture(autouse=True)
def no_download_workers(monkeypatch):
    from core.job_manager import JobManager
    monkeypatch.setattr(JobManager, "schedule_next", lambda self: None)


def _source(status="failed", *, job_id="job_source", chapters=None, output_path=None):
    from core import database as db
    db.insert_job(job_id, "123", "Original", ["11", "12"] if chapters is None else chapters)
    db.update_job(job_id, status=status, output_path=output_path)
    return job_id


def _simultaneous(count, operation):
    ready = threading.Barrier(count)
    def run(_):
        ready.wait(timeout=10)
        return operation()
    with ThreadPoolExecutor(max_workers=count) as executor:
        return list(executor.map(run, range(count)))


def test_concurrent_creation_reuses_one_job_across_managers(client):
    from core import database as db
    from core.job_manager import JobManager
    db.add_wishlist("123", "Original", "", "")
    results = _simultaneous(8, lambda: JobManager().create_job_result("123", "Title", ["12", "11", "12"]))
    assert sum(result.created for result in results) == 1
    assert len({result.job_id for result in results}) == 1
    jobs = db.get_all_jobs()
    assert len(jobs) == 1
    assert json.loads(jobs[0]["selected_photo_ids"]) == ["12", "11"]
    assert db.get_wishlist("123")["download_status"] == "queued"


def test_create_job_keeps_string_contract_and_chapter_set_identity(client):
    from core import database as db
    from core.job_manager import JobManager
    manager = JobManager()
    first = manager.create_job("123", "Title", ["12", "11"])
    assert isinstance(first, str)
    assert manager.create_job("123", "A changed display title", ["11", "12", "11"]) == first
    # A whole-album request and different explicit selections remain distinct.
    different = manager.create_job("123", "Title", ["13"])
    overlap = manager.create_job("123", "Title", ["11"])
    whole = manager.create_job("123", "Title", [])
    assert len({first, different, overlap, whole}) == 4
    assert len(db.get_all_jobs()) == 4
    assert manager.create_job("123", "Title", []) == whole


@pytest.mark.parametrize("status", ["queued", "running", "paused"])
def test_legacy_active_selection_is_compared_without_order(client, status):
    from core import database as db
    from core.task_commands import enqueue_download
    _source(status, chapters=["12", "11", "12"])
    result = enqueue_download("123", "New title", ["11", "12"])
    assert (result.job_id, result.created) == ("job_source", False)
    assert len(db.get_all_jobs()) == 1


def test_completed_job_does_not_block_an_explicit_new_download(client):
    from core import database as db
    from core.task_commands import enqueue_download
    _source("completed")
    result = enqueue_download("123", "Again", ["11", "12"])
    assert result.created and result.job_id != "job_source"
    assert len(db.get_all_jobs()) == 2


def test_create_and_retry_share_the_same_active_selection(client):
    from core import database as db
    from core.task_commands import enqueue_download, retry_download
    _source()
    created = enqueue_download("123", "Title", ["12", "11"])
    retried = retry_download("job_source")
    assert (retried.job_id, retried.created) == (created.job_id, False)
    assert len(db.get_all_jobs()) == 2


def test_retry_of_different_chapters_can_queue_behind_the_same_album(client):
    from core import database as db
    from core.task_commands import retry_download
    _source(chapters=["11"])
    _source("running", job_id="job_existing", chapters=["12"])
    result = retry_download("job_source")
    assert result.created and result.job_id != "job_existing"
    assert len(db.get_all_jobs()) == 3


@pytest.mark.parametrize("duplicate", [False, True])
def test_queueing_never_downgrades_a_running_bookmark(client, duplicate):
    from core import database as db
    from core.task_commands import enqueue_download
    db.add_wishlist("123", "Original", "", "")
    _source("running")
    db.update_wishlist_download_status("123", "downloading")
    result = enqueue_download("123", "Title", ["11", "12"] if duplicate else ["13"])
    assert result.created == (not duplicate)
    assert db.get_wishlist("123")["download_status"] == "downloading"


@pytest.mark.parametrize("operation", ["create", "retry"])
def test_bookmark_failure_rolls_back_the_entire_command(client, monkeypatch, operation):
    from core import database as db
    from core.task_commands import enqueue_download, retry_download
    db.add_wishlist("123", "Original", "", "")
    if operation == "retry":
        _source()
    before = db.get_all_jobs()
    def fail(*args, **kwargs):
        raise RuntimeError("bookmark write failed")
    monkeypatch.setattr(db, "update_wishlist_download_status", fail)
    with pytest.raises(RuntimeError, match="bookmark write failed"):
        retry_download("job_source") if operation == "retry" else enqueue_download("123", "Title", ["11"])
    assert db.get_all_jobs() == before
    assert db.get_wishlist("123")["download_status"] == "none"


def test_concurrent_retry_has_one_fully_initialized_successor(client, tmp_path, monkeypatch):
    from core import database as db
    from core.task_commands import retry_download
    folder = tmp_path / "existing"
    folder.mkdir()
    _source(output_path=str(folder))
    db.add_wishlist("123", "Original", "", "")
    original = db.update_wishlist_download_status
    observed = []
    def inspect_before_commit(album_id, status, conn=None):
        assert conn is not None
        row = conn.execute("SELECT * FROM jobs WHERE job_id != 'job_source'").fetchone()
        assert row["output_path"] == str(folder)
        # Another connection cannot see a half-initialized queued task.
        if not observed:
            assert db.get_job(row["job_id"]) is None
        observed.append(row["job_id"])
        original(album_id, status, conn=conn)
    monkeypatch.setattr(db, "update_wishlist_download_status", inspect_before_commit)
    results = _simultaneous(8, lambda: retry_download("job_source"))
    assert sum(result.created for result in results) == 1
    assert len({result.job_id for result in results}) == 1
    assert len(db.get_all_jobs()) == 2
    successor = db.get_job(results[0].job_id)
    assert successor["output_path"] == str(folder)
    assert successor["status"] == "queued"
    assert db.get_wishlist("123")["download_status"] == "queued"


def test_retry_does_not_replace_an_existing_jobs_directory(client, tmp_path):
    from core import database as db
    from core.task_commands import retry_download
    _source(output_path=str(tmp_path / "old"))
    _source("running", job_id="job_existing", output_path=str(tmp_path / "current"), chapters=["12", "11"])
    db.add_wishlist("123", "Original", "", "")
    result = retry_download("job_source")
    assert (result.job_id, result.created) == ("job_existing", False)
    assert db.get_job("job_existing")["output_path"] == str(tmp_path / "current")
    assert db.get_wishlist("123")["download_status"] == "downloading"
    assert len(db.get_all_jobs()) == 2


def test_retry_rechecks_source_status_inside_the_write_transaction(client):
    from core import database as db
    from core.task_commands import retry_download, TaskCommandError
    _source()
    assert db.get_job("job_source")["status"] == "failed"
    with db.transaction() as conn:
        conn.execute("UPDATE jobs SET status='running' WHERE job_id='job_source'")
    with pytest.raises(TaskCommandError) as failure:
        retry_download("job_source")
    assert failure.value.reason == "status"
    assert len(db.get_all_jobs()) == 1


def test_legacy_retry_tuple_contract_is_preserved(client):
    from core.job_manager import JobManager
    manager = JobManager()
    assert manager.retry_job("job_missing") == (None, "not_found")
    _source("running")
    assert manager.retry_job("job_source") == (None, "status")


def test_create_api_returns_created_then_reused_without_rescheduling(client, monkeypatch):
    from core import database as db
    from core.job_manager import job_manager
    scheduled = []
    monkeypatch.setattr(job_manager, "schedule_next", lambda: scheduled.append(True))
    first = client.post("/api/jobs", json={"album_id": "123", "photo_ids": ["12", "11"]})
    again = client.post("/api/jobs", json={"album_id": "123", "photo_ids": ["11", "12", "11"]})
    assert first.status_code == 201 and again.status_code == 200
    assert first.json["job_id"] == again.json["job_id"]
    assert first.json["created"] is True
    assert again.json["created"] is False and again.json["reason"] == "already_active"
    assert scheduled == [True] and len(db.get_all_jobs()) == 1


def test_scheduling_failure_does_not_hide_a_committed_job(client, monkeypatch):
    from core import database as db
    from core.job_manager import job_manager
    def fail():
        raise RuntimeError("scheduler temporarily unavailable")
    monkeypatch.setattr(job_manager, "schedule_next", fail)
    response = client.post("/api/jobs", json={"album_id": "123", "photo_ids": ["11"]})
    assert response.status_code == 201
    assert db.get_job(response.json["job_id"])["status"] == "queued"
    again = client.post("/api/jobs", json={"album_id": "123", "photo_ids": ["11"]})
    assert again.status_code == 200 and again.json["job_id"] == response.json["job_id"]


def test_retry_api_returns_explicit_errors_and_reuses_a_successor(client):
    from core import database as db
    missing = client.post("/api/jobs/job_missing/retry")
    assert missing.status_code == 404 and missing.json["reason"] == "not_found"
    _source("running")
    live = client.post("/api/jobs/job_source/retry")
    assert live.status_code == 400 and live.json["reason"] == "status"
    db.update_job("job_source", status="failed", selected_photo_ids='{"bad": "selection"}')
    damaged = client.post("/api/jobs/job_source/retry")
    assert damaged.status_code == 409 and damaged.json["reason"] == "invalid_source"
    db.update_job("job_source", selected_photo_ids='["11", "12"]')
    first = client.post("/api/jobs/job_source/retry")
    again = client.post("/api/jobs/job_source/retry")
    assert first.status_code == 201 and again.status_code == 200
    assert first.json["job_id"] == again.json["job_id"]
    assert again.json["created"] is False and len(db.get_all_jobs()) == 2
