"""Real SQLite contention without upstream calls or the person's runtime database."""
from contextlib import contextmanager
import sqlite3
import threading
import time

import pytest


class _Call:
    def __init__(self, operation):
        self.done = threading.Event()
        self.value = None
        self.error = None

        def run():
            try:
                self.value = operation()
            except BaseException as error:
                self.error = error
            finally:
                self.done.set()

        self.thread = threading.Thread(target=run)
        self.thread.start()

    def result(self, timeout=7):
        self.thread.join(timeout)
        assert not self.thread.is_alive(), "test caller did not drain"
        if self.error is not None:
            raise self.error
        return self.value


@pytest.fixture
def spawn():
    calls = []

    def start(operation):
        call = _Call(operation)
        calls.append(call)
        return call

    yield start
    for call in calls:
        call.thread.join(7)
    assert not any(call.thread.is_alive() for call in calls)


@pytest.fixture
def jobs(tmp_path, monkeypatch):
    from core import database as db, job_manager as module
    from core.progress import ProgressManager

    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "contention.db")
    db.init_db()
    monkeypatch.setattr(module, "progress_manager", ProgressManager())
    manager = module.JobManager()
    # A controlled local executor; conftest still blocks both sockets and libcurl.
    monkeypatch.setattr(module, "download_album_job", lambda *args, stop_event: stop_event.wait(10))
    yield db, module, manager
    assert manager.stop(timeout=7)
    assert manager._pending_operations == 0
    assert not manager._job_controls


def _job(db, job_id="job_one", album_id="123", status="queued"):
    db.insert_job(job_id, album_id, "Contention fixture", ["71"])
    if status != "queued":
        assert db.transition_job_status(job_id, ["queued"], status)


@contextmanager
def _writer(db):
    # A separate, genuine SQLite connection owns the write lock. Production's
    # five-second busy timeout is neither mocked nor reset for these tests.
    connection = db.get_db()
    assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    finally:
        connection.rollback()
        connection.close()


def _write_attempt(db, monkeypatch):
    attempted = threading.Event()
    original = db.get_db

    def connect():
        connection = original()

        def trace(sql):
            if sql.lstrip().upper().startswith(("BEGIN IMMEDIATE", "UPDATE JOBS")):
                attempted.set()

        connection.set_trace_callback(trace)
        return connection

    monkeypatch.setattr(db, "get_db", connect)
    return attempted


def _responsive(manager, spawn, expected=0):
    def probe():
        count = manager.get_running_count()
        event = manager.get_pause_event("job_probe")
        manager.clear_pause_event("job_probe")
        return count, event

    call = spawn(probe)
    assert call.done.wait(0.5), "a SQLite wait monopolized the manager lock"
    assert call.result()[0] == expected


def _bounded_stop(manager):
    started = time.monotonic()
    assert manager.stop(timeout=0.03) is False
    assert time.monotonic() - started < 0.5
    with pytest.raises(RuntimeError, match="停止中"):
        manager.start()


@pytest.mark.parametrize("command,status", [
    ("schedule_next", "queued"), ("pause_job", "running"),
    ("resume_job", "paused"), ("recover_interrupted", "running"),
])
def test_real_write_contention_leaves_manager_responsive_and_stop_tracks_work(
    jobs, spawn, monkeypatch, command, status,
):
    db, module, manager = jobs
    _job(db, status=status)
    if command == "resume_job":
        monkeypatch.setattr(manager, "schedule_next", lambda: None)
    operation = getattr(manager, command)
    with _writer(db):
        attempted = _write_attempt(db, monkeypatch)
        call = spawn(lambda: operation("job_one") if command.endswith("_job") else operation())
        assert attempted.wait(2)
        assert not call.done.wait(0.04), "the operation did not encounter the real write lock"
        _responsive(manager, spawn)
        _bounded_stop(manager)
    result = call.result()
    assert result == {"schedule_next": None, "pause_job": (True, "已暂停"),
                      "resume_job": (True, "已恢复"), "recover_interrupted": 1}[command]
    assert manager.stop(timeout=1)
    assert db.get_job("job_one")["status"] == ("paused" if command == "pause_job" else "queued")
    assert not module.progress_manager._trackers


def test_actual_five_second_claim_timeout_releases_reservation(jobs, spawn, monkeypatch):
    db, module, manager = jobs
    _job(db)
    with _writer(db):
        attempted = _write_attempt(db, monkeypatch)
        started = time.monotonic()
        call = spawn(manager.schedule_next)
        assert attempted.wait(2)
        _responsive(manager, spawn)
        _bounded_stop(manager)
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            call.result()
        assert time.monotonic() - started >= 4.5
        # A busy error ends the synchronous claim; it leaves no hidden worker.
        assert manager.stop(timeout=0)
    assert db.get_job("job_one")["status"] == "queued"
    assert not module.progress_manager._trackers


def test_concurrent_schedule_reserves_slots_and_canceled_albums_until_exit(jobs, spawn, monkeypatch):
    db, module, manager = jobs
    db.set_setting("max_running_jobs", "3")
    for job_id, album in (("job_a", "123"), ("job_a2", "123"), ("job_b", "456"),
                          ("job_c", "789"), ("job_d", "900"), ("job_e", "901")):
        _job(db, job_id, album)
    active, violations = {}, []
    observed_lock = threading.Lock()
    ready = threading.Event()

    def worker(job_id, album_id, photos, *, stop_event):
        with observed_lock:
            if album_id in active.values():
                violations.append((job_id, album_id))
            active[job_id] = album_id
            if len(active) >= 3:
                ready.set()
        try:
            assert stop_event.wait(10)
        finally:
            with observed_lock:
                del active[job_id]

    monkeypatch.setattr(module, "download_album_job", worker)
    barrier = threading.Barrier(13)

    def schedule():
        barrier.wait(3)
        manager.schedule_next()

    with _writer(db):
        attempted = _write_attempt(db, monkeypatch)
        calls = [spawn(schedule) for _ in range(12)]
        barrier.wait(3)
        assert attempted.wait(2)
        _responsive(manager, spawn)
    for call in calls:
        call.result()
    assert ready.wait(2)
    assert manager.get_running_count() == 3
    assert set(manager._running_jobs) == {"job_a", "job_b", "job_c"}
    assert db.get_job("job_a2")["status"] == "queued"
    # An available slot must not admit another chapter while the canceled
    # executor for that album is still returning from its request.
    assert manager.cancel_job("job_a") == (True, "已取消")
    db.set_setting("max_running_jobs", "4")
    manager.schedule_next()
    assert manager.get_running_count() == 4
    assert set(manager._running_jobs) == {"job_a", "job_b", "job_c", "job_d"}
    assert not violations
    assert db.get_job("job_a2")["status"] == "queued"
    assert manager.stop(timeout=3)
    assert db.get_job("job_a")["status"] == "canceled"


def test_simultaneous_database_claims_select_after_obtaining_write_lock(jobs, spawn):
    db, _, _ = jobs
    for index in range(6):
        _job(db, f"job_{index}", str(100 + index))
    barrier = threading.Barrier(7)

    def claim():
        barrier.wait(3)
        return db.claim_next_queued_job()

    with _writer(db):
        calls = [spawn(claim) for _ in range(6)]
        barrier.wait(3)
        assert not any(call.done.wait(0.02) for call in calls)
    rows = [call.result() for call in calls]
    assert all(row is not None for row in rows)
    assert len({row["job_id"] for row in rows}) == 6


def test_recovery_cannot_requeue_a_claim_between_commit_and_registration(jobs, spawn, monkeypatch):
    db, _, manager = jobs
    _job(db)
    _job(db, "job_stranded", "456", "running")
    claimed, release = threading.Event(), threading.Event()
    original = db.claim_next_queued_job

    def claim(excluded):
        row = original(excluded)
        claimed.set()
        assert release.wait(5)
        return row

    monkeypatch.setattr(db, "claim_next_queued_job", claim)
    admission = spawn(manager.schedule_next)
    try:
        assert claimed.wait(2)
        recovery = spawn(manager.recover_interrupted)
        assert not recovery.done.wait(0.05)
        _responsive(manager, spawn)
    finally:
        release.set()
    admission.result()
    assert recovery.result() == 1
    assert db.get_job("job_one")["status"] == "running"
    assert db.get_job("job_stranded")["status"] == "queued"
    assert manager.get_running_count() == 1


def test_stop_tracks_a_claim_rollback_waiting_for_sqlite(jobs, spawn, monkeypatch):
    db, module, manager = jobs
    _job(db)
    claimed, release = threading.Event(), threading.Event()
    original = db.claim_next_queued_job
    executions = []
    monkeypatch.setattr(module, "download_album_job", lambda *args, **kwargs: executions.append(args))

    def claim(excluded):
        row = original(excluded)
        claimed.set()
        assert release.wait(5)
        return row

    monkeypatch.setattr(db, "claim_next_queued_job", claim)
    admission = spawn(manager.schedule_next)
    try:
        assert claimed.wait(2)
        with _writer(db):
            attempted = _write_attempt(db, monkeypatch)
            manager.request_stop()
            release.set()
            assert attempted.wait(2)
            _responsive(manager, spawn)
            _bounded_stop(manager)
            stopping = spawn(lambda: manager.stop(timeout=3))
            assert not stopping.done.wait(0.05)
    finally:
        release.set()
    admission.result()
    assert stopping.result() is True
    assert db.get_job("job_one")["status"] == "queued"
    assert executions == []


def test_failed_thread_start_keeps_cleanup_tracked_without_holding_manager_lock(jobs, spawn, monkeypatch):
    db, module, manager = jobs
    _job(db)
    prepared, release = threading.Event(), threading.Event()
    create_tracker = module.progress_manager.create_tracker
    start_thread = threading.Thread.start

    def tracker(job_id):
        result = create_tracker(job_id)
        prepared.set()
        assert release.wait(5)
        return result

    def start(thread):
        if thread.name.startswith("dl-"):
            raise RuntimeError("fixture thread start failure")
        return start_thread(thread)

    monkeypatch.setattr(module.progress_manager, "create_tracker", tracker)
    monkeypatch.setattr(threading.Thread, "start", start)
    admission = spawn(manager.schedule_next)
    try:
        assert prepared.wait(2)
        with _writer(db):
            attempted = _write_attempt(db, monkeypatch)
            release.set()
            assert attempted.wait(2)
            _responsive(manager, spawn)
            _bounded_stop(manager)
    finally:
        release.set()
    with pytest.raises(RuntimeError, match="fixture thread start failure"):
        admission.result()
    assert db.get_job("job_one")["status"] == "failed"
    assert manager.get_running_count() == 0
    assert not module.progress_manager._trackers


@pytest.mark.parametrize("first", ["pause", "resume"])
def test_per_job_notifications_follow_commits_without_blocking_other_jobs(jobs, spawn, monkeypatch, first):
    db, module, manager = jobs
    _job(db)
    _job(db, "job_other", "456")
    db.set_setting("max_running_jobs", "2")
    manager.schedule_next()
    manager.schedule_next()
    event = manager.get_pause_event("job_one")
    if first == "resume":
        assert manager.pause_job("job_one")[0]
    committed, release = threading.Event(), threading.Event()
    if first == "pause":
        original = db.transition_job_status

        def command(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[0] == "job_one":
                committed.set()
                assert release.wait(5)
            return result

        monkeypatch.setattr(db, "transition_job_status", command)
        before, after = manager.pause_job, manager.resume_job
    else:
        original = module.resume_download

        def command(*args, **kwargs):
            result = original(*args, **kwargs)
            committed.set()
            assert release.wait(5)
            return result

        monkeypatch.setattr(module, "resume_download", command)
        before, after = manager.resume_job, manager.pause_job
    older = spawn(lambda: before("job_one"))
    try:
        assert committed.wait(2)
        newer = spawn(lambda: after("job_one"))
        assert not newer.done.wait(0.04)
        unrelated = spawn(lambda: manager.pause_job("job_other"))
        assert unrelated.done.wait(0.5), "unrelated jobs shared a command lock"
        assert unrelated.result() == (True, "已暂停")
        _responsive(manager, spawn, expected=2)
    finally:
        release.set()
    assert older.result()[0] and newer.result()[0]
    assert event.is_set() == (first == "pause")
    assert db.get_job("job_one")["status"] == ("running" if first == "pause" else "paused")


def test_resume_waiting_for_sqlite_and_executor_exit_cannot_strand_running_row(jobs, spawn, monkeypatch):
    db, module, manager = jobs
    _job(db)
    entered, release, returning = threading.Event(), threading.Event(), threading.Event()

    def worker(*args, stop_event):
        entered.set()
        assert release.wait(5)
        returning.set()

    monkeypatch.setattr(module, "download_album_job", worker)
    manager.schedule_next()
    assert entered.wait(2)
    executor = manager._running_jobs["job_one"]
    assert manager.pause_job("job_one")[0]
    try:
        with _writer(db):
            attempted = _write_attempt(db, monkeypatch)
            resume = spawn(lambda: manager.resume_job("job_one"))
            assert attempted.wait(2)
            release.set()
            assert returning.wait(2)
            # The executor cannot retire before the resume command/notification.
            executor.join(0.05)
            assert executor.is_alive()
            _responsive(manager, spawn, expected=1)
            manager.schedule_next()
            assert manager.get_running_count() == 1
        assert resume.result() == (True, "已恢复")
        executor.join(3)
        assert not executor.is_alive()
        assert manager.get_running_count() == 0
        assert db.get_job("job_one")["status"] == "queued"
        assert "job_one" not in manager._pause_events
    finally:
        release.set()


def test_pause_clear_cannot_erase_a_concurrent_stop_wakeup(jobs, monkeypatch):
    db, _, manager = jobs
    _job(db, status="running")
    event = manager.get_pause_event("job_one")
    original_clear = event.clear

    def clear_after_stop():
        manager.request_stop()
        original_clear()

    monkeypatch.setattr(event, "clear", clear_after_stop)
    assert manager.pause_job("job_one") == (True, "已暂停")
    assert event.is_set()
    assert db.get_job("job_one")["status"] == "paused"


@pytest.mark.parametrize("drained", [False, True])
def test_stop_closes_direct_command_admission_without_db_work(jobs, spawn, monkeypatch, drained):
    db, _, manager = jobs
    manager.request_stop()
    if drained:
        assert manager.stop(timeout=0)

    def forbidden(*args, **kwargs):
        pytest.fail("a stopped manager admitted a command")

    monkeypatch.setattr(db, "get_db", forbidden)
    monkeypatch.setattr(manager, "_job_control", forbidden)
    operations = [manager.schedule_next, manager.recover_interrupted,
                  lambda: manager.pause_job("job_one"), lambda: manager.resume_job("job_one")]
    calls = [spawn(operation) for operation in operations]
    results = [call.result() for call in calls]
    assert results[:2] == [None, 0]
    assert results[2][0] is False and results[3][0] is False
    assert manager._pending_operations == 0
    assert manager.stop(timeout=0)


def test_orphan_resume_releases_job_control_before_waiting_for_admission(jobs, spawn, monkeypatch):
    db, module, manager = jobs
    _job(db, status="paused")
    committed, claimed, release = threading.Event(), threading.Event(), threading.Event()
    resume_download = module.resume_download
    claim_next = db.claim_next_queued_job

    def resume(*args, **kwargs):
        result = resume_download(*args, **kwargs)
        committed.set()
        assert release.wait(5)
        return result

    def claim(excluded):
        result = claim_next(excluded)
        claimed.set()
        return result

    monkeypatch.setattr(module, "resume_download", resume)
    monkeypatch.setattr(db, "claim_next_queued_job", claim)
    resuming = spawn(lambda: manager.resume_job("job_one"))
    try:
        assert committed.wait(2)
        # This scheduler owns admission and waits for resume's job control.
        scheduling = spawn(manager.schedule_next)
        assert claimed.wait(2)
        _responsive(manager, spawn)
    finally:
        release.set()
    assert resuming.result() == (True, "已恢复")
    scheduling.result()
    assert manager.get_running_count() == 1
    assert db.get_job("job_one")["status"] == "running"
