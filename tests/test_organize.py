"""整理下载目录（organize_mode）：按作者整理只把整个目录改名（被占用时短暂重试，从不复制再删除）；扁平化只改名不复制，
页名 <章节前缀>_p<页号>，永远不会和旧版本按累加序号起的页名（<章节前缀>_NNNNN）重名；skip_existing 认得已经扁平化的页
（新页名的散图和压缩包里的页；旧页名只按整章认：正好是这一章的页数时），旧文件从不改名、覆盖或删除。

Everything runs the REAL download_album_job → _download_chapter → _download_image_single_attempt → organize_download
(→ auto-pack) in pytest's tmp folders; only the client is fake (its download_image writes a small valid WebP and
records the page URL). No network, never the real downloads/ or runtime/. A "locked" file or folder is simulated by
making os.rename / os.replace raise the Windows sharing violation for that one path; every other call goes to the
real function."""
import errno
import json
import os
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from PIL import Image

# the album the fake client serves: chapter 73's name has a space (its flattened pages are named 第3话_完__73_…)
CHAPTERS = [("71", "第1话", 3), ("72", "第2话", 2), ("73", "第3话 完", 4)]
ALL_IDS = [photo_id for photo_id, _, _ in CHAPTERS]
TOTAL = sum(pages for _, _, pages in CHAPTERS)
MARKER = ".jm-chapter.json"
ARCHIVE = "Name_3001.cbz"


# ─── fakes ────────────────────────────────────────────────────────────────────


class _Page:
    def __init__(self, photo_id, n, ext):
        self.download_url = f"https://cdn.invalid/media/photos/{photo_id}/{n:05d}.{ext}"
        self.scramble_id = None


class _Photo(list):
    def __init__(self, photo_id, name, pages, gif_pages=()):
        super().__init__(_Page(photo_id, n, "gif" if n in gif_pages else "webp") for n in range(1, pages + 1))
        self.photo_id = photo_id
        self.name = name
        self.page_arr = [f"{n:05d}.webp" for n in range(1, pages + 1)]


class _Album(list):
    name = "Name"
    author = "someone"
    tags = []


class FakeClient:
    """Serves the album (gif = {(photo_id, page number)} served as GIF pages); download_image writes a valid WebP
    whose colour encodes the run and the page, and records the page (photo_id/NNNNN.ext) of every fetch.
    Pages in `fail` fail every time they are fetched; pages in `same` ({(photo_id, page number): bytes}) are written
    as exactly those bytes (upstream serves the page byte for byte as it was saved before)."""

    def __init__(self):
        self.fetched = []
        self.run = 0
        self.gif = set()
        self.pages = {}  # photo_id → page count upstream lists now (default: CHAPTERS)
        self.fail = set()
        self.same = {}

    def get_album_detail(self, album_id):
        return _Album(_Photo(pid, name, self.pages.get(pid, pages), {n for p, n in self.gif if p == pid})
                      for pid, name, pages in CHAPTERS)

    def check_photo(self, photo):
        pass

    def download_image(self, img_url, img_save_path, scramble_id=None, decode_image=True):
        page = "/".join(img_url.rsplit("/", 2)[1:])
        self.fetched.append(page)
        if page in self.fail:
            raise OSError("network failed")
        n = int(page.split("/")[1].split(".")[0])
        if (page.split("/")[0], n) in self.same:
            Path(img_save_path).write_bytes(self.same[(page.split("/")[0], n)])
            return
        Image.new("RGB", (8, 8), (self.run * 60 % 256, n * 20, 90)).save(img_save_path, "WEBP")


class Lock:
    """os.rename / os.replace raise the Windows sharing violation (WinError 32) while `path` is the source
    (on="src") or the destination (on="dst"); times=None: every time, else only the first `times` calls.
    attempts records every call on `path`; release() lets the other program go."""

    def __init__(self, monkeypatch, function, path, on="src", times=None):
        self.real = getattr(os, function)
        self.path, self.on, self.times = Path(path), on, times
        self.attempts = []
        self.held = True
        monkeypatch.setattr(os, function, self)

    def __call__(self, src, dst, *args, **kwargs):
        if self.held and Path(src if self.on == "src" else dst) == self.path:
            self.attempts.append((str(src), str(dst)))
            if self.times is None or len(self.attempts) <= self.times:
                raise PermissionError(13, "The process cannot access the file because it is being used by "
                                          "another process", str(src), 32)
        return self.real(src, dst, *args, **kwargs)

    def release(self):
        self.held = False


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
    """Every test here runs on a tmp database and a tmp downloads folder."""
    from core import database as db, path_guard
    assert Path(db.DB_PATH).resolve().is_relative_to(tmp_path.resolve())
    assert Path(path_guard.DOWNLOAD_ROOT).resolve().is_relative_to(tmp_path.resolve())
    yield


@pytest.fixture
def no_copies(monkeypatch):
    """A move must never fall back to copy + delete (shutil.move does when a rename fails): any copy is recorded
    and fails the test at once — pytest.fail is a BaseException, so nothing in the job can swallow it."""
    copies = []

    def refuse(name):
        def copy(src, dst, *args, **kwargs):
            copies.append((name, str(src), str(dst)))
            pytest.fail(f"shutil.{name}({src}, {dst}): a move fell back to copying", pytrace=False)
        return copy
    for name in ("copytree", "copy2", "copyfile", "copy"):
        monkeypatch.setattr(shutil, name, refuse(name))
    yield copies
    assert copies == []


@pytest.fixture
def download(client, downloads, monkeypatch, no_copies):
    """download(photo_ids, organize=..., skip_existing=..., auto_pack=..., delete_originals=...) runs one real job
    for album 3001 and returns its row; download.fake.fetched lists the pages that run fetched and download.events
    the progress events it pushed. Lock retries do not sleep here."""
    from core import database as db, jm_service
    from core.progress import progress_manager
    from core.settings import update_settings
    fake = FakeClient()
    monkeypatch.setattr(jm_service, "get_client", lambda shared=True: (fake, None))
    monkeypatch.setattr(jm_service, "close_client", lambda _client: None)
    monkeypatch.setattr(jm_service, "_LOCK_RETRY_DELAYS", (0,) * len(jm_service._LOCK_RETRY_DELAYS))

    def run(photo_ids, organize="none", skip_existing=True, auto_pack=False, delete_originals=False):
        update_settings({"organize_mode": organize, "skip_existing": skip_existing, "auto_pack": auto_pack,
                         "delete_originals": delete_originals})
        fake.run += 1
        fake.fetched = []
        job_id = f"job_{fake.run:04d}"
        db.insert_job(job_id, "3001", "Name", photo_ids)
        db.update_job(job_id, status="running")
        events = progress_manager.create_tracker(job_id).subscribe()
        try:
            jm_service.download_album_job(job_id, "3001", photo_ids)
        finally:
            progress_manager.remove_tracker(job_id)
        run.events = []
        while not events.empty():
            run.events.append(events.get_nowait())
        _forget_local()
        return db.get_job(job_id)
    run.fake = fake
    run.events = []
    return run


# ─── helpers ──────────────────────────────────────────────────────────────────


def _chapters(pages=None):
    """CHAPTERS with some page counts changed ({photo_id: pages})."""
    pages = pages or {}
    return [(pid, name, pages.get(pid, count)) for pid, name, count in CHAPTERS]


def _chapter_dirs():
    return [f"{name}__{pid}" for pid, name, _ in CHAPTERS]


def _stable_names(pages=None):
    """Every page's flattened name: <chapter folder, spaces → _>_p<its own page number>.webp."""
    return sorted(f"{name.replace(' ', '_')}__{pid}_p{n:05d}.webp" for pid, name, count in _chapters(pages)
                  for n in range(1, count + 1))


def _root_files(folder):
    return sorted(p.name for p in folder.iterdir() if p.is_file())


def _chapter_contents(folder):
    return {d.name: sorted(p.name for p in d.iterdir()) for d in folder.iterdir() if d.is_dir()}


def _snapshot(folder):
    """{relative path: bytes} of every file under folder (chapter markers included)."""
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in folder.rglob("*") if p.is_file()}


def _pages_only(snapshot):
    return {name: data for name, data in snapshot.items() if not name.endswith(MARKER)}


def _reader(client, album_id="3001"):
    data = client.get(f"/api/preview/{album_id}").get_json()
    assert data["status"] == "ok", data
    return data


def _reader_pages(client, album_id="3001"):
    """What the local reader shows: GET /api/preview/<id> → total pages."""
    return _reader(client, album_id)["total_pages"]


def _reader_names(client):
    """The reader's pages in order, as paths relative to the album folder (loose) or archive entry names."""
    names = []
    for page in _reader(client)["pages"]:
        url = page["url"]
        if url.startswith("/api/preview-img/"):
            names.append(unquote(url[len("/api/preview-img/"):]).split("/", 1)[1])
        else:
            names.append(parse_qs(urlsplit(url).query)["p"][0])
    return names


def _valid(path):
    from core.jm_service import _valid_image
    return _valid_image(path)


def _image(path, colour=(10, 200, 30)):
    Image.new("RGB", (8, 8), colour).save(path, "WEBP")


def _legacy_flat_album(downloads, marker_format=2, pages=None):
    """A folder flattened by earlier versions: ONE counter ran over all chapter folders of the run (sorted), so the
    second chapter's pages are 第2话__72_00004… — not its page numbers. Chapter folders stay (their marker), empty
    otherwise. A completed job points at the folder (what the reader opens and a listed-chapters job joins)."""
    from core import database as db
    folder = downloads / "Name_3001"
    counter = 0
    for pid, name, count in sorted(_chapters(pages), key=lambda c: f"{c[1]}__{c[0]}"):
        chapter = folder / f"{name}__{pid}"
        chapter.mkdir(parents=True)
        record = {"photo_id": pid, "format": 2} if marker_format == 2 else {"photo_id": pid}
        (chapter / MARKER).write_text(json.dumps(record), encoding="utf-8")
        for n in range(1, count + 1):
            counter += 1
            _image(folder / f"{name.replace(' ', '_')}__{pid}_{counter:05d}.webp", (n * 20, counter * 10, 30))
    db.insert_job("job_legacy", "3001", "Name", ALL_IDS)
    db.update_job("job_legacy", status="completed", output_path=str(folder), completed_at=datetime.now().isoformat())
    _forget_local()
    return folder


def _legacy_names(pages=None):
    """{(photo_id, page number): the counter name _legacy_flat_album gave that page}."""
    names, counter = {}, 0
    for pid, name, count in sorted(_chapters(pages), key=lambda c: f"{c[1]}__{c[0]}"):
        for n in range(1, count + 1):
            counter += 1
            names[(pid, n)] = f"{name.replace(' ', '_')}__{pid}_{counter:05d}.webp"
    return names


def _serve_legacy_bytes(download, folder, pages=None):
    """From now on upstream serves every page byte for byte as the earlier version saved it (a plain re-download)."""
    download.fake.same = {key: (folder / name).read_bytes() for key, name in _legacy_names(pages).items()}
    return dict(download.fake.same)


def _pack(folder, delete=True):
    """What auto-pack (+ delete_originals) left behind: <folder>.cbz holding the pages, the loose images gone."""
    from core.packer import CbzPacker
    CbzPacker().pack(folder, folder / f"{folder.name}.cbz")
    if delete:
        for path in folder.rglob("*.webp"):
            path.unlink()
    _forget_local()


def _archive_names(folder):
    with zipfile.ZipFile(folder / f"{folder.name}.cbz") as archive:
        return sorted(name for name in archive.namelist() if name.endswith(".webp"))


def _archive_pages(folder):
    """{entry name: bytes} of the images in the comic's archive."""
    with zipfile.ZipFile(folder / ARCHIVE) as archive:
        return {name: archive.read(name) for name in archive.namelist() if not name.endswith(".xml")}


def _valid_bytes(data):
    from io import BytesIO
    try:
        with Image.open(BytesIO(data)) as image:
            image.verify()
        return True
    except Exception:
        return False


def _corrupt_archive_entry(folder, name):
    """Flip one byte in the middle of an entry's compressed data (bit rot): the archive still opens and its first
    page reads fine, but reading this entry fails its CRC check."""
    import struct
    path = folder / ARCHIVE
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo(name)
    data = bytearray(path.read_bytes())
    name_len, extra_len = struct.unpack("<HH", data[info.header_offset + 26:info.header_offset + 30])
    data[info.header_offset + 30 + name_len + extra_len + info.compress_size // 2] ^= 0xFF
    path.write_bytes(bytes(data))
    _forget_local()


# ─── A. by_author: one directory rename, never copy + delete ──────────────────


def test_by_author_rename_locked_every_time_keeps_the_complete_folder(client, download, downloads, monkeypatch):
    """A file in the folder stays open (antivirus, Explorer preview, an image viewer): the rename is retried briefly,
    then the organize step gives up. The job still completes and points at the complete folder where it is; no copy
    is made (shutil.move copied, then stopped deleting at the open file: a gutted folder the job pointed at and a
    full copy no job pointed at) and the author folder this call created is removed again."""
    from core import jm_service
    folder = downloads / "Name_3001"
    lock = Lock(monkeypatch, "rename", folder)
    job = download(ALL_IDS, organize="by_author")
    assert job["status"] == "completed", job["error_message"]
    assert job["output_path"] == str(folder)
    assert len(lock.attempts) == len(jm_service._LOCK_RETRY_DELAYS) + 1        # retried, then gave up
    assert sorted(p.name for p in downloads.iterdir()) == ["Name_3001"]        # no copy, no empty someone/
    assert sorted(_chapter_contents(folder)) == sorted(_chapter_dirs())
    assert _reader_pages(client) == TOTAL


def test_by_author_rename_locked_twice_then_moves_the_folder(client, download, downloads, monkeypatch):
    folder = downloads / "Name_3001"
    lock = Lock(monkeypatch, "rename", folder, times=2)
    job = download(ALL_IDS, organize="by_author")
    assert job["status"] == "completed", job["error_message"]
    assert len(lock.attempts) == 3                                              # two refusals, then the rename
    moved = downloads / "someone" / "Name_3001"
    assert job["output_path"] == str(moved)
    assert sorted(p.name for p in downloads.iterdir()) == ["someone"]
    assert sorted(_chapter_contents(moved)) == sorted(_chapter_dirs())
    assert _reader_pages(client) == TOTAL


def test_by_author_existing_author_folder_is_kept_when_the_rename_fails(client, download, downloads, monkeypatch):
    (downloads / "someone").mkdir()                                             # already there (empty): not ours
    Lock(monkeypatch, "rename", downloads / "Name_3001")
    job = download(ALL_IDS, organize="by_author")
    assert job["status"] == "completed", job["error_message"]
    assert job["output_path"] == str(downloads / "Name_3001")
    assert sorted(p.name for p in downloads.iterdir()) == ["Name_3001", "someone"]
    assert list((downloads / "someone").iterdir()) == []
    assert _reader_pages(client) == TOTAL


def test_by_author_cross_device_rename_is_not_retried_or_copied(client, download, downloads, monkeypatch):
    folder = downloads / "Name_3001"
    real = os.rename
    calls = []

    def other_disk(src, dst, *args, **kwargs):
        if Path(src) == folder:
            calls.append(dst)
            raise OSError(errno.EXDEV, "The system cannot move the file to a different disk drive")
        return real(src, dst, *args, **kwargs)
    monkeypatch.setattr(os, "rename", other_disk)
    job = download(ALL_IDS, organize="by_author")
    assert job["status"] == "completed", job["error_message"]
    assert job["output_path"] == str(folder)
    assert len(calls) == 1                                                      # not a lock: no retry
    assert sorted(p.name for p in downloads.iterdir()) == ["Name_3001"]
    assert _reader_pages(client) == TOTAL


def test_lock_retries_are_short():
    from core import jm_service
    assert 1 <= len(jm_service._LOCK_RETRY_DELAYS) <= 6
    assert sum(jm_service._LOCK_RETRY_DELAYS) <= 3 and max(jm_service._LOCK_RETRY_DELAYS) <= 1


@pytest.mark.parametrize("function, replace", [("rename", False), ("replace", True)])
def test_lock_retries_really_wait_between_attempts(monkeypatch, function, replace):
    """The job fixture makes the retries instant; here the real delays are used: a lock released after two
    attempts costs exactly the first two delays, a lock that stays costs all of them, then the error is raised."""
    from core import jm_service
    slept, calls = [], []
    monkeypatch.setattr(jm_service.time, "sleep", slept.append)

    def locked(times):
        def operation(src, dst):
            calls.append((src, dst))
            if times is None or len(calls) <= times:
                raise PermissionError(13, "being used by another process", src, 32)
        return operation
    monkeypatch.setattr(jm_service.os, function, locked(2))
    jm_service._rename_with_retry("a", "b", replace=replace)
    assert calls == [("a", "b")] * 3
    assert slept == list(jm_service._LOCK_RETRY_DELAYS[:2])

    slept.clear()
    calls.clear()
    monkeypatch.setattr(jm_service.os, function, locked(None))
    with pytest.raises(PermissionError):
        jm_service._rename_with_retry("a", "b", replace=replace)
    assert len(calls) == len(jm_service._LOCK_RETRY_DELAYS) + 1
    assert slept == list(jm_service._LOCK_RETRY_DELAYS)


def test_errors_that_are_not_locks_are_not_retried(monkeypatch):
    from core import jm_service
    slept, calls = [], []
    monkeypatch.setattr(jm_service.time, "sleep", slept.append)

    def other_disk(src, dst):
        calls.append(dst)
        raise OSError(errno.EXDEV, "The system cannot move the file to a different disk drive")
    monkeypatch.setattr(jm_service.os, "rename", other_disk)
    with pytest.raises(OSError) as raised:
        jm_service._rename_with_retry("a", "b")
    assert raised.value.errno == errno.EXDEV
    assert calls == ["b"] and slept == []


# ─── B. flat: page names p<page number>, rename only ──────────────────────────


def test_flat_page_names_never_take_a_legacy_counter_name():
    """Earlier versions named flattened pages <prefix>_NNNNN with a counter over the whole run: a name this version
    writes (p<page number> for pages, x<file name> for any other image) can never be one of those."""
    import re
    from core.jm_service import _flat_page_name
    assert _flat_page_name("第1话__71", "00001.webp") == "第1话__71_p00001.webp"
    assert _flat_page_name("第3话 完__73", "00012.webp") == "第3话_完__73_p00012.webp"
    assert _flat_page_name("第1话__71", "cover.jpg") == "第1话__71_xcover.jpg"
    assert _flat_page_name("第1话__71", "00001.q7w3e9rt.webp") == "第1话__71_x00001.q7w3e9rt.webp"
    legacy = re.compile(r"第1话__71_[0-9]{5}\.[a-z]+")
    for page in ("00001.webp", "00012.jpg", "1.webp", "000001.webp", "12345.png", "abc.png"):
        assert not legacy.fullmatch(_flat_page_name("第1话__71", page))


def test_flat_names_every_page_after_its_chapter_and_own_page_number(client, download, downloads):
    """Not one counter over all chapters of one run (that restarted every run, so the same page got a new name)."""
    job = download(ALL_IDS, organize="flat")
    assert job["status"] == "completed", job["error_message"]
    folder = downloads / "Name_3001"
    assert job["output_path"] == str(folder)
    assert _root_files(folder) == _stable_names()
    assert _chapter_contents(folder) == {name: [MARKER] for name in _chapter_dirs()}
    assert _reader_pages(client) == TOTAL


def test_flatten_never_overwrites_or_renames_legacy_counter_named_pages(downloads, no_copies):
    """F1 / T1 / DS-2 (the flatten step): chapter 72 was flattened by an earlier version under counter names
    (第2话__72_00001… hold other pages than their number says) and its pages were downloaded again. They get
    p-names: no legacy file is ever the destination, so none is overwritten, renamed or deleted."""
    from core.jm_service import organize_download
    folder = downloads / "Name_3001"
    chapter = folder / "第2话__72"
    chapter.mkdir(parents=True)
    (chapter / MARKER).write_text(json.dumps({"photo_id": "72", "format": 2}), encoding="utf-8")
    for counter in range(1, 5):
        _image(folder / f"第2话__72_{counter:05d}.webp", (counter * 30, 0, 0))
    for n in range(1, 4):
        _image(chapter / f"{n:05d}.webp", (0, n * 30, 0))
    before = _snapshot(folder)
    assert organize_download(str(folder), "flat", _Album()) == str(folder)
    after = _snapshot(folder)
    assert {name: after.get(name) for name in before if name.startswith("第2话__72_")} == \
        {name: data for name, data in before.items() if name.startswith("第2话__72_")}
    assert {n: after[f"第2话__72_p{n:05d}.webp"] for n in range(1, 4)} == \
        {n: before[f"第2话__72/{n:05d}.webp"] for n in range(1, 4)}
    assert _chapter_contents(folder) == {"第2话__72": [MARKER]}


def test_flatten_never_replaces_a_good_flattened_page_with_a_damaged_one(downloads, no_copies):
    """DS-4: a damaged (or empty) image in the chapter folder is not moved — least of all over the good flattened
    copy of the same page."""
    from core.jm_service import organize_download
    folder = downloads / "Name_3001"
    chapter = folder / "第1话__71"
    chapter.mkdir(parents=True)
    (chapter / MARKER).write_text(json.dumps({"photo_id": "71", "format": 2}), encoding="utf-8")
    _image(folder / "第1话__71_p00001.webp")
    good = (folder / "第1话__71_p00001.webp").read_bytes()
    (chapter / "00001.webp").write_bytes(good[:len(good) // 2])                 # truncated
    (chapter / "00002.webp").write_bytes(b"")                                   # empty, no flattened copy
    _image(chapter / "00003.webp")
    assert organize_download(str(folder), "flat", _Album()) == str(folder)
    assert (folder / "第1话__71_p00001.webp").read_bytes() == good
    assert _chapter_contents(folder) == {"第1话__71": [MARKER, "00001.webp", "00002.webp"]}
    assert _root_files(folder) == ["第1话__71_p00001.webp", "第1话__71_p00003.webp"]


def test_flatten_leaves_linked_images_where_they_are(downloads, monkeypatch, no_copies):
    """Only regular files are moved (this Windows account may not create symlinks: the link test is simulated)."""
    from core import jm_service
    folder = downloads / "Name_3001"
    chapter = folder / "第1话__71"
    chapter.mkdir(parents=True)
    (chapter / MARKER).write_text(json.dumps({"photo_id": "71", "format": 2}), encoding="utf-8")
    _image(chapter / "00001.webp")
    _image(chapter / "00002.webp")
    linked = chapter / "00002.webp"
    real = jm_service.is_link
    monkeypatch.setattr(jm_service, "is_link", lambda path: Path(path) == linked or real(path))
    assert jm_service.organize_download(str(folder), "flat", _Album()) == str(folder)
    assert _root_files(folder) == ["第1话__71_p00001.webp"]
    assert _chapter_contents(folder) == {"第1话__71": [MARKER, "00002.webp"]}


def test_redownload_without_skip_existing_replaces_flattened_pages(client, download, downloads):
    """Every page is fetched again; flattening meets the same page of the same chapter and replaces it (the old code
    kept the new copy in the chapter folder or gave it another number: every page shown twice)."""
    assert download(ALL_IDS, organize="flat")["status"] == "completed"
    folder = downloads / "Name_3001"
    before = _snapshot(folder)
    job = download(ALL_IDS, organize="flat", skip_existing=False)
    assert job["status"] == "completed", job["error_message"]
    assert len(download.fake.fetched) == TOTAL
    assert _root_files(folder) == _stable_names()
    assert _chapter_contents(folder) == {name: [MARKER] for name in _chapter_dirs()}
    after = _snapshot(folder)
    assert all(after[name] != before[name] for name in _stable_names())       # replaced by the new download
    assert _reader_pages(client) == TOTAL


def test_locked_page_stays_in_its_chapter_folder_and_is_flattened_next_time(client, download, downloads,
                                                                             monkeypatch):
    """A page that cannot be renamed is left where it is — never copied to the root with the original kept (the
    page shown twice). The next download of the comic neither fetches it again nor duplicates it, and flattens it."""
    folder = downloads / "Name_3001"
    locked = folder / "第2话__72" / "00002.webp"
    lock = Lock(monkeypatch, "rename", locked)
    job = download(ALL_IDS, organize="flat")
    assert job["status"] == "completed", job["error_message"]
    assert len(lock.attempts) > 1                                               # retried
    assert locked.is_file()
    assert _root_files(folder) == [n for n in _stable_names() if n != "第2话__72_p00002.webp"]
    assert _chapter_contents(folder)["第2话__72"] == [MARKER, "00002.webp"]
    assert _reader_pages(client) == TOTAL                                       # every page once
    names = _reader_names(client)                                              # F5 (documented): listed before
    assert names.index("第2话__72/00002.webp") < names.index("第2话__72_p00001.webp")   # its chapter's flat pages
    lock.release()
    job = download(ALL_IDS, organize="flat")
    assert job["status"] == "completed", job["error_message"]
    assert download.fake.fetched == []                                          # its own copy and the flattened ones
    assert _root_files(folder) == _stable_names()
    assert _chapter_contents(folder) == {name: [MARKER] for name in _chapter_dirs()}
    assert _reader_pages(client) == TOTAL
    assert sorted(_reader_names(client)) == _stable_names()


def test_locked_flattened_page_is_not_replaced_by_a_copy(client, download, downloads, monkeypatch):
    """F4 / T3: the re-downloaded page cannot replace its flattened copy (open elsewhere): it stays in its chapter
    folder, the flattened copy is untouched, nothing is copied, every other page is flattened. Until the next
    download of the comic the reader shows that page twice (documented); the next download flattens it."""
    assert download(ALL_IDS, organize="flat")["status"] == "completed"
    folder = downloads / "Name_3001"
    before = _snapshot(folder)
    lock = Lock(monkeypatch, "replace", folder / "第1话__71_p00001.webp", on="dst")
    job = download(ALL_IDS, organize="flat", skip_existing=False)
    assert job["status"] == "completed", job["error_message"]
    assert len(lock.attempts) > 1
    assert (folder / "第1话__71_p00001.webp").read_bytes() == before["第1话__71_p00001.webp"]
    assert _chapter_contents(folder)["第1话__71"] == [MARKER, "00001.webp"]
    assert _root_files(folder) == _stable_names()
    after = _snapshot(folder)
    assert all(after[name] != before[name] for name in _stable_names() if name != "第1话__71_p00001.webp")
    assert _reader_pages(client) == TOTAL + 1                                   # shown twice until the next download
    lock.release()
    job = download(ALL_IDS, organize="flat")
    assert job["status"] == "completed", job["error_message"]
    assert download.fake.fetched == []
    assert (folder / "第1话__71_p00001.webp").read_bytes() == after["第1话__71/00001.webp"]   # the newer copy
    assert _root_files(folder) == _stable_names()
    assert _chapter_contents(folder) == {name: [MARKER] for name in _chapter_dirs()}
    assert _reader_pages(client) == TOTAL


def test_locked_page_is_not_packed_under_its_chapter_path(client, download, downloads, monkeypatch):
    """DS-3: flat + auto-pack + delete originals. A page left in its chapter folder by a lock must not be packed
    under its chapter path and deleted (the next download could never flatten it, and the re-downloaded page under
    its flat name would sit next to it in the archive for good): this run does not pack; the next one flattens the
    page, then packs every page under its flat name."""
    folder = downloads / "Name_3001"
    locked = folder / "第2话__72" / "00002.webp"
    lock = Lock(monkeypatch, "rename", locked)
    job = download(ALL_IDS, organize="flat", auto_pack=True, delete_originals=True)
    assert job["status"] == "completed", job["error_message"]
    assert not (folder / ARCHIVE).exists()                                      # not packed this time
    assert locked.is_file()
    assert _root_files(folder) == [n for n in _stable_names() if n != "第2话__72_p00002.webp"]
    assert _reader_pages(client) == TOTAL
    lock.release()
    for _ in range(2):
        job = download(ALL_IDS, organize="flat", auto_pack=True, delete_originals=True)
        assert job["status"] == "completed", job["error_message"]
        assert download.fake.fetched == []
        assert _archive_names(folder) == _stable_names()
        assert _root_files(folder) == [ARCHIVE]
        assert _chapter_contents(folder) == {name: [MARKER] for name in _chapter_dirs()}
        assert _reader_pages(client) == TOTAL


# ─── C. skip_existing recognises flattened pages ──────────────────────────────


def test_redownload_all_after_flat_fetches_nothing(client, download, downloads):
    """The detail page's 下载全部 (every chapter id) after 扁平化: nothing is fetched again, nothing shown twice."""
    assert download(ALL_IDS, organize="flat")["status"] == "completed"
    folder = downloads / "Name_3001"
    before = _snapshot(folder)
    job = download(ALL_IDS, organize="flat")
    assert job["status"] == "completed", job["error_message"]
    assert job["done_pages"] == TOTAL
    assert download.fake.fetched == []
    assert _snapshot(folder) == before
    assert _reader_pages(client) == TOTAL


def test_switching_flat_to_none_then_redownloading_duplicates_nothing(client, download, downloads):
    assert download(ALL_IDS, organize="flat")["status"] == "completed"
    folder = downloads / "Name_3001"
    before = _snapshot(folder)
    job = download(ALL_IDS, organize="none")                                   # the user switched to 不整理 since
    assert job["status"] == "completed", job["error_message"]
    assert download.fake.fetched == []
    assert _snapshot(folder) == before                                          # chapter folders not refilled
    assert _reader_pages(client) == TOTAL


def test_flattened_chapter_that_grew_upstream_fetches_only_the_new_page(client, download, downloads):
    """Not exactly its page count at the root: the chapter is judged page by page — its flattened pages are kept,
    only the page upstream added is fetched (never counted present without a file)."""
    assert download(ALL_IDS, organize="flat")["status"] == "completed"
    folder = downloads / "Name_3001"
    download.fake.pages["71"] = 4
    job = download(ALL_IDS, organize="flat")
    assert job["status"] == "completed", job["error_message"]
    assert download.fake.fetched == ["71/00004.webp"]
    assert _root_files(folder) == _stable_names({"71": 4})
    assert _chapter_contents(folder) == {name: [MARKER] for name in _chapter_dirs()}
    assert _reader_pages(client) == TOTAL + 1


def test_spaced_chapter_that_grew_upstream_fetches_only_the_new_page(client, download, downloads):
    """T4: the per-page check uses the same prefix rule as the flatten step ('第3话 完' → 第3话_完)."""
    assert download(ALL_IDS, organize="flat")["status"] == "completed"
    folder = downloads / "Name_3001"
    download.fake.pages["73"] = 5
    job = download(ALL_IDS, organize="flat")
    assert job["status"] == "completed", job["error_message"]
    assert download.fake.fetched == ["73/00005.webp"]
    assert _root_files(folder) == _stable_names({"73": 5})
    assert _reader_pages(client) == TOTAL + 1


@pytest.mark.parametrize("damage", ["truncated", "empty"])
def test_damaged_flattened_page_is_fetched_again_and_replaced(client, download, downloads, damage):
    """T4: a flattened page that no longer opens does not count as present: it is fetched again and replaces the
    damaged file."""
    assert download(ALL_IDS, organize="flat")["status"] == "completed"
    folder = downloads / "Name_3001"
    page = folder / "第1话__71_p00002.webp"
    data = page.read_bytes()
    page.write_bytes(data[:len(data) // 2] if damage == "truncated" else b"")
    job = download(ALL_IDS, organize="flat")
    assert job["status"] == "completed", job["error_message"]
    assert download.fake.fetched == ["71/00002.webp"]
    assert _valid(page)
    assert _root_files(folder) == _stable_names()
    assert _chapter_contents(folder) == {name: [MARKER] for name in _chapter_dirs()}
    assert _reader_pages(client) == TOTAL


def test_damaged_chapter_page_is_fetched_again_and_replaces_its_flattened_copy(client, download, downloads):
    """DS-4: a damaged page in the chapter folder is not counted present through its flattened copy; it is fetched
    again, and the fresh page (not the damaged one) replaces the flattened copy."""
    assert download(ALL_IDS, organize="flat")["status"] == "completed"
    folder = downloads / "Name_3001"
    good = (folder / "第1话__71_p00001.webp").read_bytes()
    (folder / "第1话__71" / "00001.webp").write_bytes(good[:len(good) // 2])
    job = download(ALL_IDS, organize="flat")
    assert job["status"] == "completed", job["error_message"]
    assert download.fake.fetched == ["71/00001.webp"]
    assert _valid(folder / "第1话__71_p00001.webp")
    assert (folder / "第1话__71_p00001.webp").read_bytes() != good                # the fresh download
    assert _chapter_contents(folder) == {name: [MARKER] for name in _chapter_dirs()}
    assert _reader_pages(client) == TOTAL


def test_flattened_page_names_are_matched_exactly(client, download, downloads):
    """T4: a leftover temp name that merely starts like page 4's flat name is not page 4."""
    assert download(ALL_IDS, organize="flat")["status"] == "completed"
    folder = downloads / "Name_3001"
    _image(folder / "第1话__71_p00004.q7w3e9rt.webp")
    download.fake.pages["71"] = 4
    job = download(ALL_IDS, organize="flat")
    assert job["status"] == "completed", job["error_message"]
    assert download.fake.fetched == ["71/00004.webp"]


def test_linked_flattened_pages_do_not_count(client, download, downloads, monkeypatch):
    """T4: a flattened page (or a legacy one) that is a link does not count as present (simulated link)."""
    from core import jm_service
    assert download(ALL_IDS, organize="flat")["status"] == "completed"
    folder = downloads / "Name_3001"
    linked = {folder / "第1话__71_p00002.webp"}
    real = jm_service.is_link
    monkeypatch.setattr(jm_service, "is_link", lambda path: Path(path) in linked or real(path))
    assert download(ALL_IDS, organize="none")["status"] == "completed"
    assert download.fake.fetched == ["71/00002.webp"]


def test_linked_legacy_page_does_not_complete_its_chapter(client, download, downloads, monkeypatch):
    from core import jm_service
    folder = _legacy_flat_album(downloads)
    linked = {folder / "第2话__72_00004.webp"}
    real = jm_service.is_link
    monkeypatch.setattr(jm_service, "is_link", lambda path: Path(path) in linked or real(path))
    assert download(ALL_IDS, organize="flat")["status"] == "completed"
    assert sorted(download.fake.fetched) == ["72/00001.webp", "72/00002.webp"]


def test_linked_new_page_next_to_a_complete_legacy_chapter_does_not_block_it(client, download, downloads, monkeypatch):
    # the link check runs before a new-style name marks the chapter as having new pages (R3-1 guard order)
    from core import jm_service
    folder = _legacy_flat_album(downloads)
    _serve_legacy_bytes(download, folder)
    linked = folder / "第2话__72_p00001.webp"
    _image(linked)
    real = jm_service.is_link
    monkeypatch.setattr(jm_service, "is_link", lambda path: Path(path) == linked or real(path))
    for _ in range(2):
        assert download(ALL_IDS, organize="flat")["status"] == "completed"
        assert download.fake.fetched == []                                   # chapter 72 still counts as present


def test_real_symlinked_flattened_page_does_not_count(client, download, downloads, tmp_path):
    assert download(ALL_IDS, organize="flat")["status"] == "completed"
    folder = downloads / "Name_3001"
    outside = tmp_path / "outside.webp"
    _image(outside)
    page = folder / "第1话__71_p00002.webp"
    page.unlink()
    try:
        page.symlink_to(outside)
    except OSError:
        pytest.skip("This Windows account cannot create file symlinks")
    assert download(ALL_IDS, organize="none")["status"] == "completed"
    assert download.fake.fetched == ["71/00002.webp"]


# ─── C. folders flattened by earlier versions (counter names) ─────────────────


@pytest.mark.parametrize("organize", ["flat", "none"])
def test_legacy_counter_named_flat_folder_and_download_all_fetch_nothing(client, download, downloads, organize):
    """Folders flattened by earlier versions name the pages of later chapters by a counter (第2话__72_00004…), which
    no page number maps to: a chapter whose folder holds none of its pages while the root holds exactly its page
    count of <prefix>_NNNNN images counts as present — and its progress is reported like skipped pages."""
    folder = _legacy_flat_album(downloads)
    before = _snapshot(folder)
    assert _reader_pages(client) == TOTAL
    job = download(ALL_IDS, organize=organize)
    assert job["status"] == "completed", job["error_message"]
    assert job["output_path"] == str(folder)
    assert job["done_pages"] == TOTAL
    assert download.fake.fetched == []
    assert _snapshot(folder) == before                                          # nothing renamed, deleted or added
    assert _reader_pages(client) == TOTAL
    running = [data["done_pages"] for event, data in download.events
               if event == "progress" and data.get("status") == "running"]
    assert max(running) == TOTAL                                                # T4: progress pushed per chapter


def test_legacy_flat_folder_with_old_format_markers_and_no_gif_pages_fetches_nothing(client, download, downloads):
    """Chapters downloaded before GIF pages were kept whole carry the old marker; only their GIF pages need fetching
    again. Without GIF pages the whole flattened chapter still counts as present."""
    folder = _legacy_flat_album(downloads, marker_format=1)
    before = _pages_only(_snapshot(folder))
    job = download(ALL_IDS, organize="flat")
    assert job["status"] == "completed", job["error_message"]
    assert download.fake.fetched == []
    assert _pages_only(_snapshot(folder)) == before
    assert _reader_pages(client) == TOTAL


def test_old_format_gif_page_is_fetched_again_even_when_flattened(client, download, downloads):
    """Same GIF rule as for pages in the chapter folder: in an old-format chapter the GIF page is fetched again
    (and replaces its flattened copy); the chapter's other pages are recognised flattened and skipped."""
    download.fake.gif.add(("72", 1))
    assert download(ALL_IDS, organize="flat")["status"] == "completed"
    folder = downloads / "Name_3001"
    (folder / "第2话__72" / MARKER).write_text(json.dumps({"photo_id": "72"}), encoding="utf-8")   # old format
    before = _snapshot(folder)
    job = download(ALL_IDS, organize="flat")
    assert job["status"] == "completed", job["error_message"]
    assert download.fake.fetched == ["72/00001.gif"]
    after = _snapshot(folder)
    assert after["第2话__72_p00001.webp"] != before["第2话__72_p00001.webp"]
    assert _root_files(folder) == _stable_names()
    assert _chapter_contents(folder) == {name: [MARKER] for name in _chapter_dirs()}
    assert json.loads((folder / "第2话__72" / MARKER).read_text(encoding="utf-8"))["format"] == 2
    assert _reader_pages(client) == TOTAL


@pytest.mark.parametrize("pages, gif", [({"71": 1, "72": 3}, 3), ({"71": 3, "72": 10}, 8)])
def test_legacy_old_format_chapter_with_a_gif_page_loses_no_page(client, download, downloads, pages, gif):
    """F1 / T1 / DS-2: an old-format legacy chapter with a GIF page is judged page by page. Its counter names are
    never taken for page numbers (legacy 第2话__72_00002 holds page 1 there): every page of the chapter is fetched
    and flattened under p-names, and no legacy file is overwritten — the chapter's legacy copies are kept next to
    the new pages (shown twice, documented), nothing is lost, and later downloads fetch nothing."""
    folder = _legacy_flat_album(downloads, marker_format=1, pages=pages)
    download.fake.pages.update(pages)
    download.fake.gif.add(("72", gif))
    before = _pages_only(_snapshot(folder))
    job = download(ALL_IDS, organize="flat")
    assert job["status"] == "completed", job["error_message"]
    count = pages["72"]
    assert sorted(download.fake.fetched) == sorted(f"72/{n:05d}.{'gif' if n == gif else 'webp'}"
                                                   for n in range(1, count + 1))
    after = _pages_only(_snapshot(folder))
    assert {name: after.get(name) for name in before} == before                # every legacy file untouched
    assert all(f"第2话__72_p{n:05d}.webp" in after for n in range(1, count + 1))
    total = sum(c for _, _, c in _chapters(pages))
    assert _reader_pages(client) == total + count
    job = download(ALL_IDS, organize="flat")
    assert job["status"] == "completed", job["error_message"]
    assert download.fake.fetched == []
    assert _pages_only(_snapshot(folder)) == after
    assert _reader_pages(client) == total + count


def test_legacy_chapter_that_grew_upstream_gets_every_page(client, download, downloads):
    """F2 / T2: chapter 72 was flattened with 2 pages (第2话__72_00004/00005, its pages 1 and 2); upstream now lists
    5. The counter names are not taken for pages 4 and 5: all 5 pages are fetched, the legacy files stay."""
    folder = _legacy_flat_album(downloads)
    download.fake.pages["72"] = 5
    before = _pages_only(_snapshot(folder))
    job = download(ALL_IDS, organize="flat")
    assert job["status"] == "completed", job["error_message"]
    assert sorted(download.fake.fetched) == [f"72/{n:05d}.webp" for n in range(1, 6)]
    after = _pages_only(_snapshot(folder))
    assert {name: after.get(name) for name in before} == before
    assert all(f"第2话__72_p{n:05d}.webp" in after for n in range(1, 6))
    assert _reader_pages(client) == TOTAL + 5                                   # legacy pages 1-2 shown twice
    job = download(ALL_IDS, organize="flat")
    assert download.fake.fetched == []
    assert _reader_pages(client) == TOTAL + 5


def test_legacy_page_that_failed_in_a_skip_off_run_is_fetched_next_time(client, download, downloads):
    """T2 (b): a page that failed while re-downloading a legacy folder with skip_existing off is fetched by the next
    normal download — the legacy file whose counter equals its number (another page) does not stand in for it."""
    pages = {"72": 6}                                                           # 72: 第2话__72_00004 … _00009
    folder = _legacy_flat_album(downloads, pages=pages)
    download.fake.pages.update(pages)
    before = _pages_only(_snapshot(folder))
    download.fake.fail.add("72/00005.webp")
    job = download(ALL_IDS, organize="flat", skip_existing=False)
    assert job["status"] == "failed"
    download.fake.fail.clear()
    job = download(ALL_IDS, organize="flat")
    assert job["status"] == "completed", job["error_message"]
    assert download.fake.fetched == ["72/00005.webp"]
    after = _pages_only(_snapshot(folder))
    assert {name: after.get(name) for name in before} == before
    assert all(name in after for name in _stable_names(pages))


@pytest.mark.parametrize("organize, auto_pack", [("flat", True), ("flat", False), ("none", False)])
def test_legacy_flattened_comic_left_only_in_its_archive_fetches_nothing(client, download, downloads, organize,
                                                                         auto_pack):
    """DS-1 / F3: flat + auto-pack + delete originals on an earlier version left only Name_3001.cbz, its pages under
    counter names. 下载全部 recognises the whole chapters in the archive and fetches nothing (the first build fetched
    everything and packed the new names next to the old ones: 15 pages for 9)."""
    folder = _legacy_flat_album(downloads)
    _pack(folder)
    names = _archive_names(folder)
    assert _reader_pages(client) == TOTAL
    job = download(ALL_IDS, organize=organize, auto_pack=auto_pack, delete_originals=True)
    assert job["status"] == "completed", job["error_message"]
    assert job["done_pages"] == TOTAL
    assert download.fake.fetched == []
    assert _archive_names(folder) == names
    assert _root_files(folder) == [ARCHIVE]
    assert _reader_pages(client) == TOTAL


def test_legacy_flattened_pages_both_loose_and_packed_count_once(client, download, downloads):
    """T4: the same legacy page loose and in the archive is one page (page_key), as the reader shows it."""
    folder = _legacy_flat_album(downloads)
    _pack(folder, delete=False)
    job = download(ALL_IDS, organize="flat")
    assert job["status"] == "completed", job["error_message"]
    assert download.fake.fetched == []
    assert _reader_pages(client) == TOTAL


def test_flattened_comic_left_only_in_its_archive_fetches_nothing(client, download, downloads):
    """New p-names in the archive count per page: 下载全部 fetches nothing, and a page upstream added is the only
    one fetched (then packed with the others)."""
    job = download(ALL_IDS, organize="flat", auto_pack=True, delete_originals=True)
    assert job["status"] == "completed", job["error_message"]
    folder = downloads / "Name_3001"
    assert _root_files(folder) == [ARCHIVE]
    assert _archive_names(folder) == _stable_names()
    for organize in ("flat", "none"):
        job = download(ALL_IDS, organize=organize, auto_pack=True, delete_originals=True)
        assert job["status"] == "completed", job["error_message"]
        assert download.fake.fetched == []
        assert _archive_names(folder) == _stable_names()
        assert _reader_pages(client) == TOTAL
    download.fake.pages["71"] = 4
    job = download(ALL_IDS, organize="flat", auto_pack=True, delete_originals=True)
    assert job["status"] == "completed", job["error_message"]
    assert download.fake.fetched == ["71/00004.webp"]
    assert _archive_names(folder) == _stable_names({"71": 4})
    assert _reader_pages(client) == TOTAL + 1


def test_unorganized_comic_left_only_in_its_archive_is_downloaded_again_as_before(client, download, downloads):
    """None-mode behaviour is unchanged: pages packed under their chapter path are not looked for in the archive."""
    assert download(ALL_IDS, organize="none", auto_pack=True, delete_originals=True)["status"] == "completed"
    folder = downloads / "Name_3001"
    assert _root_files(folder) == [ARCHIVE]
    job = download(ALL_IDS, organize="none", auto_pack=True, delete_originals=True)
    assert job["status"] == "completed", job["error_message"]
    assert len(download.fake.fetched) == TOTAL
    assert _reader_pages(client) == TOTAL


@pytest.mark.parametrize("change", ["damaged", "extra", "missing"])
def test_legacy_chapter_counts_whole_only_with_exactly_its_valid_pages(client, download, downloads, change):
    """T4: one damaged legacy page, one too many or one too few: chapter 72 is judged page by page (fetched again
    under p-names), the legacy files stay as they are."""
    folder = _legacy_flat_album(downloads)
    if change == "damaged":
        page = folder / "第2话__72_00004.webp"
        page.write_bytes(page.read_bytes()[:20])
    elif change == "extra":
        _image(folder / "第2话__72_00099.webp")
    else:
        (folder / "第2话__72_00005.webp").unlink()
    before = _pages_only(_snapshot(folder))
    job = download(ALL_IDS, organize="flat")
    assert job["status"] == "completed", job["error_message"]
    assert sorted(download.fake.fetched) == ["72/00001.webp", "72/00002.webp"]
    after = _pages_only(_snapshot(folder))
    assert {name: after.get(name) for name in before} == before


@pytest.mark.parametrize("decoy", ["第1话__71_2_00003.webp", "第1话__71_00003.q7w3e9rt.webp", "第1话__71_x00003.webp",
                                   "第1话__71_00003.dat"])
def test_other_files_named_after_the_chapter_are_not_its_legacy_pages(client, download, downloads, decoy):
    """T4: only <prefix>_NNNNN.<image suffix> counts — not a sibling chapter folder's page (第1话__71_2_…), a leftover
    temp name, an x-name, or an image under a suffix the reader never shows (every decoy is a valid image)."""
    folder = _legacy_flat_album(downloads)
    (folder / "第1话__71_00003.webp").unlink()
    _image(folder / decoy)
    job = download(ALL_IDS, organize="flat")
    assert job["status"] == "completed", job["error_message"]
    assert sorted(download.fake.fetched) == ["71/00001.webp", "71/00002.webp", "71/00003.webp"]


def test_legacy_chapter_with_a_page_of_its_own_is_judged_page_by_page(client, download, downloads):
    """T4: the chapter folder holds one of its pages: the chapter is not counted whole by the legacy names."""
    folder = _legacy_flat_album(downloads)
    _image(folder / "第2话__72" / "00001.webp")
    job = download(ALL_IDS, organize="flat")
    assert job["status"] == "completed", job["error_message"]
    assert download.fake.fetched == ["72/00002.webp"]


def test_chapter_with_new_flat_pages_is_never_counted_whole_by_legacy_names(client, download, downloads):
    """A chapter that already has a p-named page is judged page by page, even when its legacy files happen to be
    exactly its page count."""
    folder = _legacy_flat_album(downloads)
    _image(folder / "第2话__72_p00001.webp")
    job = download(ALL_IDS, organize="flat")
    assert job["status"] == "completed", job["error_message"]
    assert download.fake.fetched == ["72/00002.webp"]


def test_legacy_archive_names_never_count_per_page_when_a_chapter_grew(client, download, downloads):
    """R2-T2: only the archive is left; chapter 72 was flattened with 2 pages (第2话__72_00004/00005 — its pages 1
    and 2) and upstream now lists 5. The counter names inside the archive are not taken for pages 4 and 5."""
    folder = _legacy_flat_album(downloads)
    _pack(folder)
    names = _archive_names(folder)
    download.fake.pages["72"] = 5
    job = download(ALL_IDS, organize="flat")
    assert job["status"] == "completed", job["error_message"]
    assert sorted(download.fake.fetched) == [f"72/{n:05d}.webp" for n in range(1, 6)]
    assert _archive_names(folder) == names
    job = download(ALL_IDS, organize="flat")
    assert job["status"] == "completed", job["error_message"]
    assert download.fake.fetched == []


def test_legacy_archive_names_never_count_per_page_in_an_old_format_gif_chapter(client, download, downloads):
    """R2-T2: an old-format chapter with a GIF page, left only in the archive, is judged page by page — and its
    archive counter names (第2话__72_00004 … _00013) never stand in for pages 4-10."""
    pages = {"71": 3, "72": 10}
    folder = _legacy_flat_album(downloads, marker_format=1, pages=pages)
    _pack(folder)
    names = _archive_names(folder)
    download.fake.pages.update(pages)
    download.fake.gif.add(("72", 8))
    job = download(ALL_IDS, organize="flat")
    assert job["status"] == "completed", job["error_message"]
    assert sorted(download.fake.fetched) == sorted(f"72/{n:05d}.{'gif' if n == 8 else 'webp'}" for n in range(1, 11))
    assert _archive_names(folder) == names


@pytest.mark.parametrize("damage", ["truncated", "empty"])
def test_damaged_page_in_a_complete_legacy_chapter_folder_is_fetched_again(client, download, downloads, damage):
    """R2-T4: the chapter folder of an otherwise complete legacy chapter holds a damaged page file. It is not left
    there for good (flattening never moves a damaged image): the chapter is judged page by page, the page is fetched
    again and replaces the damaged file. Re-downloaded pages identical to legacy pages are then dropped."""
    folder = _legacy_flat_album(downloads)
    _serve_legacy_bytes(download, folder)
    good = (folder / "第2话__72_00004.webp").read_bytes()
    (folder / "第2话__72" / "00001.webp").write_bytes(good[:len(good) // 2] if damage == "truncated" else b"")
    before = _pages_only(_snapshot(folder))
    job = download(ALL_IDS, organize="flat")
    assert job["status"] == "completed", job["error_message"]
    assert sorted(download.fake.fetched) == ["72/00001.webp", "72/00002.webp"]
    assert _chapter_contents(folder) == {name: [MARKER] for name in _chapter_dirs()}
    after = _pages_only(_snapshot(folder))
    assert after == {name: data for name, data in before.items() if not name.startswith("第2话__72/")}
    assert _reader_pages(client) == TOTAL


def test_damaged_leftover_that_is_not_a_page_file_does_not_block_the_whole_chapter(client, download, downloads):
    """R2-T4: only a page file (NNNNN.webp, what a re-download replaces) blocks the whole-chapter rule — not a
    damaged leftover such as an interrupted temp file, which fetching the chapter again could never replace."""
    folder = _legacy_flat_album(downloads)
    (folder / "第2话__72" / "00001.q7w3e9rt.webp").write_bytes(b"RIFF\x10\x00\x00\x00WEBPVP8 ")
    job = download(ALL_IDS, organize="flat")
    assert job["status"] == "completed", job["error_message"]
    assert download.fake.fetched == []
    assert _chapter_contents(folder)["第2话__72"] == [MARKER, "00001.q7w3e9rt.webp"]


def test_flatten_error_blocks_auto_pack_until_the_next_download(client, download, downloads, monkeypatch):
    """R2-T3 / DS-3: when flattening stops with an error (no report written), the job does not pack either — else the
    pages still in their chapter folders would be packed under their chapter paths and deleted. The next download
    flattens them and packs every page under its flat name."""
    from core import jm_service
    folder = downloads / "Name_3001"
    real = jm_service._flat_page_name

    def broken(chapter_dir_name, page_name):
        if chapter_dir_name == "第2话__72":
            raise OSError("the chapter folder cannot be listed")
        return real(chapter_dir_name, page_name)
    monkeypatch.setattr(jm_service, "_flat_page_name", broken)
    job = download(ALL_IDS, organize="flat", auto_pack=True, delete_originals=True)
    assert job["status"] == "completed", job["error_message"]
    assert not (folder / ARCHIVE).exists()
    assert _chapter_contents(folder)["第2话__72"] == [MARKER, "00001.webp", "00002.webp"]
    assert _reader_pages(client) == TOTAL
    monkeypatch.setattr(jm_service, "_flat_page_name", real)
    job = download(ALL_IDS, organize="flat", auto_pack=True, delete_originals=True)
    assert job["status"] == "completed", job["error_message"]
    assert download.fake.fetched == []
    assert _archive_names(folder) == _stable_names()
    assert _root_files(folder) == [ARCHIVE]
    assert _chapter_contents(folder) == {name: [MARKER] for name in _chapter_dirs()}
    assert _reader_pages(client) == TOTAL


# ─── B/C. a loose flattened page overrides its archive copy ───────────────────


@pytest.mark.parametrize("organize, loose", [("flat", "第1话__71_p00002.webp"), ("flat", "第1话__71_p00002.jpg"),
                                             ("none", "第1话__71_p00002.webp")])
def test_damaged_loose_flat_page_is_fetched_again_although_the_archive_has_it(client, download, downloads, organize,
                                                                              loose):
    """R2-DS-2 / R2C-2: flat + auto-pack without deleting originals leaves the loose pages and the archive. A loose
    flattened page gets damaged. The archive copy does not count while a loose file of that page exists (the reader
    and auto-pack use the loose file): the page is fetched again, and the next pack cannot put the damaged bytes over
    the only good copy."""
    job = download(ALL_IDS, organize="flat", auto_pack=True)
    assert job["status"] == "completed", job["error_message"]
    folder = downloads / "Name_3001"
    good = (folder / "第1话__71_p00002.webp").read_bytes()
    assert _archive_pages(folder)["第1话__71_p00002.webp"] == good
    (folder / "第1话__71_p00002.webp").unlink()
    (folder / loose).write_bytes(good[:len(good) // 2])
    _forget_local()
    job = download(ALL_IDS, organize=organize, auto_pack=True)
    assert job["status"] == "completed", job["error_message"]
    assert download.fake.fetched == ["71/00002.webp"]
    fresh = folder / ("第1话__71_p00002.webp" if organize == "flat" else "第1话__71/00002.webp")
    assert _valid(fresh)
    assert _archive_pages(folder)[fresh.relative_to(folder).as_posix()] == fresh.read_bytes()


def test_damaged_loose_legacy_page_is_not_completed_by_its_archive_copy(client, download, downloads):
    """R2-DS-2 / R2C-2, legacy names: the loose 第2话__72_00004 (page 1 of 72) is damaged, its archive copy is good.
    The archive copy does not complete the chapter (auto-pack would put the damaged loose file over it), so chapter 72
    is fetched again; the fresh page 1 is not dropped against that archive copy either (the pack replaces it with the
    damaged loose file) — it is kept under its p-name. Every page keeps a good copy."""
    folder = _legacy_flat_album(downloads)
    served = _serve_legacy_bytes(download, folder)
    _pack(folder, delete=False)
    damaged = folder / "第2话__72_00004.webp"
    damaged.write_bytes(served[("72", 1)][:40])
    _forget_local()
    job = download(ALL_IDS, organize="flat", auto_pack=True, delete_originals=True)
    assert job["status"] == "completed", job["error_message"]
    assert sorted(download.fake.fetched) == ["72/00001.webp", "72/00002.webp"]
    packed = _archive_pages(folder)
    assert packed["第2话__72_p00001.webp"] == served[("72", 1)]                 # page 1's only good copy
    assert "第2话__72_p00002.webp" not in packed                               # page 2: the legacy copy
    assert sorted(packed) == sorted(list(_legacy_names().values()) + ["第2话__72_p00001.webp"])
    assert _root_files(folder) == [ARCHIVE]


# ─── D. re-downloaded pages identical to legacy flattened pages ───────────────


@pytest.mark.parametrize("packed, auto_pack", [(True, True), (True, False), (False, True), (False, False)])
def test_legacy_flat_comic_redownloaded_with_skip_off_shows_each_page_once(client, download, downloads, packed,
                                                                          auto_pack):
    """R2-DS-1 / R2-T1: a comic flattened by an earlier version (loose, or left only in its archive) is downloaded
    again with skip_existing off. Every page is fetched; each one is byte for byte a legacy page the comic already
    has, so the fresh copy is dropped instead of flattened next to it: every page once, the archive keeps the legacy
    names only, nothing new is ever packed next to them."""
    folder = _legacy_flat_album(downloads)
    _serve_legacy_bytes(download, folder)
    legacy = sorted(_legacy_names().values())
    if packed:
        _pack(folder)
    before = _pages_only(_snapshot(folder))
    job = download(ALL_IDS, organize="flat", skip_existing=False, auto_pack=auto_pack, delete_originals=True)
    assert job["status"] == "completed", job["error_message"]
    assert len(download.fake.fetched) == TOTAL
    assert _chapter_contents(folder) == {name: [MARKER] for name in _chapter_dirs()}
    if auto_pack:
        assert _archive_names(folder) == legacy
        assert _root_files(folder) == [ARCHIVE]
    else:
        assert _pages_only(_snapshot(folder)) == before
    assert _reader_pages(client) == TOTAL
    job = download(ALL_IDS, organize="flat", auto_pack=auto_pack, delete_originals=True)
    assert job["status"] == "completed", job["error_message"]
    assert download.fake.fetched == []
    assert _reader_pages(client) == TOTAL


def test_incomplete_legacy_chapter_keeps_each_page_once(client, download, downloads):
    """A legacy chapter that is not recognised whole (its page 2, 第2话__72_00005, is gone) is downloaded again: the
    fresh page 1 is byte for byte the legacy 第2话__72_00004 and is dropped, the missing page 2 is flattened under
    its p-name. Every page once, the legacy files untouched. The dropped page has no p-name, so later downloads fetch
    it again (and drop it again) — never shown twice."""
    folder = _legacy_flat_album(downloads)
    served = _serve_legacy_bytes(download, folder)
    (folder / "第2话__72_00005.webp").unlink()
    before = _pages_only(_snapshot(folder))
    job = download(ALL_IDS, organize="flat")
    assert job["status"] == "completed", job["error_message"]
    assert sorted(download.fake.fetched) == ["72/00001.webp", "72/00002.webp"]
    after = _pages_only(_snapshot(folder))
    assert after == {**before, "第2话__72_p00002.webp": served[("72", 2)]}
    assert _chapter_contents(folder) == {name: [MARKER] for name in _chapter_dirs()}
    assert _reader_pages(client) == TOTAL
    job = download(ALL_IDS, organize="flat")
    assert job["status"] == "completed", job["error_message"]
    assert download.fake.fetched == ["72/00001.webp"]
    assert _pages_only(_snapshot(folder)) == after
    assert _reader_pages(client) == TOTAL


def test_each_legacy_page_stands_in_for_one_fresh_page_only(client, download, downloads):
    """Pages 1 and 3 of chapter 72 are the same picture (two blank pages); the legacy chapter lost its page 3. The
    fresh page 1 is dropped against the legacy page 1, but the fresh page 3 is not dropped against it too (a set of
    digests would): it is flattened, and the reader shows all 3 pages of 72."""
    pages = {"72": 3}
    folder = _legacy_flat_album(downloads, pages=pages)
    names = _legacy_names(pages)
    (folder / names[("72", 3)]).write_bytes((folder / names[("72", 1)]).read_bytes())
    download.fake.pages.update(pages)
    served = _serve_legacy_bytes(download, folder, pages)
    (folder / names[("72", 3)]).unlink()
    before = _pages_only(_snapshot(folder))
    job = download(ALL_IDS, organize="flat")
    assert job["status"] == "completed", job["error_message"]
    assert sorted(download.fake.fetched) == ["72/00001.webp", "72/00002.webp", "72/00003.webp"]
    assert _pages_only(_snapshot(folder)) == {**before, "第2话__72_p00003.webp": served[("72", 3)]}
    assert _reader_pages(client) == sum(count for _, _, count in _chapters(pages))


def test_fresh_page_that_differs_from_every_legacy_page_is_kept(client, download, downloads):
    """Only an exact copy is dropped: page 1 of 72 comes back with other bytes (re-encoded upstream) and is flattened
    next to its legacy copy — shown twice, nothing lost."""
    folder = _legacy_flat_album(downloads)
    _serve_legacy_bytes(download, folder)
    del download.fake.same[("72", 1)]
    before = _pages_only(_snapshot(folder))
    job = download(ALL_IDS, organize="flat", skip_existing=False)
    assert job["status"] == "completed", job["error_message"]
    assert len(download.fake.fetched) == TOTAL
    after = _pages_only(_snapshot(folder))
    assert sorted(after) == sorted(list(before) + ["第2话__72_p00001.webp"])
    assert {name: after[name] for name in before} == before
    assert _valid(folder / "第2话__72_p00001.webp")
    assert _reader_pages(client) == TOTAL + 1


@pytest.mark.parametrize("failure", ["loose file", "fresh page", "archive entry", "archive"])
def test_pages_that_cannot_be_read_never_drop_a_fresh_page(client, download, downloads, monkeypatch, failure):
    """A legacy page whose bytes cannot be read (the loose file is open elsewhere, the archive entry fails its CRC
    check, the archive cannot be opened) is not used, and a fresh page that cannot be read is not compared: the fresh
    copy of that page is flattened — possibly shown twice, never lost."""
    from core import archive_pages, jm_service
    folder = _legacy_flat_album(downloads)
    served = _serve_legacy_bytes(download, folder)
    unreadable = folder / "第2话__72_00004.webp"
    if failure in ("loose file", "fresh page"):
        if failure == "fresh page":
            unreadable = folder / "第2话__72" / "00001.webp"

        def guarded_open(path, *args, **kwargs):
            if Path(path) == unreadable:
                raise PermissionError(13, "being used by another process", str(path), 32)
            return open(path, *args, **kwargs)
        monkeypatch.setattr(jm_service, "open", guarded_open, raising=False)
    else:
        _pack(folder)
        if failure == "archive entry":
            _corrupt_archive_entry(folder, unreadable.name)
        else:
            def busy(index):
                raise archive_pages.ArchiveBusy("being used by another process")
            monkeypatch.setattr(archive_pages, "open_pages", busy)
    job = download(ALL_IDS, organize="flat", skip_existing=False)
    assert job["status"] == "completed", job["error_message"]
    assert len(download.fake.fetched) == TOTAL
    kept = ["第2话__72_p00001.webp"] if failure != "archive" else _stable_names()
    flattened = sorted(name for name in _root_files(folder) if "_p0" in name)
    assert flattened == sorted(kept)
    assert (folder / "第2话__72_p00001.webp").read_bytes() == served[("72", 1)]
    assert _chapter_contents(folder) == {name: [MARKER] for name in _chapter_dirs()}


@pytest.mark.parametrize("unusable", ["link", "bmp"])
def test_legacy_copies_the_reader_would_not_show_never_drop_a_fresh_page(client, download, downloads, monkeypatch,
                                                                         unusable):
    """Only a legacy page the reader shows as it is (a regular file, not a link — which may point anywhere and go
    away — under a suffix the reader displays) can stand for a fresh page: the fresh page 1 of 72 is flattened."""
    from core import jm_service
    folder = _legacy_flat_album(downloads)
    served = _serve_legacy_bytes(download, folder)
    legacy = folder / "第2话__72_00004.webp"
    if unusable == "link":
        real = jm_service.is_link
        monkeypatch.setattr(jm_service, "is_link", lambda path: Path(path) == legacy or real(path))
    else:
        legacy.rename(legacy.with_suffix(".bmp"))
    job = download(ALL_IDS, organize="flat", skip_existing=False)
    assert job["status"] == "completed", job["error_message"]
    assert sorted(name for name in _root_files(folder) if "_p0" in name) == ["第2话__72_p00001.webp"]
    assert (folder / "第2话__72_p00001.webp").read_bytes() == served[("72", 1)]


def test_legacy_copies_are_not_used_when_the_comic_folder_cannot_be_listed(client, download, downloads, monkeypatch):
    """The comic folder cannot be listed when the legacy pages are looked for (so it is unknown which archive pages
    a loose file stands in for): nothing is dropped, every fresh page is flattened."""
    import sys
    from core import jm_service
    folder = _legacy_flat_album(downloads)
    _serve_legacy_bytes(download, folder)
    _pack(folder)
    real = os.scandir

    def scandir(path=".", *args):
        if sys._getframe(1).f_code.co_name == "_root_entries" and Path(path) == folder:
            raise PermissionError(13, "Access is denied", str(path), 5)
        return real(path, *args)
    monkeypatch.setattr(jm_service.os, "scandir", scandir)
    job = download(ALL_IDS, organize="flat", skip_existing=False)
    assert job["status"] == "completed", job["error_message"]
    assert _root_files(folder) == sorted([ARCHIVE] + _stable_names())
    assert _chapter_contents(folder) == {name: [MARKER] for name in _chapter_dirs()}


def test_flat_download_without_legacy_pages_reads_no_page_bytes(client, download, downloads, monkeypatch):
    """Nothing to compare against: no page is hashed."""
    from core import jm_service
    read = []
    real = jm_service._sha256_of_file
    monkeypatch.setattr(jm_service, "_sha256_of_file", lambda path: read.append(path) or real(path))
    job = download(ALL_IDS, organize="flat", skip_existing=False)
    assert job["status"] == "completed", job["error_message"]
    assert _root_files(downloads / "Name_3001") == _stable_names()
    assert read == []


@pytest.mark.parametrize("packed", [False, True])
def test_legacy_pages_are_read_only_for_a_chapter_that_has_fresh_pages(client, download, downloads, monkeypatch,
                                                                       packed):
    """The legacy pages' bytes are read lazily: nothing when no chapter has fresh pages to flatten, and only the
    legacy pages of the chapter that has (72 grew upstream) — never every legacy page of the comic."""
    from contextlib import contextmanager
    from core import archive_pages, jm_service
    folder = _legacy_flat_album(downloads)
    if packed:
        _pack(folder)
    read = []
    real_hash, real_open = jm_service._sha256_of_file, archive_pages.open_pages

    def hash_file(path):
        read.append(Path(path).relative_to(folder).as_posix())
        return real_hash(path)

    @contextmanager
    def open_pages(index):
        with real_open(index) as read_page:
            def recording(page):
                read.append(f"{ARCHIVE}:{page.name}")
                return read_page(page)
            yield recording
    monkeypatch.setattr(jm_service, "_sha256_of_file", hash_file)
    monkeypatch.setattr(archive_pages, "open_pages", open_pages)
    job = download(ALL_IDS, organize="flat")
    assert job["status"] == "completed", job["error_message"]
    assert download.fake.fetched == [] and read == []
    download.fake.pages["72"] = 5
    job = download(ALL_IDS, organize="flat")
    assert job["status"] == "completed", job["error_message"]
    legacy = ["第2话__72_00004.webp", "第2话__72_00005.webp"]
    assert sorted(name for name in read if "/" not in name or name.startswith(ARCHIVE)) == \
        sorted(f"{ARCHIVE}:{name}" if packed else name for name in legacy)
    assert {name.split("/")[0] for name in read if "/" in name and not name.startswith(ARCHIVE)} == {"第2话__72"}


def test_legacy_archive_busy_while_chapters_are_checked_keeps_each_page_once(client, download, downloads,
                                                                            monkeypatch):
    """R2C-3 (common case): another program holds the archive while the chapters are checked, so nothing is
    recognised and every page is fetched; it has let go by the time the job organizes the folder, and the fresh
    pages identical to the archived legacy pages are dropped: every page once, the archive unchanged. (A held
    archive shows where the index is read: read_index reports it as transient.)"""
    from core import archive_pages, jm_service
    folder = _legacy_flat_album(downloads)
    _serve_legacy_bytes(download, folder)
    _pack(folder)
    names = _archive_names(folder)
    real_read, real_organize = archive_pages.read_index, jm_service.organize_download
    busy = [True]

    def read_index(path):
        if busy[0]:
            return archive_pages.ArchiveIndex(Path(path), "corrupt", reason="being used by another process",
                                              transient=True)
        return real_read(path)

    def organize(*args, **kwargs):
        busy[0] = False
        return real_organize(*args, **kwargs)
    monkeypatch.setattr(archive_pages, "read_index", read_index)
    monkeypatch.setattr(jm_service, "organize_download", organize)
    job = download(ALL_IDS, organize="flat", auto_pack=True, delete_originals=True)
    assert job["status"] == "completed", job["error_message"]
    assert len(download.fake.fetched) == TOTAL
    assert _archive_names(folder) == names
    assert _root_files(folder) == [ARCHIVE]
    assert _reader_pages(client) == TOTAL


def test_duplicate_that_cannot_be_removed_stays_and_blocks_auto_pack(client, download, downloads, monkeypatch):
    """A fresh page identical to a legacy page that cannot be removed (open elsewhere) is neither flattened nor
    copied: it stays in its chapter folder, and this run does not pack (it would be packed under its chapter path).
    Meanwhile the comic folder already has a copy of it (the legacy page it duplicates), so the reader shows that page
    twice (R3-3, documented). The next download removes it and packs."""
    folder = _legacy_flat_album(downloads)
    _serve_legacy_bytes(download, folder)
    locked = folder / "第2话__72" / "00001.webp"
    real = os.remove
    attempts = []

    def remove(path, *args, **kwargs):
        if Path(path) == locked and held[0]:
            attempts.append(path)
            raise PermissionError(13, "being used by another process", str(path), 32)
        return real(path, *args, **kwargs)
    held = [True]
    monkeypatch.setattr(os, "remove", remove)
    job = download(ALL_IDS, organize="flat", skip_existing=False, auto_pack=True, delete_originals=True)
    assert job["status"] == "completed", job["error_message"]
    assert len(attempts) > 1                                                    # retried
    assert not (folder / ARCHIVE).exists()
    assert _chapter_contents(folder)["第2话__72"] == [MARKER, "00001.webp"]
    assert not any("_p0" in name for name in _root_files(folder))
    assert _reader_pages(client) == TOTAL + 1                                   # it and its legacy copy, meanwhile
    held[0] = False
    job = download(ALL_IDS, organize="flat", auto_pack=True, delete_originals=True)
    assert job["status"] == "completed", job["error_message"]
    assert download.fake.fetched == ["72/00002.webp"]                          # 72 has a page of its own: by page
    assert _chapter_contents(folder) == {name: [MARKER] for name in _chapter_dirs()}
    assert _archive_names(folder) == sorted(_legacy_names().values())
    assert _root_files(folder) == [ARCHIVE]
    assert _reader_pages(client) == TOTAL


def test_locked_page_whose_flattened_copy_exists_shows_twice_until_the_next_download(client, download, downloads,
                                                                                     monkeypatch):
    """R3-3: a skip-off re-download; the fresh page 1 of 71 itself is held open (not its flattened copy), so it cannot
    replace 第1话__71_p00001. It stays in its chapter folder while the comic folder still has its flattened copy: the
    page shows twice until the next download, which flattens it over that copy."""
    assert download(ALL_IDS, organize="flat")["status"] == "completed"
    folder = downloads / "Name_3001"
    locked = folder / "第1话__71" / "00001.webp"
    lock = Lock(monkeypatch, "replace", locked)
    job = download(ALL_IDS, organize="flat", skip_existing=False)
    assert job["status"] == "completed", job["error_message"]
    assert len(lock.attempts) > 1                                               # retried
    assert _chapter_contents(folder)["第1话__71"] == [MARKER, "00001.webp"]
    assert _root_files(folder) == _stable_names()
    assert _reader_pages(client) == TOTAL + 1                                   # it and its flattened copy
    fresh = locked.read_bytes()
    lock.release()
    job = download(ALL_IDS, organize="flat")
    assert job["status"] == "completed", job["error_message"]
    assert download.fake.fetched == []
    assert (folder / "第1话__71_p00001.webp").read_bytes() == fresh
    assert _chapter_contents(folder) == {name: [MARKER] for name in _chapter_dirs()}
    assert _reader_pages(client) == TOTAL


# ─── R3-1. a damaged p-name page next to legacy pages is repaired ─────────────


@pytest.mark.parametrize("gif", [True, False])
def test_damaged_new_page_next_to_a_complete_legacy_chapter_is_fetched_again(client, download, downloads, gif):
    """R3-1 (P3): chapter 72's legacy set is complete, and its page 2 was also kept under its p-name because the
    downloaded copy differs from the legacy one (an old-format chapter's GIF page fetched whole next to its sliced
    legacy copy; or a skip-off re-download of a page upstream re-encoded). Then that p-name file gets damaged. It does
    not count as present, but it is still a new-named page of the chapter, so the chapter is not counted whole by its
    legacy names (which left the damaged file for good): page 2 is fetched again and replaces it."""
    folder = _legacy_flat_album(downloads, marker_format=1 if gif else 2)
    _serve_legacy_bytes(download, folder)
    del download.fake.same[("72", 2)]
    if gif:
        download.fake.gif.add(("72", 2))
    job = download(ALL_IDS, organize="flat", skip_existing=gif)
    assert job["status"] == "completed", job["error_message"]
    page = folder / "第2话__72_p00002.webp"
    assert _valid(page) and not (folder / "第2话__72_p00001.webp").exists()     # page 1 dropped (= legacy 00004)
    data = page.read_bytes()
    page.write_bytes(data[:len(data) // 2])
    job = download(ALL_IDS, organize="flat")
    assert job["status"] == "completed", job["error_message"]
    assert sorted(download.fake.fetched) == ["72/00001.webp", f"72/00002.{'gif' if gif else 'webp'}"]
    assert _valid(page)
    assert _chapter_contents(folder) == {name: [MARKER] for name in _chapter_dirs()}
    assert _reader_pages(client) == TOTAL + 1                                   # page 2: legacy copy and new copy
    job = download(ALL_IDS, organize="flat")
    assert job["status"] == "completed", job["error_message"]
    assert download.fake.fetched == ["72/00001.webp"]                          # only the dropped page (documented)


def test_damaged_new_page_whose_download_equals_its_legacy_copy_is_replaced(client, download, downloads):
    """R3-1 (P4): chapter 72 is judged page by page (it has valid p-names) and its p00001 gets damaged. The fresh
    page 1 is byte for byte its legacy copy 第2话__72_00004, but it is not dropped against it while its own p-name is
    there and damaged: it replaces the damaged file once (before: dropped on every download, page 1 fetched every
    time and the damaged file kept for good), and the next download fetches nothing."""
    folder = _legacy_flat_album(downloads)
    served = _serve_legacy_bytes(download, folder)
    del download.fake.same[("72", 1)], download.fake.same[("72", 2)]
    job = download(ALL_IDS, organize="flat", skip_existing=False)               # 72 re-encoded upstream: p-names
    assert job["status"] == "completed", job["error_message"]
    page = folder / "第2话__72_p00001.webp"
    data = page.read_bytes()
    page.write_bytes(data[:len(data) // 2])
    download.fake.same[("72", 1)] = served[("72", 1)]                          # served as the legacy page again
    job = download(ALL_IDS, organize="flat")
    assert job["status"] == "completed", job["error_message"]
    assert download.fake.fetched == ["72/00001.webp"]
    assert page.read_bytes() == served[("72", 1)]
    assert (folder / "第2话__72_00004.webp").read_bytes() == served[("72", 1)]  # the legacy file untouched
    assert _chapter_contents(folder) == {name: [MARKER] for name in _chapter_dirs()}
    assert _reader_pages(client) == TOTAL + 2
    job = download(ALL_IDS, organize="flat")
    assert job["status"] == "completed", job["error_message"]
    assert download.fake.fetched == []
    assert _reader_pages(client) == TOTAL + 2


# ─── F4. the comic folder is listed once per job ──────────────────────────────


def test_comic_folder_is_listed_once_per_job_not_once_per_chapter(client, download, downloads, monkeypatch):
    """F4: the chapter checks share one listing of the comic folder and one of its archives, taken once for the first
    pass. No chapter lists the folder again (each one listed it twice before — its own scan and the archive's — so a
    long flattened comic took minutes before any page was checked): a job for 1 chapter and one for all 3 list the
    folder equally often."""
    import sys
    assert download(ALL_IDS, organize="flat", auto_pack=True)["status"] == "completed"   # loose pages + archive
    folder = downloads / "Name_3001"
    real = os.scandir
    listings = []

    def scandir(path=".", *args, **kwargs):
        if Path(path) == folder:
            callers, frame = [], sys._getframe(1)
            while frame is not None:
                callers.append(frame.f_code.co_qualname)
                frame = frame.f_back
            listings.append(callers)
        return real(path, *args, **kwargs)
    monkeypatch.setattr(os, "scandir", scandir)
    counts = []
    for photo_ids in (["71"], ALL_IDS):
        listings.clear()
        job = download(photo_ids, organize="flat")
        assert job["status"] == "completed", job["error_message"]
        assert download.fake.fetched == []
        assert not [callers for callers in listings if "_download_chapter" in callers]
        checks = [callers[0] for callers in listings if "_AlbumRoot.__init__" in callers]
        assert checks == ["_AlbumRoot.__init__", "candidates"]                  # its entries once, its archives once
        counts.append(len(listings))
    assert counts[0] == counts[1]
    assert _reader_pages(client) == TOTAL
