"""Atomic terminal job commands, using only temporary SQLite databases offline."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from datetime import datetime
import json
import sqlite3
import threading

import pytest


@pytest.fixture
def database(offline, tmp_path, monkeypatch):
    from core import database as db
    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "app.db")
    db.init_db()
    return db


def _job(db, job_id="job_result", status="running", album_id="123"):
    # Known fixture states are SQL data; never bypass the public status contract.
    with db.transaction() as conn:
        conn.execute("INSERT INTO jobs(job_id,album_id,title,selected_photo_ids,status,created_at,updated_at) "
                     "VALUES(?,?,?,'[\"102\"]',?,'2026-01-01','2026-01-01')",
                     (job_id, album_id, "Atomic result", status))
    return job_id


def _bookmark_and_baseline(db):
    with db.transaction() as conn:
        conn.execute("INSERT INTO wishlist(album_id,title,download_status) VALUES('123','Atomic result','downloading')")
        conn.execute("INSERT INTO album_update_checks(album_id,baseline_ids,new_chapters,new_count) "
                     "VALUES('123','[\"101\"]','[{\"photo_id\":\"102\"},{\"photo_id\":\"103\"}]',2)")


def _checks(db, conn=None):
    if conn is None:
        with closing(db.get_db()) as reader:
            return _checks(db, reader)
    row = conn.execute("SELECT baseline_ids,new_chapters,new_count FROM album_update_checks WHERE album_id='123'").fetchone()
    return {"baseline": json.loads(row[0]), "pending": json.loads(row[1]), "count": row[2]}


def _snapshot(db):
    with closing(db.get_db()) as conn:
        return {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]
                for table in ("jobs", "wishlist", "resource_catalog", "album_update_checks", "settings")}


def _fault(db, table):
    event = "INSERT" if table == "resource_catalog" else "UPDATE"
    with db.transaction() as conn:
        conn.execute(f"CREATE TRIGGER audit_reject BEFORE {event} ON {table} "
                     f"BEGIN SELECT RAISE(ABORT,'audit {table} write fault'); END")


def test_finish_completed_commits_job_catalog_bookmark_and_baseline(database):
    from core.task_commands import finish_download
    _bookmark_and_baseline(database)
    job_id = _job(database)
    assert finish_download(job_id, "completed", photo_ids=["102"], done_pages=7,
                           completed_at="2026-02-01", output_path="offline-output") is True
    job = database.get_job(job_id)
    assert (job["status"], job["done_pages"], job["completed_at"]) == ("completed", 7, "2026-02-01")
    resource, = database.resource_records(["123"])
    assert (resource["job_id"], resource["output_path"]) == (job_id, "offline-output")
    assert database.get_wishlist("123")["download_status"] == "completed"
    assert _checks(database) == {"baseline": ["101", "102"], "pending": [{"photo_id": "103"}], "count": 1}


def test_finish_failed_commits_status_without_absorbing_chapters(database):
    from core.task_commands import finish_download
    _bookmark_and_baseline(database)
    job_id = _job(database)
    before = _checks(database)
    assert finish_download(job_id, "failed", photo_ids=["102"], done_pages=2, error_message="image failed") is True
    assert database.get_job(job_id)["error_message"] == "image failed"
    assert database.get_wishlist("123")["download_status"] == "failed"
    assert database.resource_records(["123"]) == []
    assert _checks(database) == before


@pytest.mark.parametrize("to_status,remaining,expected", [
    ("failed", "queued", "queued"),
    ("failed", "running", "downloading"),
    ("failed", "paused", "downloading"),
    ("failed", "completed", "completed"),
    ("completed", "queued", "completed"),
])
def test_finish_derives_bookmark_from_all_remaining_jobs(database, to_status, remaining, expected):
    from core.task_commands import finish_download
    _bookmark_and_baseline(database)
    job_id = _job(database)
    _job(database, "job_remaining", remaining)
    assert finish_download(job_id, to_status, photo_ids=["102"]) is True
    assert database.get_wishlist("123")["download_status"] == expected


@pytest.mark.parametrize("initial", ["paused", "queued", "canceled", "completed", "failed", "unknown", None])
@pytest.mark.parametrize("result", ["completed", "failed"])
def test_finish_never_overwrites_non_executor_or_missing_jobs(database, initial, result):
    from core.task_commands import finish_download
    _bookmark_and_baseline(database)
    if initial is not None:
        _job(database, status=initial)
    before = _snapshot(database)
    assert finish_download("job_result", result, photo_ids=["102"], done_pages=9) is False
    assert _snapshot(database) == before


@pytest.mark.parametrize("invalid", ["queued", "running", "paused", "canceled", "", None])
def test_finish_rejects_invalid_destination_without_writing(database, invalid):
    from core.task_commands import finish_download
    _bookmark_and_baseline(database)
    _job(database)
    before = _snapshot(database)
    with pytest.raises(ValueError, match="completed"):
        finish_download("job_result", invalid)
    assert _snapshot(database) == before


@pytest.mark.parametrize("table", ["resource_catalog", "wishlist", "album_update_checks"])
def test_completion_write_fault_rolls_back_every_result(database, table):
    from core.task_commands import finish_download
    _bookmark_and_baseline(database)
    job_id = _job(database)
    _fault(database, table)
    before = _snapshot(database)
    with pytest.raises(sqlite3.IntegrityError, match="audit .* write fault"):
        finish_download(job_id, "completed", photo_ids=["102"], done_pages=7, completed_at="2026-02-01")
    assert _snapshot(database) == before


def test_failure_bookmark_fault_rolls_back_status_and_metadata(database):
    from core.task_commands import finish_download
    _bookmark_and_baseline(database)
    job_id = _job(database)
    _fault(database, "wishlist")
    before = _snapshot(database)
    with pytest.raises(sqlite3.IntegrityError, match="wishlist write fault"):
        finish_download(job_id, "failed", error_message="page failed")
    assert _snapshot(database) == before


def test_exception_after_baseline_write_still_rolls_back_all_three_results(database, monkeypatch):
    from core import update_store
    from core.task_commands import finish_download
    _bookmark_and_baseline(database)
    _job(database)
    before = _snapshot(database)
    absorb = update_store.absorb_downloaded

    def fail_after_write(*args, **kwargs):
        assert absorb(*args, **kwargs) == 1
        assert kwargs["conn"].execute("SELECT status FROM jobs").fetchone()[0] == "completed"
        assert kwargs["conn"].execute("SELECT download_status FROM wishlist").fetchone()[0] == "completed"
        raise RuntimeError("after baseline write")

    monkeypatch.setattr(update_store, "absorb_downloaded", fail_after_write)
    with pytest.raises(RuntimeError, match="after baseline write"):
        finish_download("job_result", "completed", photo_ids=["102"])
    assert _snapshot(database) == before


def test_completion_without_bookmark_or_baseline_still_commits(database):
    from core.task_commands import finish_download
    job_id = _job(database)
    assert finish_download(job_id, "completed", photo_ids=["102"]) is True
    assert database.get_job(job_id)["status"] == "completed"
    assert len(database.resource_records(["123"])) == 1


@pytest.mark.parametrize("to_status", ["completed", "failed", "canceled"])
def test_update_job_rejects_status_before_applying_any_metadata(database, to_status):
    job_id = _job(database, status="canceled")
    before = _snapshot(database)
    with pytest.raises(ValueError, match="status"):
        database.update_job(job_id, status=to_status, title="must not change", done_pages=9)
    assert _snapshot(database) == before
    database.update_job(job_id, title="metadata still supported", done_pages=3, output_path="offline-output")
    job = database.get_job(job_id)
    assert (job["status"], job["title"], job["done_pages"], job["output_path"]) == (
        "canceled", "metadata still supported", 3, "offline-output")


@pytest.mark.parametrize("commit", [False, True])
def test_borrowed_connection_defers_commit_and_close_to_caller(database, commit, monkeypatch):
    from core.update_store import absorb_downloaded
    _bookmark_and_baseline(database)
    _job(database)
    conn = database.get_db()
    observer = database.get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO settings(key,value) VALUES('caller-owned','pending')")
        # Borrowing either DAO must not allocate a second connection.
        monkeypatch.setattr(database, "get_db", lambda: pytest.fail("borrowed DAO opened a connection"))
        assert database.transition_job_status("job_result", ["running"], "completed", conn=conn) is True
        assert absorb_downloaded("123", ["102"], datetime(2026, 2, 1), conn=conn) == 1
        assert conn.in_transaction
        assert conn.execute("SELECT status FROM jobs").fetchone()[0] == "completed"
        assert _checks(database, conn)["count"] == 1
        assert conn.execute("SELECT updated_at FROM album_update_checks").fetchone()[0] == "2026-02-01T00:00:00"
        assert observer.execute("SELECT status FROM jobs").fetchone()[0] == "running"
        assert observer.execute("SELECT COUNT(*) FROM resource_catalog").fetchone()[0] == 0
        assert _checks(database, observer)["count"] == 2
        assert observer.execute("SELECT COUNT(*) FROM settings WHERE key='caller-owned'").fetchone()[0] == 0
        conn.commit() if commit else conn.rollback()
        assert observer.execute("SELECT status FROM jobs").fetchone()[0] == ("completed" if commit else "running")
        assert observer.execute("SELECT COUNT(*) FROM resource_catalog").fetchone()[0] == int(commit)
        assert _checks(database, observer)["count"] == (1 if commit else 2)
        assert observer.execute("SELECT COUNT(*) FROM settings WHERE key='caller-owned'").fetchone()[0] == int(commit)
        assert conn.execute("SELECT 1").fetchone()[0] == 1
    finally:
        observer.close()
        conn.close()


@pytest.mark.parametrize("operation", ["transition", "absorb"])
def test_borrowed_connection_failure_does_not_rollback_or_close_caller_work(database, operation):
    from core.update_store import absorb_downloaded
    _bookmark_and_baseline(database)
    _job(database)
    _fault(database, "jobs" if operation == "transition" else "album_update_checks")
    with closing(database.get_db()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO settings(key,value) VALUES('caller-owned','survives')")
        with pytest.raises(sqlite3.IntegrityError, match="audit .* write fault"):
            if operation == "transition":
                database.transition_job_status("job_result", ["running"], "failed", conn=conn)
            else:
                absorb_downloaded("123", ["102"], conn=conn)
        assert conn.in_transaction
        assert conn.execute("SELECT value FROM settings WHERE key='caller-owned'").fetchone()[0] == "survives"
        conn.commit()
    assert database.get_setting("caller-owned") == "survives"
    assert database.get_job("job_result")["status"] == "running"
    assert _checks(database)["count"] == 2


def test_borrowed_state_mismatch_leaves_transaction_under_caller_control(database):
    _job(database, status="canceled")
    with closing(database.get_db()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO settings(key,value) VALUES('caller-owned','pending')")
        assert database.transition_job_status("job_result", ["running"], "completed", conn=conn) is False
        assert conn.in_transaction
        conn.rollback()
        assert conn.execute("SELECT 1").fetchone()[0] == 1
    assert database.get_setting("caller-owned") is None


def test_absorb_without_borrowed_connection_keeps_its_atomic_commit_contract(database):
    from core.update_store import absorb_downloaded
    _bookmark_and_baseline(database)
    assert absorb_downloaded("123", ["102"]) == 1
    assert _checks(database)["baseline"] == ["101", "102"]
    assert _checks(database)["count"] == 1


@pytest.mark.parametrize("status,message,ok", [
    ("queued", "已取消", True), ("running", "已取消", True), ("paused", "已取消", True),
    ("canceled", "任务已取消", True), ("completed", "任务已完成，不能取消", False),
    ("failed", "任务已失败，无需取消", False), ("unknown", "任务状态已变更，请刷新后重试", False),
    (None, "任务不存在", False),
])
def test_manager_cancel_preserves_existing_messages_and_idempotency(database, status, message, ok):
    from core.job_manager import JobManager
    _bookmark_and_baseline(database)
    if status is not None:
        _job(database, status=status)
    manager = JobManager()
    assert manager.cancel_job("job_result") == (ok, message)
    if status in ("queued", "running", "paused"):
        after = _snapshot(database)
        assert manager.cancel_job("job_result") == (True, "任务已取消")
        assert _snapshot(database) == after


@pytest.mark.parametrize("remaining,expected", [("queued", "queued"), ("running", "downloading"),
                                               ("paused", "downloading"), ("completed", "completed"),
                                               (None, "none")])
def test_cancel_derives_bookmark_from_remaining_jobs_without_touching_baseline(database, remaining, expected):
    from core.task_commands import cancel_download
    _bookmark_and_baseline(database)
    _job(database)
    if remaining is not None:
        _job(database, "job_remaining", remaining)
    baseline = _checks(database)
    assert cancel_download("job_result") == (True, "已取消")
    assert database.get_job("job_result")["status"] == "canceled"
    assert database.get_wishlist("123")["download_status"] == expected
    assert _checks(database) == baseline


def test_cancel_bookmark_fault_rolls_back_the_cancellation(database):
    from core.task_commands import cancel_download
    _bookmark_and_baseline(database)
    _job(database)
    _fault(database, "wishlist")
    before = _snapshot(database)
    with pytest.raises(sqlite3.IntegrityError, match="wishlist write fault"):
        cancel_download("job_result")
    assert _snapshot(database) == before


@pytest.mark.parametrize("first", ["finish", "cancel"])
def test_finish_and_cancel_serialize_terminal_state_and_all_side_effects(database, monkeypatch, first):
    from core.task_commands import finish_download, cancel_download
    _bookmark_and_baseline(database)
    _job(database)
    wrote, release, second_started = threading.Event(), threading.Event(), threading.Event()
    transition = database.transition_job_status
    first_status = "completed" if first == "finish" else "canceled"

    def held_transition(job_id, from_statuses, to_status, **kwargs):
        changed = transition(job_id, from_statuses, to_status, **kwargs)
        if to_status == first_status and changed:
            wrote.set()
            assert release.wait(5)
        return changed

    monkeypatch.setattr(database, "transition_job_status", held_transition)
    operations = {"finish": lambda: finish_download("job_result", "completed", photo_ids=["102"]),
                  "cancel": lambda: cancel_download("job_result")}

    def second_operation():
        second_started.set()
        return operations["cancel" if first == "finish" else "finish"]()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_result = pool.submit(operations[first])
        try:
            assert wrote.wait(3)
            second_result = pool.submit(second_operation)
            assert second_started.wait(3)
            # A concurrent reader must still see the entire original result.
            assert database.get_job("job_result")["status"] == "running"
            assert database.get_wishlist("123")["download_status"] == "downloading"
            assert database.resource_records(["123"]) == []
            assert _checks(database)["count"] == 2
        finally:
            release.set()
        assert first_result.result(timeout=5) == (True if first == "finish" else (True, "已取消"))
        assert second_result.result(timeout=5) == ((False, "任务已完成，不能取消") if first == "finish" else False)
    assert database.get_job("job_result")["status"] == first_status
    assert database.get_wishlist("123")["download_status"] == ("completed" if first == "finish" else "none")
    assert len(database.resource_records(["123"])) == int(first == "finish")
    assert _checks(database)["count"] == (1 if first == "finish" else 2)


@pytest.mark.parametrize("first", ["cancel", "enqueue"])
def test_cancel_and_enqueue_cannot_overwrite_the_remaining_chapter_status(database, monkeypatch, first):
    from core import task_commands
    _bookmark_and_baseline(database)
    _job(database)
    entered, release, second_started = threading.Event(), threading.Event(), threading.Event()
    transaction = database.transaction
    calls_lock = threading.Lock()
    calls = 0

    @contextmanager
    def held_first_transaction():
        nonlocal calls
        with calls_lock:
            calls += 1
            hold = calls == 1
        with transaction() as conn:
            yield conn
            if hold:
                entered.set()
                assert release.wait(5)

    monkeypatch.setattr(database, "transaction", held_first_transaction)
    operations = {"cancel": lambda: task_commands.cancel_download("job_result"),
                  "enqueue": lambda: task_commands.enqueue_download("123", "Next chapter", ["103"])}

    def second_operation():
        second_started.set()
        return operations["enqueue" if first == "cancel" else "cancel"]()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_result = pool.submit(operations[first])
        try:
            assert entered.wait(3)
            second_result = pool.submit(second_operation)
            assert second_started.wait(3)
        finally:
            release.set()
        results = [first_result.result(timeout=5), second_result.result(timeout=5)]
    assert (True, "已取消") in results
    queued, = [job for job in database.get_all_jobs() if job["job_id"] != "job_result"]
    assert queued["status"] == "queued"
    assert database.get_job("job_result")["status"] == "canceled"
    assert database.get_wishlist("123")["download_status"] == "queued"


@pytest.mark.parametrize("initial", ["running", "canceled"])
def test_manager_worker_exception_uses_atomic_result_and_keeps_another_job_queued(database, monkeypatch, initial):
    from core import job_manager
    _bookmark_and_baseline(database)
    _job(database, status=initial)
    _job(database, "job_remaining", "queued")

    def fail(*args, **kwargs):
        raise OSError("offline worker failure")

    monkeypatch.setattr(job_manager, "download_album_job", fail)
    manager = job_manager.JobManager()
    manager._run_job_wrapper("job_result", "123", ["102"])
    assert database.get_job("job_result")["status"] == ("failed" if initial == "running" else "canceled")
    assert database.get_wishlist("123")["download_status"] == ("queued" if initial == "running" else "downloading")
    assert _checks(database)["count"] == 2
