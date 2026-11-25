"""Regression tests for Round 3 full-codebase audit fixes (storage, logger, desktop, concurrency)."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from PIL import Image


def test_logger_sanitize_redacts_adjacent_urls_and_clean_external_truncation(client):
    from core import logger as lm
    from core import online_reader

    compact = '{"http":"http://user1:secret1@proxy1.invalid:8080","https":"socks5h://user2:secret2@proxy2.invalid:1080"}'
    cleaned = lm.sanitize(compact)
    assert "user1" not in cleaned and "secret1" not in cleaned
    assert "user2" not in cleaned and "secret2" not in cleaned
    assert "http://****@proxy1.invalid:8080" in cleaned
    assert "socks5h://****@proxy2.invalid:1080" in cleaned

    no_space = "A" * 4500
    record = logging.LogRecord("thirdparty", logging.WARNING, __file__, 1, no_space, (), None)
    lm._clean_external(record)
    assert record.msg.startswith("A" * 4000 + "…(已截断 500 字符)")

    long_exc = RuntimeError("x" * 320 + " proxy=http://leakuser:leakpass@127.0.0.1:7890")
    described = online_reader._describe(long_exc)
    assert "leakuser" not in described and "leakpass" not in described


def test_flat_pages_fullwidth_space_and_packer_skips_macos_appledouble(tmp_path, monkeypatch):
    from core import path_guard
    from core.download_folios import _title_from_output_path
    from core.download_layout import CHAPTER_MARKER
    from core.flat_pages import flatten, read_flat_page_name
    from core.packer import CbzPacker

    monkeypatch.setattr(path_guard, "DOWNLOAD_ROOT", tmp_path)
    book_dir = tmp_path / "flat_book"
    ch_dir = book_dir / "第1话\u3000特别篇__12345"
    ch_dir.mkdir(parents=True)
    (ch_dir / CHAPTER_MARKER).write_text(json.dumps({"format": 2, "photo_id": "12345"}), encoding="utf-8")
    Image.new("RGB", (8, 8), (10, 20, 30)).save(ch_dir / "00001.webp", "WEBP")

    class Photo:
        photo_id = "12345"
        name = "第1话\u3000特别篇"
        def __len__(self):
            return 1

    confirmed = flatten(book_dir, [Photo()], lambda: None)
    assert len(confirmed) == 1
    flat_file = next(iter(confirmed))
    assert read_flat_page_name(flat_file.name) == ("12345", "00001")

    chapter = tmp_path / "book" / "ch1"
    macos_dir = tmp_path / "book" / "__MACOSX"
    chapter.mkdir(parents=True)
    macos_dir.mkdir(parents=True)
    Image.new("RGB", (8, 8), (10, 20, 30)).save(chapter / "00001.webp", "WEBP")
    (chapter / "._00001.webp").write_bytes(b"\x00\x05\x16\x07appledouble-junk")
    (macos_dir / "00001.webp").write_bytes(b"\x00\x05\x16\x07macosx-junk")

    imgs = CbzPacker.packable_images(tmp_path / "book")
    assert [p.name for p in imgs] == ["00001.webp"]

    cbz_path = tmp_path / "book.cbz"
    assert CbzPacker().pack(tmp_path / "book", cbz_path) == cbz_path
    assert cbz_path.is_file()

    dl_root = tmp_path / "downloads"
    monkeypatch.setattr(path_guard, "DOWNLOAD_ROOT", dl_root)
    work_dir = dl_root / ".jmcomic-work" / "job_abc"
    work_dir.mkdir(parents=True)
    assert _title_from_output_path(str(work_dir), "12345") == ""
    assert _title_from_output_path(str(dl_root), "12345") == ""
    assert _title_from_output_path(str(dl_root / "风之纪行_12345"), "12345") == "风之纪行"


def test_local_availability_and_cover_reject_download_root_and_work_dirs(client, tmp_path, monkeypatch):
    from core import database as db, path_guard, settings as st
    from core.local_availability import is_readable

    dl_root = tmp_path / "downloads"
    work_dir = dl_root / ".jmcomic-work" / "job_leak"
    work_dir.mkdir(parents=True)
    Image.new("RGB", (12, 12), (90, 90, 90)).save(work_dir / "00001.jpg", "JPEG")
    Image.new("RGB", (12, 12), (90, 90, 90)).save(dl_root / "00001.jpg", "JPEG")
    monkeypatch.setattr(st, "DOWNLOAD_ROOT", dl_root)
    monkeypatch.setattr(path_guard, "DOWNLOAD_ROOT", dl_root)

    conn = db.get_db()
    try:
        conn.execute(
            "INSERT INTO jobs (job_id, album_id, title, status, output_path, total_pages, done_pages) "
            "VALUES ('job_root', '777001', 'BadRoot', 'completed', ?, 1, 1)",
            (str(dl_root),),
        )
        conn.execute(
            "INSERT INTO jobs (job_id, album_id, title, status, output_path, total_pages, done_pages) "
            "VALUES ('job_work', '777002', 'BadWork', 'completed', ?, 1, 1)",
            (str(work_dir),),
        )
        conn.commit()
    finally:
        conn.close()

    assert is_readable("777001") is False
    assert is_readable("777002") is False
    assert client.post("/api/jobs/job_work/open-folder").status_code == 404


def test_backup_and_restore_preserve_archive_mtime_order_and_data_only_maintenance(tmp_path, monkeypatch):
    from core import database as db
    from core.backup import create_backup
    from core.database_schema import CURRENT_SCHEMA_VERSION
    from core.restore import restore_backup

    src_home = tmp_path / "src_instance"
    src_data = src_home / "runtime" / "data"
    src_dl = src_home / "downloads"
    src_backups = tmp_path / "backups"
    src_data.mkdir(parents=True)
    src_dl.mkdir(parents=True)
    src_backups.mkdir(parents=True)

    db_file = src_data / "app.db"
    monkeypatch.setattr(db, "DB_DIR", src_data)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    db.init_db()

    album_dir = src_dl / "Book_888001"
    album_dir.mkdir()
    cbz = album_dir / "Book_888001.cbz"
    zip_f = album_dir / "Book_888001.zip"
    cbz.write_bytes(b"cbz-older")
    zip_f.write_bytes(b"zip-newer")
    base_ns = 1_700_000_000_100_000_000
    os.utime(cbz, ns=(base_ns, base_ns))
    os.utime(zip_f, ns=(base_ns + 500_000_000, base_ns + 500_000_000))  # +0.5s within same 2s DOS window

    conn = sqlite3.connect(db_file)
    try:
        conn.execute(
            "INSERT INTO album_maintenance (album_id, state, payload, error, generation) VALUES (?, 'running', ?, '', 1)",
            ("888001", json.dumps({"directory": "Book_888001", "_move": {"source": ["Old"], "target": ["Book_888001"], "identity": [1, 2]}})),
        )
        conn.commit()
    finally:
        conn.close()

    # 1. Inventory-only backup + restore clears _move and resets running -> pending cleanly
    inv_zip = src_backups / "inv.zip"
    create_backup(db_file, src_dl, inv_zip, include_downloads=False)
    dst_inv = tmp_path / "dst_inv"
    res_inv = restore_backup(inv_zip, dst_inv)
    conn = sqlite3.connect(res_inv.database)
    try:
        state, payload_raw = conn.execute("SELECT state, payload FROM album_maintenance WHERE album_id='888001'").fetchone()
        assert state == "pending" and "_move" not in json.loads(payload_raw)
    finally:
        conn.close()

    # 2. Full backup + restore preserves sub-second mtime_ns ordering between .cbz and .zip
    conn = sqlite3.connect(db_file)
    try:
        conn.execute("DELETE FROM album_maintenance")
        conn.commit()
    finally:
        conn.close()

    full_zip = src_backups / "full.zip"
    create_backup(db_file, src_dl, full_zip, include_downloads=True, service_stopped=True)
    dst_full = tmp_path / "dst_full"
    res_full = restore_backup(full_zip, dst_full)
    restored_cbz = res_full.downloads / "Book_888001" / "Book_888001.cbz"
    restored_zip = res_full.downloads / "Book_888001" / "Book_888001.zip"
    assert restored_zip.stat().st_mtime_ns > restored_cbz.stat().st_mtime_ns
    assert CURRENT_SCHEMA_VERSION >= 5


def test_progress_tracker_close_wakes_blocked_sse_immediately_and_health_counts_all_activity(client, monkeypatch):
    from core import online_reader
    from core.progress import ProgressTracker
    from core.tag_jobs import tag_jobs

    tracker = ProgressTracker("job_wakeup_test")
    q = tracker.subscribe()
    elapsed = []

    def reader():
        t0 = time.monotonic()
        list(tracker.iter_events(q))
        elapsed.append(time.monotonic() - t0)

    th = threading.Thread(target=reader)
    th.start()
    time.sleep(0.03)
    tracker.close()
    th.join(timeout=1.0)
    assert not th.is_alive() and elapsed and elapsed[0] < 0.5

    monkeypatch.setattr(online_reader, "in_flight_count", lambda: 2)
    monkeypatch.setattr(tag_jobs, "active_count", lambda: 1)
    resp = client.get("/api/system/health?activity=1")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["jm_active_clients"] >= 3
    assert "executors" in payload["jobs"]


def test_scheduler_stop_gate_and_launcher_permission_error_alive_check(tmp_path, monkeypatch):
    import launcher
    from core import desktop_platform, scheduler

    monkeypatch.setattr(scheduler, "_armed", True)
    monkeypatch.setattr(scheduler, "_draining", False)
    scheduler.request_stop()
    assert scheduler._armed is False and scheduler._draining is True
    started = []
    monkeypatch.setattr(scheduler, "start", lambda: started.append(1))
    scheduler.sync_with_settings()
    assert started == []
    monkeypatch.setattr(scheduler, "_draining", False)

    target_file = tmp_path / "Sample, Vol 1 (2).cbz"
    target_file.write_bytes(b"cbz")

    calls = []
    fake_ole32 = SimpleNamespace(
        CoInitializeEx=lambda pv, dw: (calls.append(("init", dw)), 0)[1],
        CoUninitialize=lambda: calls.append(("uninit",)),
    )
    fake_shell32 = SimpleNamespace(
        ILCreateFromPathW=lambda p: (calls.append(("pidl", p)), 12345)[1],
        SHOpenFolderAndSelectItems=lambda pidl, c, a, f: (calls.append(("select", pidl, c)), 0)[1],
        ILFree=lambda pidl: calls.append(("free", pidl)),
    )
    real_windll = desktop_platform.ctypes.WinDLL
    fake_win_dll = {"ole32": fake_ole32, "shell32": fake_shell32}
    monkeypatch.setattr(
        desktop_platform.ctypes,
        "WinDLL",
        lambda name, **kw: fake_win_dll[name] if name in fake_win_dll else real_windll(name, **kw),
    )
    fallback_called = []
    monkeypatch.setattr(desktop_platform, "_REAL_STARTFILE", lambda p: fallback_called.append(p))
    desktop_platform._select_in_explorer_windows(target_file, str(tmp_path))
    assert fallback_called == []
    assert ("pidl", str(target_file.resolve())) in calls
    assert ("select", 12345, None) in calls or ("select", 12345, 0) in calls
    assert ("free", 12345) in calls
    assert ("uninit",) in calls

    if os.name == "nt":
        monkeypatch.setattr(launcher.os, "kill", Mock(side_effect=PermissionError(5, "Access is denied")))
        assert launcher.process_alive(os.getpid()) is True
