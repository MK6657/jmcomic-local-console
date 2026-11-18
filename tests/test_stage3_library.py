import io
import json
import threading
from unittest.mock import Mock

import pytest


@pytest.fixture
def owner(monkeypatch):
    from core import tag_sync
    from core.tag_jobs import TagJobs
    from routes import api_library, api_wishlist
    service = TagJobs(capacity=1)
    for module in (api_library, api_wishlist, tag_sync):
        monkeypatch.setattr(module, "tag_jobs", service)
    yield service
    assert service.stop(2)


@pytest.mark.parametrize("method", ["post", "delete"])
@pytest.mark.parametrize("body", [b"", b"{", b"null", b"[]", b"false"])
def test_bad_tag_body_preserves_existing_tags(client, method, body):
    from core import database as db
    db.add_album_tag("100", "keep")
    endpoint = "/api/library/100/tags"
    response = getattr(client, method)(endpoint, data=body, content_type="application/json" if body else None)
    assert response.status_code == 400 and response.is_json
    assert [tag["tag"] for tag in db.get_album_tags("100")] == ["keep"]
    assert client.delete(endpoint, json={}).get_json()["deleted"] == 1


def test_sync_all_keyset_covers_library_even_after_failures(client, owner, monkeypatch):
    from core import database as db, tag_sync
    ids = [str(number) for number in range(100, 204)]
    with db.transaction() as conn:
        conn.executemany("INSERT INTO wishlist (album_id,added_at) VALUES (?, 'today')", [(aid,) for aid in ids])
    db.insert_job("catalog", "999", "catalog only", [])
    db.transition_job_status("catalog", ["queued"], "completed", output_path="catalog")
    seen = []
    def lookup(aid):
        seen.append(aid)
        if aid == ids[0]:
            raise ValueError("fixture")
        return {"tags": ["sync"], "title": aid}
    monkeypatch.setattr(tag_sync, "get_album_detail_cached", lookup)
    for _ in range(2):
        owner.submit(tag_sync.sync_all, bulk=True).result(timeout=10)
        assert seen == [*ids, "999"]
        seen.clear()
    assert db.get_album_tags("999")[0]["tag"] == "sync"
    assert db.get_album_meta(ids[-1])["tags_synced_at"]


@pytest.mark.parametrize("origin", ["bulk", "wishlist", "single"])
def test_shutdown_owns_inflight_tag_work_without_late_writes(client, owner, monkeypatch, origin):
    from core import database as db, tag_sync
    from routes import api_library
    from concurrent.futures import Future
    entered, release = threading.Event(), threading.Event()
    calls = []
    def lookup(aid):
        calls.append(aid)
        entered.set()
        assert release.wait(3)
        return {"tags": ["late"]}
    monkeypatch.setattr(tag_sync, "get_album_detail_cached", lookup)
    try:
        if origin == "single":
            monkeypatch.setattr(api_library, "get_album_detail_cached", lookup)
            original = Future.result
            monkeypatch.setattr(Future, "result", lambda self, timeout=None: original(self, timeout=0))
            assert client.post("/api/library/100/tags/sync").status_code == 504
        elif origin == "bulk":
            db.add_wishlist("100")
            db.add_wishlist("101")
            assert client.post("/api/library/tags/sync-all").status_code == 202
        else:
            response = client.post("/api/wishlist", json={"album_id": "100", "title": "t", "author": "a"})
            assert response.status_code == 201 and db.get_wishlist("100")
        assert entered.wait(1)
        assert client.post("/api/library/101/tags/sync").get_json()["error_kind"] == "busy"
        assert not owner.stop(0)
        with pytest.raises(RuntimeError):
            owner.start()
        for endpoint, body in [("/api/library/tags/sync-all", {}), ("/api/library/101/tags/sync", {}),
                               ("/api/wishlist", {"album_id": "303"})]:
            response = client.post(endpoint, json=body)
            assert response.status_code == 503 and response.get_json()["error_kind"] == "stopping"
    finally:
        release.set()
        assert owner.stop(2)
    assert calls == ["100"] and db.get_album_tags("100") == [] and db.get_wishlist("303") is None


@pytest.mark.parametrize("method", ["get_library", "get_all_wishlist", "get_library_stat"])
def test_concurrent_delete_cannot_split_library_snapshot(client, monkeypatch, method):
    from contextlib import closing
    from core import database as db
    db.add_wishlist("123", "original")
    db.add_album_tag("123", "original")
    connect = db.get_db
    with closing(connect()) as writer:
        mutations = []
        def trace(statement):
            if statement.lstrip().upper().startswith(("WITH", "SELECT")):
                mutations.append(statement)
                if len(mutations) == 2:
                    with writer:
                        writer.execute("DELETE FROM wishlist")
                        writer.execute("DELETE FROM album_tags")
        def traced():
            reader = connect()
            reader.set_trace_callback(trace)
            return reader
        monkeypatch.setattr(db, "get_db", traced)
        result = getattr(db, method)()
        assert len(mutations) >= 2 and not writer.execute("SELECT * FROM wishlist").fetchall()
    assert result["total"] == 1
    if method == "get_library_stat":
        assert result["wishlist_count"] == result["tag_count"] == 1
    else:
        assert result["items"][0]["album_id"] == "123"


def test_cached_truncated_only_page_is_missing_everywhere(client, monkeypatch, tmp_path):
    from core import database as db, path_guard
    monkeypatch.setattr(path_guard, "DOWNLOAD_ROOT", tmp_path)
    folder = tmp_path / "album"
    folder.mkdir()
    page = folder / "01.jpg"
    page.write_bytes(b"page")
    db.add_wishlist("123")
    db.insert_job("job", "123", "", [])
    db.transition_job_status("job", ["queued"], "completed", output_path=str(folder))
    assert client.get("/api/library/123").get_json()["item"]["readable"]
    page.write_bytes(b"")
    for path in ("/api/library?status=missing", "/api/wishlist?status=missing"):
        item, = client.get(path).get_json()["items"]
        assert not item["readable"] and item["files_missing"] and item["local_problem"] == "deleted"
    assert client.get("/api/library/stats").get_json()["readable_count"] == 0


def test_downloads_page_does_not_query_job_history(client, monkeypatch):
    from routes import page_routes
    query = Mock(side_effect=AssertionError("unneeded download history query"))
    monkeypatch.setattr(page_routes, "get_db", query)
    assert client.get("/downloads").status_code == 200
    query.assert_not_called()


def test_settings_import_respects_explicit_omitted_and_invalid_proxy(client):
    def upload(data):
        return client.post("/api/settings/import", data={
            "file": (io.BytesIO(json.dumps(data).encode()), "settings.json")}).get_json()
    proxy = "socks5h://127.0.0.1:8888"
    assert upload({"proxy": proxy})["imported"] == 1
    assert upload({"timeout": 22})["imported"] == 1
    result = upload({"proxy": "invalid", "timeout": 23})
    assert (result["imported"], result["skipped"]) == (1, 1)
    saved = client.get("/api/settings").get_json()["settings"]
    assert saved["proxy"] == proxy and saved["timeout"] == "23"
