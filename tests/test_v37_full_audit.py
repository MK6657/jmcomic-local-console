"""Offline regression tests for v3.7 full-repository audit fixes."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest


def test_safe_local_next_blocks_control_char_and_authority_open_redirects(client):
    for bad_next in (
        "/%09/evil.example/phish",
        "/%0a/evil.example",
        "/%0d/evil.example",
        "//evil.example/path",
        "/\\evil.example/path",
        "https://evil.example/path",
        "/ui/classic",
    ):
        resp = client.get(f"/ui/classic?next={bad_next}")
        assert resp.status_code == 303
        assert resp.headers["Location"].endswith("/downloads"), (bad_next, resp.headers["Location"])

    good = client.get("/ui/atelier?next=/library%3Fsort%3Dtitle%23top")
    assert good.status_code == 303
    assert good.headers["Location"].endswith("/library?sort=title#top")


def test_origin_sec_fetch_site_and_form_media_type_guards(client):
    # Case-insensitive same-origin Origin header must be accepted
    ok_resp = client.post(
        "/api/wishlist/check",
        json={"album_ids": ["100"]},
        headers={"Host": "localhost:5000", "Origin": "HTTP://LocalHost:5000"},
    )
    assert ok_resp.status_code == 200

    # Cross-port Origin must be rejected
    bad_origin = client.post(
        "/api/wishlist/check",
        json={"album_ids": ["100"]},
        headers={"Host": "localhost:5000", "Origin": "http://localhost:9999"},
    )
    assert bad_origin.status_code == 403

    # Cross-port Sec-Fetch-Site: same-site must be rejected on /api/*, /ui/*, and non-GET requests
    assert client.get("/api/settings", headers={"Sec-Fetch-Site": "same-site"}).status_code == 403
    assert client.get("/ui/classic", headers={"Sec-Fetch-Site": "same-site"}).status_code == 403
    assert client.post("/api/wishlist/check", json={"album_ids": []}, headers={"Sec-Fetch-Site": "same-site"}).status_code == 403

    # Top-level GET navigation (e.g. browser address bar / bookmark) stays allowed
    assert client.get("/downloads", headers={"Sec-Fetch-Site": "none"}).status_code == 200

    # HTML form / plain-text Content-Types on /api/* must return 415 even with empty body
    for ctype in ("application/x-www-form-urlencoded", "text/plain", "multipart/form-data; boundary=xyz"):
        r = client.post("/api/jobs/clear/finished", data=b"", headers={"Content-Type": ctype})
        assert r.status_code == 415, (ctype, r.status_code)


def test_theme_spec_is_dark_and_ssr_scheme_attributes(client):
    from core.ui_themes import THEME_BY_KEY

    assert THEME_BY_KEY["night"].is_dark is True
    assert THEME_BY_KEY["cyber"].is_dark is True
    assert THEME_BY_KEY["sage"].is_dark is False
    assert THEME_BY_KEY["paper"].is_dark is False

    client.set_cookie("jmconsole_edition", "atelier")
    assert client.post("/api/settings", json={"ui_theme": "night"}).status_code == 200
    html = client.get("/").get_data(as_text=True)
    assert 'data-theme="night"' in html
    assert 'data-bs-theme="dark"' in html
    assert 'data-set-theme="night" aria-pressed="true" data-scheme="dark"' in html
    assert 'data-set-theme="cyber" aria-pressed="false" data-scheme="dark"' in html


def test_inventory_only_restore_clears_published_tasks_and_preserves_pending_maintenance(tmp_path, monkeypatch):
    import core.database as db
    from core.backup import create_backup
    from core.restore import restore_backup

    src_home = tmp_path / "src_home"
    src_data = src_home / "runtime" / "data"
    src_dl = src_home / "downloads"
    src_backups = src_home / "backups"
    for p in (src_data, src_dl, src_backups):
        p.mkdir(parents=True)

    db_file = src_data / "app.db"
    monkeypatch.setattr(db, "DB_DIR", src_data)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    db.init_db()

    album_dir = src_dl / "Album_777001"
    album_dir.mkdir()
    (album_dir / "00001.webp").write_bytes(b"fake-webp-bytes")

    db.insert_job("job777", "777001", "Album 777001", [])
    db.transition_job_status("job777", ["queued"], "completed", output_path=str(album_dir), total_pages=1, done_pages=1)

    conn = sqlite3.connect(db_file)
    try:
        conn.execute(
            "INSERT INTO task_page_receipts (job_id, page, digest) VALUES ('job777', '00001.webp', 'a' || zeroblob(0))"
        )
        conn.execute("UPDATE task_page_receipts SET digest = ? WHERE job_id = 'job777'", ("a" * 64,))
        conn.execute(
            "INSERT INTO published_tasks (job_id, album_id, job_order, snapshot, manifest) VALUES (?, ?, 1, ?, ?)",
            ("job777", "777001", "snap777", json.dumps({"files": ["00001.webp"]})),
        )
        conn.execute(
            "INSERT INTO album_maintenance (album_id, state, payload, error, generation) VALUES (?, 'running', ?, '', 1)",
            (
                "777001",
                json.dumps({"directory": "Album_777001", "_move": {"source": ["Old"], "target": ["Album_777001"], "identity": [1, 2]}}),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    inv_zip = src_backups / "inv_v37.zip"
    create_backup(db_file, src_dl, inv_zip, include_downloads=False)
    dst_inv = tmp_path / "dst_inv_v37"
    res = restore_backup(inv_zip, dst_inv)

    conn = sqlite3.connect(res.database)
    try:
        pub_count = conn.execute("SELECT COUNT(*) FROM published_tasks").fetchone()[0]
        rec_count = conn.execute("SELECT COUNT(*) FROM task_page_receipts").fetchone()[0]
        state, payload_raw = conn.execute("SELECT state, payload FROM album_maintenance WHERE album_id='777001'").fetchone()
        assert pub_count == 0
        assert rec_count == 0
        assert state == "pending"
        assert "_move" not in json.loads(payload_raw)
    finally:
        conn.close()


def test_mark_completed_jobs_superseded_handles_deleted_by_author_folder(tmp_path, monkeypatch):
    import core.database as db

    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "app.db")
    db.init_db()

    missing_dir = tmp_path / "downloads" / "by_author" / "SomeAuthor" / "DeletedBook_880011"
    existing_dir = tmp_path / "downloads" / "by_author" / "SomeAuthor" / "KeptBook_880012"
    existing_dir.mkdir(parents=True)

    db.insert_job("job_del", "880011", "DeletedBook", ["101"])
    db.transition_job_status("job_del", ["queued"], "completed", output_path=str(missing_dir), total_pages=5, done_pages=5)

    db.insert_job("job_keep", "880012", "KeptBook", ["102"])
    db.transition_job_status("job_keep", ["queued"], "completed", output_path=str(existing_dir), total_pages=5, done_pages=5)

    db.mark_completed_jobs_superseded("880011", None, "job_replacement_1", missing_predicate=lambda p: not Path(p).exists())
    db.mark_completed_jobs_superseded("880012", None, "job_replacement_2", missing_predicate=lambda p: not Path(p).exists())

    conn = sqlite3.connect(tmp_path / "app.db")
    try:
        sup_del = conn.execute("SELECT superseded_at FROM jobs WHERE job_id='job_del'").fetchone()[0]
        cat_del = conn.execute("SELECT superseded_at, title FROM resource_catalog WHERE job_id='job_del'").fetchone()
        sup_keep = conn.execute("SELECT superseded_at FROM jobs WHERE job_id='job_keep'").fetchone()[0]
        assert sup_del is not None
        assert cat_del[0] == sup_del and cat_del[1] == "DeletedBook"
        assert sup_keep is None
    finally:
        conn.close()


def test_unicode_case_insensitive_library_search_and_author_filter(tmp_path, monkeypatch):
    import core.database as db

    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "app.db")
    db.init_db()

    db.add_wishlist("660001", title="魔王Ⅱ的逆袭", author="ＡＢＣ老師", cover_url="")
    db.add_album_tag("660001", "ＮＴＲ", source="user")
    db.add_wishlist("660002", title="普通标题", author="其他作者", cover_url="")

    by_roman = db.get_library(keyword="魔王ⅱ")
    assert [it["album_id"] for it in by_roman["items"]] == ["660001"]

    by_fullwidth_tag = db.get_library(keyword="ＮＴＲ")
    assert [it["album_id"] for it in by_fullwidth_tag["items"]] == ["660001"]

    by_fullwidth_author = db.get_library(author="ａｂｃ老師")
    assert [it["album_id"] for it in by_fullwidth_author["items"]] == ["660001"]


def test_image_client_pool_disables_thread_local_curl_and_closes_stale_synchronously(monkeypatch):
    from core import jm_service, sdk_adapter

    captured_kwargs = []

    class DummyClient:
        def __init__(self, tag):
            self.tag = tag
            self.closed_on_thread = None

        def set_cache_dict(self, _val):
            pass

    monkeypatch.setattr(
        sdk_adapter,
        "new_client",
        lambda option, **kwargs: (captured_kwargs.append(kwargs), DummyClient(len(captured_kwargs)))[1],
    )
    closed_records = []
    orig_close = jm_service.close_client

    def track_close(client):
        if isinstance(client, DummyClient):
            client.closed_on_thread = threading.get_ident()
            closed_records.append((client.tag, client.closed_on_thread))
            return True
        return orig_close(client)

    monkeypatch.setattr(jm_service, "close_client", track_close)

    pool = jm_service._JobImageClientPool(max_idle=4)
    c1 = pool.acquire()
    assert isinstance(c1, DummyClient)
    assert captured_kwargs[-1].get("image_only") is True
    assert captured_kwargs[-1].get("use_thread_local_curl") is False

    pool.release(c1)
    jm_service.invalidate_option_cache()

    caller_tid = threading.get_ident()
    c2 = pool.acquire()
    assert c2 is not c1
    assert (c1.tag, caller_tid) in closed_records
    pool.close_all()


def test_cancel_job_pushes_canceled_sse_event_and_closes_tracker(tmp_path, monkeypatch):
    import core.database as db
    from core.job_manager import JobManager
    from core.progress import progress_manager

    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "app.db")
    db.init_db()

    job_id = "cancel_sse_1"
    db.insert_job(job_id, "550001", "Cancel SSE Book", [])
    db.transition_job_status(job_id, ["queued"], "running")

    tracker = progress_manager.create_tracker(job_id)
    sub_queue = tracker.subscribe()
    events = []
    done = threading.Event()

    def reader():
        for chunk in tracker.iter_events(sub_queue):
            events.append(chunk)
        done.set()

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    mgr = JobManager()
    ok, msg = mgr.cancel_job(job_id)
    assert ok is True and msg == "已取消"
    assert done.wait(timeout=3.0)
    joined = "".join(events)
    assert "event: canceled" in joined
    tracker.unsubscribe(sub_queue)
    assert tracker.subscriber_count() == 0
    progress_manager.remove_tracker(job_id)


def test_tag_sync_converts_stopping_errors_to_background_calls_stopped(monkeypatch):
    from core import tag_sync
    from core.background_calls import BackgroundCallsStopped
    from core.metadata_budget import MetadataStopped
    from core.upstream import UpstreamFailure

    monkeypatch.setattr(tag_sync.tag_jobs, "require_accepting", lambda: None)

    monkeypatch.setattr(tag_sync, "get_album_detail_cached", lambda _aid: (_ for _ in ()).throw(MetadataStopped("stop")))
    with pytest.raises(BackgroundCallsStopped):
        tag_sync.sync_one("12345")

    monkeypatch.setattr(
        tag_sync,
        "get_album_detail_cached",
        lambda _aid: (_ for _ in ()).throw(UpstreamFailure("stopping", "shutting down")),
    )
    with pytest.raises(BackgroundCallsStopped):
        tag_sync.sync_one("12345")


def test_logger_sanitizes_cookies_proxies_query_tokens_and_truncated_proxy_passwords():
    from core.logger import sanitize, sanitize_fields

    fields = sanitize_fields(
        {
            "cookies": {"AVS": "secret_session"},
            "proxies": {"https": "http://user:pass@127.0.0.1:7890"},
            "avs": "raw_avs_cookie",
            "ipb_pass_hash": "hash123",
            "safe": "visible",
        }
    )
    assert fields["cookies"] == "****"
    assert fields["proxies"] == "****"
    assert fields["avs"] == "****"
    assert fields["ipb_pass_hash"] == "****"
    assert fields["safe"] == "visible"

    qs = sanitize("GET https://example.com/api?token=my_secret_123&page=2#frag")
    assert "my_secret_123" not in qs
    assert "&page=2#frag" in qs

    truncated = sanitize("Proxy connect failed: http://proxyuser:987654")
    assert "987654" not in truncated
    assert "proxyuser" not in truncated
    assert sanitize("Local healthcheck http://127.0.0.1:5000/api/health") == "Local healthcheck http://127.0.0.1:5000/api/health"
