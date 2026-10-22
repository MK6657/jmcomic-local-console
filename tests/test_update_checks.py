"""检查新章节（PR-A）：基线、检查结论、资格、节奏、接口，以及“检查从不下载”的证明。

Everything runs in pytest's tmp folders with the network blocked (conftest): the real downloads/ and runtime/ are
never touched, the upstream is always a fake (a stubbed fetch, a strict fake client, or the real client whose
libcurl call is blocked), and the background thread is only started once, with a stubbed fetch and a one-hour
startup delay. Downloads use the REAL download_album_job with a fake client (as in test_offline_labels.py)."""
import ast
import json
import random
import shutil
import threading
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
UPDATE_MODULES = ("core/update_store.py", "core/update_checker.py", "routes/api_updates.py")

# the album the fake download client serves: chapter 71 has 3 pages, 72 has 2, 73 has 4
CHAPTERS = [("71", "第1话", 3), ("72", "第2话", 2), ("73", "第3话", 4)]
CH74 = ("74", "第4话", 5)

UPDATE_KEYS = {"state", "last_result", "baseline_count", "baseline_source", "baseline_at", "upstream_count",
               "removed_count", "new_count", "new_chapters", "confirmed_at", "last_success_at", "last_attempt_at",
               "last_trigger", "error", "next_check_at", "paused_until"}


# ─── fakes ────────────────────────────────────────────────────────────────────


class _Photo(list):
    def __init__(self, photo_id, name, pages):
        super().__init__(range(pages))
        self.photo_id = photo_id
        self.name = name
        self.page_arr = [f"{n:05d}.webp" for n in range(1, pages + 1)]


class _Album(list):
    name = "Name"
    author = "someone"
    tags = []

    def __init__(self, photos, album_id="3001", episode_list=None):
        super().__init__(photos)
        self.album_id = album_id
        if episode_list is not None:
            self.episode_list = episode_list


class _Client:
    """The download's client: serves the album and 'checks' photos (no network)."""

    def __init__(self, album):
        self.album = album

    def get_album_detail(self, album_id):
        return self.album

    def check_photo(self, photo):
        pass


class _UpstreamAlbum:
    """What client.get_album_detail returns to a check: an album id and jmcomic's episode_list."""

    def __init__(self, album_id, episodes):
        self.album_id = album_id
        self.episode_list = list(episodes)

    def __iter__(self):
        return iter(())


class _MirrorClient:
    """A strict check client: answers per mirror; anything but the five allowed members fails the test."""

    def __init__(self, answers):
        self.answers = dict(answers)
        self.calls = []
        self.current = None
        self.retry_times = 0

    def get_domain_list(self):
        return list(self.answers)

    def set_domain_list(self, domains):
        self.calls.append(("set_domain_list", list(domains)))
        self.current = domains[0]

    def set_cache_dict(self, value):
        self.calls.append(("set_cache_dict", value))

    def get_album_detail(self, album_id):
        self.calls.append(("get_album_detail", album_id))
        answer = self.answers[self.current]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def __getattr__(self, name):
        pytest.fail(f"a check used client.{name}")


def _episodes(chapters):
    """['71', ...] or [(pid, index, title), ...] → [(pid, index, title)] in upstream order."""
    out = []
    for n, entry in enumerate(chapters, 1):
        out.append(tuple(entry) if isinstance(entry, (tuple, list)) else (str(entry), n, f"第{n}话"))
    return out


class FakeUpstream:
    """Replaces update_checker.fetch_upstream_episodes: album_id → chapter list, a CheckFailed, or a callable.
    Records every call and the maximum number of calls in flight; can block on `gate`."""

    def __init__(self):
        self.albums = {}
        self.calls = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.gate = None
        self.entered = threading.Event()
        self._lock = threading.Lock()

    def set(self, album_id, value):
        self.albums[str(album_id)] = value

    def __call__(self, album_id):
        from core.update_checker import CheckFailed
        with self._lock:
            self.calls.append(album_id)
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            self.entered.set()
            if self.gate is not None:
                assert self.gate.wait(10)
            value = self.albums.get(album_id)
            if value is None:
                raise CheckFailed("network", "FixtureOffline", 1)
            if callable(value):
                value = value()
            if isinstance(value, BaseException):
                raise value
            return _episodes(value), 1
        finally:
            with self._lock:
                self.in_flight -= 1


class Clock:
    """Pinned wall clock and monotonic clock for update_checker (store writes use the times it is given)."""

    def __init__(self):
        self.wall = datetime.now().replace(microsecond=0)
        self.mono = 10_000.0

    def now(self):
        return self.wall

    def monotonic(self):
        return self.mono

    def advance(self, seconds):
        seconds = seconds.total_seconds() if isinstance(seconds, timedelta) else seconds
        self.wall += timedelta(seconds=seconds)
        self.mono += seconds
        return self.wall


# ─── fixtures ─────────────────────────────────────────────────────────────────


def _forget_local():
    from core import local_availability
    with local_availability._cache_lock:
        local_availability._cache.clear()


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
    """G9: every test here runs on a tmp database and a tmp downloads folder."""
    from core import database as db, path_guard
    assert Path(db.DB_PATH).resolve().is_relative_to(tmp_path.resolve())
    assert Path(path_guard.DOWNLOAD_ROOT).resolve().is_relative_to(tmp_path.resolve())
    yield


@pytest.fixture
def download(client, downloads, monkeypatch):
    """The real download_album_job; only the client and fetching one chapter are replaced.
    download.serve(chapters) changes what upstream lists for the download's own fetch; download.rename(name)
    changes the album's upstream title."""
    from core import database as db, jm_service
    from core.progress import progress_manager
    monkeypatch.setattr(jm_service, "close_client", lambda _client: None)
    served = {"chapters": list(CHAPTERS), "name": _Album.name}

    def get_client(shared=True):
        album = _Album([_Photo(*c) for c in served["chapters"]],
                       episode_list=[(pid, str(n), name) for n, (pid, name, _) in enumerate(served["chapters"], 1)])
        album.name = served["name"]
        return _Client(album), None
    monkeypatch.setattr(jm_service, "get_client", get_client)

    def run(job_id, photo_ids, outcome="completed", written=None, pack=False, pack_format="cbz", insert=True,
            organize="none"):
        """insert=False runs a job that already exists (e.g. one a batch download queued)."""
        from core.settings import update_settings
        update_settings({"auto_pack": "true" if pack else "false", "delete_originals": "true" if pack else "false",
                         "pack_format": pack_format, "organize_mode": organize})

        def chapter(job_id, album_id, album, album_dir, photo, total_pages, done_pages, pending_images,
                    failed_pages, *rest):
            folder = jm_service._chapter_output_dir(album_dir, photo)
            for n in range(1, (written or len(photo)) + 1):
                if organize == "flat":      # flattening moves only valid images: write real ones
                    from PIL import Image
                    Image.new("RGB", (8, 8), (int(photo.photo_id) % 256, n * 20 % 256, 90)).save(
                        folder / f"{n:05d}.webp", "WEBP")
                else:
                    (folder / f"{n:05d}.webp").write_bytes(f"{photo.photo_id}-{n}".encode())
                done_pages[0] += 1
            if outcome == "failed":
                failed_pages.append("下载失败")
            elif outcome == "canceled":
                db.update_job(job_id, status="canceled")
        monkeypatch.setattr(jm_service, "_download_chapter", chapter)
        if insert:
            db.insert_job(job_id, "3001", "Name", photo_ids)
        else:
            assert db.get_job(job_id)["status"] == "queued"
        db.update_job(job_id, status="running")
        progress_manager.create_tracker(job_id)
        try:
            jm_service.download_album_job(job_id, "3001", photo_ids)
        finally:
            progress_manager.remove_tracker(job_id)
        _forget_local()
        return db.get_job(job_id)["status"]

    run.serve = lambda chapters: served.__setitem__("chapters", list(chapters))
    run.rename = lambda name: served.__setitem__("name", name)
    return run


@pytest.fixture
def local_album(downloads):
    """A finished download made directly on disk: marked chapter folders (or only a CBZ / ZIP of them) and a
    completed job. baseline=True also records the download baseline (the full upstream list `chapters`)."""
    from core import database as db, update_store
    from core.packer import CbzPacker
    made = []

    def make(album_id="3001", chapters=("71", "72", "73"), kind="loose", selected=None, baseline=True, title=None):
        title = title or f"Album{album_id}"
        folder = downloads / f"{title}_{album_id}"
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
        _forget_local()
        return folder
    return make


@pytest.fixture
def checker(client, monkeypatch):
    """update_checker with clean runtime state and pinned clocks; _uniform returns the lower bound."""
    from core import update_checker
    update_checker._reset_for_tests()
    clock = Clock()
    monkeypatch.setattr(update_checker, "_now", clock.now)
    monkeypatch.setattr(update_checker, "_mono", clock.monotonic)
    monkeypatch.setattr(update_checker, "_uniform", lambda low, high: low)
    yield clock
    update_checker._reset_for_tests()
    assert not any(t.name == "update-checker" and t.is_alive() for t in threading.enumerate())


@pytest.fixture
def upstream(checker, monkeypatch):
    from core import update_checker
    fake = FakeUpstream()
    monkeypatch.setattr(update_checker, "fetch_upstream_episodes", fake)
    return fake


def _check(album_id="3001", trigger="auto"):
    from core import update_checker
    return update_checker.check_album(album_id, trigger, lock_wait=0)["outcome"]


def _row(album_id="3001"):
    from core import update_store
    return update_store.get(album_id)


def _state(client, album_id="3001"):
    data = client.get(f"/api/updates/{album_id}").get_json()
    assert data["status"] == "ok"
    return data


def _new_ids(album_id="3001"):
    return [c["photo_id"] for c in _row(album_id)["new_chapters"]]


def _due(*album_ids, clock):
    from core import update_store
    update_store.mark_due(album_ids, clock.now())


def _log_rows():
    from core import database as db
    conn = db.get_db()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM update_check_log ORDER BY id")]
    finally:
        conn.close()


def _library_item(client, album_id, path="/api/library"):
    items = client.get(path + "?page_size=200").get_json()["items"]
    return next(item for item in items if item["album_id"] == album_id)


# ─── A. schema and settings ───────────────────────────────────────────────────


def test_old_database_gets_update_tables_idempotently(client, download):
    from core import database as db
    assert download("ja", ["71"]) == "completed"
    conn = db.get_db()
    try:
        conn.executescript("DROP TABLE album_update_checks; DROP TABLE update_check_log;")  # a pre-PR-A database
        before = [tuple(r) for r in conn.execute("SELECT * FROM jobs ORDER BY id")]
    finally:
        conn.close()
    db.init_db()
    db.init_db()
    conn = db.get_db()
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'index')")}
        after = [tuple(r) for r in conn.execute("SELECT * FROM jobs ORDER BY id")]
        rows = conn.execute("SELECT COUNT(*) FROM album_update_checks").fetchone()[0]
        logs = conn.execute("SELECT COUNT(*) FROM update_check_log").fetchone()[0]
    finally:
        conn.close()
    assert {"album_update_checks", "update_check_log", "idx_update_checks_next", "idx_update_checks_new",
            "idx_update_check_log_started"} <= names
    assert after == before and rows == 0 and logs == 0     # the migration inserts nothing and never touches jobs


@pytest.mark.parametrize("value", ["yes", "1", 1, None, ""])
def test_auto_update_check_default_on_and_validated(client, value):
    assert client.get("/api/settings").get_json()["settings"]["auto_update_check"] == "true"   # on for everyone
    response = client.post("/api/settings", json={"auto_update_check": value})
    assert response.status_code == 400
    assert response.get_json()["message"] == "auto_update_check 必须为 true 或 false"
    assert client.get("/api/settings").get_json()["settings"]["auto_update_check"] == "true"
    for accepted, stored in ((False, "false"), ("TRUE", "true"), ("false", "false")):
        saved = client.post("/api/settings", json={"auto_update_check": accepted})
        assert saved.status_code == 200 and saved.get_json()["settings"]["auto_update_check"] == stored


def test_auto_update_check_survives_export_import(client):
    import io
    client.post("/api/settings", json={"auto_update_check": "false"})
    exported = client.get("/api/settings/export")
    assert json.loads(exported.get_data(as_text=True))["auto_update_check"] == "false"
    client.post("/api/settings", json={"auto_update_check": "true"})
    upload = {"file": (io.BytesIO(exported.get_data()), "settings.json")}
    assert client.post("/api/settings/import", data=upload, content_type="multipart/form-data").status_code == 200
    assert client.get("/api/settings").get_json()["settings"]["auto_update_check"] == "false"


# ─── B. baseline from downloads ───────────────────────────────────────────────


def test_first_download_records_full_upstream_baseline(client, download):
    before = datetime.now().replace(microsecond=0)
    assert download("ja", ["71"]) == "completed"
    after = datetime.now()
    row = _row()
    assert row["baseline_ids"] == ["71", "72", "73"]              # the whole upstream list, not only chapter 71
    assert row["baseline_source"] == "download" and row["result"] is None and row["new_count"] == 0
    due = datetime.fromisoformat(row["next_check_at"])
    assert before + timedelta(hours=23) <= due <= after + timedelta(hours=25)
    data = _state(client)
    assert data["eligible"] is True and data["update"]["state"] == "never"
    assert data["update"]["baseline_count"] == 3 and data["update"]["baseline_source"] == "download"
    assert _log_rows() == []                                       # recording a baseline is not a check


def test_partial_download_unselected_chapters_are_never_new(client, download, upstream):
    assert download("ja", ["71"]) == "completed"
    upstream.set("3001", ["71", "72", "73"])
    assert _check() == "no_update"
    assert _row()["new_count"] == 0 and _state(client)["update"]["state"] == "no_update"


def test_complete_download_then_new_chapter_confirmed(client, download, upstream):
    assert download("ja", []) == "completed"
    upstream.set("3001", ["71", "72", "73", "74"])
    assert _check() == "new"
    assert _new_ids() == ["74"]
    update = _state(client)["update"]
    assert update["state"] == "new" and update["new_count"] == 1 and update["new_chapters"][0]["photo_id"] == "74"


def test_cbz_only_download_is_target_and_badged(client, download, downloads, upstream):
    assert download("ja", ["71"], pack=True) == "completed"
    folder = downloads / "Name_3001"
    assert not list(folder.rglob("*.webp")) and (folder / "Name_3001.cbz").exists()   # archive-only
    upstream.set("3001", ["71", "72", "73", "74"])
    assert _check() == "new"
    assert _state(client)["eligible"] is True
    item = _library_item(client, "3001")
    assert item["readable"] is True and item["archive"] == "cbz"
    assert item["update"]["state"] == "new" and item["update"]["new_count"] == 1


def _pending_74(download, upstream, checker):
    """Chapter 71 downloaded, then a check confirms chapter 74 as new."""
    assert download("ja", ["71"]) == "completed"
    upstream.set("3001", ["71", "72", "73", "74"])
    assert _check() == "new"
    confirmed = _row()["new_chapters"][0]["confirmed_at"]
    download.serve(CHAPTERS + [CH74])
    checker.advance(3600)
    return confirmed


def test_completed_job_absorbs_downloaded_new_chapters(client, download, upstream, checker):
    _pending_74(download, upstream, checker)
    assert download("jb", ["74"]) == "completed"
    row = _row()
    assert row["new_count"] == 0 and row["new_chapters"] == [] and "74" in row["baseline_ids"]
    assert _check() == "no_update"


def test_failed_job_absorbs_nothing(client, download, upstream, checker):
    confirmed = _pending_74(download, upstream, checker)
    assert download("jb", ["74"], outcome="failed", written=1) == "failed"
    row = _row()
    assert _new_ids() == ["74"] and row["new_chapters"][0]["confirmed_at"] == confirmed
    assert "74" not in row["baseline_ids"]


def test_cancelled_job_absorbs_nothing(client, download, upstream, checker):
    confirmed = _pending_74(download, upstream, checker)
    assert download("jb", ["74"], outcome="canceled") == "canceled"
    assert _new_ids() == ["74"] and _row()["new_chapters"][0]["confirmed_at"] == confirmed


def test_retry_of_old_job_keeps_pending_new_chapters(client, download, upstream, checker):
    confirmed = _pending_74(download, upstream, checker)
    baseline_at = _row()["baseline_at"]
    # a retry / new job for chapters the user already had: its fetch lists 74, but must not swallow it
    assert download("jb", ["71"]) == "completed"
    assert download("jc", ["72"], outcome="failed", written=1) == "failed"
    row = _row()
    assert _new_ids() == ["74"] and row["new_chapters"][0]["confirmed_at"] == confirmed
    assert row["baseline_at"] == baseline_at and row["result"] == "new"
    assert _state(client)["update"]["state"] == "new"


def test_multiple_jobs_one_album_one_row(client, download):
    from core import database as db
    assert download("ja", ["71"]) == "completed"
    first = _row()
    assert download("jb", ["72"]) == "completed"
    assert download("jc", ["73"], outcome="failed", written=1) == "failed"
    conn = db.get_db()
    try:
        assert conn.execute("SELECT COUNT(*) FROM album_update_checks").fetchone()[0] == 1
    finally:
        conn.close()
    row = _row()
    assert row["baseline_ids"] == ["71", "72", "73"] and row["baseline_at"] == first["baseline_at"]


def test_redownload_after_files_deleted_resets_baseline(client, download, downloads, upstream, checker):
    _pending_74(download, upstream, checker)
    shutil.rmtree(downloads / "Name_3001")
    _forget_local()
    assert _state(client) == {"status": "ok", "album_id": "3001", "eligible": False}
    assert download("jb", ["71"]) == "completed"                  # a fresh local copy
    row = _row()
    assert row["baseline_ids"] == ["71", "72", "73", "74"] and row["baseline_source"] == "download"
    assert row["new_count"] == 0 and row["result"] is None and row["last_success_at"] is None
    assert _state(client)["update"]["state"] == "never"


def test_baseline_hook_failure_never_fails_download(client, download, monkeypatch):
    from core import update_store

    def boom(*args, **kwargs):
        raise RuntimeError("hook exploded")
    monkeypatch.setattr(update_store, "note_download_started", boom)
    monkeypatch.setattr(update_store, "absorb_downloaded", boom)
    assert download("ja", ["71"]) == "completed"
    assert download("jb", ["72"], outcome="failed", written=1) == "failed"   # a failed job still fails normally
    assert _row() is None


def test_download_observation_never_adds_new_ids(client, download):
    assert download("ja", ["71"]) == "completed"
    download.serve(CHAPTERS + [CH74])                  # upstream now lists 74 in the download's own fetch
    assert download("jb", ["72"]) == "completed"
    row = _row()
    assert row["new_count"] == 0 and row["baseline_ids"] == ["71", "72", "73"] and row["result"] is None
    assert download("jc", []) == "completed"           # everything, 74 included
    row = _row()
    assert row["new_count"] == 0 and row["baseline_ids"] == ["71", "72", "73", "74"] and row["result"] is None
    assert _log_rows() == []


# ─── C. check semantics ───────────────────────────────────────────────────────


def test_legacy_first_check_records_baseline_without_claim(client, local_album, upstream):
    local_album(baseline=False)                        # downloaded before PR-A: no row
    assert _row() is None and _state(client)["update"]["state"] == "never"
    upstream.set("3001", ["71", "72", "73", "74", "75"])
    assert _check() == "baseline"
    row = _row()
    assert row["baseline_ids"] == ["71", "72", "73", "74", "75"] and row["baseline_source"] == "first_check"
    assert row["new_count"] == 0 and row["result"] == "baseline"
    update = _state(client)["update"]
    assert update["state"] == "baseline" and update["upstream_count"] == 5
    assert _library_item(client, "3001")["update"] is None


def test_no_update(client, local_album, upstream):
    local_album()
    upstream.set("3001", ["71", "72", "73"])
    assert _check() == "no_update"
    row = _row()
    assert row["result"] == "no_update" and row["new_count"] == 0 and row["upstream_count"] == 3
    assert _state(client)["update"]["state"] == "no_update"


def test_new_confirmed_only_after_success_in_upstream_order(client, local_album, upstream, checker):
    local_album()
    assert _state(client)["update"]["new_count"] == 0       # nothing is claimed before a successful check
    upstream.set("3001", [("71", 1, "第1话"), ("72", 2, "第2话"), ("73", 3, "第3话"),
                          ("76", 5, "番外"), ("75", 4, "第4话")])
    assert _check() == "new"
    chapters = _row()["new_chapters"]
    stamp = checker.now().isoformat()
    assert chapters == [{"photo_id": "76", "index": 5, "title": "番外", "confirmed_at": stamp},
                        {"photo_id": "75", "index": 4, "title": "第4话", "confirmed_at": stamp}]
    logs = _log_rows()
    assert len(logs) == 1 and logs[0]["outcome"] == "new" and logs[0]["new_count"] == 2
    assert logs[0]["requests"] == 1 and logs[0]["upstream_count"] == 5
    update = _state(client)["update"]
    assert [c["photo_id"] for c in update["new_chapters"]] == ["76", "75"] and update["confirmed_at"] == stamp


def test_confirmed_at_kept_across_checks(client, local_album, upstream, checker):
    local_album()
    upstream.set("3001", ["71", "72", "73", "74"])
    assert _check() == "new"
    first = checker.now().isoformat()
    later = checker.advance(timedelta(days=1)).isoformat()
    upstream.set("3001", ["71", "72", "73", "74", "75"])
    assert _check() == "new"
    assert [(c["photo_id"], c["confirmed_at"]) for c in _row()["new_chapters"]] == [("74", first), ("75", later)]
    assert _state(client)["update"]["confirmed_at"] == first


def test_failure_keeps_confirmed_and_never_adds(client, local_album, upstream, checker):
    from core.update_checker import CheckFailed
    local_album("3001")                                       # never checked
    upstream.set("3001", CheckFailed("timeout", "Timeout", 1))
    assert _check("3001") == "failed"
    row = _row("3001")
    assert row["new_count"] == 0 and row["result"] is None and row["error_kind"] == "timeout"
    assert row["fail_count"] == 1
    assert _state(client, "3001")["update"]["state"] == "failed"
    assert _library_item(client, "3001")["update"] is None    # a failure never creates a chip

    local_album("3002")                                       # confirmed 74, then a failed check
    upstream.set("3002", ["71", "72", "73", "74"])
    assert _check("3002") == "new"
    confirmed = _row("3002")["new_chapters"]
    checker.advance(timedelta(days=1))
    upstream.set("3002", CheckFailed("network", "ConnectionError", 5))
    assert _check("3002") == "failed"
    row = _row("3002")
    assert row["new_chapters"] == confirmed and row["result"] == "new" and row["error_kind"] == "network"
    update = _state(client, "3002")["update"]
    assert update["state"] == "failed" and update["new_count"] == 1 and update["last_result"] == "new"
    assert update["error"] == {"kind": "network", "at": checker.now().isoformat(), "fail_count": 1}
    assert _library_item(client, "3002")["update"]["state"] == "new"   # still confirmed by the earlier success


def test_removed_only_is_no_update_and_acknowledged(client, local_album, upstream):
    local_album()
    upstream.set("3001", ["71", "72"])
    assert _check() == "no_update"
    row = _row()
    assert row["gone_ids"] == ["73"] and row["removed_ids"] == [] and row["new_count"] == 0
    assert _state(client)["update"]["state"] == "no_update"


def test_new_with_removal_is_changed_and_excluded_from_pending(client, local_album, upstream):
    from core import update_store
    local_album()
    upstream.set("3001", ["71", "72", "74"])
    assert _check() == "changed"
    row = _row()
    assert row["removed_ids"] == ["73"] and _new_ids() == ["74"] and row["gone_ids"] == []
    update = _state(client)["update"]
    assert update["state"] == "changed" and update["removed_count"] == 1 and update["new_count"] == 1
    assert update_store.pending_new_chapters() == []
    assert client.get("/api/updates/pending").get_json()["items"] == []
    states = client.post("/api/updates/states", json={"album_ids": ["3001"]}).get_json()["updates"]
    assert states["3001"]["state"] == "changed" and states["3001"]["removed_count"] == 1


def test_old_removal_does_not_taint_later_new_chapter(client, local_album, upstream, checker):
    from core import update_store
    local_album()
    upstream.set("3001", ["71", "72"])
    assert _check() == "no_update"                  # 73 vanished: acknowledged, no alarm
    checker.advance(timedelta(days=1))
    upstream.set("3001", ["71", "72", "74"])
    assert _check() == "new"                        # not 'changed': the old removal was already acknowledged
    assert [item["album_id"] for item in update_store.pending_new_chapters()] == ["3001"]


def test_reappearing_chapter_is_not_new(client, local_album, upstream, checker):
    local_album()
    upstream.set("3001", ["71", "72"])
    assert _check() == "no_update"
    checker.advance(timedelta(days=1))
    upstream.set("3001", ["71", "72", "73"])
    assert _check() == "no_update"
    row = _row()
    assert row["new_count"] == 0 and row["gone_ids"] == []


def test_single_chapter_album_gaining_chapters(client, local_album, upstream):
    # jmcomic lists a one-chapter album as one episode whose photo id is the album id
    local_album("3001", chapters=("3001",))
    upstream.set("3001", ["3001", "3002"])
    assert _check("3001") == "new" and _new_ids("3001") == ["3002"]
    local_album("3010", chapters=("3010",))
    upstream.set("3010", ["3011", "3012"])           # the album-id chapter was replaced
    assert _check("3010") == "changed"
    assert _row("3010")["removed_ids"] == ["3010"] and _new_ids("3010") == ["3011", "3012"]


@pytest.mark.parametrize("shape", ["empty", "duplicate", "non_numeric", "placeholder", "too_many"])
def test_invalid_upstream_shapes_are_failures(client, local_album, checker, monkeypatch, shape):
    from core import jm_service
    local_album()
    episodes = {
        "empty": [],
        "duplicate": [("71", "1", "a"), ("71", "2", "b")],
        "non_numeric": [("71", "1", "a"), ("7x", "2", "b")],
        "placeholder": [("71", "1", "a"), ("72", "2", "b"), ("73", "3", "c"), ("74", "4", "d")],
        "too_many": [(str(10000 + n), str(n), "x") for n in range(1, 5002)],
    }[shape]
    album = _UpstreamAlbum("350234" if shape == "placeholder" else "3001", episodes)   # 禁漫娘 has another id
    fake = _MirrorClient({"https://d1": album, "https://d2": album})
    monkeypatch.setattr(jm_service, "new_check_client", lambda timeout: fake)
    monkeypatch.setattr(jm_service, "close_client", lambda _client: None)
    assert _check() == "failed"
    row = _row()
    assert row["error_kind"] == "upstream_error" and row["new_count"] == 0 and row["result"] is None
    assert [c for c in fake.calls if c[0] == "get_album_detail"] == [("get_album_detail", "3001")] * 2


def test_error_classification_and_mirror_walk(client, checker, monkeypatch):
    from jmcomic import JsonResolveFailException, MissingAlbumPhotoException
    from core import jm_service, update_checker
    from core.update_checker import CheckFailed
    album = _UpstreamAlbum("3001", [("71", "1", "a")])
    monkeypatch.setattr(jm_service, "close_client", lambda _client: None)

    def fetch(answers):
        fake = _MirrorClient(answers)
        monkeypatch.setattr(jm_service, "new_check_client", lambda timeout: fake)
        try:
            return update_checker.fetch_upstream_episodes("3001"), fake
        except CheckFailed as failure:
            return failure, fake

    failure, fake = fetch({"d1": MissingAlbumPhotoException("missing", {}), "d2": album})
    assert (failure.kind, failure.requests) == ("not_found", 1)            # stops: another mirror won't have it
    assert fake.calls == [("set_domain_list", ["d1"]), ("get_album_detail", "3001")]
    (episodes, requests), fake = fetch({"d1": JsonResolveFailException("bad json", {}), "d2": album})
    assert episodes == [("71", 1, "a")] and requests == 2                 # an unreadable answer: next mirror
    assert update_checker._cursor == 1
    failure, fake = fetch({"d1": RuntimeError("Operation timed out after 20000 ms"),
                           "d2": TimeoutError("Operation timed out")})
    assert fake.calls[0] == ("set_domain_list", ["d2"])                   # starts at the mirror that answered
    assert (failure.kind, failure.requests) == ("timeout", 2)
    failure, _ = fetch({"d1": RuntimeError("boom"), "d2": RuntimeError("Operation timed out")})
    assert failure.kind == "network"
    failure, _ = fetch({"d1": JsonResolveFailException("bad", {}), "d2": ConnectionError("refused")})
    assert failure.kind == "upstream_error"
    assert "boom" not in failure.detail and len(failure.detail) <= 120      # only an exception class name


def test_check_racing_download_completion_never_overclaims(client, local_album, upstream):
    from core import update_store
    local_album()                                      # baseline 71–73; a download of everything is running

    def download_finishes_mid_fetch():
        update_store.absorb_downloaded("3001", ["71", "72", "73", "74"])
        return ["71", "72", "73", "74"]
    upstream.set("3001", download_finishes_mid_fetch)
    assert _check() == "no_update"                     # 74 was downloaded: never claimed as new
    assert _row()["new_count"] == 0


# ─── D. eligibility ───────────────────────────────────────────────────────────


def test_partial_cbz_only_and_zip_only_are_checked(client, local_album, upstream):
    local_album("3001", selected=["71"])
    local_album("3002", kind="cbz")
    local_album("3003", kind="zip")
    for album_id in ("3001", "3002", "3003"):
        upstream.set(album_id, ["71", "72", "73", "74"])
        assert _state(client, album_id)["eligible"] is True
        assert _check(album_id) == "new"
    assert upstream.calls == ["3001", "3002", "3003"]
    states = client.post("/api/updates/states", json={"album_ids": ["3001", "3002", "3003"]}).get_json()
    assert set(states["updates"]) == {"3001", "3002", "3003"}


def test_online_only_favourite_is_never_a_target(client, upstream, checker):
    from core import database as db, update_checker
    db.add_wishlist("3009", "Favourite")
    response = client.post("/api/updates/3009/check", json={})
    assert response.status_code == 409
    assert response.get_json() == {"status": "error", "reason": "not_target",
                                   "message": "本地还没有已下载的内容，不检查新章节"}
    assert _state(client, "3009") == {"status": "ok", "album_id": "3009", "eligible": False}
    update_checker.tick()
    assert upstream.calls == [] and _row("3009") is None
    assert _library_item(client, "3009", "/api/wishlist")["update"] is None


@pytest.mark.parametrize("case", ["deleted", "superseded", "failed_only", "canceled_only",
                                  "corrupt_archive", "empty_archive"])
def test_non_local_albums_skipped_without_request(client, local_album, downloads, upstream, checker, case):
    from core import database as db, update_checker, update_store
    if case in ("deleted", "superseded"):
        folder = local_album()
        if case == "deleted":
            shutil.rmtree(folder)
        else:
            conn = db.get_db()
            try:
                conn.execute("UPDATE jobs SET superseded_at=?", (datetime.now().isoformat(),))
                conn.commit()
            finally:
                conn.close()
    elif case in ("failed_only", "canceled_only"):
        folder = local_album()
        conn = db.get_db()
        try:
            conn.execute("UPDATE jobs SET status=?", ("failed" if case == "failed_only" else "canceled",))
            conn.commit()
        finally:
            conn.close()
    else:
        folder = downloads / "Album3001_3001"
        folder.mkdir()
        archive = folder / "Album3001_3001.cbz"
        if case == "corrupt_archive":
            archive.write_bytes(b"this is not a zip file")
        else:
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("ComicInfo.xml", "<ComicInfo/>")
        db.insert_job("job_x", "3001", "Album3001", [])
        db.update_job("job_x", status="completed", output_path=str(folder))
        update_store.note_download_started("3001", _episodes(["71", "72"]), had_local_content=False)
    _forget_local()
    upstream.set("3001", ["71", "72", "73", "74"])
    _due("3001", clock=checker)
    update_checker.tick()
    assert upstream.calls == []
    postponed = datetime.fromisoformat(_row()["next_check_at"])
    if case in ("failed_only", "canceled_only"):
        assert "3001" not in update_store.due_candidates(checker.now(), checker.now() + timedelta(days=8))
    else:
        assert postponed == checker.now() + timedelta(seconds=update_checker.NOT_READABLE_POSTPONE)
    response = client.post("/api/updates/3001/check")
    assert response.status_code == 409 and response.get_json()["reason"] == "not_target"
    assert upstream.calls == [] and _row()["last_attempt_at"] is None


def test_active_job_album_skipped_by_auto_manual_allowed(client, local_album, upstream, checker):
    from core import database as db, update_checker
    local_album()
    upstream.set("3001", ["71", "72", "73"])
    db.insert_job("job_queued", "3001", "Album3001", ["73"])        # queued, never run (no job manager here)
    _due("3001", clock=checker)
    update_checker.tick()
    assert upstream.calls == []
    response = client.post("/api/updates/3001/check")
    assert response.status_code == 200 and response.get_json()["outcome"] == "no_update"
    assert upstream.calls == ["3001"]


def test_stale_row_hidden_when_not_readable(client, local_album, upstream):
    from core import database as db
    folder = local_album()
    db.add_wishlist("3001", "Album3001")
    upstream.set("3001", ["71", "72", "73", "74"])
    assert _check() == "new"
    shutil.rmtree(folder)
    _forget_local()
    assert _state(client) == {"status": "ok", "album_id": "3001", "eligible": False}
    assert client.post("/api/updates/states", json={"album_ids": ["3001"]}).get_json()["updates"] == {}
    assert client.get("/api/updates/pending").get_json()["items"] == []
    assert _library_item(client, "3001")["update"] is None
    assert client.get("/api/library/3001").get_json()["item"]["update"] is None
    assert _library_item(client, "3001", "/api/wishlist")["update"] is None


# ─── E. never download ────────────────────────────────────────────────────────


def _tree(root):
    return sorted((str(p.relative_to(root)), p.stat().st_size if p.is_file() else -1, p.stat().st_mtime_ns)
                  for p in root.rglob("*"))


def _table(name):
    from core import database as db
    conn = db.get_db()
    try:
        return [tuple(r) for r in conn.execute(f"SELECT * FROM {name} ORDER BY 1")]
    finally:
        conn.close()


def test_checks_never_create_jobs_touch_files_or_caches(client, local_album, downloads, upstream, checker,
                                                        monkeypatch):
    from core import database as db, jm_service, update_checker
    from core.job_manager import JobManager
    from core.progress import progress_manager
    from core.update_checker import CheckFailed
    local_album("3001")                                   # → new
    local_album("3002")                                   # → no_update
    local_album("3003", baseline=False)                   # → baseline
    local_album("3004", chapters=("81", "82"))            # → changed
    local_album("3005")                                   # → failed (network)
    local_album("3006")                                   # → failed (not_found)
    db.add_wishlist("3001", "Album3001")
    db.add_wishlist("3009", "Favourite")
    db.upsert_album_meta("3001", title="Album3001", author="someone")
    db.add_album_tag("3001", "tag", source="user")
    for album_id in ("3001", "3002"):
        db.set_cached_album_detail(album_id, json.dumps({"album_id": album_id, "photos": []}))
    upstream.set("3001", ["71", "72", "73", "74"])
    upstream.set("3002", ["71", "72", "73"])
    upstream.set("3003", ["71", "72"])
    upstream.set("3004", ["82", "83"])
    upstream.set("3005", CheckFailed("network", "ConnectionError", 5))
    upstream.set("3006", CheckFailed("not_found", "MissingAlbumPhotoException", 1))
    _due("3001", "3002", "3003", clock=checker)
    jobs, wishlist, meta, tags = _table("jobs"), _table("wishlist"), _table("album_meta"), _table("album_tags")
    cache = _table("album_detail_cache")
    tree = _tree(downloads)

    def trap(name):
        def fail(*args, **kwargs):
            pytest.fail(f"checking for new chapters called {name}")
        return fail
    monkeypatch.setattr(JobManager, "create_job", trap("create_job"))
    monkeypatch.setattr(JobManager, "_schedule_next", trap("_schedule_next"))
    monkeypatch.setattr(JobManager, "retry_job", trap("retry_job"))
    monkeypatch.setattr(db, "insert_job", trap("insert_job"))
    monkeypatch.setattr(jm_service, "download_album_job", trap("download_album_job"))
    monkeypatch.setattr(progress_manager, "create_tracker", trap("create_tracker"))
    monkeypatch.setattr(db, "update_wishlist_download_status", trap("update_wishlist_download_status"))
    monkeypatch.setattr(db, "upsert_album_meta", trap("upsert_album_meta"))

    for _ in range(3):
        update_checker.tick()
        checker.advance(update_checker.GAP)
    outcomes = {}
    for album_id in ("3004", "3004", "3005", "3006"):
        response = client.post(f"/api/updates/{album_id}/check", json={"photos": ["99"]})
        assert response.status_code == 200
        outcomes.setdefault(album_id, []).append(response.get_json()["outcome"])
        checker.advance(10)
    assert upstream.calls == ["3001", "3002", "3003", "3004", "3005", "3006"]
    assert outcomes == {"3004": ["changed", "throttled"], "3005": ["failed"], "3006": ["failed"]}
    assert [_row(a)["result"] for a in ("3001", "3002", "3003")] == ["new", "no_update", "baseline"]
    assert _row("3006")["error_kind"] == "not_found"
    for album_id in ("3001", "3002", "3003", "3004", "3005", "3006", "3009"):
        assert client.get(f"/api/updates/{album_id}").status_code == 200
    assert client.post("/api/updates/states", json={"album_ids": ["3001", "3004"]}).status_code == 200
    for path in ("/api/updates/summary", "/api/updates/pending", "/api/library", "/api/library/3001",
                 "/api/wishlist"):
        assert client.get(path).status_code == 200, path

    assert _table("jobs") == jobs                           # row for row: no job was created or changed
    assert (_table("wishlist"), _table("album_meta"), _table("album_tags")) == (wishlist, meta, tags)
    assert _table("album_detail_cache") == [row for row in cache if row[0] != "3001"]   # only 3001's row went
    assert _tree(downloads) == tree                         # not a file or folder touched
    assert not any(t.name.startswith("dl-") for t in threading.enumerate())


FORBIDDEN = {
    "create_job", "insert_job", "retry_job", "claim_next_queued_job", "schedule_next", "_schedule_next",
    "download_album_job", "download_album", "download_photo", "download_image", "check_photo",
    "check_album_photos", "get_photo_detail", "get_jm_image", "new_image_client", "JmDownloader",
    "SubscribeAlbumUpdatePlugin", "get_client", "get_album_detail_cached", "get_cached_album_detail",
    "set_cached_album_detail", "update_wishlist_download_status", "upsert_album_meta", "batch_sync_auto_tags",
    "create_tracker",
}
JM_SERVICE_ALLOWED = {"new_check_client", "close_client", "forget_album_detail"}


def test_update_modules_reference_no_download_code():
    album_detail_calls = {}
    for rel in UPDATE_MODULES:
        source = (ROOT / rel).read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "job_manager" not in alias.name and "jm_plugin" not in alias.name, (rel, alias.name)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                names = {alias.name for alias in node.names}
                assert "job_manager" not in module and "jm_plugin" not in module, (rel, module)
                assert not names & {"job_manager", "jm_plugin"} and not names & FORBIDDEN, (rel, names)
                if module.endswith("jm_service"):
                    assert names <= JM_SERVICE_ALLOWED, (rel, names)
            elif isinstance(node, ast.Name):
                assert node.id not in FORBIDDEN, (rel, node.id)
            elif isinstance(node, ast.Attribute):
                assert node.attr not in FORBIDDEN, (rel, node.attr)
                if isinstance(node.value, ast.Name) and node.value.id == "jm_service":
                    assert node.attr in JM_SERVICE_ALLOWED, (rel, node.attr)
        album_detail_calls[rel] = source.count(".get_album_detail(")
    assert album_detail_calls == {"core/update_store.py": 0, "core/update_checker.py": 1, "routes/api_updates.py": 0}
    checker = (ROOT / "core/update_checker.py").read_text(encoding="utf-8")
    fetch = checker[checker.index("def fetch_upstream_episodes"):checker.index("def check_album")]
    assert ".get_album_detail(" in fetch
    store = ast.parse((ROOT / "core/update_store.py").read_text(encoding="utf-8"))
    imported = {alias.name for node in ast.walk(store) if isinstance(node, ast.Import) for alias in node.names}
    imported |= {f"{node.module or ''}:{alias.name}" for node in ast.walk(store)
                 if isinstance(node, ast.ImportFrom) for alias in node.names}
    assert imported == {"json", "random", "contextlib:contextmanager", "datetime:datetime", "datetime:timedelta",
                        ":database", "validation:validate_numeric"}          # the store is database only


def test_fetch_calls_only_album_detail_one_per_mirror(client, checker, monkeypatch):
    from jmcomic import MissingAlbumPhotoException
    from core import jm_service, update_checker
    from core.update_checker import CheckFailed
    closed = []
    monkeypatch.setattr(jm_service, "close_client", closed.append)
    album = _UpstreamAlbum("3001", [("71", "1", "第1话"), ("72", "2", "第2话")])
    strict = _MirrorClient({"https://d1": ConnectionError("refused"), "https://d2": album})
    monkeypatch.setattr(jm_service, "new_check_client", lambda timeout: strict)
    episodes, requests = update_checker.fetch_upstream_episodes("3001")
    assert episodes == [("71", 1, "第1话"), ("72", 2, "第2话")] and requests == 2
    assert strict.calls == [("set_domain_list", ["https://d1"]), ("get_album_detail", "3001"),
                            ("set_domain_list", ["https://d2"]), ("get_album_detail", "3001")]
    assert closed == [strict]

    monkeypatch.setattr(update_checker, "_cursor", 0)
    missing = _MirrorClient({"https://d1": MissingAlbumPhotoException("missing", {}), "https://d2": album})
    monkeypatch.setattr(jm_service, "new_check_client", lambda timeout: missing)
    with pytest.raises(CheckFailed) as failure:
        update_checker.fetch_upstream_episodes("3001")
    assert failure.value.kind == "not_found" and failure.value.requests == 1
    assert missing.calls == [("set_domain_list", ["https://d1"]), ("get_album_detail", "3001")]
    assert closed == [strict, missing]


@pytest.mark.parametrize("client_type", ["api", "html"])
def test_real_check_client_fresh_uncached_no_retry(client, client_type):
    from core import jm_service, update_checker
    from core.settings import update_settings
    update_settings({"timeout": "30", "retry_times": "5", "client_type": client_type})
    jm_service.invalidate_option_cache()
    before = jm_service.get_active_client_count()
    real = jm_service.new_check_client(update_checker._request_timeout())    # constructing never connects
    try:
        assert real.get_cache_dict() is None and real.retry_times == 0
        assert 0 < real.get_meta_data("timeout") <= 20
        assert len(real.get_domain_list()) == 5
        assert jm_service.get_active_client_count() == before + 1
    finally:
        jm_service.close_client(real)
    assert jm_service.get_active_client_count() == before


def test_real_client_at_most_one_request_per_mirror(client, local_album, checker, monkeypatch):
    from curl_cffi.curl import Curl
    from core import jm_service, update_checker
    jm_service.invalidate_option_cache()
    performed = []

    def perform(self, *args, **kwargs):
        performed.append(1)
        raise RuntimeError("counted, never sent")
    monkeypatch.setattr(Curl, "perform", perform)
    local_album()
    before = jm_service.get_active_client_count()
    assert update_checker.check_album("3001", "manual", lock_wait=0)["outcome"] == "failed"
    assert len(performed) == 5                                   # each configured mirror once, never more
    assert jm_service.get_active_client_count() == before       # the client was closed
    assert _row()["error_kind"] == "network"
    assert _log_rows()[-1]["requests"] == 5
    assert _state(client)["update"]["state"] == "failed"


def test_check_ignores_detail_cache_and_request_body(client, local_album, checker, monkeypatch):
    from core import database as db, jm_service
    local_album(baseline=False)
    db.set_cached_album_detail("3001", json.dumps({
        "album_id": "3001", "chapter_count": 5,
        "photos": [{"photo_id": str(p), "title": "x", "page_count": 1} for p in range(71, 76)]}))

    def forbidden(*args, **kwargs):
        pytest.fail("a check read a cache or the shared client")
    monkeypatch.setattr(db, "get_cached_album_detail", forbidden)
    monkeypatch.setattr(jm_service, "get_album_detail_cached", forbidden)
    monkeypatch.setattr(jm_service, "get_album_detail", forbidden)
    monkeypatch.setattr(jm_service, "get_client", forbidden)
    album = _UpstreamAlbum("3001", [("71", "1", "a"), ("72", "2", "b"), ("73", "3", "c")])
    monkeypatch.setattr(jm_service, "new_check_client", lambda timeout: _MirrorClient({"https://d1": album}))
    monkeypatch.setattr(jm_service, "close_client", lambda _client: None)
    response = client.post("/api/updates/3001/check",
                           json={"photos": [{"photo_id": str(p)} for p in range(71, 80)], "album_ids": ["1"]})
    assert response.status_code == 200
    data = response.get_json()
    assert data["outcome"] == "baseline"
    assert data["update"]["upstream_count"] == 3 and data["update"]["baseline_count"] == 3
    assert _row()["baseline_ids"] == ["71", "72", "73"]


def test_detail_cache_dropped_only_for_that_album_on_new(client, local_album, upstream, checker):
    from core import database as db, jm_service
    local_album("3001")
    local_album("3002")

    def seed(album_id):
        db.set_cached_album_detail(album_id, json.dumps({"album_id": album_id, "photos": []}))
        with jm_service._detail_cache_lock:
            jm_service._detail_cache[(str(db.DB_PATH), album_id)] = {"data": {"album_id": album_id}, "ts": 0}

    def cached():
        with jm_service._detail_cache_lock:
            memory = {key[1] for key in jm_service._detail_cache if key[0] == str(db.DB_PATH)}
        return {row[0] for row in _table("album_detail_cache")}, memory
    seed("3001")
    seed("3002")
    upstream.set("3001", ["71", "72", "73", "74"])
    upstream.set("3002", ["71", "72", "73"])
    assert _check("3001") == "new"
    assert cached() == ({"3002"}, {"3002"})               # only the album with newly confirmed chapters
    assert _check("3002") == "no_update"
    seed("3001")
    checker.advance(timedelta(days=1))
    assert _check("3001") == "new"                         # 74 again: nothing newly confirmed, cache kept
    assert cached() == ({"3001", "3002"}, {"3001", "3002"})
    with jm_service._detail_cache_lock:
        jm_service._detail_cache.clear()


def test_create_app_and_settings_save_never_start_thread(client):
    import importlib
    import io
    from app import create_app
    from core import update_checker
    create_app()
    assert client.post("/api/settings", json={"auto_update_check": "true"}).status_code == 200
    exported = client.get("/api/settings/export").get_data()
    upload = {"file": (io.BytesIO(exported), "settings.json")}
    assert client.post("/api/settings/import", data=upload, content_type="multipart/form-data").status_code == 200
    importlib.import_module("core.update_checker")
    assert not any(t.name == "update-checker" for t in threading.enumerate())
    status = update_checker.runtime_status()
    assert status["armed"] is False and status["running"] is False
    assert client.get("/api/updates/summary").get_json()["runtime"]["phase"] == "not_running"


# ─── F. pacing ────────────────────────────────────────────────────────────────


def test_tick_checks_at_most_one_album(client, local_album, upstream, checker):
    from core import update_checker
    for album_id in ("3001", "3002", "3003"):
        local_album(album_id)
        upstream.set(album_id, ["71", "72", "73"])
    _due("3001", "3002", "3003", clock=checker)
    assert update_checker.tick() == update_checker.GAP
    assert upstream.calls == ["3001"]
    assert update_checker.tick() == update_checker.GAP        # the gap has not passed: nothing more
    assert upstream.calls == ["3001"]
    checker.advance(update_checker.GAP)
    update_checker.tick()
    assert upstream.calls == ["3001", "3002"]


def test_gap_after_any_check_including_manual(client, local_album, upstream, checker):
    from core import update_checker
    for album_id in ("3001", "3002"):
        local_album(album_id)
        upstream.set(album_id, ["71", "72", "73"])
    _due("3002", clock=checker)
    assert client.post("/api/updates/3001/check").get_json()["outcome"] == "no_update"
    assert update_checker.tick() == update_checker.GAP
    checker.advance(update_checker.GAP - 1)
    assert update_checker.tick() == 1
    assert upstream.calls == ["3001"]
    checker.advance(1)
    update_checker.tick()
    assert upstream.calls == ["3001", "3002"]


def test_daily_cadence_after_success(client, local_album, upstream, checker):
    from core import update_checker
    local_album()
    upstream.set("3001", ["71", "72", "73"])
    _due("3001", clock=checker)
    update_checker.tick()
    start = checker.now()
    assert datetime.fromisoformat(_row()["next_check_at"]) == start + timedelta(hours=23)   # 23–25 h, lower bound
    checker.advance(timedelta(hours=23) - timedelta(seconds=1))
    assert update_checker.tick() == update_checker.IDLE[0]
    assert upstream.calls == ["3001"]
    checker.advance(1)
    update_checker.tick()
    assert upstream.calls == ["3001", "3001"]


def test_failure_backoff_ladder(client, local_album, upstream, checker, monkeypatch):
    from core import update_checker
    from core.update_checker import CheckFailed
    monkeypatch.setattr(update_checker, "_uniform", random.uniform)
    local_album()
    upstream.set("3001", CheckFailed("upstream_error", "JsonResolveFailException", 1))
    for n, expected in enumerate((1800, 3600, 7200, 14400, 28800, 57600, 86400, 86400), 1):
        now = checker.now()
        assert _check() == "failed"
        row = _row()
        delay = (datetime.fromisoformat(row["next_check_at"]) - now).total_seconds()
        assert expected * 0.9 - 1 <= delay <= expected * 1.1 + 1, (n, delay)
        assert row["fail_count"] == n
        checker.advance(delay)


def test_network_down_pause_and_resume(client, local_album, upstream, checker):
    from core import update_checker
    from core.update_checker import CheckFailed
    for album_id in ("3001", "3002", "3003"):
        local_album(album_id)
        upstream.set(album_id, CheckFailed("network", "ConnectionError", 5))
    _due("3001", "3002", "3003", clock=checker)
    update_checker.tick()
    checker.advance(update_checker.GAP)
    update_checker.tick()                                           # 2nd automatic network failure → 15 min pause
    assert upstream.calls == ["3001", "3002"]
    assert update_checker.paused_until() == (checker.now() + timedelta(seconds=900)).isoformat()
    status = update_checker.runtime_status()                        # shown before the next tick, too
    assert status["phase"] == "paused" and status["resume_at"] == update_checker.paused_until()
    checker.advance(update_checker.GAP)
    assert update_checker.tick() == 900 - update_checker.GAP
    assert upstream.calls == ["3001", "3002"]                       # paused: no request
    assert update_checker.runtime_status()["phase"] == "paused"
    update = _state(client, "3003")["update"]
    assert update["paused_until"] == update_checker.paused_until() and update["next_check_at"] >= update["paused_until"]
    checker.advance(900 - update_checker.GAP)
    update_checker.tick()                                           # the 3rd failure → 30 min
    assert upstream.calls == ["3001", "3002", "3003"]
    assert update_checker.paused_until() == (checker.now() + timedelta(seconds=1800)).isoformat()
    upstream.set("3001", ["71", "72", "73"])
    response = client.post("/api/updates/3001/check")               # a manual check ignores the pause …
    assert response.get_json()["outcome"] == "no_update"
    assert update_checker.paused_until() is None                   # … and its success ends it
    assert update_checker._net_failures == 0
    assert update_checker.runtime_status()["phase"] == "idle"


def test_legacy_albums_spread_newest_first(client, checker):
    from core import database as db, update_store
    base = datetime(2026, 1, 1)

    def add_jobs(count, offset=0):
        conn = db.get_db()
        try:
            conn.executemany(
                "INSERT INTO jobs (job_id, album_id, title, selected_photo_ids, status, created_at, updated_at, "
                "completed_at) VALUES (?, ?, 't', '[]', 'completed', ?, ?, ?)",
                [(f"j{offset + n}", str(10000 + offset + n), (base + timedelta(minutes=n)).isoformat(),
                  (base + timedelta(minutes=n)).isoformat(), (base + timedelta(minutes=n)).isoformat())
                 for n in range(count)])
            conn.commit()
        finally:
            conn.close()

    def schedule():
        conn = db.get_db()
        try:
            return [(r[0], datetime.fromisoformat(r[1])) for r in conn.execute(
                "SELECT album_id, next_check_at FROM album_update_checks ORDER BY next_check_at, album_id")]
        finally:
            conn.close()
    now = checker.now()
    add_jobs(10)
    assert update_store.register_legacy(now) == 10
    rows = schedule()
    assert [album_id for album_id, _ in rows] == [str(10000 + n) for n in range(9, -1, -1)]   # newest first
    assert [due - now for _, due in rows] == [timedelta(minutes=30 * n) for n in range(10)]
    assert all(_row(album_id)["baseline_ids"] is None for album_id, _ in rows)
    assert update_store.register_legacy(now) == 0                   # idempotent

    conn = db.get_db()
    try:
        conn.execute("DELETE FROM album_update_checks")
        conn.commit()
    finally:
        conn.close()
    add_jobs(290, offset=10)
    assert update_store.register_legacy(now) == 300
    rows = schedule()
    last = rows[-1][1] - now
    assert timedelta(hours=23) < last < timedelta(hours=24)         # 300 albums: about 288 s apart
    assert rows[1][1] - rows[0][1] == timedelta(seconds=288)
    assert len(update_store.due_candidates(now, now + timedelta(days=8), 500)) == 1   # not all due at once


def test_lease_prevents_recheck_after_crash(client, local_album, upstream, checker):
    from core import update_checker, update_store
    local_album()
    now = checker.now()
    seen = []

    def during_fetch():
        row = _row()
        seen.append((row["last_attempt_at"], row["next_check_at"]))
        raise RuntimeError("the process died here")                 # nothing recorded after the lease
    upstream.set("3001", during_fetch)
    _due("3001", clock=checker)
    with pytest.raises(RuntimeError):
        update_checker.tick()
    assert seen == [(now.isoformat(), (now + timedelta(hours=1)).isoformat())]
    assert update_store.due_candidates(now + timedelta(minutes=59), now + timedelta(days=8)) == []
    assert update_store.due_candidates(now + timedelta(hours=1), now + timedelta(days=8)) == ["3001"]
    assert update_checker.runtime_status()["current"] is None       # the lock and state were released
    assert update_checker._check_lock.acquire(blocking=False)
    update_checker._check_lock.release()


def test_startup_delay_before_first_tick(client, checker, upstream, monkeypatch):
    from core import update_checker
    monkeypatch.setattr(update_checker, "_uniform", random.uniform)

    class FakeEvent:
        def __init__(self, answers):
            self.answers = list(answers)
            self.waits = []

        def wait(self, seconds):
            self.waits.append(seconds)
            return self.answers.pop(0)

        def is_set(self):
            return False
    stopped = FakeEvent([True])
    update_checker._loop(stopped)
    assert len(stopped.waits) == 1 and 300 <= stopped.waits[0] <= 420
    assert update_checker.runtime_status()["phase"] == "starting"
    ticks = []
    monkeypatch.setattr(update_checker, "tick", lambda: ticks.append(1) or 42)
    running = FakeEvent([False, True])
    update_checker._loop(running)
    assert 300 <= running.waits[0] <= 420 and running.waits[1] == 42 and ticks == [1]
    assert upstream.calls == []


def test_defers_while_download_running(client, local_album, upstream, checker):
    from core import database as db, update_checker
    local_album()
    upstream.set("3001", ["71", "72", "73"])
    db.insert_job("job_running", "3999", "Other", [])
    db.update_job("job_running", status="running")
    _due("3001", clock=checker)
    assert update_checker.tick() == update_checker.DEFER_RUNNING
    assert update_checker.runtime_status()["phase"] == "deferred"
    assert upstream.calls == []


def test_only_one_check_in_flight(client, local_album, upstream, checker, monkeypatch):
    from core import update_checker
    local_album("3001")
    local_album("3002")
    upstream.set("3001", ["71", "72", "73"])
    upstream.set("3002", ["71", "72", "73"])
    upstream.gate = threading.Event()
    results, errors = [], []

    def run():
        try:
            results.append(update_checker.check_album("3001", "manual", lock_wait=5))
        except BaseException as e:  # noqa: BLE001 — reported below
            errors.append(e)
    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert upstream.entered.wait(5)
        assert update_checker.is_checking("3001") and _state(client, "3001")["update"]["state"] == "checking"
        _due("3002", clock=checker)
        assert update_checker.tick() == update_checker.BUSY_RETRY      # the background waits for the lock
        monkeypatch.setattr(update_checker, "MANUAL_LOCK_WAIT", 0.2)
        response = client.post("/api/updates/3002/check")
        assert response.status_code == 409
        assert response.get_json() == {"status": "error", "reason": "busy", "message": "正在检查别的漫画，请稍后再试"}
    finally:
        upstream.gate.set()
        worker.join(10)
    assert not errors and results[0]["outcome"] == "no_update"
    assert upstream.max_in_flight == 1 and upstream.calls == ["3001"]


def test_same_album_coalesced_and_manual_throttled(client, local_album, upstream, checker, monkeypatch):
    from core import update_checker, update_store
    local_album()
    upstream.set("3001", ["71", "72", "73"])

    class AnotherCheckRanWhileWaiting:
        """While this request waited for the lock, a check of the same album started and finished."""

        def __init__(self):
            self.lock = threading.Lock()

        def acquire(self, blocking=True, timeout=-1):
            ran_at = checker.advance(5)
            update_store.begin_check("3001", "auto", ran_at)
            update_store.record_success("3001", _episodes(["71", "72", "73"]), "auto", ran_at, ran_at, 1,
                                        ran_at + timedelta(hours=23))
            return self.lock.acquire(blocking, timeout)

        def release(self):
            self.lock.release()
    monkeypatch.setattr(update_checker, "_check_lock", AnotherCheckRanWhileWaiting())
    assert update_checker.check_album("3001", "manual", lock_wait=1)["outcome"] == "coalesced"
    assert upstream.calls == []
    monkeypatch.setattr(update_checker, "_check_lock", threading.Lock())
    checker.advance(30)
    response = client.post("/api/updates/3001/check")
    assert response.status_code == 200 and response.get_json()["outcome"] == "throttled"
    assert upstream.calls == []
    checker.advance(31)
    assert client.post("/api/updates/3001/check").get_json()["outcome"] == "no_update"
    assert upstream.calls == ["3001"]


def test_manual_works_with_auto_off(client, local_album, upstream, checker):
    from core import update_checker
    from core.settings import update_settings
    update_settings({"auto_update_check": "false"})
    local_album()
    upstream.set("3001", ["71", "72", "73", "74"])
    response = client.post("/api/updates/3001/check")
    data = response.get_json()
    assert response.status_code == 200 and data["outcome"] == "new"
    assert data["auto_enabled"] is False and data["update"]["next_check_at"] is None
    assert data["update"]["state"] == "new"
    assert _library_item(client, "3001")["update"]["state"] == "new"   # results still shown while off
    _due("3001", clock=checker)
    checker.advance(update_checker.GAP)
    assert update_checker.tick() == update_checker.DISABLED_POLL
    assert upstream.calls == ["3001"]


def test_disabled_setting_background_does_nothing(client, local_album, upstream, checker):
    from core import update_checker
    from core.settings import update_settings
    local_album(baseline=False)
    upstream.set("3001", ["71", "72", "73"])
    update_settings({"auto_update_check": "false"})
    assert update_checker.tick() == update_checker.DISABLED_POLL
    assert update_checker.runtime_status()["phase"] == "off"
    assert upstream.calls == [] and _row() is None                # not even registered
    update_settings({"auto_update_check": "true"})
    update_checker.tick()
    assert upstream.calls == ["3001"]                              # resumes on the next tick


def test_clock_moved_back_guard(client, local_album, upstream, checker):
    from core import database as db, update_checker
    local_album("3001")
    local_album("3002")
    upstream.set("3001", ["71", "72", "73"])
    upstream.set("3002", ["71", "72", "73"])
    conn = db.get_db()
    try:
        conn.execute("UPDATE album_update_checks SET next_check_at=? WHERE album_id='3001'",
                     ((checker.now() + timedelta(days=30)).isoformat(),))    # written before the clock went back
        conn.execute("UPDATE album_update_checks SET next_check_at=? WHERE album_id='3002'",
                     ((checker.now() + timedelta(days=7)).isoformat(),))
        conn.commit()
    finally:
        conn.close()
    update_checker.tick()
    assert upstream.calls == ["3001"]
    checker.advance(update_checker.GAP)
    update_checker.tick()
    assert upstream.calls == ["3001"]                               # 7 days ahead is plausible: not due


def test_start_stop_thread_with_stub(client, upstream, checker, monkeypatch):
    from core import update_checker
    monkeypatch.setattr(update_checker, "STARTUP_DELAY", 3600)
    update_checker.start()
    update_checker.start()                                          # idempotent
    try:
        assert [t.name for t in threading.enumerate()].count("update-checker") == 1
        started = threading.Event()
        for _ in range(100):                                        # the thread records its startup wait
            if update_checker.runtime_status()["resume_at"]:
                break
            started.wait(0.05)
        status = update_checker.runtime_status()
        assert status["armed"] and status["running"] and status["phase"] == "starting"
        assert status["resume_at"] == (checker.now() + timedelta(seconds=3600)).isoformat()
        summary = client.get("/api/updates/summary").get_json()
        assert summary["runtime"]["phase"] == "starting"
    finally:
        update_checker.stop()
    assert not any(t.name == "update-checker" for t in threading.enumerate())
    status = update_checker.runtime_status()
    assert not status["armed"] and not status["running"] and upstream.calls == []


# ─── G. API ───────────────────────────────────────────────────────────────────


def test_get_state_shapes_per_state(client, local_album, upstream, checker):
    from core import update_checker
    from core.update_checker import CheckFailed
    local_album("3001")                                        # never (download baseline)
    local_album("3002", baseline=False)                        # never (legacy) → baseline
    local_album("3003")                                        # no_update
    local_album("3004")                                        # new
    local_album("3005")                                        # changed
    local_album("3006")                                        # failed
    local_album("3007")                                        # checking
    for album_id, value in {"3002": ["71", "72"], "3003": ["71", "72", "73"], "3004": ["71", "72", "73", "74"],
                            "3005": ["72", "73", "74"], "3006": CheckFailed("not_found", "Missing", 1),
                            "3007": ["71", "72", "73"]}.items():
        upstream.set(album_id, value)
    for album_id in ("3002", "3003", "3004", "3005", "3006"):
        _check(album_id)
    expected = {"3001": "never", "3002": "baseline", "3003": "no_update", "3004": "new", "3005": "changed",
                "3006": "failed"}
    for album_id, state in expected.items():
        data = _state(client, album_id)
        assert set(data) == {"status", "album_id", "eligible", "auto_enabled", "checking", "update"}
        assert data["eligible"] is True and data["auto_enabled"] is True and data["checking"] is False
        assert set(data["update"]) == UPDATE_KEYS and data["update"]["state"] == state, album_id
    new = _state(client, "3004")["update"]
    assert set(new["new_chapters"][0]) == {"photo_id", "index", "title", "confirmed_at"}
    assert new["last_result"] == "new" and new["last_trigger"] == "auto" and new["error"] is None
    assert new["next_check_at"] == (checker.now() + timedelta(hours=23)).isoformat()
    never = _state(client, "3001")["update"]
    assert never["baseline_count"] == 3 and never["last_result"] is None and never["new_chapters"] == []
    failed = _state(client, "3006")["update"]
    assert failed["error"]["kind"] == "not_found" and failed["error"]["fail_count"] == 1
    upstream.gate = threading.Event()
    upstream.entered.clear()
    worker = threading.Thread(target=lambda: update_checker.check_album("3007", "manual", lock_wait=5))
    worker.start()
    try:
        assert upstream.entered.wait(5)
        data = _state(client, "3007")
        assert data["checking"] is True and data["update"]["state"] == "checking"
        summary = client.get("/api/updates/summary").get_json()
        assert summary["runtime"]["current"]["album_id"] == "3007"
    finally:
        upstream.gate.set()
        worker.join(10)
    assert _state(client, "3007")["update"]["state"] == "no_update"


def test_db_only_endpoints_never_fetch(client, local_album, upstream, checker, monkeypatch):
    from core import database as db, jm_service, update_checker
    local_album("3001")
    local_album("3002")
    db.add_wishlist("3001", "Album3001")
    upstream.set("3001", ["71", "72", "73", "74"])
    assert _check("3001") == "new"

    def forbidden(*args, **kwargs):
        pytest.fail("a read-only endpoint went online")
    for name in ("new_check_client", "get_client", "get_album_detail", "get_album_detail_cached"):
        monkeypatch.setattr(jm_service, name, forbidden)
    monkeypatch.setattr(update_checker, "fetch_upstream_episodes", forbidden)
    responses = [
        client.get("/api/updates/3001"), client.get("/api/updates/3002"), client.get("/api/updates/3999"),
        client.post("/api/updates/states", json={"album_ids": ["3001", "3002"]}),
        client.get("/api/updates/summary"), client.get("/api/updates/pending"),
        client.get("/api/library"), client.get("/api/library/3001"), client.get("/api/wishlist"),
    ]
    assert [r.status_code for r in responses] == [200] * len(responses)


def test_states_validation(client):
    def post(body):
        response = client.post("/api/updates/states", json=body)
        return response.status_code, response.get_json().get("message")
    assert post({}) == (400, "缺少 album_ids 列表")
    assert post({"album_ids": "3001"}) == (400, "缺少 album_ids 列表")
    assert post({"album_ids": [str(n) for n in range(201)]}) == (400, "album_ids 数量过多，最大 200")
    assert post({"album_ids": ["3001", "12x"]}) == (400, "album_ids 只能包含纯数字")
    assert post({"album_ids": [True]}) == (400, "album_ids 只能包含纯数字")
    assert post({"album_ids": [-1]}) == (400, "album_ids 只能包含纯数字")
    assert post({"album_ids": [str(n) for n in range(200)]}) == (200, None)
    assert post({"album_ids": [3001]}) == (200, None)
    for path in ("/api/updates/12x", "/api/updates/12x/check"):
        response = client.get(path) if not path.endswith("check") else client.post(path)
        assert response.status_code == 400 and response.get_json()["message"] == "album_id 必须是纯数字"


def test_summary_counts_and_checks_24h(client, local_album, upstream, checker):
    from core import database as db
    from core.settings import update_settings
    from core.update_checker import CheckFailed
    for album_id in ("3001", "3002", "3003", "3005"):
        local_album(album_id)
    local_album("3004", baseline=False)                       # never checked
    stale = local_album("3006")                                # 'new', then its files are deleted
    upstream.set("3001", ["71", "72", "73", "74"])
    upstream.set("3002", ["72", "73", "74"])
    upstream.set("3003", CheckFailed("timeout", "Timeout", 5))
    upstream.set("3005", ["71", "72", "73"])
    upstream.set("3006", ["71", "72", "73", "74"])
    conn = db.get_db()
    try:
        conn.execute("INSERT INTO update_check_log (album_id, trigger, started_at, finished_at, outcome) "
                     "VALUES ('3001', 'auto', ?, ?, 'no_update')",
                     ((checker.now() - timedelta(days=2)).isoformat(),) * 2)
        conn.commit()
    finally:
        conn.close()
    for album_id in ("3006", "3001", "3002", "3003", "3005"):
        _check(album_id)
        checker.advance(60)
    shutil.rmtree(stale)
    _forget_local()
    data = client.get("/api/updates/summary").get_json()
    assert data["status"] == "ok" and data["enabled"] is True
    assert data["runtime"]["phase"] == "not_running" and data["runtime"]["armed"] is False
    assert data["counts"] == {"targets": 5, "checked": 3, "with_updates": 2, "changed": 1, "failing": 1, "never": 2}
    assert data["checks_24h"] == 5                            # the 2-day-old row is not counted
    last = data["last"]
    assert last["album_id"] == "3005" and last["outcome"] == "no_update" and last["title"] == "Album3005"
    assert last["upstream_count"] == 3 and last["error_kind"] is None
    update_settings({"auto_update_check": "false"})
    assert client.get("/api/updates/summary").get_json()["runtime"]["phase"] == "off"


def test_pending_contract_shape_for_pr_b(client, local_album, upstream, checker):
    from core import database as db, update_store
    local_album("3001", title="Title3001")
    local_album("3002")
    stale = local_album("3003")
    db.upsert_album_meta("3001", title="Meta title")
    upstream.set("3001", ["71", "72", "73", "74"])
    upstream.set("3002", ["72", "73", "74"])                  # changed: not for the batch
    upstream.set("3003", ["71", "72", "73", "74"])
    for album_id in ("3001", "3002", "3003"):
        _check(album_id)
    first = checker.now().isoformat()
    checker.advance(timedelta(days=1))
    upstream.set("3001", [("71", 1, "第1话"), ("72", 2, "第2话"), ("73", 3, "第3话"), ("74", 4, "第4话"),
                          ("75", 5, "第5话")])
    _check("3001")
    shutil.rmtree(stale)
    _forget_local()
    items = update_store.pending_new_chapters()
    assert [item["album_id"] for item in items] == ["3001", "3003"]     # 3003 is filtered by the caller
    item = items[0]
    assert set(item) == {"album_id", "title", "photo_ids", "chapters", "confirmed_at", "checked_at"}
    assert item["title"] == "Meta title" and item["photo_ids"] == ["74", "75"]
    assert item["chapters"] == [
        {"photo_id": "74", "index": 4, "title": "第4话", "confirmed_at": first},
        {"photo_id": "75", "index": 5, "title": "第5话", "confirmed_at": checker.now().isoformat()}]
    assert item["confirmed_at"] == first and item["checked_at"] == checker.now().isoformat()
    assert items[1]["title"] == "Album3003"                          # no album_meta: the job title
    assert [i["album_id"] for i in update_store.pending_new_chapters(["3003"])] == ["3003"]
    served = client.get("/api/updates/pending").get_json()
    assert served["status"] == "ok" and served["items"] == [item]   # only readable albums
    assert update_store.mark_due(["3001"], checker.now()) == 1       # PR-B's batch goes through the paced loop
    assert _row("3001")["next_check_at"] == checker.now().isoformat()
    assert upstream.calls == ["3001", "3002", "3003", "3001"]        # marking due fetched nothing


def test_library_and_wishlist_items_carry_update_only_when_readable(client, local_album, upstream, checker):
    from core import database as db
    local_album("3001")
    local_album("3002")
    stale = local_album("3004")
    for album_id, title in (("3001", "Album3001"), ("3002", "Album3002"), ("3003", "Online"),
                            ("3004", "Album3004")):
        db.add_wishlist(album_id, title)
    upstream.set("3001", [("71", 1, "第1话"), ("72", 2, "第2话"), ("73", 3, "第3话"), ("74", 4, "第4话")])
    upstream.set("3002", ["71", "72", "73"])
    upstream.set("3004", ["71", "72", "73", "74"])
    for album_id in ("3001", "3002", "3004"):
        _check(album_id)
    shutil.rmtree(stale)
    _forget_local()
    expected = {"state": "new", "new_count": 1, "removed_count": 0, "confirmed_at": checker.now().isoformat(),
                "titles": ["第4话"]}
    for path in ("/api/library", "/api/wishlist"):
        updates = {item["album_id"]: item["update"] for item in
                   client.get(path + "?page_size=200").get_json()["items"]}
        assert updates == {"3001": expected, "3002": None, "3003": None, "3004": None}, path
    assert client.get("/api/library/3001").get_json()["item"]["update"] == expected
    states = client.post("/api/updates/states", json={"album_ids": ["3001", "3002", "3003", "3004"]}).get_json()
    assert states["updates"] == {"3001": expected}


# ─── review round: acknowledged removals, clock moved back, lock wait, mirror budget, manual cadence ───


def test_a_reviewed_change_does_not_taint_the_next_new_chapter(client, local_album, upstream, checker):
    # 73 vanished while 74 appeared ('changed'); the user downloaded 74, which ends the notice.
    # A chapter published after that is plainly new, never 'changed' again because of 73.
    from core import update_store
    local_album()
    upstream.set("3001", ["71", "72", "74"])
    assert _check() == "changed"
    update_store.absorb_downloaded("3001", ["74"])
    assert _row()["removed_ids"] == [] and _row()["gone_ids"] == ["73"] and _row()["new_count"] == 0
    checker.advance(timedelta(days=1))
    upstream.set("3001", ["71", "72", "74", "75"])
    assert _check() == "new" and _new_ids() == ["75"]
    assert [(item["album_id"], item["photo_ids"]) for item in update_store.pending_new_chapters()] == [("3001", ["75"])]


def test_manual_check_still_runs_after_the_clock_moves_back(client, local_album, upstream, checker):
    local_album()
    upstream.set("3001", ["71", "72", "73"])
    assert client.post("/api/updates/3001/check").get_json()["outcome"] == "no_update"   # baseline from the download
    checker.advance(timedelta(hours=-2))          # Windows time sync corrected a fast clock
    calls = len(upstream.calls)
    data = client.post("/api/updates/3001/check").get_json()
    assert data["outcome"] == "no_update" and len(upstream.calls) == calls + 1   # a real check, not 'coalesced'


def test_manual_check_waits_for_the_lock(client, local_album, upstream, checker):
    from core import update_checker
    local_album()
    upstream.set("3001", ["71", "72", "73"])
    lock = update_checker._check_lock
    assert lock.acquire(blocking=False)            # a background check holds the lock ...
    threading.Timer(0.3, lock.release).start()     # ... and finishes a moment later
    response = client.post("/api/updates/3001/check")
    assert response.status_code == 200 and response.get_json()["outcome"] == "no_update"  # waited, not 409


def test_no_new_mirror_after_the_check_budget(client, checker, monkeypatch):
    from core import jm_service, update_checker
    from core.update_checker import CheckFailed

    class Slow(_MirrorClient):
        def get_album_detail(self, album_id):
            checker.advance(30)                    # each dead mirror costs 30 s
            return super().get_album_detail(album_id)
    fake = Slow({f"d{n}": ConnectionError("refused") for n in range(1, 6)})
    monkeypatch.setattr(jm_service, "new_check_client", lambda timeout: fake)
    monkeypatch.setattr(jm_service, "close_client", lambda _client: None)
    with pytest.raises(CheckFailed) as failure:
        update_checker.fetch_upstream_episodes("3001")
    assert failure.value.requests == 2              # 60 s spent: no third mirror after the 45 s budget
    assert [c for c in fake.calls if c[0] == "get_album_detail"] == [("get_album_detail", "3001")] * 2


def test_manual_success_schedules_the_next_check_a_day_later(client, local_album, upstream, checker):
    local_album()
    upstream.set("3001", ["71", "72", "73"])
    client.post("/api/updates/3001/check")
    assert datetime.fromisoformat(_row()["next_check_at"]) == checker.now() + timedelta(hours=23)


# ─── PR-B: jobs queued by a batch download, run with the real download_album_job ───


@pytest.fixture
def batch(client, monkeypatch):
    """core.batch_downloads with scheduling recorded (the jobs are run here by hand, never by the manager)."""
    from core import batch_downloads
    from core.job_manager import JobManager
    scheduled = []
    monkeypatch.setattr(JobManager, "_schedule_next", lambda self: scheduled.append(1))
    yield batch_downloads
    assert scheduled == []                      # the store-level confirm never schedules by itself


def _batch_photos(batch, kind="new_chapters"):
    return [(item["album_id"], item["photo_ids"]) for item in batch.preview(kind)["items"]]


def _batch_confirm(batch, kind):
    created = batch.confirm(kind, batch.preview(kind)["token"])["created"]
    assert len(created) == 1
    return created[0]


def test_batch_new_chapters_job_absorbs_on_completion(client, download, upstream, checker, batch):
    from core import database as db, update_store
    _pending_74(download, upstream, checker)
    db.add_wishlist("3001", "Name")
    assert _batch_photos(batch) == [("3001", ["74"])]
    job = _batch_confirm(batch, "new_chapters")
    assert job["photo_ids"] == ["74"] and db.get_wishlist("3001")["download_status"] == "queued"
    assert download(job["job_id"], job["photo_ids"], insert=False) == "completed"
    row = _row()
    assert row["new_count"] == 0 and "74" in row["baseline_ids"]
    assert update_store.pending_new_chapters() == [] and _batch_photos(batch) == []
    assert db.get_wishlist("3001")["download_status"] == "completed"
    assert _check() == "no_update"


@pytest.mark.parametrize("outcome", ["failed", "canceled"])
def test_batch_job_failed_or_canceled_keeps_pending(client, download, upstream, checker, batch, outcome):
    confirmed = _pending_74(download, upstream, checker)
    job = _batch_confirm(batch, "new_chapters")
    assert download(job["job_id"], job["photo_ids"], outcome=outcome, written=1, insert=False) == outcome
    assert _new_ids() == ["74"] and _row()["new_chapters"][0]["confirmed_at"] == confirmed
    assert _batch_photos(batch) == [("3001", ["74"])]           # offered again: nothing was absorbed


def test_a2_job_completes_then_leaves_a2(client, download, upstream, checker, batch):
    from core import database as db
    db.add_wishlist("3001", "Name")
    assert _batch_photos(batch, "undownloaded_favourites") == [("3001", [])]
    job = _batch_confirm(batch, "undownloaded_favourites")
    assert download(job["job_id"], [], insert=False) == "completed"
    assert _row()["baseline_ids"] == ["71", "72", "73"]          # the first download sets the baseline
    assert _batch_photos(batch, "undownloaded_favourites") == []
    assert _batch_photos(batch) == []                            # not a new-chapters target before a check says so
    upstream.set("3001", ["71", "72", "73", "74"])
    assert _check() == "new"
    assert _batch_photos(batch) == [("3001", ["74"])]
    assert _batch_photos(batch, "undownloaded_favourites") == []


# ─── partial jobs write into the folder the reader opens (upstream renamed, organized, archive only) ───


def _dirs(root):
    """Every folder under root, relative, as posix paths (chapter folders included)."""
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_dir())


def _local_photo_pages(client, album_id="3001"):
    """What the reader shows: pages per chapter (photo_id) from GET /api/preview/<id>."""
    data = client.get(f"/api/preview/{album_id}").get_json()
    assert data["status"] == "ok", data
    pages = {}
    for page in data["pages"]:
        pages[page["photo_id"]] = pages.get(page["photo_id"], 0) + 1
    return pages


def test_new_chapter_job_after_an_upstream_rename_writes_into_the_readable_folder(
        client, download, downloads, upstream, checker, batch):
    from core import database as db, update_store
    _pending_74(download, upstream, checker)                  # 71 downloaded into Name_3001
    download.rename("Renamed")                                # the upstream title changed since
    job = _batch_confirm(batch, "new_chapters")
    assert download(job["job_id"], job["photo_ids"], insert=False) == "completed"
    assert _dirs(downloads) == ["Name_3001", "Name_3001/第1话__71", "Name_3001/第4话__74"]   # no Renamed_3001
    assert db.get_job(job["job_id"])["output_path"] == str(downloads / "Name_3001")
    assert _local_photo_pages(client) == {"71": 3, "74": 5}   # the reader shows the old and the new chapter
    assert update_store.pending_new_chapters() == [] and "74" in _row()["baseline_ids"]


def test_detail_page_selected_chapters_after_a_rename_join_the_readable_folder(client, download, downloads):
    from core import database as db
    assert download("ja", ["71"]) == "completed"
    download.rename("Renamed")
    assert download("jb", ["72"]) == "completed"              # POST /api/jobs from the detail page: the same path
    assert db.get_job("jb")["output_path"] == str(downloads / "Name_3001")
    assert _local_photo_pages(client) == {"71": 3, "72": 2}


def test_rename_with_archive_only_content_merges_into_one_archive(client, download, downloads, upstream, checker,
                                                                   batch):
    from core import archive_pages
    assert download("ja", ["71"], pack=True) == "completed"   # packed, the loose pages deleted: only the CBZ is left
    folder = downloads / "Name_3001"
    assert list(folder.rglob("*.webp")) == [] and (folder / "Name_3001.cbz").is_file()
    upstream.set("3001", ["71", "72", "73", "74"])
    assert _check() == "new"
    download.serve(CHAPTERS + [CH74])
    download.rename("Renamed")
    job = _batch_confirm(batch, "new_chapters")
    assert download(job["job_id"], job["photo_ids"], pack=True, insert=False) == "completed"
    assert not (downloads / "Renamed_3001").exists()
    archive_pages.clear_cache()
    index = archive_pages.read_index(folder / "Name_3001.cbz")
    assert index.status == "ok" and len(index.pages) == 3 + 5   # the old chapter kept, the new one added
    assert _local_photo_pages(client) == {"71": 3, "74": 5}


def test_organized_folder_gets_the_new_chapters_and_is_not_organized_again(client, download, downloads, upstream,
                                                                            checker, batch):
    from core import database as db
    assert download("ja", ["71"], organize="by_author") == "completed"
    folder = downloads / "someone" / "Name_3001"
    assert db.get_job("ja")["output_path"] == str(folder)
    upstream.set("3001", ["71", "72", "73", "74"])
    assert _check() == "new"
    download.serve(CHAPTERS + [CH74])
    download.rename("Renamed")
    job = _batch_confirm(batch, "new_chapters")
    assert download(job["job_id"], job["photo_ids"], insert=False, organize="by_author") == "completed"
    assert _dirs(downloads) == ["someone", "someone/Name_3001", "someone/Name_3001/第1话__71",
                                "someone/Name_3001/第4话__74"]            # no someone/someone/, nothing at the top
    assert db.get_job(job["job_id"])["output_path"] == str(folder)
    assert _local_photo_pages(client) == {"71": 3, "74": 5}


def test_whole_album_job_and_unreadable_folders_keep_the_upstream_name(client, download, downloads):
    import shutil
    from core import database as db
    assert download("ja", ["71"]) == "completed"
    download.rename("Renamed")
    assert download("jb", []) == "completed"                  # a whole-comic download: named after upstream as before
    assert db.get_job("jb")["output_path"] == str(downloads / "Renamed_3001")
    shutil.rmtree(downloads / "Renamed_3001")                 # the folder the reader opened is gone
    download.rename("Third")
    assert download("jc", ["72"]) == "completed"              # nothing readable to join: a new folder as before
    assert db.get_job("jc")["output_path"] == str(downloads / "Third_3001")


def test_detail_page_download_all_after_a_rename_joins_the_readable_folder(client, download, downloads):
    # the detail page's 下载全部 sends every chapter id (not []): it is a listed-chapters job
    from core import database as db
    assert download("ja", ["71"]) == "completed"
    download.rename("Renamed")
    assert download("jb", ["71", "72", "73"]) == "completed"
    assert db.get_job("jb")["output_path"] == str(downloads / "Name_3001")
    assert not (downloads / "Renamed_3001").exists()
    assert _local_photo_pages(client) == {"71": 3, "72": 2, "73": 4}      # no chapter twice


def test_flat_mode_still_flattens_a_joined_author_folder(client, download, downloads):
    from core import database as db
    assert download("ja", ["71"], organize="by_author") == "completed"
    folder = downloads / "someone" / "Name_3001"
    download.rename("Renamed")
    assert download("jb", ["72"], organize="flat") == "completed"         # the user switched to 扁平化 since
    assert db.get_job("jb")["output_path"] == str(folder)
    assert [p.name for p in folder.glob("第2话__72_*.webp")]                # flattened inside the joined folder
    assert sorted(p.name for p in downloads.iterdir()) == ["someone"]
    assert sorted(p.name for p in (downloads / "someone").iterdir()) == ["Name_3001"]   # no someone/someone/


def test_a_partial_job_never_joins_the_root_itself(client, download, downloads):
    from core import database as db
    other = downloads / "Other_4001" / "c1"
    other.mkdir(parents=True)
    (other / "00001.webp").write_bytes(b"page")                           # the root has pages somewhere below
    db.insert_job("j0", "3001", "Name", ["71"])                          # a hand-edited or legacy row
    db.update_job("j0", status="completed", output_path=str(downloads), completed_at=datetime.now().isoformat())
    _forget_local()
    assert download("jb", ["72"]) == "completed"
    assert db.get_job("jb")["output_path"] == str(downloads / "Name_3001")
    assert sorted(p.name for p in downloads.iterdir()) == ["Name_3001", "Other_4001"]   # nothing written into downloads/


def test_a_partial_job_never_joins_an_empty_shell(client, download, downloads):
    import shutil
    from core import database as db
    assert download("ja", ["71"]) == "completed"
    folder = downloads / "Name_3001"
    for child in folder.iterdir():
        shutil.rmtree(child)                                              # the folder is left, its pages are gone
    _forget_local()
    download.rename("Renamed")
    assert download("jb", ["72"]) == "completed"
    assert db.get_job("jb")["output_path"] == str(downloads / "Renamed_3001")


def test_a_partial_job_never_joins_a_superseded_folder(client, download, downloads):
    import shutil
    from core import database as db
    assert download("ja", ["71"]) == "completed"
    shutil.rmtree(downloads / "Name_3001")
    assert download("jf", ["71"], outcome="failed") == "failed"          # recreates Name_3001: ja is superseded
    assert db.get_job("ja")["superseded_at"]
    download.rename("Renamed")
    assert download("jb", ["72"]) == "completed"
    assert db.get_job("jb")["output_path"] == str(downloads / "Renamed_3001")
