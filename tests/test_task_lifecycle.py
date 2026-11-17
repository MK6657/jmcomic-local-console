"""Offline task lifecycle: explicit pauses survive restart and shutdown is bounded."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time
from unittest.mock import Mock

import pytest


def _job(job_id, album_id="123", status="queued"):
    from core import database as db
    db.insert_job(job_id, album_id, "Lifecycle", ["71"])
    if status != "queued":
        db.transition_job_status(
            job_id, ["queued", "running", "paused", "completed", "failed", "canceled"], status,
        )


def test_recovery_preserves_pauses_terminal_states_and_owned_executors(client):
    from core import database as db
    from core.job_manager import JobManager
    manager = JobManager()
    for album_id in ("123", "456", "789"):
        db.add_wishlist(album_id, "Lifecycle")
    _job("job_interrupted", "123", "running")
    _job("job_paused", "123", "paused")
    _job("job_other_interrupted", "456", "running")
    _job("job_live", "789", "running")
    _job("job_completed", "101", "completed")
    _job("job_canceled", "102", "canceled")
    _job("job_failed", "103", "failed")
    manager._running_jobs["job_live"] = Mock()  # executor ownership, including an admission in progress
    assert manager.recover_interrupted() == 2
    assert manager.recover_interrupted() == 0
    assert db.get_job("job_interrupted")["status"] == "queued"
    assert db.get_job("job_other_interrupted")["status"] == "queued"
    assert db.get_job("job_live")["status"] == "running"
    assert db.get_job("job_paused")["status"] == "paused"
    assert db.get_wishlist("123")["download_status"] == "downloading"
    assert db.get_wishlist("456")["download_status"] == "queued"
    assert [db.get_job(name)["status"] for name in ("job_completed", "job_canceled", "job_failed")] == [
        "completed", "canceled", "failed"]


def test_recovery_rolls_back_statuses_when_bookmark_sync_fails(client, monkeypatch):
    from core import database as db, task_commands
    from core.job_manager import JobManager
    _job("job_interrupted", status="running")
    db.add_wishlist("123", "Lifecycle")
    db.update_wishlist_download_status("123", "downloading")
    monkeypatch.setattr(task_commands, "_sync_wishlist", Mock(side_effect=RuntimeError("storage unavailable")))
    with pytest.raises(RuntimeError, match="storage unavailable"):
        JobManager().recover_interrupted()
    assert db.get_job("job_interrupted")["status"] == "running"
    assert db.get_wishlist("123")["download_status"] == "downloading"


def test_orphan_pause_resumes_to_queued_before_dispatch(client, monkeypatch):
    from core import database as db
    from core.job_manager import JobManager
    manager = JobManager()
    _job("job_paused", status="paused")
    db.add_wishlist("123", "Lifecycle")
    seen = []
    monkeypatch.setattr(manager, "schedule_next", lambda: seen.append(db.get_job("job_paused")["status"]))
    assert manager.resume_job("job_paused") == (True, "已恢复")
    assert seen == ["queued"]
    assert db.get_wishlist("123")["download_status"] == "queued"
    assert manager.get_running_count() == 0


def test_owned_pause_resumes_its_executor_and_wakes_event(client, monkeypatch):
    import core.job_manager as module
    from core import database as db
    manager = module.JobManager()
    started, proceed, resumed = threading.Event(), threading.Event(), threading.Event()
    observed = []
    def worker(job_id, album_id, photos, *, stop_event):
        event = manager.get_pause_event(job_id)
        started.set()
        assert event.wait(5)
        observed.append(db.get_job(job_id)["status"])
        resumed.set()
        assert proceed.wait(5)
    monkeypatch.setattr(module, "download_album_job", worker)
    _job("job_paused")
    manager.schedule_next()
    try:
        assert started.wait(3)
        assert manager.pause_job("job_paused")[0]
        assert manager.resume_job("job_paused")[0]
        assert resumed.wait(3)
        assert observed == ["running"]
        assert manager.get_running_count() == 1
    finally:
        manager.request_stop()
        proceed.set()
        assert manager.stop(timeout=3)
    assert db.get_job("job_paused")["status"] == "queued"


def test_pause_cannot_erase_a_concurrent_resume_notification(client, monkeypatch):
    from core import database as db
    from core.job_manager import JobManager
    manager = JobManager()
    _job("job_pause_race", status="running")
    manager._running_jobs["job_pause_race"] = Mock(is_alive=Mock(return_value=True))
    event = manager.get_pause_event("job_pause_race")
    committed, release, resume_entered = threading.Event(), threading.Event(), threading.Event()
    transition = db.transition_job_status
    def delayed_pause(*args, **kwargs):
        result = transition(*args, **kwargs)
        committed.set()
        assert release.wait(5)
        return result
    monkeypatch.setattr(db, "transition_job_status", delayed_pause)
    outcomes = []
    pausing = threading.Thread(target=lambda: outcomes.append(manager.pause_job("job_pause_race")))
    def resume():
        resume_entered.set()
        outcomes.append(manager.resume_job("job_pause_race"))
    resuming = threading.Thread(target=resume)
    pausing.start()
    assert committed.wait(3)
    resuming.start()
    assert resume_entered.wait(3)
    release.set()
    pausing.join(timeout=3)
    resuming.join(timeout=3)
    assert not pausing.is_alive() and not resuming.is_alive()
    assert all(ok for ok, message in outcomes) and len(outcomes) == 2
    assert db.get_job("job_pause_race")["status"] == "running"
    assert event.is_set()


@pytest.mark.parametrize("paused", [False, True])
def test_shutdown_requeues_running_preserves_user_pause_and_wakes_waiters(client, monkeypatch, paused):
    import core.job_manager as module
    from core import database as db
    manager = module.JobManager()
    entered, waiting = threading.Event(), threading.Event()
    def worker(job_id, album_id, photos, *, stop_event):
        pause_event = manager.get_pause_event(job_id)
        entered.set()
        if paused:
            assert waiting.wait(5)
            assert pause_event.wait(5)
        else:
            assert stop_event.wait(5)
    monkeypatch.setattr(module, "download_album_job", worker)
    _job("job_shutdown")
    db.add_wishlist("123", "Lifecycle")
    manager.schedule_next()
    assert entered.wait(3)
    if paused:
        assert manager.pause_job("job_shutdown")[0]
        waiting.set()
    assert manager.stop(timeout=3)
    assert manager.get_running_count() == 0
    job = db.get_job("job_shutdown")
    assert job["status"] == ("paused" if paused else "queued")
    assert job["completed_at"] is None
    assert job["error_message"] is None
    assert db.resource_records(["123"]) == []
    if not paused:
        assert db.get_wishlist("123")["download_status"] == "queued"


def test_stop_uses_one_total_budget_and_cannot_restart_undrained_workers(client, monkeypatch):
    import core.job_manager as module
    from core import database as db
    manager = module.JobManager()
    entered = threading.Barrier(3)
    release = threading.Event()
    def worker(job_id, album_id, photos, *, stop_event):
        entered.wait(timeout=5)
        assert release.wait(5)  # emulate an in-flight request that cannot yet stop
    monkeypatch.setattr(module, "download_album_job", worker)
    db.set_setting("max_running_jobs", "2")
    _job("job_one", "123")
    _job("job_two", "456")
    _job("job_queued", "789")
    manager.schedule_next()
    manager.schedule_next()
    entered.wait(timeout=5)
    try:
        started = time.monotonic()
        assert not manager.stop(timeout=0.04)
        assert time.monotonic() - started < 0.3
        assert manager.get_running_count() == 2
        assert manager._stop_event.is_set()
        with pytest.raises(RuntimeError, match="停止中"):
            manager.start()
        manager.schedule_next()
        assert db.get_job("job_queued")["status"] == "queued"
    finally:
        release.set()
        assert manager.stop(timeout=3)
    assert db.get_job("job_one")["status"] == db.get_job("job_two")["status"] == "queued"


def test_request_stop_is_nonblocking_during_a_claim_and_prevents_executor_start(client, monkeypatch):
    import core.job_manager as module
    from core import database as db
    manager = module.JobManager()
    claimed, release = threading.Event(), threading.Event()
    original = db.claim_next_queued_job
    def claim(excluded):
        row = original(excluded)
        claimed.set()
        assert release.wait(5)
        return row
    monkeypatch.setattr(db, "claim_next_queued_job", claim)
    download = Mock()
    monkeypatch.setattr(module, "download_album_job", download)
    _job("job_claim")
    admission = threading.Thread(target=manager.schedule_next)
    admission.start()
    try:
        assert claimed.wait(3)
        started = time.monotonic()
        manager.request_stop()
        assert time.monotonic() - started < 0.1
        assert not manager.stop(timeout=0.02)
    finally:
        release.set()
        admission.join(timeout=3)
    assert not admission.is_alive()
    assert manager.stop(timeout=1)
    assert db.get_job("job_claim")["status"] == "queued"
    download.assert_not_called()


def test_stop_before_worker_target_enters_never_runs_download(client, monkeypatch):
    import core.job_manager as module
    from core import database as db
    manager = module.JobManager()
    _job("job_gate")
    original = manager._run_job_wrapper
    gate, release = threading.Event(), threading.Event()
    def delayed(*args):
        gate.set()
        assert release.wait(5)
        original(*args)
    monkeypatch.setattr(manager, "_run_job_wrapper", delayed)
    download = Mock()
    monkeypatch.setattr(module, "download_album_job", download)
    manager.schedule_next()
    assert gate.wait(3)
    manager.request_stop()
    release.set()
    assert manager.stop(timeout=3)
    assert db.get_job("job_gate")["status"] == "queued"
    download.assert_not_called()


def test_concurrent_start_is_idempotent_and_stop_cleans_scheduler(client, monkeypatch):
    from core.job_manager import JobManager
    manager = JobManager()
    monkeypatch.setattr(manager, "_schedule_next", lambda: None)
    barrier = threading.Barrier(8)
    def start(_):
        barrier.wait(timeout=5)
        manager.start()
        return manager._scheduler_thread
    with ThreadPoolExecutor(max_workers=8) as pool:
        schedulers = list(pool.map(start, range(8)))
    assert len({id(thread) for thread in schedulers}) == 1
    assert schedulers[0].is_alive()
    assert manager.stop(timeout=3)
    assert manager._scheduler_thread is None
    manager.start()
    assert manager._scheduler_thread is not schedulers[0]
    assert manager.stop(timeout=3)


def test_scheduler_start_failure_can_be_retried_without_orphaned_thread(client, monkeypatch):
    import core.job_manager as module
    manager = module.JobManager()
    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, "start", Mock(side_effect=RuntimeError("cannot create thread")))
        with pytest.raises(RuntimeError, match="cannot create thread"):
            manager.start()
    assert manager._scheduler_thread is None
    assert manager._stop_event.is_set()
    monkeypatch.setattr(manager, "_schedule_next", lambda: None)
    manager.start()
    assert manager.stop(timeout=3)


def test_stop_catches_a_start_that_overlapped_its_nonblocking_request(client, monkeypatch):
    from core.job_manager import JobManager
    manager = JobManager()
    monkeypatch.setattr(manager, "_schedule_next", lambda: None)
    result = []
    with manager._lock:
        stopping = threading.Thread(target=lambda: result.append(manager.stop(timeout=3)))
        stopping.start()
        assert manager._stop_event.wait(2)  # stop has signaled and is waiting for the admission lock
        manager.start()                    # an overlapping explicit start wins the lock first
        scheduler = manager._scheduler_thread
    stopping.join(timeout=4)
    assert result == [True]
    assert not scheduler.is_alive()
    assert manager._scheduler_thread is None
    assert manager._stop_event.is_set()


def test_pause_event_created_after_stop_signal_is_already_awake(client):
    from core.job_manager import JobManager
    manager = JobManager()
    manager.request_stop()
    assert manager.get_pause_event("job_late_pause").is_set()
    assert manager.stop(timeout=0)


def test_orphan_resume_transaction_rolls_back_when_bookmark_sync_fails(client, monkeypatch):
    from core import database as db, task_commands
    from core.job_manager import JobManager
    manager = JobManager()
    _job("job_paused", status="paused")
    monkeypatch.setattr(task_commands, "_sync_wishlist", Mock(side_effect=RuntimeError("write failed")))
    with pytest.raises(RuntimeError, match="write failed"):
        manager.resume_job("job_paused")
    assert db.get_job("job_paused")["status"] == "paused"


def test_user_cancel_during_shutdown_is_not_requeued(client, monkeypatch):
    import core.job_manager as module
    from core import database as db
    manager = module.JobManager()
    entered, release = threading.Event(), threading.Event()
    def worker(job_id, album_id, photos, *, stop_event):
        entered.set()
        assert release.wait(5)
    monkeypatch.setattr(module, "download_album_job", worker)
    _job("job_cancel")
    manager.schedule_next()
    assert entered.wait(3)
    manager.request_stop()
    assert manager.cancel_job("job_cancel")[0]
    release.set()
    assert manager.stop(timeout=3)
    assert db.get_job("job_cancel")["status"] == "canceled"


def test_executor_start_failure_releases_capacity_and_tracker(client, monkeypatch):
    import core.job_manager as module
    from core import database as db
    from core.progress import progress_manager
    manager = module.JobManager()
    _job("job_start_failure")
    monkeypatch.setattr(threading.Thread, "start", Mock(side_effect=RuntimeError("cannot create executor")))
    with pytest.raises(RuntimeError, match="cannot create executor"):
        manager.schedule_next()
    assert manager.get_running_count() == 0
    assert db.get_job("job_start_failure")["status"] == "failed"
    assert progress_manager.get_tracker("job_start_failure") is None


def test_exception_while_shutdown_does_not_mark_task_failed(client, monkeypatch):
    import core.job_manager as module
    from core import database as db
    manager = module.JobManager()
    entered = threading.Event()
    def worker(job_id, album_id, photos, *, stop_event):
        entered.set()
        assert stop_event.wait(5)
        raise OSError("request interrupted while stopping")
    monkeypatch.setattr(module, "download_album_job", worker)
    _job("job_stopping_error")
    manager.schedule_next()
    assert entered.wait(3)
    assert manager.stop(timeout=3)
    assert db.get_job("job_stopping_error")["status"] == "queued"
    assert db.get_job("job_stopping_error")["error_message"] is None
