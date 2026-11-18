"""Behavioral regressions for the independently reproduced audit findings."""
from contextlib import closing, nullcontext
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image


class Photo:
    def __init__(self, photo_id="102", pages=None):
        self.photo_id = photo_id
        self.page_arr = pages
        self.name = "Chapter"
        self.from_album = None

    def __len__(self):
        return len(self.page_arr or ())


class Album(list):
    album_id = "123"
    name = "Audit fixture"
    author = "Offline"
    tags = []
    episode_list = [("101", 1, "Old"), ("102", 2, "New")]


def test_timed_out_chapter_returns_a_stable_snapshot(client):
    from core import jm_service as service
    entered, release, exited = threading.Event(), threading.Event(), threading.Event()
    photo = Photo(pages=[])
    album = Album([photo])
    def check(working):
        entered.set()
        try:
            assert release.wait(3)
            working.page_arr.extend(["1.webp", "2.webp"])
        finally:
            exited.set()
    failed = []
    try:
        result = service.check_album_photos(SimpleNamespace(check_photo=check), album, timeout=0.03, failed=failed)
        assert entered.is_set() and result[0].page_arr == []
        assert failed == [result[0]]
    finally:
        release.set()
        assert exited.wait(2)
    assert result[0].page_arr == [] and photo.page_arr == []


def test_failed_sdk_mutation_is_not_published_as_a_success(client):
    from core import jm_service as service
    photo = Photo(pages=[])
    def check(working):
        working.page_arr.append("partial.webp")
        raise ConnectionError("chapter failed after a partial update")
    failed = []
    result = service.check_album_photos(SimpleNamespace(check_photo=check), Album([photo]), failed=failed)
    assert result[0].page_arr == [] and failed == [result[0]]


def test_partial_detail_does_not_poison_either_cache_and_next_request_recovers(client, monkeypatch):
    from core import database as db, jm_service as service
    entered, release, exited = threading.Event(), threading.Event(), threading.Event()
    calls = []
    def check(photo):
        calls.append(photo)
        if len(calls) == 1:
            entered.set()
            try:
                assert release.wait(3)
                photo.page_arr = ["1.webp", "2.webp", "3.webp"]
            finally:
                exited.set()
        else:
            photo.page_arr = ["1.webp", "2.webp", "3.webp"]
    upstream = SimpleNamespace(check_photo=check, get_album_detail=lambda _: Album([Photo()]))
    monkeypatch.setattr(service, "get_client", lambda: (upstream, None))
    monkeypatch.setattr(service, "close_client", lambda _: None)
    original = service.check_album_photos
    monkeypatch.setattr(service, "check_album_photos",
                        lambda *args, **kwargs: original(*args, timeout=0.03, **kwargs))
    key = (str(db.DB_PATH), "123")
    try:
        first = service.get_album_detail_cached("123")
        assert entered.is_set()
        assert first["photos"][0]["page_count"] is None
        assert first["photos"][0]["page_count_known"] is False
        assert first["details_complete"] is False
        assert db.get_cached_album_detail("123") is None and key not in service._detail_cache
    finally:
        release.set()
        assert exited.wait(2)
    assert first["photos"][0]["page_count"] is None
    second = service.get_album_detail_cached("123")
    assert second["photos"][0]["page_count"] == 3 and second["details_complete"] is True
    assert db.get_cached_album_detail("123")["detail"]["photos"][0]["page_count"] == 3
    assert service.get_album_detail_cached("123") == second and len(calls) == 2


def test_legacy_unknown_zero_cache_is_refetched_but_confirmed_empty_is_cacheable(client, monkeypatch):
    from core import database as db, jm_service as service
    old = {"album_id": "123", "photos": [{"photo_id": "102", "page_count": 0}], "chapter_count": 1}
    db.set_cached_album_detail("123", json.dumps(old))
    service._detail_cache[(str(db.DB_PATH), "123")] = {"data": old, "ts": time.time()}
    complete = {**old, "details_complete": True,
                "photos": [{"photo_id": "102", "page_count": 0, "page_count_known": True}]}
    fetch = Mock(return_value=complete)
    monkeypatch.setattr(service, "get_album_detail", fetch)
    assert service.get_album_detail_cached("123") == complete
    assert service.get_album_detail_cached("123") == complete
    fetch.assert_called_once()


@pytest.mark.parametrize("reported_pages", [None, []])
def test_normal_sdk_return_without_page_list_stays_unknown(client, monkeypatch, reported_pages):
    from core import database as db, jm_service as service
    checks = []
    def check(photo):
        checks.append(photo)
        photo.page_arr = reported_pages
    upstream = SimpleNamespace(check_photo=check, get_album_detail=lambda _: Album([Photo()]))
    monkeypatch.setattr(service, "get_client", lambda: (upstream, None))
    monkeypatch.setattr(service, "close_client", lambda _: None)
    detail = service.get_album_detail_cached("123")
    again = service.get_album_detail_cached("123")
    if reported_pages is None:
        assert detail["details_complete"] is False and detail["photos"][0]["page_count"] is None
        assert again["photos"][0]["page_count_known"] is False
        assert len(checks) == 2 and db.get_cached_album_detail("123") is None
    else:
        assert detail["details_complete"] is True and detail["photos"][0]["page_count"] == 0
        assert again == detail and len(checks) == 1
        assert db.get_cached_album_detail("123") is not None


@pytest.mark.parametrize("image_threads", [1, 2])
@pytest.mark.parametrize("replacement_fails", [False, True])
def test_existing_bomb_image_never_aborts_other_pages(client, tmp_path, monkeypatch, image_threads, replacement_fails):
    from core import database as db, jm_service as service, path_guard
    from core.settings import update_settings
    folder = tmp_path / "downloads"
    folder.mkdir()
    monkeypatch.setattr(path_guard, "DOWNLOAD_ROOT", folder)
    monkeypatch.setattr(service, "DOWNLOAD_ROOT", folder)
    db.insert_job("audit_images", "123", "images", [])
    assert db.transition_job_status("audit_images", ["queued"], "running")
    update_settings({"image_threads": image_threads, "retry_times": 0, "skip_existing": True})
    bomb = folder / "00001.webp"
    Image.new("RGB", (20, 20), "red").save(bomb, "WEBP")
    original = bomb.read_bytes()
    Image.new("RGB", (3, 3), "blue").save(folder / "00002.webp", "WEBP")
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)
    assert service._valid_image(bomb) is False
    monkeypatch.setattr(service, "_chapter_output_dir", lambda *args: folder)
    monkeypatch.setattr(service, "get_client", lambda **kwargs: (object(), None))
    monkeypatch.setattr(service, "close_client", lambda _: None)
    downloaded = []
    def download(_, *, img_save_path, **kwargs):
        downloaded.append(img_save_path)
        if replacement_fails:
            raise ConnectionError("synthetic failure")
        Image.new("RGB", (3, 3), "green").save(img_save_path, "WEBP")
    monkeypatch.setattr(service.image_download, "download_image", download)
    class Chapter(list):
        photo_id = "102"
    photo = Chapter([SimpleNamespace(download_url=f"https://fixture.invalid/{i}", scramble_id=None, is_gif=False)
                     for i in range(2)])
    done, pending, failed = [0], [], []
    service._download_chapter("audit_images", "123", Album([photo]), folder, photo, 2,
                              done, pending, failed, None, None, threading.Lock(), 1, 1)
    assert len(downloaded) == 1 and not failed
    if replacement_fails:
        assert done == [1] and len(pending) == 1
        assert pending[0]["idx"] == 0 and bomb.read_bytes() == original
    else:
        assert done == [2] and not pending and service._valid_image(bomb)


def test_invalid_new_image_is_recorded_and_old_page_is_preserved(client, tmp_path, monkeypatch):
    from core import database as db, jm_service as service, path_guard
    from core.settings import update_settings
    folder = tmp_path / "downloads"
    folder.mkdir()
    monkeypatch.setattr(path_guard, "DOWNLOAD_ROOT", folder)
    update_settings({"retry_times": 0})
    target = folder / "00001.webp"
    target.write_bytes(b"previous page")
    db.insert_job("audit_bad_image", "123", "bad", [])
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)
    monkeypatch.setattr(service.image_download, "download_image",
                        lambda _, **kwargs: Image.new("RGB", (20, 20)).save(kwargs["img_save_path"], "WEBP"))
    failed, pending = [], []
    service._download_image_single_attempt(None, "audit_bad_image", "123", None, "", [1], 0,
        SimpleNamespace(is_gif=False), "https://fixture.invalid/page", target, None,
        [0], 1, failed, pending, None, folder, 2, 1, threading.Lock())
    assert len(failed) == 1 and not pending and target.read_bytes() == b"previous page"


@pytest.mark.parametrize("fault", ["wishlist", "baseline", "both_statuses"])
def test_real_completion_path_never_reports_partially_committed_success(client, tmp_path, monkeypatch, fault):
    from core import database as db, jm_service as service, path_guard, update_store
    from core.progress import progress_manager
    from core.settings import update_settings
    folder = tmp_path / "downloads"
    folder.mkdir()
    monkeypatch.setattr(path_guard, "DOWNLOAD_ROOT", folder)
    monkeypatch.setattr(service, "DOWNLOAD_ROOT", folder)
    db.add_wishlist("123", "audit")
    db.insert_job("audit_complete", "123", "audit", ["102"])
    assert db.transition_job_status("audit_complete", ["queued"], "running")
    db.update_wishlist_download_status("123", "downloading")
    update_store.note_download_started("123", [("101", 1, "Old")], False)
    with closing(db.get_db()) as connection:
        with connection:
            if fault == "wishlist":
                connection.execute("CREATE TRIGGER reject_done BEFORE UPDATE ON wishlist "
                                   "WHEN NEW.download_status='completed' BEGIN SELECT RAISE(ABORT,'fixture fault'); END")
            elif fault == "baseline":
                connection.execute("CREATE TRIGGER reject_done BEFORE UPDATE ON album_update_checks "
                                   "BEGIN SELECT RAISE(ABORT,'fixture fault'); END")
            else:
                connection.execute("CREATE TRIGGER reject_done BEFORE UPDATE ON jobs "
                                   "WHEN NEW.status IN ('completed','failed') BEGIN SELECT RAISE(ABORT,'fixture fault'); END")
    photo = Photo(pages=["1.webp"])
    upstream = SimpleNamespace(get_album_detail=lambda _: Album([photo]), check_photo=lambda _: None)
    monkeypatch.setattr(service, "get_client", lambda **kwargs: (upstream, None))
    monkeypatch.setattr(service, "close_client", lambda _: None)
    monkeypatch.setattr(service, "download_budget", lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr(service, "is_readable", lambda _: True)
    monkeypatch.setattr(service, "_reusable_album_dir", lambda _: None)
    monkeypatch.setattr(service, "_mark_chapter_current", lambda *args: None)
    def chapter(*args):
        target = service._chapter_output_dir(args[3], args[4]) / '00001.webp'
        with Image.new('RGB', (3, 3), 'green') as image:
            image.save(target, 'WEBP')
        args[10].confirm(target)
        args[6][0] = 1
    monkeypatch.setattr(service, "_download_chapter", chapter)
    terminals = []
    finish = service.finish_download
    def observe_finish(job, status, **kwargs):
        terminals.append(status)
        return finish(job, status, **kwargs)
    monkeypatch.setattr(service, 'finish_download', observe_finish)
    update_settings({"photo_threads": 1, "auto_pack": False, "organize_mode": "none"})
    tracker = progress_manager.create_tracker("audit_complete")
    events = tracker.subscribe()
    try:
        service.download_album_job("audit_complete", "123", ["102"])
        assert terminals[:2] == ['completed', 'failed']
        assert db.get_job("audit_complete")["status"] == ("running" if fault == "both_statuses" else "failed")
        assert not db.resource_records(["123"])
        assert db.get_wishlist("123")["download_status"] != "completed"
        with closing(db.get_db()) as connection:
            baseline = connection.execute("SELECT baseline_ids FROM album_update_checks WHERE album_id='123'").fetchone()[0]
        assert json.loads(baseline) == ["101"]
        assert tracker.is_done()
        assert not any(event == "completed" for event, payload in list(events.queue))
    finally:
        progress_manager.remove_tracker("audit_complete")


def test_missing_tracker_cannot_change_a_canceled_task(client):
    from core import database as db, jm_service as service
    db.insert_job("audit_canceled", "123", "canceled", [])
    assert db.transition_job_status("audit_canceled", ["queued"], "canceled")
    service.download_album_job("audit_canceled", "123", [])
    assert db.get_job("audit_canceled")["status"] == "canceled"
