"""Offline SSE capacity and lifetime contracts, without HTTP or upstream calls."""
import json
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from core import progress
from core.progress import ProgressManager, ProgressTracker, StreamCapacityError


def test_default_limits_and_status_snapshot():
    manager = ProgressManager()
    tracker = manager.create_tracker("job")
    leases = [manager.acquire_stream(tracker) for _ in range(8)]
    try:
        assert manager.stream_status() == {
            "active_streams": 8,
            "max_streams": 32,
            "max_streams_per_job": 8,
            "streams_by_job": {"job": 8},
        }
        snapshot = manager.stream_status()
        snapshot["streams_by_job"].clear()
        assert manager.active_stream_count("job") == 8
        with pytest.raises(StreamCapacityError):
            manager.acquire_stream(tracker)
        assert tracker.subscriber_count() == 8
    finally:
        for lease in leases:
            lease.close()
    assert manager.stream_status()["streams_by_job"] == {}
    assert manager.active_stream_count() == tracker.subscriber_count() == 0


def test_global_limit_across_trackers_rejects_before_subscribing(monkeypatch):
    manager = ProgressManager(max_streams=2, max_streams_per_job=1)
    trackers = [manager.create_tracker(str(index)) for index in range(3)]
    leases = [manager.acquire_stream(tracker) for tracker in trackers[:2]]
    try:
        def unexpected_subscription():
            pytest.fail("capacity rejection must not allocate a subscriber queue")

        monkeypatch.setattr(trackers[2], "subscribe", unexpected_subscription)
        for _ in range(3):
            with pytest.raises(StreamCapacityError):
                manager.acquire_stream(trackers[2])
        assert manager.active_stream_count() == 2
        assert manager.active_stream_count("2") == trackers[2].subscriber_count() == 0
        assert manager.stream_status()["streams_by_job"] == {"0": 1, "1": 1}
    finally:
        for lease in leases:
            lease.close()
    assert manager.active_stream_count() == 0


def test_per_job_limit_allows_other_job_and_reuses_closed_slot():
    manager = ProgressManager(max_streams=2, max_streams_per_job=1)
    first, second = (manager.create_tracker(name) for name in ("first", "second"))
    lease = manager.acquire_stream(first)
    for _ in range(3):
        with pytest.raises(StreamCapacityError):
            manager.acquire_stream(first)
    assert first.subscriber_count() == manager.active_stream_count() == 1
    other = manager.acquire_stream(second)
    lease.close()
    replacement = manager.acquire_stream(first)
    try:
        assert replacement.queue is not lease.queue
        assert manager.active_stream_count() == 2
        assert first.subscriber_count() == second.subscriber_count() == 1
    finally:
        other.close()
        replacement.close()
    assert manager.active_stream_count() == 0


@pytest.mark.parametrize("action", ["close", "remove", "close_all"])
def test_tracker_cleanup_does_not_release_http_leases(action):
    manager = ProgressManager(max_streams=2, max_streams_per_job=1)
    tracker = manager.create_tracker("job")
    tracker.subscribe()  # Internal subscriptions retain their original semantics.
    lease = manager.acquire_stream(tracker)
    if action == "close":
        tracker.close()
    elif action == "remove":
        manager.remove_tracker("job")
    else:
        manager.close_all()
    assert tracker.is_done() and tracker.subscriber_count() == 0
    assert list(tracker.iter_events(lease.queue)) == []
    assert manager.active_stream_count() == manager.active_stream_count("job") == 1
    replacement_tracker = manager.create_tracker("job")
    with pytest.raises(StreamCapacityError):
        manager.acquire_stream(replacement_tracker)
    other = manager.acquire_stream(manager.create_tracker("other"))
    assert manager.active_stream_count() == 2
    lease.close()
    lease.close()
    replacement = manager.acquire_stream(replacement_tracker)
    try:
        assert manager.active_stream_count("job") == 1
        assert manager.active_stream_count() == 2
    finally:
        other.close()
        replacement.close()
    assert manager.stream_status()["streams_by_job"] == {}
    assert manager.active_stream_count() == 0


def test_lease_can_close_before_generator_starts():
    manager = ProgressManager(max_streams=2, max_streams_per_job=1)
    tracker = manager.create_tracker("job")
    lease = manager.acquire_stream(tracker)
    events = tracker.iter_events(lease.queue, max_duration=300)
    events.close()  # A never-started generator does not execute its finally/except.
    assert manager.active_stream_count() == tracker.subscriber_count() == 1
    lease.close()
    lease.close()
    assert manager.active_stream_count() == tracker.subscriber_count() == 0


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt])
def test_failed_subscription_rolls_back_reserved_capacity(monkeypatch, failure):
    manager = ProgressManager(max_streams=2, max_streams_per_job=1)
    tracker = manager.create_tracker("job")
    subscribe = tracker.subscribe

    def failing_subscribe():
        assert manager.active_stream_count() == manager.active_stream_count("job") == 1
        raise failure("subscription failed")

    monkeypatch.setattr(tracker, "subscribe", failing_subscribe)
    for _ in range(3):
        with pytest.raises(failure, match="subscription failed"):
            manager.acquire_stream(tracker)
        assert tracker.subscriber_count() == manager.active_stream_count() == 0
        assert manager.stream_status()["streams_by_job"] == {}
    monkeypatch.setattr(tracker, "subscribe", subscribe)
    manager.acquire_stream(tracker).close()
    assert manager.active_stream_count() == 0


def test_unsubscribe_error_releases_capacity_once(monkeypatch):
    manager = ProgressManager(max_streams=2, max_streams_per_job=1)
    tracker = manager.create_tracker("job")
    lease = manager.acquire_stream(tracker)

    def failing_unsubscribe(client_queue):
        raise RuntimeError("unsubscribe failed")

    monkeypatch.setattr(tracker, "unsubscribe", failing_unsubscribe)
    with pytest.raises(RuntimeError, match="unsubscribe failed"):
        lease.close()
    for _ in range(3):
        lease.close()
    assert manager.active_stream_count() == manager.active_stream_count("job") == 0
    assert manager.stream_status()["streams_by_job"] == {}
    tracker.close()


@pytest.mark.parametrize("job_count,expected", [(1, 1), (3, 2)])
def test_concurrent_acquire_and_duplicate_close_keep_counts_atomic(job_count, expected):
    manager = ProgressManager(max_streams=2, max_streams_per_job=1)
    trackers = [manager.create_tracker(str(index)) for index in range(job_count)]
    workers = 24
    barrier = threading.Barrier(workers)

    def acquire(index):
        barrier.wait(timeout=10)
        try:
            return manager.acquire_stream(trackers[index % job_count])
        except StreamCapacityError:
            return None

    with ThreadPoolExecutor(max_workers=workers) as executor:
        leases = [lease for lease in executor.map(acquire, range(workers)) if lease is not None]
    assert len(leases) == manager.active_stream_count() == expected
    assert sum(tracker.subscriber_count() for tracker in trackers) == expected
    assert all(manager.active_stream_count(tracker.job_id) <= 1 for tracker in trackers)

    def close(index):
        barrier.wait(timeout=10)
        leases[index % len(leases)].close()
        return manager.active_stream_count()

    with ThreadPoolExecutor(max_workers=workers) as executor:
        counts = list(executor.map(close, range(workers)))
    assert min(counts) >= 0
    assert manager.active_stream_count() == 0
    assert manager.stream_status()["streams_by_job"] == {}
    assert all(tracker.subscriber_count() == 0 for tracker in trackers)


def test_tracker_operations_do_not_hold_manager_lock(monkeypatch):
    manager = ProgressManager()
    tracker = manager.create_tracker("job")

    def outside_manager_lock(operation):
        def checked(*args):
            assert manager._lock.acquire(blocking=False), "manager/tracker locks must not nest"
            manager._lock.release()
            return operation(*args)
        return checked

    for name in ("subscribe", "unsubscribe", "close"):
        monkeypatch.setattr(tracker, name, outside_manager_lock(getattr(tracker, name)))
    lease = manager.acquire_stream(tracker)
    manager.remove_tracker("job")
    lease.close()
    assert manager.active_stream_count() == 0


def test_internal_subscriptions_are_independent_and_done_events_still_drain():
    manager = ProgressManager(max_streams=2, max_streams_per_job=1)
    tracker = manager.create_tracker("job")
    queues = [tracker.subscribe() for _ in range(10)]
    lease = manager.acquire_stream(tracker)
    tracker.push("progress", {"pages": 1, "title": "进度"})
    tracker.push("completed", {"job_id": "job"})
    tracker.mark_done()
    expected = [
        f"event: progress\ndata: {json.dumps({'pages': 1, 'title': '进度'}, ensure_ascii=False)}\n\n",
        'event: completed\ndata: {"job_id": "job"}\n\n',
    ]
    for client_queue in [*queues, lease.queue]:
        assert list(tracker.iter_events(client_queue)) == expected
    assert manager.active_stream_count() == 1
    lease.close()
    assert tracker.subscriber_count() == 10
    tracker.unsubscribe(queues[0])
    tracker.unsubscribe(queues[0])
    assert tracker.subscriber_count() == 9
    tracker.close()
    assert tracker.subscriber_count() == manager.active_stream_count() == 0


def test_legacy_queue_fallback_and_generator_exit_unsubscribe():
    tracker = ProgressTracker("job")
    assert list(tracker.iter_events()) == []
    client_queue = tracker.subscribe()
    tracker.push("progress", {"pages": 1})
    events = tracker.iter_events()
    assert next(events).startswith("event: progress\n")
    events.close()
    assert client_queue.empty() and tracker.subscriber_count() == 0
    assert not tracker.is_done()


def test_slow_subscriber_queue_remains_bounded_and_receives_latest_event():
    tracker = ProgressTracker("job")
    client_queue = tracker.subscribe()
    for index in range(601):
        tracker.push("progress", {"index": index})
    assert client_queue.qsize() <= 500
    tracker.mark_done()
    events = list(tracker.iter_events(client_queue))
    assert json.loads(events[-1].split("data: ", 1)[1]) == {"index": 600}
    tracker.close()


@pytest.fixture
def clock(monkeypatch):
    class Clock:
        now = 100.0

        def advance(self, duration):
            self.now += duration

    clock = Clock()
    monkeypatch.setattr(progress, "time", SimpleNamespace(monotonic=lambda: clock.now))
    return clock


def test_lifetime_bounds_heartbeat_waits_without_marking_job_failed(clock, monkeypatch):
    manager = ProgressManager()
    tracker = manager.create_tracker("job")
    lease = manager.acquire_stream(tracker)
    timeouts = []

    def idle_wait(timeout):
        timeouts.append(timeout)
        clock.advance(timeout)
        raise queue.Empty

    monkeypatch.setattr(lease.queue, "get", idle_wait)
    assert list(tracker.iter_events(lease.queue, max_duration=5)) == [": heartbeat\n\n"] * 2
    assert timeouts == pytest.approx([2, 2, 1])
    assert not tracker.is_done()
    assert manager.active_stream_count() == tracker.subscriber_count() == 1
    lease.close()
    assert manager.active_stream_count() == tracker.subscriber_count() == 0


def test_lifetime_ends_even_when_progress_is_continuous(clock, monkeypatch):
    tracker = ProgressTracker("job")
    client_queue = tracker.subscribe()
    for index in range(5):
        tracker.push("progress", {"index": index})
    get = client_queue.get

    def busy_wait(timeout):
        clock.advance(1)
        return get(timeout=timeout)

    monkeypatch.setattr(client_queue, "get", busy_wait)
    events = list(tracker.iter_events(client_queue, max_duration=3))
    assert len(events) == 2
    assert all(event.startswith("event: progress\n") for event in events)
    assert not tracker.is_done()
    tracker.close()


def test_lifetime_counts_time_between_consumer_reads(clock):
    tracker = ProgressTracker("job")
    client_queue = tracker.subscribe()
    tracker.push("progress", {"pages": 1})
    tracker.push("progress", {"pages": 2})
    events = tracker.iter_events(client_queue, max_duration=300)
    assert next(events).startswith("event: progress\n")
    clock.advance(300)
    with pytest.raises(StopIteration):
        next(events)
    assert client_queue.qsize() == 1
    assert not tracker.is_done()
    tracker.close()


@pytest.mark.parametrize("duration", [0, -1])
def test_expired_lifetime_does_not_consume_queued_events(clock, duration):
    tracker = ProgressTracker("job")
    client_queue = tracker.subscribe()
    tracker.push("progress", {"pages": 1})
    assert list(tracker.iter_events(client_queue, max_duration=duration)) == []
    assert client_queue.qsize() == 1
    assert not tracker.is_done()
    tracker.close()


def test_omitted_lifetime_preserves_legacy_heartbeat_behavior(clock, monkeypatch):
    tracker = ProgressTracker("job")
    client_queue = tracker.subscribe()
    timeouts = []

    def idle_wait(timeout):
        timeouts.append(timeout)
        clock.advance(301)
        raise queue.Empty

    monkeypatch.setattr(client_queue, "get", idle_wait)
    events = tracker.iter_events(client_queue)
    assert next(events) == next(events) == ": heartbeat\n\n"
    assert timeouts == [2, 2]
    assert not tracker.is_done()
    tracker.mark_done()
    with pytest.raises(StopIteration):
        next(events)
    tracker.close()
