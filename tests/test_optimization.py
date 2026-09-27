"""Measured database/request regressions; no upstream or personal data access."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from unittest.mock import Mock


def test_simultaneous_detail_requests_fetch_once(client, monkeypatch):
    from core import jm_service as service
    barrier = threading.Barrier(8)
    fetch = Mock(return_value={"title": "test", "tags": ["original"]})
    monkeypatch.setattr(service, "get_album_detail", fetch)
    def read():
        barrier.wait(timeout=5)
        return service.get_album_detail_cached("123")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: read(), range(8)))
    fetch.assert_called_once_with("123")
    results[0]["tags"].append("caller-only")
    assert results[1]["tags"] == ["original"]
    assert service.get_album_detail_cached("123")["tags"] == ["original"]


def test_detail_cache_honors_ttl_for_both_tiers(client, monkeypatch):
    from core import database as db, jm_service as service
    fetch = Mock(return_value={"title": "fresh"})
    monkeypatch.setattr(service, "get_album_detail", fetch)
    db.set_cached_album_detail("123", '{"title":"old"}')
    conn = db.get_db()
    with conn:
        conn.execute("UPDATE album_detail_cache SET cached_at=?", ((datetime.now() - timedelta(seconds=60)).isoformat(),))
    conn.close()
    assert service.get_album_detail_cached("123", ttl=100)["title"] == "old"
    fetch.assert_not_called()
    assert service.get_album_detail_cached("123", ttl=10)["title"] == "fresh"
    fetch.assert_called_once()
    service.get_album_detail_cached("123", ttl=0)
    assert fetch.call_count == 2


def test_corrupt_persistent_detail_is_refetched(client, monkeypatch):
    from core import database as db, jm_service as service
    db.set_cached_album_detail("123", "broken JSON")
    fetch = Mock(return_value={"title": "repaired"})
    monkeypatch.setattr(service, "get_album_detail", fetch)
    assert service.get_album_detail_cached("123")["title"] == "repaired"
    assert db.get_cached_album_detail("123")["detail"]["title"] == "repaired"


def test_clear_cache_does_not_allow_inflight_request_to_refill(client, monkeypatch):
    from core import database as db, jm_service as service
    started, resume = threading.Event(), threading.Event()
    def fetch(_):
        started.set()
        assert resume.wait(timeout=5)
        return {"title": "in-flight"}
    monkeypatch.setattr(service, "get_album_detail", fetch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(service.get_album_detail_cached, "123")
        try:
            assert started.wait(timeout=3)
            assert client.post("/api/system/clear-cache").status_code == 200
        finally:
            resume.set()
        assert future.result()["title"] == "in-flight"
    assert db.get_cached_album_detail("123") is None
    assert (str(db.DB_PATH), "123") not in service._detail_cache


def test_clear_cache_removes_both_tiers_but_preserves_jobs(client, monkeypatch):
    from core import database as db, jm_service as service
    db.insert_job("job_keep", "123", "keep", [])
    fetch = Mock(return_value={"title": "old"})
    monkeypatch.setattr(service, "get_album_detail", fetch)
    service.get_album_detail_cached("123")
    assert client.post("/api/system/clear-cache").status_code == 200
    fetch.return_value = {"title": "new"}
    assert service.get_album_detail_cached("123")["title"] == "new"
    assert fetch.call_count == 2
    assert db.get_job("job_keep") is not None


def test_library_queued_filter_and_duplicate_tags(client):
    from core import database as db
    db.add_wishlist("123", "queued", "author", "")
    db.add_wishlist("456", "not queued", "author", "")
    db.update_wishlist_download_status("123", "queued")
    db.add_album_tag("123", "sample", source="user")
    result = client.get("/api/library?status=queued&tag=sample,SAMPLE").get_json()
    assert result["total"] == 1
    assert result["items"][0]["album_id"] == "123"


def test_library_page_uses_one_connection_and_four_queries(client, monkeypatch, tmp_path):
    import shutil
    from core import database as db, path_guard
    monkeypatch.setattr(path_guard, "DOWNLOAD_ROOT", tmp_path)
    output = tmp_path / "images"
    (output / "ch1").mkdir(parents=True)
    (output / "ch1" / "001.jpg").write_bytes(b"page")
    for n in range(25):
        db.insert_job(f"job_{n}", str(n + 1), "test", [])
        db.update_job(f"job_{n}", status="completed", output_path=str(output))
    original = db.get_db
    statements = []
    def connect():
        conn = original()
        conn.set_trace_callback(statements.append)
        return conn
    counted = Mock(side_effect=connect)
    monkeypatch.setattr(db, "get_db", counted)
    result = db.get_library(page_size=25)
    assert len(result["items"]) == 25
    assert counted.call_count == 1
    assert len(statements) == 4
    # file_exists follows the shared readable rule (it said "exists" for an empty folder); still no stale answer
    # after external directory changes
    items = client.get("/api/library?page_size=25").get_json()["items"]
    assert all(item["file_exists"] and item["readable"] for item in items)
    shutil.rmtree(output)
    items = client.get("/api/library?page_size=25").get_json()["items"]
    assert not any(item["file_exists"] or item["readable"] for item in items)


def test_tag_autocomplete_finds_rare_tags_and_escapes_wildcards(client):
    from core import database as db
    db.add_wishlist("123", "test", "author", "")
    for n in range(105):
        db.add_album_tag("123", f"popular-{n:03d}", source="user")
    db.add_album_tag("123", "zz-rare-tag", source="user")
    db.add_album_tag("123", "literal%tag", source="user")
    assert client.get("/api/library/search-tags?q=zz-rare").get_json()["tags"] == [{"tag": "zz-rare-tag", "count": 1}]
    assert client.get("/api/library/search-tags", query_string={"q": "%"}).get_json()["tags"] == [{"tag": "literal%tag", "count": 1}]


def test_busy_album_does_not_block_unrelated_queued_job(client, monkeypatch):
    from core import database as db
    from core.job_manager import JobManager
    from core.progress import progress_manager
    manager = JobManager()
    manager._running_jobs["job_active"] = Mock()
    manager._running_albums["job_active"] = "123"
    db.set_setting("max_running_jobs", "2")
    db.insert_job("job_blocked", "123", "same", [])
    db.insert_job("job_free", "456", "other", [])
    monkeypatch.setattr(threading.Thread, "start", Mock())
    try:
        manager.schedule_next()
        assert db.get_job("job_blocked")["status"] == "queued"
        assert db.get_job("job_free")["status"] == "running"
        assert manager.get_running_count() == 2
    finally:
        progress_manager.remove_tracker("job_free")


def test_bulk_tag_sync_rejects_duplicates_and_releases_lock(client, monkeypatch):
    from routes import api_library
    callbacks = []
    def thread_factory(*args, **kwargs):
        callbacks.append(kwargs["target"])
        return Mock()
    monkeypatch.setattr(api_library.threading, "Thread", thread_factory)
    assert client.post("/api/library/tags/sync-all").status_code == 202
    try:
        assert client.post("/api/library/tags/sync-all").status_code == 409
    finally:
        callbacks[0]()
    assert not api_library._bulk_sync_lock.locked()


def test_bulk_sync_failed_thread_start_releases_lock(client, monkeypatch):
    from routes import api_library
    monkeypatch.setattr(api_library.threading.Thread, "start", Mock(side_effect=RuntimeError))
    assert client.post("/api/library/tags/sync-all").status_code == 503
    assert not api_library._bulk_sync_lock.locked()


def test_archive_cache_eviction_is_thread_safe(client, monkeypatch):
    from routes import api_jobs
    monkeypatch.setattr(api_jobs, "_cbz_cache", {})
    monkeypatch.setattr(api_jobs, "_CBZ_CACHE_MAX", 5)
    monkeypatch.setattr(api_jobs, "_scan_cbz_path", lambda _: True)
    with ThreadPoolExecutor(max_workers=12) as pool:
        assert all(pool.map(lambda n: api_jobs._check_cbz_path(str(n)), range(100)))
    assert len(api_jobs._cbz_cache) <= 5
