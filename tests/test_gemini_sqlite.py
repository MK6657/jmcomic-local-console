from contextlib import closing
from datetime import datetime, timedelta
from io import BytesIO
import json
from unittest.mock import Mock

import pytest
from PIL import Image

from core import database as db


def _import_wishlist(client, rows):
    payload = BytesIO(json.dumps(rows, ensure_ascii=False).encode("utf-8"))
    return client.post("/api/wishlist/import-file", data={"file": (payload, "wishlist.json")})


def _completed(album_id, job_order, path=None, *, superseded_at=None):
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO resource_catalog (job_id, job_order, album_id, output_path, created_at, superseded_at) "
            "VALUES (?, ?, ?, ?, '2026-01-01T00:00:00', ?)",
            (f"job_{job_order}", job_order, album_id, str(path) if path else None, superseded_at),
        )


def _loose_page(folder):
    folder.mkdir()
    page = folder / "00001.png"
    Image.new("RGB", (2, 2), "blue").save(page)
    return page


def test_queue_claim_rolls_back_if_the_bookmark_write_fails(client, monkeypatch):
    from core import task_commands

    db.add_wishlist("601")
    job_id = task_commands.enqueue_download("601", "Queued title", []).job_id
    write_bookmark = db.update_wishlist_download_status

    def fail(album_id, status, *, conn):
        assert conn.in_transaction
        assert conn.execute("SELECT status FROM jobs WHERE job_id=?", (job_id,)).fetchone()[0] == "running"
        write_bookmark(album_id, status, conn=conn)
        assert conn.execute("SELECT download_status FROM wishlist").fetchone()[0] == "downloading"
        raise RuntimeError("injected bookmark write failure")

    with monkeypatch.context() as failure:
        failure.setattr(db, "update_wishlist_download_status", fail)
        with pytest.raises(RuntimeError, match="bookmark write"):
            db.claim_next_queued_job()
    assert db.get_job(job_id)["status"] == db.get_wishlist("601")["download_status"] == "queued"
    assert db.claim_next_queued_job()["job_id"] == job_id
    assert db.get_job(job_id)["status"] == "running"
    assert db.get_wishlist("601")["download_status"] == "downloading"
    assert task_commands.cancel_download(job_id)[0]
    assert db.clear_jobs_by_status("canceled") == 1
    assert db.get_job(job_id) is None
    assert db.get_wishlist("601")["download_status"] == "none"
    assert db.get_wishlist_downloading_count() == 0


@pytest.mark.parametrize("added_at", ["2026-01-01T01:02:03.123456", "2026-01-01T01:02:03.123Z", None])
def test_wishlist_export_and_reimport_keep_metadata_and_order_across_file_batches(client, added_at):
    album_ids = [str(album_id) for album_id in range(901, 1104)]
    with db.transaction() as conn:
        conn.executemany(
            "INSERT INTO wishlist(album_id,title,author,cover_url,added_at) VALUES (?, '  ', '', '', ?)",
            [(album_id, added_at) for album_id in album_ids],
        )
    db.upsert_album_meta(album_ids[0], "补齐标题", "补齐作者", "/cover.png")
    db.update_wishlist_title(album_ids[-1], "收藏标题", "收藏作者", "/local-cover.png")
    db.upsert_album_meta(album_ids[-1], "缓存标题", "缓存作者", "/cached-cover.png")

    def listed():
        return [item for page in (1, 2) for item in client.get(
            "/api/wishlist", query_string={"page": page, "page_size": 200}
        ).get_json()["items"]]

    fields = ("album_id", "title", "author", "cover_url", "added_at")
    source = [tuple(item[field] for field in fields) for item in listed()]
    rows = client.get("/api/wishlist/export").get_json()
    assert [tuple(item[field] for field in fields) for item in rows] == source
    assert (album_ids[0], "补齐标题", "补齐作者", "/cover.png", added_at) in source
    assert (album_ids[-1], "收藏标题", "收藏作者", "/local-cover.png", added_at) in source

    with db.transaction() as conn:
        conn.execute("DELETE FROM wishlist")
        conn.execute("DELETE FROM album_meta")
    for batch in (rows[:200], rows[200:]):
        assert _import_wishlist(client, batch).get_json()["added"] == len(batch)
        assert _import_wishlist(client, batch).get_json()["skipped_existing"] == len(batch)
    assert [tuple(item[field] for field in fields) for item in listed()] == source


def test_wishlist_import_orders_missing_or_invalid_dates_within_a_file(client):
    rows = [
        {"album_id": "602", "added_at": "2026-02-30T01:02:03"},
        {"album_id": "601"},
        {"album_id": "603", "added_at": {"unexpected": True}},
    ]
    assert _import_wishlist(client, rows).get_json()["added"] == 3
    restored = client.get("/api/wishlist").get_json()["items"]
    assert [item["album_id"] for item in restored] == ["602", "601", "603"]
    timestamps = [datetime.fromisoformat(item["added_at"]) for item in restored]
    assert timestamps[0] - timestamps[2] == timedelta(microseconds=2)


def test_manually_added_auto_tag_survives_resync_and_auto_clear(client):
    db.add_wishlist("601")
    assert db.batch_sync_auto_tags("601", ["pin", "old"]) == 2
    route = "/api/library/601/tags"
    response = client.post(route, json={"tags": [" PIN ", "pin"]})
    assert response.status_code == 200 and response.get_json()["added"] == response.get_json()["skipped"] == 1
    assert db.batch_sync_auto_tags("601", ["pin", "new"]) == 1
    assert [(tag["tag"], tag["source"]) for tag in db.get_album_tags("601")] == [("new", "auto"), ("pin", "user")]
    assert client.delete(route, json={"source": "auto"}).get_json()["deleted"] == 1
    assert [(tag["tag"], tag["source"]) for tag in db.get_album_tags("601")] == [("pin", "user")]


def test_library_updated_sort_preserves_a_completed_albums_timestamp_on_unbookmark(client):
    first, second = "2026-01-10T01:02:03", "2026-01-11T01:02:03"
    db.add_wishlist("601", added_at=first)
    db.add_wishlist("602", added_at=second)
    _completed("601", 1)
    _completed("602", 2)
    db.upsert_album_meta("601", "First")
    db.upsert_album_meta("602", "Second")
    with db.transaction() as conn:
        conn.execute("UPDATE album_meta SET updated_at=? WHERE album_id='601'", (second,))
        conn.execute("UPDATE album_meta SET updated_at=? WHERE album_id='602'", (first,))

    def listed():
        response = client.get("/api/library?sort=updated_at")
        return [(row["album_id"], row["updated_at"], row["is_wishlisted"])
                for row in response.get_json()["items"]]

    assert listed() == [("601", second, 1), ("602", first, 1)]
    assert client.delete("/api/wishlist/601").status_code == 200
    assert listed() == [("601", second, 0), ("602", first, 1)]


def test_history_retains_latest_200_distinct_words_and_compacts_legacy_duplicates(client, monkeypatch):
    class SearchClock(datetime):
        @classmethod
        def now(cls):
            return cls(2026, 11, 20, 12, 0)

    base = datetime(2026, 1, 1)
    newer = "2026-02-01T00:00:00"
    legacy = [(f"term-{index:03d}", (base + timedelta(seconds=index)).isoformat()) for index in range(210)]
    legacy += [("term-000", newer)] + [("term-000", "2026-01-01T00:00:00")] * 235
    with db.transaction() as conn:
        conn.executemany("INSERT INTO search_history(keyword,created_at) VALUES (?,?)", legacy)
    assert db.get_search_history(limit=1) == [{"keyword": "term-000", "created_at": newer}]

    monkeypatch.setattr(db, "datetime", SearchClock)
    db.add_search_history("中文/空 格")
    db.add_search_history("term-100")
    with closing(db.get_db()) as conn:
        assert tuple(conn.execute("SELECT COUNT(*), COUNT(DISTINCT keyword) FROM search_history").fetchone()) == (200, 200)
    history = db.get_search_history(limit=200)
    assert [row["keyword"] for row in history[:3]] == ["term-100", "中文/空 格", "term-000"]
    assert history[0]["created_at"] == history[1]["created_at"] == SearchClock.now().isoformat()
    assert {row["keyword"] for row in history[2:]} == {"term-000"} | {
        f"term-{index:03d}" for index in range(12, 210) if index != 100
    }


def test_latest_resource_lookup_returns_one_row_per_album_and_keeps_superseded_evidence(client):
    with db.transaction() as conn:
        conn.executemany(
            "INSERT INTO resource_catalog (job_id, job_order, album_id, created_at) "
            "VALUES (?, ?, '601', '2026-01-01T00:00:00')",
            [(f"old_{job_order}", job_order) for job_order in range(1, 501)],
        )
    _completed("601", 501, superseded_at="2026-01-02T00:00:00")
    _completed("602", 502)
    latest = db.resource_records(["601", "602", "601"], latest_only=True)
    assert [row["job_id"] for row in latest] == ["job_502", "job_501"]
    assert latest[1]["superseded_at"] and len(db.resource_records(["601", "602"])) == 502


@pytest.mark.parametrize("route,filters", [
    ("/api/library", {"status": "readable", "q": "target%_"}),
    ("/api/library", {"status": "readable", "author": "Target Author"}),
    ("/api/library", {"status": "readable", "tag": "a,b,A"}),
    ("/api/library", {"status": "readable", "q": "target%_", "author": "Target Author", "tag": "a,b"}),
    ("/api/wishlist", {"status": "readable", "q": "target%_"}),
])
def test_list_probes_only_completed_albums_matching_its_display_filters(client, tmp_path, monkeypatch, route, filters):
    from core import local_availability, path_guard

    monkeypatch.setattr(path_guard, "DOWNLOAD_ROOT", tmp_path)
    folder = tmp_path / "target"
    _loose_page(folder)
    db.add_wishlist("601")
    db.upsert_album_meta("601", "target%_ title", "Target Author")
    db.add_album_tag("601", "a")
    db.add_album_tag("601", "b")
    _completed("601", 1, folder)
    for job_order in range(2, 43):
        album_id = str(600 + job_order)
        db.add_wishlist(album_id, "targetNO title", "Other Author")
        db.add_album_tag(album_id, "a")
        _completed(album_id, job_order, tmp_path / album_id)
    checked = Mock(wraps=local_availability._folder_state)
    monkeypatch.setattr(local_availability, "_folder_state", checked)

    response = client.get(route, query_string=filters)
    assert response.status_code == 200
    result = response.get_json()
    assert result["total"] == 1 and [item["album_id"] for item in result["items"]] == ["601"]
    assert result["items"][0]["readable"] is True
    assert [call.args[0] for call in checked.call_args_list] == [str(folder)]
    if route.endswith("wishlist"):
        assert result["group_counts"] == {"readable": 1, "active": 0, "failed": 0, "missing": 0, "none": 0}


def test_stats_cache_limits_file_checks_and_refreshes_on_catalog_change_or_ttl(client, tmp_path, monkeypatch):
    from core import local_availability, path_guard
    from routes import api_library

    monkeypatch.setattr(path_guard, "DOWNLOAD_ROOT", tmp_path)
    clock = Mock(return_value=10.0)
    monkeypatch.setattr(api_library, "monotonic", clock)
    checked = Mock(wraps=local_availability._folder_state)
    monkeypatch.setattr(local_availability, "_folder_state", checked)
    page = _loose_page(tmp_path / "original")
    _completed("601", 1, page.parent)
    for job_order in range(2, 26):
        album_id = str(600 + job_order)
        _completed(album_id, job_order, tmp_path / album_id)

    def stats():
        response = client.get("/api/library/stats")
        assert response.status_code == 200
        return response.get_json()

    assert stats()["readable_count"] == 1 and checked.call_count == 25
    db.add_wishlist("601")
    assert stats()["wishlist_count"] == 1 and checked.call_count == 25
    page.unlink()
    assert stats()["readable_count"] == 1 and checked.call_count == 25
    clock.return_value += api_library._READABLE_COUNT_TTL
    assert stats()["readable_count"] == 0 and checked.call_count == 50
    newest = _loose_page(tmp_path / "updated")
    _completed("601", 26, newest.parent)
    updated = stats()
    assert updated["downloaded_count"] == 25 and updated["readable_count"] == 1 and checked.call_count == 75
    newest.unlink()
    assert client.get("/api/library/601").get_json()["item"]["readable"] is False
    assert stats()["readable_count"] == 0 and checked.call_count == 101
