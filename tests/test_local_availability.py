"""Shared "readable offline" rule used by search, detail, downloads, wishlist and library,
and POST /api/jobs/<id>/open-folder, which must not recreate the folder of a finished download."""
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def downloads(client, tmp_path, monkeypatch):
    from core import path_guard
    from routes import api_preview
    root = tmp_path / "downloads"
    root.mkdir()
    monkeypatch.setattr(path_guard, "DOWNLOAD_ROOT", root)
    monkeypatch.setattr(api_preview, "DOWNLOAD_ROOT", root)  # the reader builds page URLs relative to it
    return root


def _job(job_id, album_id, status, output_path):
    from core import database as db
    db.insert_job(job_id, album_id, "t", [])
    db.update_job(job_id, status=status, output_path=str(output_path) if output_path else None)


def _album(folder, *pages):
    """A downloaded album folder; pages are relative file names (default: one page in the folder itself)."""
    folder.mkdir(parents=True, exist_ok=True)
    for page in pages or ("001.jpg",):
        (folder / page).parent.mkdir(parents=True, exist_ok=True)
        (folder / page).write_bytes(b"page")
    return folder


def test_completed_job_with_a_page_is_readable(downloads):
    from core.local_availability import readable_album_ids, is_readable
    _album(downloads / "A")
    _job("j1", "111", "completed", downloads / "A")
    _job("j2", "222", "running", downloads / "A")          # not finished
    _job("j3", "333", "completed", downloads / "missing")   # folder deleted
    _job("j4", "444", "failed", downloads / "A")
    assert readable_album_ids(["111", "222", "333", "444", "555"]) == {"111"}
    assert is_readable("111") and not is_readable("333")


def test_latest_completed_job_decides_like_the_local_reader(downloads, tmp_path):
    import time
    from core.local_availability import readable_album_ids
    _album(downloads / "old")
    _job("old", "777", "completed", downloads / "old")
    time.sleep(0.01)
    _job("new", "777", "completed", downloads / "gone")  # latest completed job lost its folder
    assert readable_album_ids(["777"]) == set()


def test_folders_outside_downloads_and_bad_ids_are_ignored(downloads, tmp_path):
    from core.local_availability import readable_album_ids, MAX_IDS
    _album(tmp_path / "elsewhere")
    _job("x", "888", "completed", tmp_path / "elsewhere")
    assert readable_album_ids(["888", "abc", "", "1 OR 1=1"]) == set()
    assert readable_album_ids([]) == set()
    _album(downloads / "B")
    _job("y", "999", "completed", downloads / "B")
    many = [str(n) for n in range(100000, 100000 + MAX_IDS)] + ["999"]
    assert "999" not in readable_album_ids(many)  # beyond the cap: not checked
    assert readable_album_ids(["999", "999"]) == {"999"}


# ─── "Readable" means there is at least one page the reader can show ───


def test_a_folder_without_pages_is_not_readable(client, downloads):
    # open-folder used to recreate a deleted download as an empty folder; every page then offered 阅读 for nothing
    from core.local_availability import readable_album_ids
    (downloads / "empty").mkdir()
    _job("e", "501", "completed", downloads / "empty")
    _album(downloads / "no-pages", "album.cbz", "info.json", "cover.jpg.txt", "sub/readme.md")
    _job("n", "502", "completed", downloads / "no-pages")
    (downloads / "empty-chapters" / "第1话").mkdir(parents=True)
    _job("c", "503", "completed", downloads / "empty-chapters")
    assert readable_album_ids(["501", "502", "503"]) == set()
    assert client.post("/api/preview/available", json={"album_ids": ["501", "502", "503"]}).get_json()["readable"] == []
    for album_id in ("501", "502", "503"):  # the reader agrees: nothing to show
        assert client.get(f"/api/preview/{album_id}").status_code == 404


@pytest.mark.parametrize("page", ["001.jpg", "001.JPEG", "p.png", "p.webp", "p.gif", "第1话/001.jpg", "a/b/c/001.png"])
def test_any_reader_image_anywhere_in_the_tree_is_enough(client, downloads, page):
    from core.local_availability import readable_album_ids
    _album(downloads / "album", "notes.txt", page)
    _job("j", "601", "completed", downloads / "album")
    assert readable_album_ids(["601"]) == {"601"}
    data = client.get("/api/preview/601").get_json()
    assert data["status"] == "ok" and data["total_pages"] == 1  # the reader shows exactly that page


def test_non_reader_image_types_do_not_count(downloads):
    from core.local_availability import readable_album_ids
    from core.validation import ALLOWED_EXTENSIONS
    _album(downloads / "album", "001.bmp", "002.avif", "003.tif")  # exportable, but the reader skips them
    _job("j", "602", "completed", downloads / "album")
    assert not {".bmp", ".avif", ".tif"} & ALLOWED_EXTENSIONS
    assert readable_album_ids(["602"]) == set()


@pytest.mark.skipif(os.name != "nt", reason="Windows directory junction")
def test_pages_reachable_only_through_a_link_do_not_count(client, downloads):
    import _winapi
    from core.local_availability import readable_album_ids
    target = _album(downloads / "real")            # a real album, inside the download folder
    _album(downloads / "linked")
    os.remove(downloads / "linked" / "001.jpg")
    _winapi.CreateJunction(str(target), str(downloads / "linked" / "chapter"))
    _winapi.CreateJunction(str(target), str(downloads / "root-link"))
    try:
        _job("l", "701", "completed", downloads / "linked")      # only a junction inside
        _job("r", "702", "completed", downloads / "root-link")   # the album folder itself is a junction
        _job("t", "703", "completed", target)
        assert readable_album_ids(["701", "702", "703"]) == {"703"}
        assert client.get("/api/preview/701").status_code == 404  # the reader does not follow it either
    finally:
        os.rmdir(downloads / "linked" / "chapter")
        os.rmdir(downloads / "root-link")
    assert (target / "001.jpg").is_file()


def test_page_check_stops_at_the_first_page(downloads, monkeypatch):
    # cheap enough for 200 albums per request: two folder listings, no stat of every chapter folder or page
    from core.local_availability import has_page_image
    album = downloads / "big"
    for chapter in range(40):
        _album(album / f"{chapter:03d}", *[f"{n:03d}.jpg" for n in range(30)])
    counts = {"scandir": 0, "stat": 0, "lstat": 0}

    def counting(name):
        original = getattr(os, name)

        def wrapper(*args, **kwargs):
            counts[name] += 1
            return original(*args, **kwargs)
        return wrapper
    with monkeypatch.context() as patch:
        for name in counts:
            patch.setattr(os, name, counting(name))
        found = has_page_image(album)
    assert found
    assert counts["scandir"] == 2  # the album folder and its first chapter, not all 40 chapters
    assert counts["stat"] + counts["lstat"] <= 1  # only the album folder itself; entries come from the listing
    assert not has_page_image(downloads / "does-not-exist")


def test_safe_files_is_unchanged(downloads):
    from core.file_tree import safe_files
    album = _album(downloads / "album", "10.jpg", "2.jpg", "sub/1.jpg", "notes.txt")
    assert [p.relative_to(album).as_posix() for p in safe_files(album)] == ["2.jpg", "10.jpg", "notes.txt", "sub/1.jpg"]


# ─── POST /api/jobs/<id>/open-folder ───


@pytest.fixture
def startfile(monkeypatch):
    calls = []
    monkeypatch.setattr(os, "startfile", lambda path, *a, **k: calls.append(str(path)), raising=False)
    return calls


@pytest.mark.parametrize("status", ["completed", "failed", "canceled"])
def test_open_folder_does_not_recreate_a_finished_download(client, downloads, startfile, status):
    folder = downloads / "deleted-album"
    _job("job_gone", "801", status, folder)
    response = client.post("/api/jobs/job_gone/open-folder")
    assert response.status_code == 404
    assert "下载文件夹已不存在" in response.get_json()["message"]
    assert not folder.exists() and startfile == []


def test_open_folder_on_a_deleted_download_leaves_it_unreadable(client, downloads, startfile):
    from core.local_availability import readable_album_ids
    folder = _album(downloads / "album")
    _job("job_done", "802", "completed", folder)
    assert readable_album_ids(["802"]) == {"802"}
    for page in folder.iterdir():
        page.unlink()
    folder.rmdir()                                   # the user deletes the download
    client.post("/api/jobs/job_done/open-folder")
    assert not folder.exists()
    assert readable_album_ids(["802"]) == set()
    assert client.get("/api/library?status=readable").get_json()["total"] == 0


@pytest.mark.parametrize("status", ["queued", "running", "paused"])
def test_open_folder_still_creates_the_folder_of_an_unfinished_job(client, downloads, startfile, status):
    # a retried job reuses the old output path before the download recreates it: opening it still works
    folder = downloads / "not-yet"
    _job("job_active", "803", status, folder)
    response = client.post("/api/jobs/job_active/open-folder")
    assert response.status_code == 200 and response.get_json()["status"] == "ok"
    assert folder.is_dir() and startfile == [str(folder)]


def test_open_folder_opens_an_existing_folder(client, downloads, startfile):
    folder = _album(downloads / "album")
    _job("job_ok", "804", "completed", folder)
    assert client.post("/api/jobs/job_ok/open-folder").status_code == 200
    assert startfile == [str(folder)]


def test_open_folder_keeps_its_other_checks(client, downloads, startfile, tmp_path):
    _job("job_nopath", "805", "completed", None)
    assert client.post("/api/jobs/job_nopath/open-folder").status_code == 400
    _job("job_outside", "806", "completed", tmp_path / "outside")
    assert client.post("/api/jobs/job_outside/open-folder").status_code == 403
    assert not (tmp_path / "outside").exists()
    assert client.post("/api/jobs/job_missing/open-folder").status_code == 404
    assert startfile == []


def test_open_folder_errors_reach_the_user():
    # the downloads page and the home page both go through window.openFolder: the server's message is shown
    source = (ROOT / "static" / "js" / "utils.js").read_text(encoding="utf-8")
    body = source[source.index("window.openFolder = function"):]
    body = body[:body.index("\n};")]
    assert re.search(r"showToast\('打开文件夹失败: ' \+ err\.message, 'danger'\)", body)


# ─── POST /api/jobs/<id>/retry: 重新下载 on a finished card ───


def test_retry_redownloads_a_completed_job_whose_folder_is_gone(client, downloads, startfile):
    # open-folder tells the user the folder is gone and "可以重新下载"; the same card's 重新下载 answered 404 原任务不存在
    import json
    from core import database as db
    db.add_wishlist("901", "Title", "", "")
    db.insert_job("job_done", "901", "Title", ["11", "12"])
    db.update_job("job_done", status="completed", output_path=str(downloads / "gone"))
    assert client.post("/api/jobs/job_done/open-folder").status_code == 404
    response = client.post("/api/jobs/job_done/retry")
    assert response.status_code == 201 and response.get_json()["status"] == "ok"
    new = db.get_job(response.get_json()["job_id"])
    assert (new["album_id"], new["title"], json.loads(new["selected_photo_ids"]), new["status"]) == (
        "901", "Title", ["11", "12"], "queued")
    assert new["output_path"] is None  # no deleted path: opening the queued job must not recreate an empty folder
    assert db.get_job("job_done")["status"] == "completed"  # the old record is kept, like failed/canceled retries
    assert db.get_wishlist("901")["download_status"] == "queued"
    assert not (downloads / "gone").exists() and startfile == []


def test_retry_of_a_completed_job_keeps_its_existing_folder(client, downloads):
    from core import database as db
    folder = _album(downloads / "album")
    _job("job_done", "902", "completed", folder)
    response = client.post("/api/jobs/job_done/retry")
    assert response.status_code == 201
    assert db.get_job(response.get_json()["job_id"])["output_path"] == str(folder)


@pytest.mark.parametrize("status", ["queued", "running", "paused"])
def test_retry_refuses_unfinished_jobs_with_a_truthful_message(client, downloads, status):
    from core import database as db
    _job("job_live", "903", status, None)
    response = client.post("/api/jobs/job_live/retry")
    assert response.status_code == 400 and response.get_json()["message"] == "该任务状态不能重试"
    assert len(db.get_all_jobs()) == 1
    missing = client.post("/api/jobs/job_nope/retry")
    assert missing.status_code == 404 and missing.get_json()["message"] == "原任务不存在"


# ─── A re-download into the folder of a deleted download ───
# jm_service always downloads into DOWNLOAD_ROOT/<name>_<album_id>. When that folder was deleted, a re-download
# recreates it; if the re-download then fails or is canceled, its partial pages must not make the old completed
# download count as complete again (they did: 可离线阅读, the reader opened the partial pages, 失败 hid the failure).


class _Photo(list):
    def __init__(self, photo_id, pages):
        super().__init__(range(pages))
        self.photo_id = photo_id
        self.name = "第1话"
        self.page_arr = [f"{n:05d}.webp" for n in range(1, pages + 1)]


class _Album(list):
    name = "Name"
    author = "someone"
    tags = []


class _Client:
    def __init__(self, album):
        self.album = album

    def get_album_detail(self, album_id):
        return self.album

    def check_photo(self, photo):
        pass


@pytest.fixture
def download(client, downloads, monkeypatch):
    """Runs the real download_album_job for a two-page album. Only the chapter download is replaced: it writes
    `pages` pages into the real chapter folder, calls `during()` while the job is still running, then ends the job
    as `outcome` says (completed / failed / canceled)."""
    from core import database as db, jm_service
    from core.progress import progress_manager
    monkeypatch.setattr(jm_service, "DOWNLOAD_ROOT", downloads)
    monkeypatch.setattr(jm_service, "close_client", lambda _client: None)

    def run(job_id, album_id, pages, outcome="completed", during=None):
        album = _Album([_Photo("7" + album_id, 2)])
        monkeypatch.setattr(jm_service, "get_client", lambda shared=True: (_Client(album), None))

        def chapter(job_id, album_id, album, album_dir, photo, total_pages, done_pages, pending_images,
                    failed_pages, *rest):
            folder = jm_service._chapter_output_dir(album_dir, photo)
            for n in range(1, pages + 1):
                (folder / f"{n:05d}.webp").write_bytes(b"page")
                done_pages[0] += 1
            if during:
                during()
            if outcome == "failed":
                failed_pages.append("第 2 页下载失败")
            elif outcome == "canceled":
                db.transition_job_status(job_id, ["running"], "canceled")
        monkeypatch.setattr(jm_service, "_download_chapter", chapter)
        if not db.get_job(job_id):
            db.insert_job(job_id, album_id, "Name", [])
        db.update_job(job_id, status="running")
        progress_manager.create_tracker(job_id)
        try:
            jm_service.download_album_job(job_id, album_id, [])
        finally:
            progress_manager.remove_tracker(job_id)
        return db.get_job(job_id)
    return run


def _library_item(client, album_id):
    return {i["album_id"]: i for i in client.get("/api/library").get_json()["items"]}[album_id]


def _buckets(client, album_id):
    from core.database import LIBRARY_STATUS_FILTERS
    return [status for status in LIBRARY_STATUS_FILTERS
            if album_id in [i["album_id"] for i in
                            client.get("/api/library", query_string={"status": status}).get_json()["items"]]]


@pytest.mark.parametrize("outcome, group, bucket", [("failed", "failed", "failed"), ("canceled", "none", "missing")])
def test_a_failed_redownload_into_a_deleted_folder_is_not_readable(client, downloads, download, outcome, group, bucket):
    import shutil
    from core import database as db
    from core.local_availability import readable_album_ids
    db.add_wishlist("2001", "Name", "someone", "")
    first = download("job_a", "2001", pages=2)
    folder = Path(first["output_path"])
    assert first["status"] == "completed" and readable_album_ids(["2001"]) == {"2001"}
    shutil.rmtree(folder)  # the user deletes the download
    assert readable_album_ids(["2001"]) == set() and _buckets(client, "2001") == ["missing"]
    seen = []
    second = download("job_b", "2001", pages=1, outcome=outcome, during=lambda: seen.append(
        (_library_item(client, "2001")["status_group"], readable_album_ids(["2001"]))))
    assert second["status"] == outcome and Path(second["output_path"]) == folder  # the same folder, recreated
    assert [p.name for p in folder.rglob("*.webp")] == ["00001.webp"]  # one page of two on disk again
    assert seen == [("active", set())]  # while it downloads: 排队中 / 下载中, not 可离线阅读
    assert readable_album_ids(["2001"]) == set()
    item = _library_item(client, "2001")
    assert (item["status_group"], item["readable"], item["files_missing"]) == (group, False, outcome == "canceled")
    assert _buckets(client, "2001") == [bucket]  # a failed re-download is listed under 失败 again
    favourite = client.get("/api/wishlist").get_json()["items"][0]
    assert (favourite["status_group"], favourite["readable"]) == (group, False)
    assert client.post("/api/preview/available", json={"album_ids": ["2001"]}).get_json()["readable"] == []
    assert client.get("/api/preview/2001").status_code == 404  # the reader agrees: no partial pages as the old download
    exported = client.post("/api/export/job_a/zip")
    assert exported.status_code == 404 and "重新下载" in exported.get_json()["message"]  # nor exported as it
    exported.close()


def test_existing_databases_get_the_superseded_column(client, tmp_path, monkeypatch):
    import sqlite3
    from core import database as db
    from core.local_availability import readable_album_ids
    old = tmp_path / "old-install"
    old.mkdir()
    conn = sqlite3.connect(old / "app.db")
    conn.execute("""CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT UNIQUE NOT NULL,
        album_id TEXT NOT NULL, title TEXT, selected_photo_ids TEXT, status TEXT NOT NULL DEFAULT 'queued',
        total_pages INTEGER DEFAULT 0, done_pages INTEGER DEFAULT 0, current_photo TEXT, current_image TEXT,
        output_path TEXT, error_message TEXT, created_at TEXT, updated_at TEXT, completed_at TEXT)""")
    conn.execute("INSERT INTO jobs (job_id, album_id, title, status, created_at) VALUES ('old', '1', 't', 'completed', 'x')")
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()
    monkeypatch.setattr(db, "DB_DIR", old)
    monkeypatch.setattr(db, "DB_PATH", old / "app.db")
    db.init_db()
    db.init_db()  # idempotent
    assert db.get_job("old")["superseded_at"] is None and db.get_job("old")["status"] == "completed"
    assert readable_album_ids(["1"]) == set()  # no folder: queried without errors


def test_a_folder_recreated_by_open_folder_before_the_redownload_counts_too(client, downloads, download, startfile):
    # a retried failed job reuses the old path; opening its folder while it is queued recreates the folder empty
    import shutil
    from core.local_availability import readable_album_ids
    folder = Path(download("job_a", "2001", pages=2)["output_path"])
    shutil.rmtree(folder)
    _job("job_b", "2001", "queued", folder)
    assert client.post("/api/jobs/job_b/open-folder").status_code == 200 and folder.is_dir()
    assert download("job_b", "2001", pages=1, outcome="failed")["status"] == "failed"
    assert readable_album_ids(["2001"]) == set() and _buckets(client, "2001") == ["failed"]


def test_a_failed_update_of_an_intact_download_keeps_it_readable(client, downloads, download):
    # the first download's pages never went away: a later failed job (more chapters, an update) does not hide them
    from core.local_availability import readable_album_ids
    download("job_a", "2001", pages=2)
    assert download("job_b", "2001", pages=1, outcome="failed")["status"] == "failed"
    assert readable_album_ids(["2001"]) == {"2001"} and _buckets(client, "2001") == ["readable"]
    assert client.get("/api/preview/2001").get_json()["total_pages"] == 2


def test_a_completed_redownload_is_readable_again(client, downloads, download):
    import shutil
    from core.local_availability import readable_album_ids
    shutil.rmtree(download("job_a", "2001", pages=2)["output_path"])
    assert download("job_b", "2001", pages=1, outcome="failed")["status"] == "failed"
    assert readable_album_ids(["2001"]) == set()
    assert download("job_c", "2001", pages=2)["status"] == "completed"
    assert readable_album_ids(["2001"]) == {"2001"} and _buckets(client, "2001") == ["readable"]
    assert client.get("/api/preview/2001").get_json()["total_pages"] == 2
    exported = client.post("/api/export/job_c/zip")
    assert exported.status_code == 200
    exported.close()  # removes the temporary ZIP


# ─── Whole-library checks reuse earlier answers for unchanged folders ───


def test_list_requests_do_not_walk_every_download_folder_again(client, downloads, monkeypatch):
    # the favourites list, the library status filters and the stats judge the whole library on every request
    # (page change, sort, filter, debounced search): every downloaded folder was resolved and listed each time
    import shutil
    from core import database as db
    for n in range(30):
        album_id = str(4000 + n)
        _job(f"j{n}", album_id, "completed", _album(downloads / f"A{n}", "第1话/001.jpg"))
        db.add_wishlist(album_id, f"T{n}", "", "")

    def readable_counts():
        return (client.get("/api/wishlist?page_size=10").get_json()["group_counts"]["readable"],
                client.get("/api/library?status=readable&page_size=10").get_json()["total"],
                client.get("/api/library/stats").get_json()["readable_count"])
    assert readable_counts() == (30, 30, 30)
    calls = {"scandir": 0, "realpath": 0}

    def counting(name, original):
        def wrapper(*args, **kwargs):
            calls[name] += 1
            return original(*args, **kwargs)
        return wrapper
    with monkeypatch.context() as patch:
        patch.setattr(os, "scandir", counting("scandir", os.scandir))
        patch.setattr(os.path, "realpath", counting("realpath", os.path.realpath))
        assert readable_counts() == (30, 30, 30)
    assert calls == {"scandir": 0, "realpath": 0}  # unchanged folders: nothing listed or resolved again
    # still exact: a deleted folder, or one whose pages were deleted, stops counting at once
    shutil.rmtree(downloads / "A0")
    (downloads / "A1" / "第1话" / "001.jpg").unlink()
    assert readable_counts() == (28, 28, 28)
    (downloads / "A1" / "第1话" / "002.jpg").write_bytes(b"page")  # a new job completing re-checks its folder
    _job("j1b", "4001", "completed", downloads / "A1")
    assert readable_counts() == (29, 29, 29)


def test_library_file_exists_agrees_with_files_missing(client, downloads):
    # an empty folder of a completed download was reported as file_exists: true next to files_missing: true
    (downloads / "empty").mkdir()
    _job("e1", "9001", "completed", downloads / "empty")
    _job("r1", "9002", "completed", _album(downloads / "full"))
    _job("g1", "9003", "completed", downloads / "gone")
    items = {i["album_id"]: i for i in client.get("/api/library").get_json()["items"]}
    fields = ("file_exists", "readable", "files_missing")
    assert [items["9001"][k] for k in fields] == [False, False, True]
    assert [items["9002"][k] for k in fields] == [True, True, False]
    assert [items["9003"][k] for k in fields] == [False, False, True]
    assert client.get("/api/library/9001").get_json()["item"]["file_exists"] is False
