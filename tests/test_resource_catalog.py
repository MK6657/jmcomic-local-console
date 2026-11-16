"""Resource identity survives task cleanup; no real files or upstream requests are used."""
import json
import sqlite3
from datetime import datetime, timedelta

import pytest
from PIL import Image


@pytest.fixture
def downloads(client, tmp_path, monkeypatch):
    from core import path_guard
    from routes import api_preview
    root = tmp_path / "downloads"
    root.mkdir()
    monkeypatch.setattr(path_guard, "DOWNLOAD_ROOT", root)
    monkeypatch.setattr(api_preview, "DOWNLOAD_ROOT", root)
    return root


def _page(folder, name="00001.png"):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    Image.new("RGB", (4, 4), "white").save(path)
    return path


def _completed(folder, job_id="job_resource_old", album_id="3001", created_at=None):
    from core import database as db
    db.insert_job(job_id, album_id, "Catalog title", ["71"])
    values = {"status": "completed", "output_path": str(folder)}
    if created_at is not None:
        with db.transaction() as connection:
            connection.execute("UPDATE jobs SET created_at=? WHERE job_id=?", (created_at, job_id))
    db.update_job(job_id, **values)
    db.upsert_album_meta(album_id, title="Catalog title", author="Catalog author")


@pytest.mark.parametrize("action", ["delete", "completed", "finished"])
def test_history_cleanup_keeps_library_reader_tags_and_files(client, downloads, action):
    from core import database as db, local_availability
    folder = downloads / "Catalog_3001"
    image = _page(folder)
    _completed(folder)
    db.add_album_tag("3001", "catalog")
    if action == "delete":
        response = client.delete("/api/jobs/job_resource_old")
    else:
        response = client.post("/api/jobs/clear/" + action)
    assert response.status_code == 200
    assert db.get_job("job_resource_old") is None
    assert image.is_file()
    assert local_availability.is_readable("3001")
    assert local_availability.readable_folder("3001") == str(folder)
    library = client.get("/api/library").get_json()
    assert library["total"] == 1
    assert library["items"][0]["readable"] is True
    assert db.get_library_stat()["downloaded_count"] == 1
    assert db.get_all_tags()[0]["tag"] == "catalog"
    assert db.search_library_tags("catalog")[0]["tag"] == "catalog"
    preview = client.get("/api/preview/3001")
    assert preview.status_code == 200 and preview.get_json()["total_pages"] == 1
    assert client.get(preview.get_json()["pages"][0]["url"]).status_code == 200


def test_cleaned_completed_favourite_stays_downloaded_and_not_batch_target(client, downloads):
    from core import batch_downloads, database as db, local_availability
    folder = downloads / "Catalog_3001"
    _page(folder)
    db.add_wishlist("3001", "Catalog title")
    _completed(folder)
    db.clear_jobs_by_status("completed")
    assert db.get_wishlist("3001")["download_status"] == "completed"
    assert db.album_job_facts(["3001"])["3001"]["has_completed"]
    assert db.get_completed_album_ids(wishlist_only=True) == ["3001"]
    readable = local_availability.readable_among(db.get_completed_album_ids())
    assert db.get_all_wishlist(readable_ids=readable)["items"][0]["status_group"] == "readable"
    assert batch_downloads.preview("undownloaded_favourites")["items"] == []
    with db.transaction() as conn:
        assert not db.insert_job_guarded(conn, "job_forbidden", "3001", "t", [], completed="forbidden")
        assert db.insert_job_guarded(conn, "job_new_chapter", "3001", "t", ["72"], completed="required")


@pytest.mark.parametrize("temporary", ["00001.q7w3e9rt.webp", ".jmcomic-staging/owner/00001.webp"])
def test_preview_direct_url_cannot_expose_unpublished_pages(client, downloads, temporary):
    folder = downloads / "Catalog_3001"
    _page(folder)
    _completed(folder)
    staged = folder / temporary
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(b"unpublished page")
    response = client.get("/api/preview-img/Catalog_3001/" + temporary)
    assert response.status_code == 404
    assert staged.is_file()  # filtering must never delete unknown leftovers
    preview = client.get("/api/preview/3001").get_json()
    assert preview["total_pages"] == 1


def test_new_chapters_remain_checkable_and_batch_downloadable_after_cleanup(client, downloads):
    from core import batch_downloads, database as db, update_store
    folder = downloads / "Catalog_3001"
    _page(folder)
    _completed(folder)
    now = datetime(2026, 1, 2, 12)
    update_store.note_download_started("3001", [("71", 1, "First")], False, now=now)
    update_store.record_success("3001", [("71", 1, "First"), ("72", 2, "New")], "manual",
                                now, now, 1, now + timedelta(days=1))
    db.clear_jobs_by_status("completed")
    assert update_store.due_candidates(now + timedelta(days=2), now + timedelta(days=5)) == ["3001"]
    assert update_store.next_due_at() == now + timedelta(days=1)
    plan = batch_downloads.preview("new_chapters")
    assert [(item["album_id"], item["photo_ids"]) for item in plan["items"]] == [("3001", ["72"])]
    result = batch_downloads.confirm("new_chapters", plan["token"])
    assert len(result["created"]) == 1
    assert json.loads(db.get_job(result["created"][0]["job_id"])["selected_photo_ids"]) == ["72"]


def test_failed_replacement_invalidates_resource_even_after_history_is_gone(client, downloads):
    from core import database as db, local_availability
    folder = downloads / "Catalog_3001"
    original = _page(folder)
    _completed(folder)
    db.delete_job("job_resource_old")
    original.unlink()
    db.insert_job("job_resource_new", "3001", "Replacement", [])
    db.update_job("job_resource_new", status="running", output_path=str(folder))
    assert db.mark_completed_jobs_superseded("3001", str(folder), "job_resource_new") == 1
    _page(folder, "00002.png")  # residual page of an unsuccessful replacement
    db.update_job("job_resource_new", status="failed")
    assert db.get_completed_job_by_album_id("3001")["superseded_at"]
    assert not local_availability.is_readable("3001")
    assert client.get("/api/preview/3001").status_code == 404
    db.update_job("job_resource_new", status="completed")
    assert local_availability.is_readable("3001")
    assert db.get_completed_job_by_album_id("3001")["job_id"] == "job_resource_new"


def test_latest_resource_stays_authoritative_after_deleting_newest_history(client, downloads):
    from core import database as db, local_availability
    older, newer = downloads / "Old_3001", downloads / "Missing_3001"
    _page(older)
    _completed(older, "job_older", created_at="2026-01-01T00:00:00")
    _completed(newer, "job_newer", created_at="2026-01-02T00:00:00")
    db.delete_job("job_newer")
    assert db.get_completed_job_by_album_id("3001")["job_id"] == "job_newer"
    assert not local_availability.is_readable("3001")  # never fall back to a different older directory


def test_failed_update_of_intact_resource_does_not_hide_existing_pages(client, downloads):
    from core import database as db, local_availability
    folder = downloads / "Catalog_3001"
    _page(folder)
    _completed(folder)
    db.clear_jobs_by_status("completed")
    db.insert_job("job_failed_update", "3001", "update", ["72"])
    db.update_job("job_failed_update", status="failed", output_path=str(folder))
    assert local_availability.is_readable("3001")
    assert len(db.resource_records(["3001"])) == 1


def test_completed_transition_and_resource_registration_are_atomic(client, downloads):
    from core import database as db
    folder = downloads / "Catalog_3001"
    _page(folder)
    db.insert_job("job_transition", "3001", "t", ["71"])
    assert not db.transition_job_status("job_transition", ["running"], "completed", output_path=str(folder))
    assert db.resource_records(["3001"]) == []
    assert db.transition_job_status("job_transition", ["queued"], "completed", output_path=str(folder))
    assert db.get_completed_job_by_album_id("3001")["output_path"] == str(folder)
    with pytest.raises(RuntimeError):
        with db.transaction() as conn:
            conn.execute("INSERT INTO jobs (job_id, album_id, status, output_path) "
                         "VALUES ('job_rollback', '4001', 'completed', ?)", (str(folder),))
            assert db.resource_records(["4001"], conn=conn)
            raise RuntimeError("roll back completion and its catalog record")
    assert db.get_job("job_rollback") is None
    assert db.resource_records(["4001"]) == []


def test_revoking_completion_removes_its_evidence_but_deleting_history_does_not(client, downloads):
    from core import database as db, local_availability
    folder = downloads / "Catalog_3001"
    image = _page(folder)
    _completed(folder)
    db.update_job("job_resource_old", status="failed")
    assert db.resource_records(["3001"]) == []
    assert not local_availability.is_readable("3001")
    assert image.is_file()
    db.update_job("job_resource_old", status="completed")
    db.delete_job("job_resource_old")
    assert local_availability.is_readable("3001")


def test_v2_migration_preserves_latest_order_superseded_and_runs_once(client, downloads, tmp_path, monkeypatch):
    from core import database as db, local_availability
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    folder = downloads / "Legacy_3001"
    _page(folder)
    connection = sqlite3.connect(legacy / "app.db")
    connection.execute("""CREATE TABLE jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT UNIQUE NOT NULL, album_id TEXT NOT NULL,
        title TEXT, selected_photo_ids TEXT, status TEXT NOT NULL DEFAULT 'queued', total_pages INTEGER DEFAULT 0,
        done_pages INTEGER DEFAULT 0, current_photo TEXT, current_image TEXT, output_path TEXT, error_message TEXT,
        created_at TEXT, updated_at TEXT, completed_at TEXT, superseded_at TEXT)""")
    connection.executemany("INSERT INTO jobs (job_id, album_id, title, status, output_path, created_at, superseded_at) "
                           "VALUES (?, ?, 'Legacy', ?, ?, '2026-01-01', ?)", [
        ("job_legacy_old", "3001", "completed", str(folder), None),
        ("job_legacy_new", "3001", "completed", str(downloads / "Missing"), None),
        ("job_legacy_superseded", "4001", "completed", str(folder), "2026-01-02"),
        ("job_legacy_failed", "5001", "failed", str(folder), None),
    ])
    connection.execute("PRAGMA user_version = 2")
    connection.commit()
    connection.close()
    monkeypatch.setattr(db, "DB_DIR", legacy)
    monkeypatch.setattr(db, "DB_PATH", legacy / "app.db")
    db.init_db()
    db.init_db()
    connection = db.get_db()
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == db.CURRENT_SCHEMA_VERSION
        assert connection.execute("SELECT COUNT(*) FROM resource_catalog").fetchone()[0] == 3
    finally:
        connection.close()
    assert db.get_completed_job_by_album_id("3001")["job_id"] == "job_legacy_new"
    db.clear_jobs_by_status("completed")
    db.init_db()
    assert len(db.resource_records(["3001", "4001"])) == 3
    assert not local_availability.is_readable("3001")
    assert not local_availability.is_readable("4001")
    assert db.resource_records(["5001"]) == []
