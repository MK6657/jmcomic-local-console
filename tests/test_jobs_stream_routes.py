"""HTTP stream admission and cleanup using disposable progress managers."""
import pytest


@pytest.fixture
def streams(client, monkeypatch):
    from core.progress import ProgressManager
    from routes import api_jobs
    manager = ProgressManager(max_streams=2, max_streams_per_job=1)
    monkeypatch.setattr(api_jobs, "progress_manager", manager)
    yield manager
    manager.close_all()
    assert manager.active_stream_count() == 0


def make_response(client, job_id):
    from routes.api_jobs import job_events
    with client.application.test_request_context(f"/api/jobs/{job_id}/events"):
        return job_events(job_id)


def test_capacity_rejected_before_streaming_status_is_committed(client, streams):
    first = streams.create_tracker("one")
    streams.create_tracker("two")
    streams.create_tracker("three")
    response = make_response(client, "one")
    second = make_response(client, "two")
    try:
        rejected = make_response(client, "one")
        assert rejected.status_code == 429 and rejected.headers["Retry-After"] == "5"
        assert rejected.get_json()["reason"] == "sse_limit"
        rejected = make_response(client, "three")
        assert rejected.status_code == 429
        assert first.subscriber_count() == 1
        assert streams.active_stream_count() == 2
    finally:
        second.close()
        response.close()


def test_unconsumed_response_releases_reservation_and_is_idempotent(client, streams):
    tracker = streams.create_tracker("one")
    response = make_response(client, "one")
    assert tracker.subscriber_count() == streams.active_stream_count() == 1
    response.close()
    response.close()
    assert tracker.subscriber_count() == streams.active_stream_count() == 0


def test_response_construction_failure_releases_reserved_stream(client, streams, monkeypatch):
    from routes import api_jobs
    tracker = streams.create_tracker("one")
    def fail(*args, **kwargs):
        raise RuntimeError("response cannot be created")
    monkeypatch.setattr(api_jobs, "Response", fail)
    with pytest.raises(RuntimeError, match="cannot be created"):
        make_response(client, "one")
    assert streams.active_stream_count() == tracker.subscriber_count() == 0


def test_live_response_uses_bounded_lifetime_and_always_releases(client, streams, monkeypatch):
    tracker = streams.create_tracker("one")
    seen = []
    def events(queue, *, max_duration):
        seen.append(max_duration)
        yield "event: progress\ndata: {}\n\n"
    monkeypatch.setattr(tracker, "iter_events", events)
    response = client.get("/api/jobs/one/events")
    assert response.status_code == 200 and response.mimetype == "text/event-stream"
    assert response.headers["Cache-Control"] == "no-store"
    try:
        assert b"connected" in response.data and b"progress" in response.data
        assert seen == [300]
    finally:
        response.close()


def test_tracker_removal_does_not_release_unclosed_http_response(client, streams):
    tracker = streams.create_tracker("one")
    response = make_response(client, "one")
    streams.remove_tracker("one")
    assert tracker.is_done() and tracker.subscriber_count() == 0
    assert streams.active_stream_count() == 1
    response.close()
