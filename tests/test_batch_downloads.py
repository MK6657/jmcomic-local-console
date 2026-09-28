"""批量下载（PR-B）：「下载新章节」「下载未下载的收藏」「下载选中的收藏」和收藏单行「下载」在服务端的保证。

Everything runs in pytest's tmp folders with the network blocked (conftest): the real downloads/ and runtime/ are
never touched (asserted by _isolated; conftest also points the app root at a temporary folder before any test module
is imported, and checks it before create_app()). No test starts a download: JobManager._schedule_next is replaced by a
recorder, and both references to download_album_job fail the test when called. Update-check rows are written
straight through core.update_store (begin_check + record_success / record_failure): no checker, no client.
No core module is imported at collection time (core.logger opens its log files where the app root points then)."""
import ast
import json
import re
import shutil
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
T0 = datetime(2026, 9, 1, 12, 0, 0)
PREVIEW = "/api/batch-downloads/preview"
CONFIRM = "/api/batch-downloads/confirm"
PREVIEW_KEYS = {"status", "kind", "token", "limit", "items", "counts", "skipped", "more", "review", "out_of_scope",
                "window", "queue", "settings"}
WINDOW_KEYS = {"enabled", "open", "start", "end", "never", "invalid", "opens_tomorrow"}


# ─── helpers ──────────────────────────────────────────────────────────────────


def _forget_local():
    from core import local_availability
    with local_availability._cache_lock:
        local_availability._cache.clear()


def _episodes(chapters):
    """['71', ...] or [(pid, index, title), ...] → [(pid, index, title)] in upstream order."""
    out = []
    for n, entry in enumerate(chapters, 1):
        out.append(tuple(entry) if isinstance(entry, (tuple, list)) else (str(entry), n, f"第{n}话"))
    return out


def _sql(statement, params=()):
    from core import database as db
    conn = db.get_db()
    try:
        conn.execute(statement, params)
        conn.commit()
    finally:
        conn.close()


def _rows(sql, params=()):
    from core import database as db
    conn = db.get_db()
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def _jobs():
    return _rows("SELECT * FROM jobs ORDER BY id")


def _table(name):
    from core import database as db
    conn = db.get_db()
    try:
        return [tuple(r) for r in conn.execute(f"SELECT * FROM {name} ORDER BY 1")]
    finally:
        conn.close()


def _tree(root):
    return sorted((str(p.relative_to(root)), p.stat().st_size if p.is_file() else -1, p.stat().st_mtime_ns)
                  for p in root.rglob("*"))


def _job(job_id, album_id, status, output_path=None, photo_ids=(), created_at=None, title="t"):
    from core import database as db
    db.insert_job(job_id, album_id, title, list(photo_ids))
    db.update_job(job_id, status=status, output_path=str(output_path) if output_path else None)
    if created_at:
        _sql("UPDATE jobs SET created_at=? WHERE job_id=?", (created_at, job_id))


def _fav(album_id, title=None, added_at=None, legacy=None):
    from core import database as db
    db.add_wishlist(album_id, f"Fav{album_id}" if title is None else title)
    if added_at:
        _sql("UPDATE wishlist SET added_at=? WHERE album_id=?", (added_at, album_id))
    if legacy is not None:
        _sql("UPDATE wishlist SET download_status=? WHERE album_id=?", (legacy, album_id))


def favourite(album_id, title=None, added_at=None, jobs=(), legacy=None):
    """A favourite, then its jobs in order (each later than the one before), then an optional legacy status."""
    _fav(album_id, title, added_at)
    for n, status in enumerate(jobs, 1):
        _job(f"j{album_id}_{n}", album_id, status)
    if legacy is not None:
        _sql("UPDATE wishlist SET download_status=? WHERE album_id=?", (legacy, album_id))


def confirm_new(album_id, upstream, at=T0):
    """A successful check at `at` that saw `upstream` (database only)."""
    from core import update_store
    update_store.begin_check(album_id, "auto", at)
    return update_store.record_success(album_id, _episodes(upstream), "auto", at, at, 1, at + timedelta(days=1))


def fail_check(album_id, at=T0):
    from core import update_store
    update_store.begin_check(album_id, "auto", at)
    return update_store.record_failure(album_id, "network", "ConnectionError", "auto", at, at, 1, lambda n: 60)


def preview(client, kind, **body):
    response = client.post(PREVIEW, json={"kind": kind, **body})
    assert response.status_code == 200, response.get_json()
    data = response.get_json()
    assert set(data) == PREVIEW_KEYS and data["status"] == "ok" and data["kind"] == kind
    return data


def confirm(client, kind, token, **body):
    return client.post(CONFIRM, json={"kind": kind, "token": token, **body})


def _ids(data):
    return [item["album_id"] for item in data["items"]]


def _photos(data):
    return [(item["album_id"], item["photo_ids"]) for item in data["items"]]


def _reasons(data):
    return [(item["album_id"], item["reason"]) for item in data["skipped"]]


def _fake_now(monkeypatch, pinned):
    from core import scheduler

    class Pinned(datetime):
        @classmethod
        def now(cls, tz=None):
            return pinned
    monkeypatch.setattr(scheduler, "datetime", Pinned)


# ─── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def downloads(client, tmp_path, monkeypatch):
    from core import archive_pages, jm_service, path_guard
    from routes import api_export, api_preview
    root = tmp_path / "downloads"
    root.mkdir()
    for module in (path_guard, api_preview, api_export, jm_service):
        monkeypatch.setattr(module, "DOWNLOAD_ROOT", root)
    archive_pages.clear_cache()
    _forget_local()
    yield root
    archive_pages.clear_cache()
    _forget_local()


@pytest.fixture(autouse=True)
def _isolated(downloads, tmp_path):
    """Every test here runs on a tmp database and a tmp downloads folder."""
    from core import database as db, path_guard
    assert Path(db.DB_PATH).resolve().is_relative_to(tmp_path.resolve())
    assert Path(path_guard.DOWNLOAD_ROOT).resolve().is_relative_to(tmp_path.resolve())
    yield


class _Starts:
    def __init__(self, real):
        self.real = real      # the real JobManager._schedule_next
        self.calls = []       # the jobs table [(job_id, status)] each time something asked to schedule


@pytest.fixture(autouse=True)
def no_start(client, monkeypatch):
    """Scheduling is only recorded; a real download fails the test; no dl-* thread is left behind."""
    import core.job_manager as job_manager_module
    from core import jm_service
    from core.job_manager import JobManager
    starts = _Starts(JobManager.__dict__["_schedule_next"])

    def record(manager):
        starts.calls.append([(job["job_id"], job["status"]) for job in _jobs()])

    def refuse(*args, **kwargs):
        pytest.fail("a batch test started a real download")
    monkeypatch.setattr(JobManager, "_schedule_next", record)
    monkeypatch.setattr(job_manager_module, "download_album_job", refuse)
    monkeypatch.setattr(jm_service, "download_album_job", refuse)
    yield starts
    assert not any(t.name.startswith("dl-") and t.is_alive() for t in threading.enumerate())


@pytest.fixture
def make_local(downloads):
    """A finished download made directly on disk: marked chapter folders (or only a CBZ / ZIP of them) and a
    completed job. baseline=True records the download baseline (the full upstream list `chapters`).
    parent puts the folder under downloads/<parent>/ (organized by author)."""
    from core import database as db, update_store
    from core.packer import CbzPacker
    made = []

    def make(album_id="3001", chapters=("71", "72", "73"), kind="loose", selected=None, favourite=False,
             parent=None, baseline=True, title=None, added_at=None):
        title = title or f"Album{album_id}"
        base = downloads if parent is None else downloads / parent
        base.mkdir(parents=True, exist_ok=True)
        folder = base / f"{title}_{album_id}"
        folder.mkdir(exist_ok=True)
        selected = list(chapters if selected is None else selected)
        for n, photo_id in enumerate(chapters, 1):
            if photo_id not in selected:
                continue
            chapter = folder / f"第{n}话__{photo_id}"
            chapter.mkdir(exist_ok=True)
            (chapter / ".jm-chapter.json").write_text(json.dumps({"photo_id": photo_id, "format": 2}),
                                                     encoding="utf-8")
            for page in (1, 2):
                (chapter / f"{page:05d}.webp").write_bytes(f"{photo_id}-{page}".encode())
        if kind in ("cbz", "zip"):
            CbzPacker().pack(folder, folder / f"{folder.name}.{kind}")
            for page in folder.rglob("*.webp"):
                page.unlink()
        made.append(album_id)
        job_id = f"job_{album_id}_{len(made)}"
        db.insert_job(job_id, album_id, title, selected)
        db.update_job(job_id, status="completed", output_path=str(folder), completed_at=datetime.now().isoformat())
        if baseline:
            update_store.note_download_started(album_id, _episodes(chapters), had_local_content=False)
        if favourite:
            _fav(album_id, title, added_at)
        _forget_local()
        return folder
    return make


def _lists_seed(downloads):
    """tests/test_lists.py's favourites seed (one favourite per status group)."""
    from core import database as db
    ok = downloads / "ok"
    ok.mkdir()
    (ok / "001.webp").write_bytes(b"page")
    seed = [
        ("101", "Beta", "2026-01-01T00:00:01", None, [("j101", "completed", ok, None)]),
        ("102", "alpha", "2026-01-01T00:00:02", None, []),
        ("103", "", "2026-01-01T00:00:03", "", []),
        ("104", "Queued one", "2026-01-01T00:00:04", "queued", [("j104", "queued", None, None)]),
        ("105", "Running one", "2026-01-01T00:00:05", None, [("j105", "running", None, None)]),
        ("106", "Failed one", "2026-01-01T00:00:06", "failed", [("j106", "failed", None, None)]),
        ("107", "Gone", "2026-01-01T00:00:07", "completed", [("j107", "completed", downloads / "deleted", None)]),
        ("108", "Legacy dl", "2026-01-01T00:00:08", "downloading", []),
        ("109", "Legacy fail", "2026-01-01T00:00:09", "failed", []),
        ("110", "Legacy done", "2026-01-01T00:00:10", "completed", []),
        ("111", "Read+queued", "2026-01-01T00:00:11", "queued",
         [("j111a", "completed", ok, "2026-01-02T00:00:00"), ("j111b", "queued", None, "2026-01-03T00:00:00")]),
        ("112", "Read then failed", "2026-01-01T00:00:12", "failed",
         [("j112a", "completed", ok, "2026-01-02T00:00:00"), ("j112b", "failed", None, "2026-01-03T00:00:00")]),
        ("113", "Failed then canceled", "2026-01-01T00:00:13", "none",
         [("j113a", "failed", None, "2026-01-02T00:00:00"), ("j113b", "canceled", None, "2026-01-03T00:00:00")]),
        ("114", "Odd legacy", "2026-01-01T00:00:14", " QUEUED ", []),
        ("115", "Paused one", "2026-01-01T00:00:15", "downloading", [("j115", "paused", None, None)]),
    ]
    for album_id, title, added_at, legacy, jobs in seed:
        _fav(album_id, title, added_at, legacy)
        for job_id, status, output, created_at in jobs:
            _job(job_id, album_id, status, output, created_at=created_at)
    db.upsert_album_meta("102", author="Meta Author")


def _wishlist_none_ids():
    from core import database as db
    from core.local_availability import readable_among
    readable = readable_among(db.get_completed_album_ids(wishlist_only=True))
    data = db.get_all_wishlist(page=1, page_size=200, status="none", readable_ids=readable)
    return [item["album_id"] for item in data["items"]]


# ─── A. 下载新章节 targets ─────────────────────────────────────────────────────


def test_new_chapters_exact_confirmed_ids(client, make_local):
    from core import database as db
    make_local("3001")
    db.upsert_album_meta("3001", title="Meta 3001")
    confirm_new("3001", ["71", "72", "73", "74", "75"])
    data = preview(client, "new_chapters")
    assert _photos(data) == [("3001", ["74", "75"])]
    item = data["items"][0]
    assert set(item) == {"album_id", "title", "scope", "photo_ids", "chapters", "confirmed_at", "checked_at"}
    assert item["scope"] == "chapters" and item["title"] == "Meta 3001"
    assert [c["photo_id"] for c in item["chapters"]] == ["74", "75"]
    assert item["confirmed_at"] == T0.isoformat() and item["checked_at"] == T0.isoformat()
    assert data["counts"] == {"albums": 1, "chapters": 2}
    assert data["skipped"] == [] and data["review"] == [] and data["more"] == 0 and data["limit"] == 50
    assert data["out_of_scope"] == {"changed": 0}
    assert re.fullmatch(r"[0-9a-f]{32}", data["token"])
    pending = client.get("/api/updates/pending").get_json()["items"]
    assert [(p["album_id"], p["photo_ids"]) for p in pending] == _photos(data)   # the same ids PR-A confirmed


def test_partial_download_unselected_never_targeted(client, make_local):
    make_local("3001", selected=["71"])                  # only 71 of 71-73 was ever downloaded
    confirm_new("3001", ["71", "72", "73", "74"])
    assert _photos(preview(client, "new_chapters")) == [("3001", ["74"])]   # never 72 / 73


@pytest.mark.parametrize("kind", ["cbz", "zip"])
def test_cbz_and_zip_only_albums_targeted(client, make_local, kind):
    folder = make_local("3001", kind=kind)
    assert not list(folder.rglob("*.webp")) and (folder / f"{folder.name}.{kind}").exists()
    confirm_new("3001", ["71", "72", "73", "74"])
    data = preview(client, "new_chapters")
    assert _photos(data) == [("3001", ["74"])]
    assert confirm(client, "new_chapters", data["token"]).status_code == 201
    assert [json.loads(j["selected_photo_ids"]) for j in _jobs() if j["status"] == "queued"] == [["74"]]


def test_no_update_baseline_never_checked_not_targets(client, make_local):
    make_local("3001")
    confirm_new("3001", ["71", "72", "73"])               # no_update
    make_local("3002", baseline=False)
    confirm_new("3002", ["71", "72", "73", "74"])         # first check of a pre-PR-A album: a baseline, no claim
    make_local("3003")                                    # a download baseline, never checked
    make_local("3004", baseline=False)                    # no row at all
    data = preview(client, "new_chapters")
    assert data["items"] == [] and data["skipped"] == [] and data["counts"] == {"albums": 0, "chapters": 0}


def test_failed_checks_keep_confirmed_never_add(client, make_local):
    make_local("3001")
    confirm_new("3001", ["71", "72", "73", "74"])
    fail_check("3001", T0 + timedelta(days=1))
    make_local("3002")
    fail_check("3002")
    fail_check("3002", T0 + timedelta(days=1))
    data = preview(client, "new_chapters")
    assert _photos(data) == [("3001", ["74"])] and data["items"][0]["confirmed_at"] == T0.isoformat()


def test_changed_excluded_listed_in_review(client, make_local):
    folder = make_local("3001")
    confirm_new("3001", ["71", "73", "74"])               # 72 vanished while 74 appeared: 'changed'
    data = preview(client, "new_chapters")
    assert data["items"] == [] and data["skipped"] == []
    assert data["review"] == [{"album_id": "3001", "title": "Album3001", "new_count": 1, "removed_count": 1}]
    assert data["out_of_scope"] == {"changed": 1}
    shutil.rmtree(folder)
    _forget_local()
    data = preview(client, "new_chapters")
    assert data["review"] == [] and data["out_of_scope"] == {"changed": 0}


@pytest.mark.parametrize("case", ["deleted", "superseded", "online_only", "corrupt_cbz"])
def test_not_readable_dropped_silently(client, make_local, downloads, case):
    from core import database as db, update_store
    if case in ("deleted", "superseded"):
        folder = make_local("3001")
        if case == "deleted":
            shutil.rmtree(folder)
        else:
            _sql("UPDATE jobs SET superseded_at=?", (datetime.now().isoformat(),))
    else:
        if case == "online_only":
            _fav("3001", "Online")                        # a favourite with a stale row and no download
        else:
            folder = downloads / "Album3001_3001"
            folder.mkdir()
            (folder / "Album3001_3001.cbz").write_bytes(b"this is not a zip file")
            db.insert_job("job_x", "3001", "Album3001", [])
            db.update_job("job_x", status="completed", output_path=str(folder))
        update_store.note_download_started("3001", _episodes(["71", "72", "73"]), had_local_content=False)
    confirm_new("3001", ["71", "72", "73", "74"])
    _forget_local()
    assert [item["album_id"] for item in update_store.pending_new_chapters()] == ["3001"]   # the row is there
    data = preview(client, "new_chapters")
    assert data["items"] == [] and data["skipped"] == [] and data["review"] == []


@pytest.mark.parametrize("active", ["queued", "running", "paused"])
def test_active_album_skipped(client, make_local, active):
    folder = make_local("3001")
    confirm_new("3001", ["71", "72", "73", "74"])
    for n, final in enumerate(("failed", "canceled", "completed")):
        _job(f"jx{n}", "3001", active, photo_ids=["74"], title="Album3001")
        data = preview(client, "new_chapters")
        assert data["items"] == [] and data["skipped"] == [
            {"album_id": "3001", "title": "Album3001", "reason": "active"}]
        _sql("UPDATE jobs SET status=?, output_path=? WHERE job_id=?",
             (final, str(folder) if final == "completed" else None, f"jx{n}"))
        _forget_local()
        data = preview(client, "new_chapters")
        assert _photos(data) == [("3001", ["74"])] and data["skipped"] == [], final


def test_multiple_jobs_one_comic(client, make_local, downloads):
    make_local("3001", parent="Someone")                  # the older completed download, organized by author
    newer = downloads / "New_3001" / "第1话__71"
    newer.mkdir(parents=True)
    (newer / "00001.webp").write_bytes(b"page")
    _job("j_new", "3001", "completed", downloads / "New_3001")          # the newest completed: directly in downloads
    _job("j_failed", "3001", "failed")
    _job("j_canceled", "3001", "canceled")
    confirm_new("3001", ["71", "72", "73", "74"])
    _forget_local()
    data = preview(client, "new_chapters")
    assert _photos(data) == [("3001", ["74"])] and data["skipped"] == []   # decided by the newest completed job
    before = len(_jobs())
    assert confirm(client, "new_chapters", data["token"]).status_code == 201
    assert len(_jobs()) == before + 1


def test_organized_folder_skipped(client, make_local):
    make_local("3001", parent="author")
    confirm_new("3001", ["71", "72", "73", "74"])
    data = preview(client, "new_chapters")
    assert data["items"] == [] and _reasons(data) == [("3001", "organized")]


def test_baseline_ids_never_requested(client, make_local):
    make_local("3001")
    confirm_new("3001", ["71", "72", "73", "74", "75"])

    def pending(ids):
        chapters = [{"photo_id": p, "index": n, "title": f"第{n}话", "confirmed_at": T0.isoformat()}
                    for n, p in enumerate(ids, 1)]
        _sql("UPDATE album_update_checks SET new_chapters=?, new_count=? WHERE album_id='3001'",
             (json.dumps(chapters), len(chapters)))
    pending(["72", "74", "75"])                           # 72 is already in the baseline
    data = preview(client, "new_chapters")
    assert _photos(data) == [("3001", ["74", "75"])]
    assert [c["photo_id"] for c in data["items"][0]["chapters"]] == ["74", "75"]
    pending(["72", "73"])
    data = preview(client, "new_chapters")
    assert data["items"] == [] and data["skipped"] == []


def test_too_many_chapters_skipped(client, make_local):
    from core import update_store
    make_local("3001")
    confirm_new("3001", ["71", "72", "73"] + [str(10000 + n) for n in range(1001)])
    assert len(update_store.pending_new_chapters()[0]["photo_ids"]) == 1001
    data = preview(client, "new_chapters")
    assert data["items"] == [] and _reasons(data) == [("3001", "too_many")]


def test_non_favourite_local_comic_included(client, make_local):
    make_local("3001")
    confirm_new("3001", ["71", "72", "73", "74"])
    data = preview(client, "new_chapters")
    assert _photos(data) == [("3001", ["74"])]
    assert confirm(client, "new_chapters", data["token"]).status_code == 201
    assert _table("wishlist") == []                       # no favourite is created


def test_new_chapter_job_never_whole_album(client, make_local, monkeypatch):
    from core import batch_downloads
    for album_id in ("3001", "3002"):
        make_local(album_id)
        confirm_new(album_id, ["71", "72", "73", "74"])
    data = preview(client, "new_chapters")
    assert confirm(client, "new_chapters", data["token"]).status_code == 201
    assert all(json.loads(j["selected_photo_ids"]) == ["74"] for j in _jobs() if j["status"] == "queued")
    assert not [j for j in _jobs() if j["status"] == "queued" and j["selected_photo_ids"] == "[]"]

    make_local("3003")
    before = _table("jobs")
    broken = {"album_id": "3003", "title": "x", "scope": "chapters", "photo_ids": [], "chapters": [],
              "confirmed_at": None, "checked_at": None}
    monkeypatch.setattr(batch_downloads, "_plan_new_chapters", lambda conn, readable, review: ([broken], [], [], 0))
    token = preview(client, "new_chapters")["token"]
    with pytest.raises(ValueError):
        batch_downloads.confirm("new_chapters", token)
    response = confirm(client, "new_chapters", token)
    assert response.status_code == 500
    assert response.get_json()["message"] == "没能加入下载队列，没有创建任何任务，请重新打开清单"
    assert _table("jobs") == before                        # [] never became 'download everything'


# ─── B. 下载未下载的收藏 targets ────────────────────────────────────────────────


def test_undownloaded_equals_none_filter_group(client, make_local, downloads):
    from core import database as db
    _lists_seed(downloads)
    favourite("201", "Never", "2026-01-02T00:00:01")
    favourite("202", "Canceled", "2026-01-02T00:00:02", jobs=["canceled"])
    favourite("203", "Fail, cancel", "2026-01-02T00:00:03", jobs=["failed", "canceled"])
    favourite("204", "Canceled then failed", "2026-01-02T00:00:04", jobs=["canceled", "failed"])
    make_local("205", selected=["71"], favourite=True, added_at="2026-01-02T00:00:05")    # readable, partial
    make_local("206", kind="cbz", favourite=True, added_at="2026-01-02T00:00:06")         # CBZ only
    shutil.rmtree(make_local("207", favourite=True, added_at="2026-01-02T00:00:07"))     # deleted
    corrupt = downloads / "Bad_208"
    corrupt.mkdir()
    (corrupt / "Bad_208.cbz").write_bytes(b"not a zip")
    _job("j208", "208", "completed", corrupt)
    _fav("208", "Corrupt", "2026-01-02T00:00:08")
    make_local("209")                                                                     # not a favourite
    _job("j210", "210", "canceled")                                                       # not a favourite
    _forget_local()
    data = preview(client, "undownloaded_favourites")
    expected = {"102", "103", "113", "201", "202", "203"}
    assert set(_ids(data)) == set(_wishlist_none_ids()) == expected
    assert _ids(data) == _wishlist_none_ids()                    # the list default order: newest added first
    conn = db.get_db()
    try:
        assert {row["album_id"] for row in db.undownloaded_favourites(conn)} == expected
    finally:
        conn.close()
    states = {item["album_id"]: item["state"] for item in data["items"]}
    assert states == {"102": "never", "103": "never", "201": "never",
                      "113": "canceled", "202": "canceled", "203": "canceled"}
    assert all(item["scope"] == "all" and item["photo_ids"] == [] for item in data["items"])
    assert data["counts"] == {"albums": 6, "chapters": None} and data["skipped"] == []
    assert data["items"][0]["album_id"] == "203" and data["items"][0]["title"] == "Fail, cancel"
    assert next(i for i in data["items"] if i["album_id"] == "103")["title"] == "103"   # empty title → the id


def test_cancel_and_fail_orders(client, downloads):
    favourite("301", jobs=["canceled"])
    favourite("302", jobs=["failed", "canceled"])
    favourite("303", jobs=["canceled", "failed"])                  # stays in 失败 for its own retry
    _fav("304")
    _job("j304a", "304", "completed", downloads / "gone_304")      # downloaded, files deleted …
    _job("j304b", "304", "canceled")                               # … then a re-download was cancelled: missing
    data = preview(client, "undownloaded_favourites")
    assert set(_ids(data)) == {"301", "302"}


def test_legacy_status_rows(client):
    for n, legacy in enumerate(("", "none", "queued", "downloading", "failed", "completed", "downloaded")):
        favourite(f"70{n}", legacy=legacy)
    assert set(_ids(preview(client, "undownloaded_favourites"))) == {"700", "701"}


def test_records_cleared_skipped(client, downloads):
    from core import update_store
    from core.packer import CbzPacker
    favourite("401", added_at="2026-01-01T00:00:01")
    update_store.note_download_started("401", _episodes(["71"]), had_local_content=False)   # a check row, no jobs
    favourite("402", added_at="2026-01-01T00:00:02")
    (downloads / "X_402" / "第1话__71").mkdir(parents=True)
    (downloads / "X_402" / "第1话__71" / "00001.webp").write_bytes(b"page")
    favourite("403", added_at="2026-01-01T00:00:03")
    (downloads / "Y_403").mkdir()                                   # an empty folder is not local content
    favourite("404", added_at="2026-01-01T00:00:04", jobs=["canceled"])
    (downloads / "Z_404").mkdir()
    (downloads / "Z_404" / "00001.webp").write_bytes(b"page")        # a canceled job's leftovers: still in
    favourite("405", added_at="2026-01-01T00:00:05")
    packed = downloads / "W_405"
    (packed / "第1话__71").mkdir(parents=True)
    (packed / "第1话__71" / "00001.webp").write_bytes(b"page")
    CbzPacker().pack(packed, packed / "W_405.cbz")
    for page in packed.rglob("*.webp"):
        page.unlink()                                               # only a CBZ is left
    data = preview(client, "undownloaded_favourites")
    assert set(_ids(data)) == {"403", "404"}
    assert _reasons(data) == [("405", "records_cleared"), ("402", "records_cleared"), ("401", "records_cleared")]


def _pages_in(folder, cbz=False):
    """One loose page in a chapter folder of `folder`, or (cbz=True) only a CBZ of it."""
    from core.packer import CbzPacker
    (folder / "第1话__71").mkdir(parents=True)
    (folder / "第1话__71" / "00001.webp").write_bytes(b"page")
    if cbz:
        CbzPacker().pack(folder, folder / f"{folder.name}.cbz")
        for page in folder.rglob("*.webp"):
            page.unlink()


@pytest.mark.parametrize("kind", ["undownloaded_favourites", "selected_favourites"])
def test_records_cleared_folder_evidence(client, downloads, no_start, kind):
    """Favourites with no job rows and no check row: leftover files at the top level of downloads/ or one level down
    (organize_mode by_author: downloads/<author>/<name>_<id>) mean the records were cleared, so the comic is never
    queued whole again. Empty folders are not local content; nothing deeper and nothing inside a comic folder counts."""
    cases = {
        "402": (downloads / "X_402", "loose"),
        "405": (downloads / "W_405", "cbz"),
        "403": (downloads / "Y_403", "empty"),
        "406": (downloads / "Some author" / "Organized_406", "loose"),
        "407": (downloads / "Some author" / "Packed_407", "cbz"),
        "408": (downloads / "Some author" / "Empty_408", "empty"),
        "409": (downloads / "a" / "b" / "Deep_409", "loose"),             # two levels down: not looked at
        "410": (downloads / "Other_999" / "第1话__410", "chapter"),        # a chapter folder of another comic
    }
    for n, (album_id, (folder, content)) in enumerate(cases.items(), 1):
        favourite(album_id, added_at=f"2026-01-01T00:00:{n:02d}")
        if content in ("empty", "chapter"):
            folder.mkdir(parents=True)
            if content == "chapter":
                (folder / "00001.webp").write_bytes(b"page")
        else:
            _pages_in(folder, cbz=content == "cbz")
    body = {"album_ids": list(cases)} if kind == "selected_favourites" else {}
    data = preview(client, kind, **body)
    cleared = {"402", "405", "406", "407"}
    assert set(_ids(data)) == set(cases) - cleared
    assert sorted(_reasons(data)) == sorted((a, "records_cleared") for a in cleared)
    response = confirm(client, kind, data["token"], **body)
    assert response.status_code == 201
    queued = {(j["album_id"], j["selected_photo_ids"]) for j in _jobs() if j["status"] == "queued"}
    assert queued == {(a, "[]") for a in set(cases) - cleared}           # none for the comics already on disk


def test_non_favourites_never_action_2(client, make_local):
    from core import database as db
    _job("j801", "801", "canceled")                                 # not a favourite, never completed
    db.upsert_album_meta("802", title="Only meta")
    make_local("803")                                               # a local non-favourite
    favourite("804")
    data = preview(client, "undownloaded_favourites")
    assert _ids(data) == ["804"] and data["skipped"] == []
    assert confirm(client, "undownloaded_favourites", data["token"]).status_code == 201
    assert [j["album_id"] for j in _jobs() if j["status"] == "queued"] == ["804"]


def test_out_of_scope_counts(client, downloads):
    _lists_seed(downloads)
    counts = client.get("/api/wishlist").get_json()["group_counts"]
    data = preview(client, "undownloaded_favourites")
    assert data["out_of_scope"] == {k: counts[k] for k in ("readable", "active", "failed", "missing")}
    assert data["out_of_scope"] == {"readable": 3, "active": 5, "failed": 2, "missing": 2}
    assert len(data["items"]) == counts["none"]


def test_undownloaded_scope_whole_album(client):
    from core import database as db
    favourite("501", "Fav title")
    data = preview(client, "undownloaded_favourites")
    response = confirm(client, "undownloaded_favourites", data["token"])
    assert response.status_code == 201
    body = response.get_json()
    assert body["counts"] == {"albums": 1, "chapters": None}
    job = _jobs()[-1]
    assert (job["album_id"], job["status"], job["selected_photo_ids"], job["title"]) == ("501", "queued", "[]",
                                                                                        "Fav title")
    assert body["created"] == [{"album_id": "501", "job_id": job["job_id"], "title": "Fav title", "photo_ids": []}]
    assert db.get_wishlist("501")["download_status"] == "queued"
    item = client.get("/api/wishlist").get_json()["items"][0]
    assert item["status_group"] == "active" and item["activity"] == "queued"


def test_cap_50_newest_first_then_rest(client):
    for n in range(55):
        favourite(str(9000 + n), added_at=f"2026-01-01T00:{n // 60:02d}:{n % 60:02d}")
    data = preview(client, "undownloaded_favourites")
    assert len(data["items"]) == 50 and data["more"] == 5
    assert _ids(data) == [str(9000 + n) for n in range(54, 4, -1)]    # newest added first
    response = confirm(client, "undownloaded_favourites", data["token"])
    assert response.status_code == 201 and len(response.get_json()["created"]) == 50
    rest = preview(client, "undownloaded_favourites")
    assert _ids(rest) == ["9004", "9003", "9002", "9001", "9000"] and rest["more"] == 0


# ─── C. disjointness and dedupe ───────────────────────────────────────────────


def test_disjoint_by_construction(client, make_local, downloads):
    from core import database as db, update_store
    states = ("readable", "partial", "cbz", "deleted", "never", "canceled", "failed", "active")
    checks = ("pending", "changed", "none")
    album = {}
    n = 5000
    for fav in (True, False):
        for state in states:
            for check in checks:
                album_id = str(n)
                n += 1
                album[album_id] = (fav, state, check)
                local = state in ("readable", "partial", "cbz", "deleted")
                if local:
                    folder = make_local(album_id, kind="cbz" if state == "cbz" else "loose",
                                        selected=["71"] if state == "partial" else None, favourite=fav)
                    if state == "deleted":
                        shutil.rmtree(folder)
                else:
                    if fav:
                        _fav(album_id)
                    if state != "never":
                        _job(f"j{album_id}", album_id, {"active": "queued"}.get(state, state))
                if check != "none":
                    if not local:
                        update_store.note_download_started(album_id, _episodes(["71", "72", "73"]), False)
                    confirm_new(album_id, ["71", "72", "73", "74"] if check == "pending" else ["71", "73", "74"])
    _forget_local()
    a1 = set(_ids(preview(client, "new_chapters")))
    a2_data = preview(client, "undownloaded_favourites")
    a2 = set(_ids(a2_data))
    assert a1 and a2 and not a1 & a2
    assert a1 == {a for a, (fav, state, check) in album.items()
                  if state in ("readable", "partial", "cbz") and check == "pending"}
    assert a2 == {a for a, (fav, state, check) in album.items()
                  if fav and ((state == "never" and check == "none") or state == "canceled")}
    completed = {row["album_id"] for row in _rows("SELECT DISTINCT album_id FROM jobs WHERE status='completed'")}
    assert a1 <= completed and not a2 & completed
    db.clear_jobs_by_status("completed")
    _forget_local()
    assert preview(client, "new_chapters")["items"] == []
    after = preview(client, "undownloaded_favourites")
    cleared = {a for a, (fav, state, check) in album.items()
               if fav and state in ("readable", "partial", "cbz", "deleted")}
    assert cleared <= {a for a, reason in _reasons(after) if reason == "records_cleared"}
    assert not cleared & set(_ids(after))


@pytest.mark.parametrize("kind", ["new_chapters", "undownloaded_favourites"])
def test_confirm_twice_no_duplicate(client, make_local, kind):
    make_local("3001")
    confirm_new("3001", ["71", "72", "73", "74"])
    favourite("3100")
    album_id = "3001" if kind == "new_chapters" else "3100"
    data = preview(client, kind)
    assert _ids(data) == [album_id]
    assert confirm(client, kind, data["token"]).status_code == 201
    count = len(_jobs())
    again = confirm(client, kind, data["token"])
    assert again.status_code == 409
    body = again.get_json()
    assert body["reason"] == "stale" and body["preview"]["items"] == []
    if kind == "new_chapters":
        assert _reasons(body["preview"]) == [("3001", "active")]
    assert len(_jobs()) == count


def test_concurrent_confirms_one_wins(app, client, make_local):
    for album_id in ("3001", "3002", "3003"):
        make_local(album_id)
        confirm_new(album_id, ["71", "72", "73", "74"])
    token = preview(client, "new_chapters")["token"]
    barrier = threading.Barrier(2)
    statuses = []

    def run():
        other = app.test_client()
        barrier.wait(5)
        statuses.append(other.post(CONFIRM, json={"kind": "new_chapters", "token": token}).status_code)
    workers = [threading.Thread(target=run) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(60)
    assert sorted(statuses) == [201, 409]
    assert [j["album_id"] for j in _jobs() if j["status"] == "queued"] == ["3001", "3002", "3003"]


def test_manual_job_between_preview_and_confirm(client, make_local):
    make_local("3001")
    confirm_new("3001", ["71", "72", "73", "74"])
    data = preview(client, "new_chapters")
    assert client.post("/api/jobs", json={"album_id": "3001", "photo_ids": ["74"]}).status_code == 201
    response = confirm(client, "new_chapters", data["token"])
    assert response.status_code == 409
    assert _reasons(response.get_json()["preview"]) == [("3001", "active")]
    assert len([j for j in _jobs() if j["status"] == "queued"]) == 1


def test_photo_ids_deduped_upstream_order(client, make_local):
    make_local("3001")
    confirm_new("3001", ["71", "72", "73", ("76", 6, "番外"), ("74", 4, "第4话"), ("75", 5, "第5话")])
    chapters = json.loads(_rows("SELECT new_chapters FROM album_update_checks")[0]["new_chapters"])
    _sql("UPDATE album_update_checks SET new_chapters=?, new_count=?",
         (json.dumps(chapters[:2] + [chapters[1]] + chapters[2:]), 4))
    data = preview(client, "new_chapters")
    assert _photos(data) == [("3001", ["76", "74", "75"])]
    assert [c["photo_id"] for c in data["items"][0]["chapters"]] == ["76", "74", "75"]
    assert confirm(client, "new_chapters", data["token"]).status_code == 201
    assert json.loads(_jobs()[-1]["selected_photo_ids"]) == ["76", "74", "75"]


def test_write_time_guard_keeps_the_actions_disjoint(client, make_local, monkeypatch, no_start):
    """Even a wrong plan cannot cross the line: 下载新章节 only for albums with a completed download, the favourites
    actions only for albums without one. One bad item rolls back the whole confirm."""
    from core import batch_downloads
    make_local("3001", favourite=True)                          # completed download
    favourite("3100")                                           # never downloaded
    favourite("3101")
    before = _table("jobs")

    def whole(album_id):
        return {"album_id": album_id, "title": album_id, "scope": "all", "photo_ids": [], "state": "never"}
    monkeypatch.setattr(batch_downloads, "_plan_favourites", lambda conn, dirs: ([whole("3101"), whole("3001")], []))
    data = preview(client, "undownloaded_favourites")
    assert _ids(data) == ["3101", "3001"]
    response = confirm(client, "undownloaded_favourites", data["token"])
    assert response.status_code == 500 and _table("jobs") == before   # 3101's insert was rolled back too

    chapters = {"album_id": "3100", "title": "x", "scope": "chapters", "photo_ids": ["74"], "chapters": [],
                "confirmed_at": None, "checked_at": None}
    monkeypatch.setattr(batch_downloads, "_plan_new_chapters", lambda conn, readable, review: ([chapters], [], [], 0))
    data = preview(client, "new_chapters")
    assert confirm(client, "new_chapters", data["token"]).status_code == 500
    assert _table("jobs") == before and no_start.calls == []


def test_insert_job_guarded(client, make_local):
    from core import database as db
    make_local("3002")
    _job("j3003", "3003", "queued")
    make_local("3006")
    _job("j3006", "3006", "paused")
    before = _table("jobs")
    with db.transaction() as conn:
        assert db.insert_job_guarded(conn, "g1", "3001", "t", ["74"], completed="required") is False
        assert db.insert_job_guarded(conn, "g2", "3002", "t", [], completed="forbidden") is False
        for mode in ("any", "required", "forbidden"):
            assert db.insert_job_guarded(conn, f"g3{mode}", "3003", "t", [], completed=mode) is False
            assert db.insert_job_guarded(conn, f"g6{mode}", "3006", "t", ["74"], completed=mode) is False
        with pytest.raises(ValueError):
            db.insert_job_guarded(conn, "g4", "3004", "t", [], completed="sometimes")
    assert _table("jobs") == before
    with db.transaction() as conn:
        assert db.insert_job_guarded(conn, "g5", "3005", "t", [], completed="forbidden") is True
        assert db.insert_job_guarded(conn, "g7", "3002", "t", ["74"], completed="required") is True
        assert db.insert_job_guarded(conn, "g8", "3008", "t", [], completed="any") is True
        assert db.insert_job_guarded(conn, "g9", "3008", "t", [], completed="any") is False   # g8 is active now
    added = {j["job_id"]: j for j in _jobs()}
    assert {"g5", "g7", "g8"} <= set(added) and "g9" not in added
    assert added["g7"]["selected_photo_ids"] == json.dumps(["74"]) and added["g7"]["status"] == "queued"


# ─── D. protocol ──────────────────────────────────────────────────────────────


def _mixed_state(make_local):
    from core import database as db
    make_local("3001", favourite=True)
    confirm_new("3001", ["71", "72", "73", "74"])
    make_local("3002")
    confirm_new("3002", ["71", "73", "74"])                         # changed → review
    db.upsert_album_meta("3001", title="Meta 3001")
    db.set_cached_album_detail("3001", json.dumps({"album_id": "3001", "photos": []}))
    favourite("3100", jobs=["canceled"])
    favourite("3101")
    favourite("3102", jobs=["failed"])


@pytest.mark.parametrize("kind", ["new_chapters", "undownloaded_favourites", "selected_favourites"])
def test_preview_read_only(client, make_local, downloads, no_start, kind):
    _mixed_state(make_local)
    tables = ("jobs", "wishlist", "album_update_checks", "update_check_log", "album_meta", "album_detail_cache",
              "settings")
    before = {name: _table(name) for name in tables}
    tree = _tree(downloads)
    body = {"album_ids": ["3001", "3100", "3101", "3102", "3999"]} if kind == "selected_favourites" else {}
    first = preview(client, kind, **body)
    second = preview(client, kind, **body)
    assert first == second and first["items"]
    assert {name: _table(name) for name in tables} == before
    assert _tree(downloads) == tree
    assert no_start.calls == []


def test_confirm_requires_valid_token(client, make_local):
    make_local("3001")
    confirm_new("3001", ["71", "72", "73", "74"])
    token = preview(client, "new_chapters")["token"]
    for bad in (None, token[:31], token.upper() if token.upper() != token else "A" * 32, "g" * 32, 12345, ""):
        body = {"kind": "new_chapters"} if bad is None else {"kind": "new_chapters", "token": bad}
        response = client.post(CONFIRM, json=body)
        assert response.status_code == 400, bad
        assert response.get_json()["message"] == "缺少确认信息，请重新打开清单"
    wrong = confirm(client, "new_chapters", "0" * 32)
    assert wrong.status_code == 409 and wrong.get_json()["reason"] == "stale"
    assert set(wrong.get_json()["preview"]) == PREVIEW_KEYS
    assert [j["status"] for j in _jobs()] == ["completed"]           # nothing was queued


def test_stale_growth(client, make_local):
    make_local("3001")
    make_local("3002")
    confirm_new("3001", ["71", "72", "73", "74"])
    data = preview(client, "new_chapters")
    assert _ids(data) == ["3001"]
    confirm_new("3002", ["71", "72", "73", "74"], at=T0 + timedelta(hours=1))
    stale = confirm(client, "new_chapters", data["token"])
    assert stale.status_code == 409
    fresh = stale.get_json()["preview"]
    assert _ids(fresh) == ["3001", "3002"]
    assert stale.get_json()["message"] == "清单有变化，已按最新情况重新列出，请再看一下后确认"
    assert not [j for j in _jobs() if j["status"] == "queued"]
    assert confirm(client, "new_chapters", fresh["token"]).status_code == 201
    assert [j["album_id"] for j in _jobs() if j["status"] == "queued"] == ["3001", "3002"]


def test_stale_shrink(client, make_local):
    from core import update_store
    make_local("3001")
    confirm_new("3001", ["71", "72", "73", "74", "75"])
    data = preview(client, "new_chapters")
    update_store.absorb_downloaded("3001", ["74", "75"])            # downloaded in the meantime
    stale = confirm(client, "new_chapters", data["token"])
    assert stale.status_code == 409 and stale.get_json()["preview"]["items"] == []
    assert not [j for j in _jobs() if j["status"] == "queued"]


def test_stale_same_album_gains_a_chapter(client, make_local):
    """The token covers each comic's chapter ids: a chapter confirmed after the preview is never queued unseen."""
    make_local("3001")
    confirm_new("3001", ["71", "72", "73", "74", "75"])
    data = preview(client, "new_chapters")
    assert _photos(data) == [("3001", ["74", "75"])]
    confirm_new("3001", ["71", "72", "73", "74", "75", "76"], at=T0 + timedelta(hours=1))
    stale = confirm(client, "new_chapters", data["token"])
    assert stale.status_code == 409 and stale.get_json()["reason"] == "stale"
    assert _photos(stale.get_json()["preview"]) == [("3001", ["74", "75", "76"])]
    assert not [j for j in _jobs() if j["status"] == "queued"]


def test_busy_maps_to_409(client, make_local, no_start, monkeypatch):
    """A confirm still holding the lock: both endpoints answer 409 busy and create nothing."""
    from core import batch_downloads
    make_local("3001")
    confirm_new("3001", ["71", "72", "73", "74"])
    favourite("3100")
    token = preview(client, "new_chapters")["token"]
    rows = len(_jobs())
    monkeypatch.setattr(batch_downloads, "LOCK_WAIT", 0.01)
    assert batch_downloads._CONFIRM_LOCK.acquire(timeout=5)
    try:
        answers = [confirm(client, "new_chapters", token),
                   client.post("/api/wishlist/download", json={"ids": ["3100"]})]
    finally:
        batch_downloads._CONFIRM_LOCK.release()
    for response in answers:
        assert response.status_code == 409 and response.get_json()["reason"] == "busy"
        assert response.get_json()["message"] == "上一次确认还在处理，请稍后再试"
    assert len(_jobs()) == rows and no_start.calls == []


def test_exclude_only_shrinks(client, make_local):
    for album_id in ("3001", "3002", "3003"):
        make_local(album_id)
        confirm_new(album_id, ["71", "72", "73", "74"])
    data = preview(client, "new_chapters")
    assert _ids(data) == ["3001", "3002", "3003"]
    nothing = confirm(client, "new_chapters", data["token"], exclude=["3001", "3002", "3003"])
    assert nothing.status_code == 400
    assert nothing.get_json() == {"status": "error", "reason": "nothing_selected", "message": "没有勾选要下载的漫画"}
    for bad in (["abc"], [True], "3001", [1.5], [str(n) for n in range(51)]):
        response = confirm(client, "new_chapters", data["token"], exclude=bad)
        assert response.status_code == 400, bad
    assert not [j for j in _jobs() if j["status"] == "queued"]
    response = confirm(client, "new_chapters", data["token"], exclude=["3002", "9999"])
    assert response.status_code == 201
    assert [c["album_id"] for c in response.get_json()["created"]] == ["3001", "3003"]
    assert [j["album_id"] for j in _jobs() if j["status"] == "queued"] == ["3001", "3003"]
    assert _ids(preview(client, "new_chapters")) == ["3002"]        # unticked: offered again next time


def test_confirm_creates_expected_rows(client, make_local, no_start):
    from core import database as db
    make_local("3001", favourite=True, title="Local3001")
    db.upsert_album_meta("3001", title="Meta 3001")
    confirm_new("3001", ["71", "72", "73", "74", "75"])
    make_local("3002")                                              # not a favourite
    confirm_new("3002", ["71", "72", "73", "74"])
    favourite("3101", "Fav title")
    data = preview(client, "new_chapters")
    response = confirm(client, "new_chapters", data["token"])
    assert response.status_code == 201
    body = response.get_json()
    assert set(body) == {"status", "kind", "created", "counts", "window", "queue"}
    assert body["counts"] == {"albums": 2, "chapters": 3} and set(body["window"]) == WINDOW_KEYS
    assert body["queue"] == {"ahead": 2, "max_running": 1}
    queued = {j["album_id"]: j for j in _jobs() if j["status"] == "queued"}
    assert json.loads(queued["3001"]["selected_photo_ids"]) == ["74", "75"] and queued["3001"]["title"] == "Meta 3001"
    assert json.loads(queued["3002"]["selected_photo_ids"]) == ["74"] and queued["3002"]["title"] == "Album3002"
    assert [(c["album_id"], c["job_id"]) for c in body["created"]] == [("3001", queued["3001"]["job_id"]),
                                                                      ("3002", queued["3002"]["job_id"])]
    assert db.get_wishlist("3001")["download_status"] == "queued" and db.get_wishlist("3002") is None
    assert len(no_start.calls) == 1                                  # scheduled once, after the commit:
    assert {(queued[a]["job_id"], "queued") for a in queued} <= set(no_start.calls[0])
    data = preview(client, "undownloaded_favourites")
    assert confirm(client, "undownloaded_favourites", data["token"]).status_code == 201
    job = _jobs()[-1]
    assert (job["album_id"], job["selected_photo_ids"], job["title"]) == ("3101", "[]", "Fav title")
    assert db.get_wishlist("3101")["download_status"] == "queued" and len(no_start.calls) == 2


@pytest.mark.parametrize("path", [PREVIEW, CONFIRM])
def test_validation(client, path):
    token = {"token": "0" * 32} if path == CONFIRM else {}

    def post(body):
        response = client.post(path, json={**token, **body})
        return response.status_code, response.get_json()["message"]
    assert post({"kind": "bogus"}) == (400, "kind 不对")
    assert post({}) == (400, "kind 不对")
    assert post({"kind": "new_chapters", "album_ids": ["1"]}) == (400, "album_ids 只用于下载选中的收藏")
    assert post({"kind": "undownloaded_favourites", "album_ids": []}) == (400, "album_ids 只用于下载选中的收藏")
    for ids in (None, [], [str(n) for n in range(51)], [True], [1.5], ["12a"], "3001", [-1], [""]):
        body = {"kind": "selected_favourites"} if ids is None else {"kind": "selected_favourites", "album_ids": ids}
        assert post(body) == (400, "请选择 1-50 个收藏"), ids
    for payload in ("[]", "7", '"text"', "null"):
        response = client.post(path, data=payload, content_type="application/json")
        assert response.status_code == 400 and response.is_json
    foreign = client.post(path, json={"kind": "new_chapters", **token}, headers={"Origin": "https://foreign.example"})
    assert foreign.status_code == 403
    if path == PREVIEW:
        assert client.post(path, json={"kind": "selected_favourites", "album_ids": [3001, "3001"]}).status_code == 200
    assert _jobs() == []


def test_nothing_downloads_automatically(client, make_local, no_start):
    make_local("3001", favourite=True)
    confirm_new("3001", ["71", "72", "73", "74"])                   # a check discovered a new chapter
    favourite("3100")
    for path in ("/api/updates/3001", "/api/updates/summary", "/api/updates/pending", "/api/library",
                 "/api/wishlist"):
        assert client.get(path).status_code == 200, path
    assert client.post("/api/updates/states", json={"album_ids": ["3001"]}).status_code == 200
    preview(client, "new_chapters")
    preview(client, "undownloaded_favourites")
    preview(client, "selected_favourites", album_ids=["3001", "3100"])
    assert [j["status"] for j in _jobs()] == ["completed"] and no_start.calls == []

    forbidden = {"schedule_next", "_schedule_next", "Thread", "download_album_job", "create_job"}
    banned_modules = ("job_manager", "jm_service", "jmcomic", "scheduler")
    source = ast.parse((ROOT / "core" / "batch_downloads.py").read_text(encoding="utf-8"))
    for node in ast.walk(source):
        if isinstance(node, ast.Import):
            assert not any(bad in alias.name for alias in node.names for bad in banned_modules)
        elif isinstance(node, ast.ImportFrom):
            names = {alias.name for alias in node.names}
            assert not any(bad in (node.module or "") for bad in banned_modules), node.module
            assert not names & set(banned_modules) and not names & forbidden, names
        elif isinstance(node, ast.Name):
            assert node.id not in forbidden, node.id
        elif isinstance(node, ast.Attribute):
            assert node.attr not in forbidden, node.attr

    def core_imports(module):
        """(core modules, other top-level packages) a core module imports, anywhere in the file"""
        found, external = set(), set()
        for node in ast.walk(ast.parse((ROOT / "core" / f"{module}.py").read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                name = node.module or ""
                if node.level == 1:
                    found |= {name.split(".")[0]} if name else {alias.name for alias in node.names}
                elif name.startswith("core."):
                    found.add(name.split(".")[1])
                else:
                    external.add(name.split(".")[0])
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("core."):
                        found.add(alias.name.split(".")[1])
                    else:
                        external.add(alias.name.split(".")[0])
        return found, external
    seen, todo, externals = set(), ["batch_downloads"], set()
    while todo:
        module = todo.pop()
        if module in seen:
            continue
        seen.add(module)
        found, external = core_imports(module)
        externals |= external
        todo += [m for m in found if (ROOT / "core" / f"{m}.py").exists()]
    assert not seen & {"job_manager", "jm_service", "scheduler", "update_checker", "jm_plugin"}, seen
    assert "jmcomic" not in externals and "curl_cffi" not in externals

    for rel in ("core/update_store.py", "core/update_checker.py", "core/scheduler.py", "routes/api_updates.py"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [alias.name for alias in node.names] + [getattr(node, "module", None) or ""]
                assert not any("batch_downloads" in name for name in names), rel


def test_window_state(client, make_local, monkeypatch):
    from core import scheduler
    from core import database as db
    from core.settings import update_settings

    def at(hour):
        return datetime(2026, 9, 28, hour, 30)
    off = scheduler.window_state(at(1))
    assert off == {"enabled": False, "open": True, "start": 23, "end": 7, "never": False, "invalid": False,
                   "opens_tomorrow": False}
    update_settings({"schedule_enabled": "true", "schedule_start": "23", "schedule_end": "7"})
    assert scheduler.window_state(at(1))["open"] is True and scheduler.is_schedule_time(at(1)) is True
    noon = scheduler.window_state(at(12))
    assert noon["open"] is False and noon["opens_tomorrow"] is False and noon["never"] is False
    update_settings({"schedule_start": "8", "schedule_end": "22"})
    assert scheduler.window_state(at(23)) == {"enabled": True, "open": False, "start": 8, "end": 22,
                                              "never": False, "invalid": False, "opens_tomorrow": True}
    assert scheduler.window_state(at(5))["opens_tomorrow"] is False
    assert scheduler.window_state(at(10))["open"] is True
    update_settings({"schedule_start": "5", "schedule_end": "5"})
    five = scheduler.window_state(at(5))
    assert five["never"] is True and five["open"] is False and five["opens_tomorrow"] is False
    for bad in ("x", "25", "-1"):
        db.set_setting("schedule_start", bad)
        state = scheduler.window_state(at(23))
        assert state["invalid"] is True and state["never"] is True and state["open"] is False
        assert state["start"] is None and state["opens_tomorrow"] is False
        assert not any(scheduler.is_schedule_time(at(h)) for h in range(24))   # the gate never opens either

    update_settings({"schedule_start": "23", "schedule_end": "7"})
    _fake_now(monkeypatch, at(12))
    expected = scheduler.window_state(at(12))
    make_local("3001")
    confirm_new("3001", ["71", "72", "73", "74"])
    favourite("3100")
    data = preview(client, "new_chapters")
    assert data["window"] == expected and data["queue"] == {"ahead": 0, "max_running": 1}
    assert data["settings"] == {"skip_existing": True}
    response = confirm(client, "new_chapters", data["token"])
    assert response.status_code == 201 and response.get_json()["window"] == expected
    single = client.post("/api/wishlist/download", json={"ids": ["3100"]})
    assert single.status_code == 201 and single.get_json()["window"] == expected


def test_window_closed_jobs_stay_queued(client, make_local, no_start, monkeypatch):
    from core import database as db, scheduler
    from core.job_manager import JobManager
    from core.settings import update_settings
    update_settings({"schedule_enabled": "true", "schedule_start": "23", "schedule_end": "7"})
    monkeypatch.setattr(scheduler, "is_schedule_time", lambda now=None: False)
    claims = []
    monkeypatch.setattr(db, "claim_next_queued_job", lambda *a, **k: claims.append(a))
    make_local("3001")
    confirm_new("3001", ["71", "72", "73", "74"])
    favourite("3100")
    for kind in ("new_chapters", "undownloaded_favourites"):
        data = preview(client, kind)
        assert data["window"]["open"] is False
        assert confirm(client, kind, data["token"]).status_code == 201
    manager = JobManager()
    for _ in range(3):
        no_start.real(manager)                                       # the real scheduling step, window closed
    assert claims == [] and manager.get_running_count() == 0
    assert sorted((j["album_id"], j["status"]) for j in _jobs() if j["status"] != "completed") == [
        ("3001", "queued"), ("3100", "queued")]


def test_batch_routes_never_fetch(client, make_local, monkeypatch):
    from core import jm_service, update_checker

    def forbidden(*args, **kwargs):
        pytest.fail("a batch download endpoint went online")
    for name in ("new_check_client", "get_client", "get_album_detail", "get_album_detail_cached"):
        monkeypatch.setattr(jm_service, name, forbidden)
    monkeypatch.setattr(update_checker, "fetch_upstream_episodes", forbidden)
    make_local("3001")
    confirm_new("3001", ["71", "72", "73", "74"])
    favourite("3100")
    favourite("3101")
    favourite("3102")
    for kind, body in (("selected_favourites", {"album_ids": ["3102"]}), ("new_chapters", {}),
                       ("undownloaded_favourites", {})):
        data = preview(client, kind, **body)
        assert data["items"], kind
        assert confirm(client, kind, data["token"], **body).status_code == 201, kind
    assert client.post("/api/wishlist/download", json={"ids": ["3200"]}).status_code == 201


def test_pending_new_chapters_conn_param(client, make_local):
    from core import database as db, update_store
    make_local("3001")
    make_local("3002")
    confirm_new("3001", ["71", "72", "73", "74"])
    confirm_new("3002", ["71", "73", "74"])
    conn = db.get_db()
    try:
        assert update_store.pending_new_chapters(conn=conn) == update_store.pending_new_chapters()
        assert update_store.pending_new_chapters(["3001"], conn=conn) == update_store.pending_new_chapters(["3001"])
        assert update_store.changed_albums(conn) == update_store.changed_albums() == [
            {"album_id": "3002", "title": "Album3002", "new_count": 1, "removed_count": 1}]
        assert conn.execute("SELECT 1").fetchone()[0] == 1           # the caller's connection is left open
    finally:
        conn.close()
    store = ast.parse((ROOT / "core" / "update_store.py").read_text(encoding="utf-8"))
    imported = {alias.name for node in ast.walk(store) if isinstance(node, ast.Import) for alias in node.names}
    imported |= {f"{node.module or ''}:{alias.name}" for node in ast.walk(store)
                 if isinstance(node, ast.ImportFrom) for alias in node.names}
    assert imported == {"json", "random", "contextlib:contextmanager", "datetime:datetime", "datetime:timedelta",
                        ":database", "validation:validate_numeric"}          # still database only


def test_readable_among_moved_and_reexported(client, make_local):
    from core import local_availability
    from routes import api_library
    assert api_library.readable_among is local_availability.readable_among
    make_local("3001")
    ids = [str(n) for n in range(4000, 4450)] + ["3001"]           # more than one MAX_IDS chunk
    assert local_availability.readable_among(ids) == {"3001"}


def test_job_ids_share_one_generator(client, no_start):
    from core import database as db
    from core.job_manager import JobManager
    ids = {db.new_job_id() for _ in range(50)}
    assert len(ids) == 50 and all(re.fullmatch(r"job_[0-9a-f]{12}", i) for i in ids)
    job_id = JobManager().create_job("3001", "t", ["71"])
    assert re.fullmatch(r"job_[0-9a-f]{12}", job_id) and db.get_job(job_id)["status"] == "queued"


# ─── F. existing endpoints ────────────────────────────────────────────────────


def test_selected_favourites_rule(client, make_local, downloads):
    from core import update_store
    favourite("6001", "Never")
    favourite("6002", "Canceled", jobs=["canceled"])
    favourite("6003", "Failed", jobs=["failed"])
    _fav("6004", "Missing")
    _job("j6004", "6004", "completed", downloads / "gone_6004")
    make_local("6005", favourite=True, title="Readable")
    make_local("6006", selected=["71"], favourite=True, title="Partial")
    make_local("6007", kind="cbz", favourite=True, title="Cbz")
    favourite("6008", "Active", jobs=["queued"])
    favourite("6009", "Cleared")
    update_store.note_download_started("6009", _episodes(["71"]), had_local_content=False)
    selection = ["6001", "6002", "6003", "6004", "6005", "6006", "6007", "6008", "6009", "6010", "6001"]
    data = preview(client, "selected_favourites", album_ids=selection)
    assert _ids(data) == ["6001", "6002"]
    assert [item["state"] for item in data["items"]] == ["never", "canceled"]
    assert _reasons(data) == [("6003", "failed"), ("6004", "missing"), ("6005", "readable"), ("6006", "readable"),
                              ("6007", "readable"), ("6008", "active"), ("6009", "records_cleared"),
                              ("6010", "not_favourite")]
    assert data["skipped"][0]["title"] == "Failed" and data["skipped"][-1]["title"] == "6010"
    assert data["out_of_scope"] == {} and data["review"] == []
    other = confirm(client, "selected_favourites", data["token"], album_ids=["6001"])
    assert other.status_code == 409                                  # the token covers the selection too
    response = confirm(client, "selected_favourites", data["token"], album_ids=selection)
    assert response.status_code == 201
    assert [(c["album_id"], c["photo_ids"]) for c in response.get_json()["created"]] == [("6001", []), ("6002", [])]
    queued = [(j["album_id"], j["selected_photo_ids"]) for j in _jobs() if j["status"] == "queued"]
    assert queued == [("6008", "[]"), ("6001", "[]"), ("6002", "[]")]


def test_wishlist_download_single_path(client, make_local, no_start):
    from core import database as db
    favourite("7001", "Busy one", jobs=["queued"])
    rows = len(_jobs())
    response = client.post("/api/wishlist/download", json={"ids": ["7001"]})
    assert response.status_code == 200
    body = response.get_json()
    assert body["job_ids"] == [] and body["skipped"] == [{"album_id": "7001", "reason": "active"}]
    assert set(body["window"]) == WINDOW_KEYS and len(_jobs()) == rows and no_start.calls == []

    favourite("7002", "Clean one")
    response = client.post("/api/wishlist/download", json={"ids": ["7002"]})
    assert response.status_code == 201 and response.get_json()["skipped"] == []
    job = _jobs()[-1]
    assert response.get_json()["job_ids"] == [{"album_id": "7002", "job_id": job["job_id"]}]
    assert (job["album_id"], job["title"], job["selected_photo_ids"], job["status"]) == ("7002", "Clean one", "[]",
                                                                                        "queued")
    assert db.get_wishlist("7002")["download_status"] == "queued" and len(no_start.calls) == 1

    favourite("7003", "")
    db.upsert_album_meta("7003", title="Meta 7003")
    response = client.post("/api/wishlist/download", json={"ids": ["7003", "7003", 7001]})
    assert response.status_code == 201
    assert [j["album_id"] for j in response.get_json()["job_ids"]] == ["7003"]
    assert response.get_json()["skipped"] == [{"album_id": "7001", "reason": "active"}]
    assert [j["title"] for j in _jobs() if j["album_id"] == "7003"] == ["Meta 7003"]   # one job, not two

    make_local("7004", favourite=True)                               # readable: the page asks first, the server allows
    response = client.post("/api/wishlist/download", json={"ids": ["7004"]})
    assert response.status_code == 201 and len(no_start.calls) == 3
    for bad in ({"ids": []}, {"ids": ["x"]}, {"ids": [str(n) for n in range(51)]}, {"ids": "7002"}):
        assert client.post("/api/wishlist/download", json=bad).status_code == 400
