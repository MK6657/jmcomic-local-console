"""Offline observations at the actual image-fetch, reader and filesystem boundaries."""
import io
import json
import re
import threading
import zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from fake_transport import owned_client


def image_bytes(color):
    data = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(data, "WEBP", lossless=True)
    return data.getvalue()


class Photo(list):
    def __init__(self, photo_id, name, pages):
        self.photo_id, self.name = photo_id, name
        self.page_arr = [f"{number:05d}.webp" for number in range(1, pages + 1)]
        super().__init__(SimpleNamespace(download_url=f"https://images.invalid/{photo_id}/{page}", scramble_id=None)
                         for page in self.page_arr)


class Book(list):
    name, author, tags = "Book", "Writer", []

    @property
    def episode_list(self):
        return [(photo.photo_id, number, photo.name) for number, photo in enumerate(self, 1)]


@pytest.fixture
def pipeline(client, tmp_path, monkeypatch):
    from core import album_maintenance, database as db, jm_service, path_guard
    from core.progress import progress_manager
    from core.settings import update_settings
    from routes import api_export, api_preview

    root = tmp_path / "downloads"
    root.mkdir()
    for module in (jm_service, path_guard, api_preview, api_export):
        monkeypatch.setattr(module, "DOWNLOAD_ROOT", root)
    update_settings({"image_threads": 3, "photo_threads": 2, "retry_times": 0, "skip_existing": False})
    books = {"100": Book([Photo("71", "第一章", 1), Photo("72", "第二章", 3)])}
    default = image_bytes("red")
    state = SimpleNamespace(root=root, books=books, payloads={}, calls=Counter(), on_fetch=lambda url: None)
    lock = threading.Lock()

    def construct(shared=False):
        return owned_client(SimpleNamespace(get_album_detail=books.__getitem__, check_photo=lambda photo: None)), None

    def fetch(client, *, img_url, img_save_path, **kwargs):
        with lock:
            state.calls[img_url] += 1
        state.on_fetch(img_url)
        Path(img_save_path).write_bytes(state.payloads.get(img_url, default))

    monkeypatch.setattr(jm_service, "get_client", construct)
    monkeypatch.setattr(jm_service.image_download, "download_image", fetch)

    def run(job_id, album_id="100", selection=(), stop_event=None, on_tracker=None):
        db.insert_job(job_id, album_id, "Book", list(selection))
        assert db.transition_job_status(job_id, ["queued"], "running")
        tracker = progress_manager.create_tracker(job_id)
        if on_tracker:
            on_tracker(tracker)
        try:
            jm_service.download_album_job(job_id, album_id, list(selection), stop_event=stop_event)
            work = album_maintenance.claim()
            if work:
                album_maintenance.run(work, stop_event or threading.Event())
            return db.get_job(job_id)
        finally:
            progress_manager.remove_tracker(job_id)

    state.run = run
    return state


def stored_pages(folder, fmt):
    if fmt is None:
        return {path.relative_to(folder).as_posix(): path.read_bytes() for path in folder.rglob("*.webp")}
    with zipfile.ZipFile(folder / f"{folder.name}.{fmt}") as archive:
        assert archive.testzip() is None
        return {name: archive.read(name) for name in archive.namelist() if name.endswith(".webp")}


def test_v4_resume_only_adopts_signed_pages_even_when_skip_is_enabled(pipeline):
    from core import jm_service, publication
    from core.download_checkpoint import DownloadCheckpoint
    from core.download_layout import chapter_output_dir
    photo = pipeline.books['100'][1]
    old = pipeline.root / 'Book_100'
    old.mkdir()
    folder = chapter_output_dir(old, photo)
    valid, replaced, unrelated = [folder / f'{i:05d}.webp' for i in (1, 2, 3)]
    for path in (valid, replaced, unrelated):
        path.write_bytes(image_bytes('red'))
    receipt = DownloadCheckpoint('old-job', {'total_pages': 3, 'output_path': str(old)})
    receipt.confirm(valid)
    receipt.confirm(replaced)
    replaced.unlink()
    replaced.write_bytes(image_bytes('blue') + b'different-length')
    ws = publication.Workspace(pipeline.root, 'old-job', '100', old, receipt)
    seeds = ws.seed([photo], old, True, jm_service._valid_image, jm_service.image_is_gif)
    assert [p.name for p in seeds] == [valid.name] and seeds[0].read_bytes() == valid.read_bytes()
    assert replaced.exists() and unrelated.exists()


@pytest.mark.parametrize("fmt", [None, "cbz", "zip"], ids=["loose", "cbz", "zip"])
def test_flat_whole_subset_rename_then_content_change(pipeline, client, fmt):
    from core.settings import update_settings
    update_settings({"organize_mode": "flat", "auto_pack": bool(fmt), "pack_format": fmt or "cbz",
                     "delete_originals": bool(fmt)})
    book = pipeline.books["100"]
    book[0].name = book[1].name
    def published(job_id, selection=()):
        row = pipeline.run(job_id, selection=selection)
        assert row['status'] == 'completed'
        folder = Path(row['output_path'])
        pages = stored_pages(folder, fmt)
        keys = [re.fullmatch(r'[^/]+__([0-9]+)_p([0-9]+)\.webp', name) for name in pages]
        assert all(keys) and len(keys) == 4
        if fmt:
            assert not list(folder.rglob('*.webp'))
        return {match.groups(): data for match, data in zip(keys, pages.values())}
    original = published('whole')
    assert len(set(original.values())) == 1  # Four distinct page identities may contain identical bytes.

    book.reverse()
    book[0].name = "章名已改"
    assert published('subset', ['72']) == original
    assert published('again') == original
    assert client.get("/api/preview/100").get_json()["total_pages"] == 4

    changed = image_bytes("blue")
    pipeline.payloads[book[0][1].download_url] = changed
    newest = {**original, ('72', '00002'): changed}
    assert published('changed', ['72']) == newest
    assert published('repeat_changed', ['72']) == newest
    preview = client.get('/api/preview/100').get_json()
    assert preview['total_pages'] == 4
    assert Counter(client.get(page['url']).data for page in preview['pages']) == Counter(newest.values())
    for job_id, count, blue in [('whole', 4, 0), ('changed', 3, 1)]:
        with client.post(f'/api/export/{job_id}/zip') as response:
            assert response.status_code == 200
            with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
                data = [archive.read(name) for name in archive.namelist() if name.endswith('.webp')]
                assert len(data) == count and data.count(changed) == blue


def test_unknown_flat_and_chapter_files_survive_pack_and_cleanup(pipeline):
    from core.download_finalize import finalize_files
    from core.download_layout import chapter_output_dir
    folder = pipeline.root / "Book_100"
    legacy = {"第一章__71_00001.webp": image_bytes("red"), "手工目录/00001.webp": image_bytes("green"),
              "第一章__71/00001.webp": image_bytes("gold"), "第二章__72/00099.webp": image_bytes("gray"),
              "第二章__72/cover.webp": image_bytes("blue"), "notes.txt": b"notes"}
    for name, data in legacy.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (folder / "第二章__72/.jm-chapter.json").write_text(json.dumps({"photo_id": "72", "format": 2}))
    photos = pipeline.books['100']
    def legacy_finalize():
        for photo in photos:
            chapter = chapter_output_dir(folder, photo)
            for number in range(1, len(photo) + 1):
                (chapter / f'{number:05d}.webp').write_bytes(image_bytes('red'))
        finalize_files(str(folder), 'flat', photos, photos, keep_location=True,
                       settings={'auto_pack': 'true', 'delete_originals': 'true'},
                       check_control=lambda: None, path_changed=lambda path: None, archiving=lambda: None)
    for job_id in ("first", "again"):
        legacy_finalize()
        assert {name: (folder / name).read_bytes() for name in legacy} == legacy
        assert len(stored_pages(folder, "cbz")) == 4 + len(legacy) - 1
    archive = folder / "Book_100.cbz"
    previous = archive.read_bytes()
    (folder / "手工目录/00001.webp").write_bytes(image_bytes("white"))
    legacy_finalize()
    assert archive.read_bytes() == previous
    assert (folder / "手工目录/00001.webp").read_bytes() == image_bytes("white")


def test_three_publications_behind_pause_restore_one_shelf_for_all_jobs(pipeline, client):
    from core import album_maintenance, database as db
    from core.settings import update_settings
    update_settings({'organize_mode': 'none', 'auto_pack': False})
    db.insert_job('pause', '100', 'Book', [])
    assert db.transition_job_status('pause', ['queued'], 'paused')
    for job_id, selection in [('first', ['71']), ('second', ['72']), ('third', ['71'])]:
        assert pipeline.run(job_id, selection=selection)['status'] == 'completed'
    assert not album_maintenance.claim()
    assert db.transition_job_status('pause', ['paused'], 'canceled')
    work = album_maintenance.claim()
    assert work and work['payload']['preferred_directory'] == 'Book_100'
    album_maintenance.run(work, threading.Event())
    assert album_maintenance.statuses(['100'])['100']['state'] == 'completed'
    shelf = pipeline.root / 'Book_100'
    assert album_maintenance.public_directory('100') == shelf
    assert {row['output_path'] for row in db.resource_records(['100'])} == {str(shelf)}
    assert {client.get(f'/api/jobs/{job_id}').json['job']['output_path']
            for job_id in ('first', 'second', 'third')} == {str(shelf)}
    assert client.get('/api/preview/100').json['total_pages'] == 4


class InFlight:
    def __init__(self):
        self.changed = threading.Condition()
        self.active, self.peak = Counter(), Counter()
        self.release = threading.Event()

    @contextmanager
    def held(self, name):
        with self.changed:
            self.active[name] += 1
            self.peak[name] = max(self.peak[name], self.active[name])
            self.changed.notify_all()
        try:
            assert self.release.wait(12)
            yield
        finally:
            with self.changed:
                self.active[name] -= 1
                self.changed.notify_all()

    def wait_for(self, **counts):
        with self.changed:
            assert self.changed.wait_for(lambda: all(self.active[key] >= count for key, count in counts.items()), 6)


@pytest.mark.parametrize("limit", [3, 7])
def test_nested_downloads_leave_foreground_and_prefetch_capacity(pipeline, tmp_path, monkeypatch, limit):
    from core import online_reader as reader
    from core.settings import update_settings
    update_settings({"image_threads": limit, "photo_threads": 2, "max_running_jobs": 2})
    for album_id in ("100", "200"):
        pipeline.books[album_id] = Book([Photo(album_id + chapter, chapter, 3) for chapter in ("1", "2")])
    probe = InFlight()
    payload = image_bytes("green")

    def download(url):
        with probe.held("download"):
            pass

    def read(url, photo_id, number, deadline, background=False):
        with probe.held("prefetch" if background else "foreground"):
            return payload

    pipeline.on_fetch = download
    monkeypatch.setattr(reader, "CACHE_ROOT", tmp_path / "reader")
    monkeypatch.setattr(reader, "_download", read)
    monkeypatch.setattr(reader, "_PREFETCH_IDLE", 0.05)
    reader.start()
    reader.clear_cache()
    for photo_id, pages in [("901", 3), ("902", 1), ("903", 1), ("904", 1), ("905", 1)]:
        reader._remember_photos([Photo(photo_id, "online", pages)])
    cached = reader._page_path("901", 0)
    cached.parent.mkdir(parents=True)
    cached.write_bytes(payload)
    with ThreadPoolExecutor(6) as pool:
        jobs = [pool.submit(pipeline.run, "job" + album_id, album_id) for album_id in pipeline.books]
        try:
            probe.wait_for(download=limit)
            assert reader.image_file("901", 0) == cached
            foreground = [pool.submit(reader.image_file, str(photo_id), 0) for photo_id in range(902, 906)]
            probe.wait_for(download=limit, prefetch=2, foreground=4)
        finally:
            probe.release.set()
        assert all(future.result(6)["status"] == "completed" for future in jobs)
        assert all(future.result(6).read_bytes() == payload for future in foreground)
    with reader._prefetch_cond:
        assert reader._prefetch_cond.wait_for(lambda: not reader._prefetch_active, 4)
        owners = list(reader._prefetch_threads)
    for owner in owners:
        owner.join(2)
    reader.clear_cache()
    assert probe.peak == {"download": limit, "foreground": 4, "prefetch": 2}
    assert not any(probe.active.values()) and sum(pipeline.calls.values()) == 12


@pytest.mark.parametrize("retry", [False, True], ids=["first-pass", "second-pass"])
@pytest.mark.parametrize("action", ["cancel", "shutdown"])
def test_waiting_image_stops_without_another_fetch_or_releasing_the_owner(pipeline, monkeypatch, retry, action):
    from core import download_capacity
    from core.settings import update_settings
    from core.task_commands import cancel_download
    update_settings({"image_threads": 1, "retry_times": int(retry)})
    pipeline.books.update({"100": Book([Photo("71", "Owner", 1)]), "200": Book([Photo("72", "Waiter", 1)])})
    owner_url = pipeline.books["100"][0][0].download_url
    waiter_url = pipeline.books["200"][0][0].download_url
    probe = InFlight()
    retry_ready, proceed, waiting, stop = (threading.Event() for _ in range(4))
    wait = download_capacity._available.wait

    def observe_wait(timeout):
        waiting.set()
        return wait(timeout)

    def fetch(url):
        if url == owner_url:
            with probe.held("owner"):
                pass
        elif retry and pipeline.calls[url] == 1:
            raise OSError("first pass unavailable")

    def observe_retry(tracker):
        push = tracker.push

        def progress(event, payload):
            push(event, payload)
            if payload.get("message", "").startswith("正在重试"):
                retry_ready.set()
                assert proceed.wait(8)

        monkeypatch.setattr(tracker, "push", progress)

    monkeypatch.setattr(download_capacity._available, "wait", observe_wait)
    pipeline.on_fetch = fetch
    with ThreadPoolExecutor(2) as pool:
        try:
            if retry:
                waiter = pool.submit(pipeline.run, "waiter", "200", (), stop, observe_retry)
                assert retry_ready.wait(4)
            owner = pool.submit(pipeline.run, "owner", "100")
            probe.wait_for(owner=1)
            if retry:
                proceed.set()
            else:
                waiter = pool.submit(pipeline.run, "waiter", "200", (), stop)
            assert waiting.wait(3)
            if action == "cancel":
                assert cancel_download("waiter")[0]
            else:
                stop.set()
            row = waiter.result(2)
            assert row["status"] == ("canceled" if action == "cancel" else "running")
            assert row["done_pages"] == 0 and not row["error_message"]
            assert pipeline.calls[waiter_url] == int(retry) and not owner.done()
        finally:
            proceed.set()
            probe.release.set()
        assert owner.result(4)["status"] == "completed"
    assert probe.active["owner"] == download_capacity._active == 0
    assert pipeline.run("later", "200")["status"] == "completed"


def test_shutdown_after_author_move_does_not_nest_the_resumed_directory(pipeline, monkeypatch):
    from core import album_maintenance, database as db
    from core.job_manager import JobManager
    from core.packer import CbzPacker
    from core.settings import update_settings
    update_settings({"organize_mode": "by_author", "auto_pack": True, "delete_originals": True})
    manager = JobManager()
    at_pack, release = threading.Event(), threading.Event()
    original_pack = CbzPacker.pack

    def after_move(self, *args, **kwargs):
        if not at_pack.is_set():
            at_pack.set()
            assert release.wait(8)
        return original_pack(self, *args, **kwargs)

    monkeypatch.setattr(CbzPacker, "pack", after_move)
    db.insert_job("restart", "100", "Book", [])
    manager.start()
    folder = pipeline.root / "Writer/Book_100"
    try:
        assert at_pack.wait(4)
        assert Path(db.get_job("restart")["output_path"]) == folder
        assert not manager.pause_job("restart")[0]
        assert not manager.stop(timeout=0)
    finally:
        release.set()
        assert manager.stop(timeout=5)
    assert db.get_job('restart')['status'] == 'completed' and not (folder / 'Book_100.cbz').exists()
    assert album_maintenance.statuses(['100'])['100']['state'] == 'pending'
    assert len(stored_pages(folder, None)) == 4
    resumed = JobManager()
    resumed.start()
    try:
        with resumed._changed:
            assert resumed._changed.wait_for(lambda: album_maintenance.statuses(['100'])['100']['state'] == 'completed', 6)
    finally:
        assert resumed.stop(timeout=3)
    assert db.get_job("restart")["status"] == "completed"
    assert Path(db.get_job("restart")["output_path"]) == folder and not (folder.parent / "Writer").exists()
    assert len(stored_pages(folder, "cbz")) == 4 and not list(folder.rglob("*.webp"))
    assert list(pipeline.calls.values()) == [1] * 4


def test_failed_download_reports_exact_failed_page_count_without_off_by_one(pipeline, monkeypatch):
    from core import jm_service, publication

    pipeline.books["600"] = Book([Photo("601", "六页章", 6)])
    pipeline.on_fetch = lambda url: (_ for _ in ()).throw(OSError("upstream page broken"))
    all_failed = pipeline.run("all-6-failed", "600")
    assert all_failed["status"] == "failed"
    assert all_failed["done_pages"] == 0
    assert "（共 6 页失败）" in all_failed["error_message"]
    assert "（共 7 页失败）" not in all_failed["error_message"]
    assert "下载不完整" not in all_failed["error_message"]
    assert publication.private_path(Path(all_failed["output_path"]), pipeline.root)
    assert not (pipeline.root / "Book_600").exists()

    bad_url = pipeline.books["100"][1][-1].download_url
    pipeline.on_fetch = lambda url: (_ for _ in ()).throw(OSError("last page broken")) if url == bad_url else None
    one_failed = pipeline.run("one-of-4-failed", "100")
    assert one_failed["status"] == "failed"
    assert one_failed["done_pages"] == 3
    assert "下载不完整" not in one_failed["error_message"]
    assert publication.private_path(Path(one_failed["output_path"]), pipeline.root)
    assert not (pipeline.root / "Book_100").exists()

    monkeypatch.setattr(jm_service, "_download_chapter", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("chapter crashed")))
    chapter_crashed = pipeline.run("chapter-crashed", "100")
    assert chapter_crashed["status"] == "failed"
    assert chapter_crashed["done_pages"] == 0
    assert "下载不完整：0/4 页" in chapter_crashed["error_message"]
    assert not (pipeline.root / "Book_100").exists()
