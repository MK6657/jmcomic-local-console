"""Regression coverage for the second audit. All user data stays isolated."""
import io
import json
import os
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image


@pytest.mark.parametrize("route", ["/api/jobs", "/api/wishlist", "/api/wishlist/check",
    "/api/wishlist/download", "/api/wishlist/import", "/api/settings", "/api/library/123/tags",
    "/api/batch-downloads/preview", "/api/batch-downloads/confirm"])
@pytest.mark.parametrize("payload", [None, [], 7, "text", True])
def test_non_object_json_is_400(client, route, payload):
    response = client.post(route, data=json.dumps(payload), content_type="application/json")
    assert response.status_code == 400
    assert response.is_json


@pytest.mark.parametrize("headers", [
    {"Origin": "https://foreign.example"}, {"Origin": "null"},
    {"Host": "foreign.example"}, {"Sec-Fetch-Site": "cross-site"},
])
def test_foreign_browser_cannot_read_private_settings(client, headers):
    assert client.get("/api/settings", headers=headers).status_code == 403


def test_same_origin_is_allowed(client):
    assert client.get("/api/settings", headers={"Origin": "http://localhost"}).status_code == 200


@pytest.mark.parametrize("patch", [
    {"timeout": 999999}, {"timeout": True}, {"timeout": 5.5},
    {"proxy": None}, {"proxy": "http://[broken"}, {"proxy": "file://host/path"},
    {"proxy": "http://user:secret@host:99999"}, {"client_type": "invalid"},
    {"photo_threads": 100}, {"pack_format": "pdf"}, {"not_a_setting": 1},
])
def test_invalid_settings_reject_whole_patch(client, patch):
    before = client.get("/api/settings").get_json()["settings"]
    response = client.post("/api/settings", json={"retry_times": 7, **patch})
    assert response.status_code == 400
    assert "secret" not in response.get_data(as_text=True)
    assert client.get("/api/settings").get_json()["settings"] == before


def test_settings_normalize_and_support_zero_retries(client):
    response = client.post("/api/settings", json={"retry_times": 0, "client_type": " api ", "timeout": " 41 "})
    assert response.status_code == 200
    settings = response.get_json()["settings"]
    assert (settings["retry_times"], settings["client_type"], settings["timeout"]) == ("0", "api", "41")


def test_import_counts_rejected_settings(client):
    response = client.post("/api/settings/import", data={
        "file": (io.BytesIO(b'{"timeout":999999,"retry_times":0}'), "settings.json"),
    })
    result = response.get_json()
    assert (result["imported"], result["skipped"]) == (1, 1)


@pytest.mark.parametrize("value", ["123\n", "１２３", "1" * 21, None, [], "http://example.test"])
def test_numeric_identifiers_are_strict(value):
    from core.validation import validate_numeric
    assert not validate_numeric(value)


@pytest.mark.parametrize("photo_ids", [[{}], [None], ["not-an-id"], [True], "123"])
def test_job_rejects_invalid_chapters(client, photo_ids):
    assert client.post("/api/jobs", json={"album_id": "123", "photo_ids": photo_ids}).status_code == 400


@pytest.mark.parametrize("body", [{"tags": "wrong"}, {"tags": None}, {"source": "wrong"}, {"unknown": True}])
def test_bad_delete_filter_does_not_clear_tags(client, body):
    from core import database as db
    db.add_album_tag("123", "keep-me", source="user")
    assert client.delete("/api/library/123/tags", json=body).status_code == 400
    assert len(db.get_album_tags("123")) == 1


def create_job(path, status="completed"):
    from core import database as db
    db.insert_job("job_audit", "123", "Audit", [])
    db.update_job("job_audit", status=status, output_path=str(path))


@pytest.fixture
def album_dir(client, tmp_path, monkeypatch):
    from core import path_guard
    from routes import api_preview, api_export
    root = tmp_path / "downloads"
    root.mkdir()
    monkeypatch.setattr(path_guard, "DOWNLOAD_ROOT", root)
    monkeypatch.setattr(api_preview, "DOWNLOAD_ROOT", root)
    monkeypatch.setattr(api_export, "DOWNLOAD_ROOT", root)
    album = root / "Album #1"
    album.mkdir()
    return album


def make_image(path, color="white"):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (16, 16), color).save(path, "PNG")


def test_preview_includes_root_and_nested_images_with_safe_urls(client, album_dir):
    make_image(album_dir / "2.png")
    make_image(album_dir / "10.png")
    make_image(album_dir / "chapter" / "nested" / "1.png")
    create_job(album_dir)
    data = client.get("/api/preview/123").get_json()
    assert data["total_pages"] == 3
    assert data["pages"][0]["url"].endswith("/2.png")
    assert data["pages"][1]["url"].endswith("/10.png")
    assert "%23" in data["pages"][0]["url"]
    assert client.get(data["pages"][0]["url"]).status_code == 200


def test_zip_and_pdf_exports_are_readable(client, album_dir):
    import pikepdf
    make_image(album_dir / "2.png")
    make_image(album_dir / "10.png")
    create_job(album_dir)
    response = client.post("/api/export/job_audit/zip")
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        assert len(archive.namelist()) == 2
        assert archive.testzip() is None
    response.close()
    response = client.post("/api/export/job_audit/pdf")
    assert response.status_code == 200
    with pikepdf.Pdf.open(io.BytesIO(response.data)) as pdf:
        assert len(pdf.pages) == 2
    response.close()


def test_export_ignores_external_links(client, album_dir, tmp_path):
    from core.file_tree import safe_files
    outside = tmp_path / "private.png"
    make_image(outside)
    make_image(album_dir / "safe.png")
    link = album_dir / "private.png"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("This Windows account cannot create file symlinks")
    assert link not in safe_files(album_dir)
    create_job(album_dir)
    response = client.post("/api/export/job_audit/zip")
    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        assert all("private" not in name for name in archive.namelist())
    response.close()


def test_zip_error_cleans_temporary_file(client, album_dir, monkeypatch, tmp_path):
    from routes import api_export
    create_job(album_dir)
    temp_dir = tmp_path / "exports"
    temp_dir.mkdir()
    monkeypatch.setattr(api_export.tempfile, "tempdir", str(temp_dir))
    monkeypatch.setattr(api_export, "safe_files", Mock(side_effect=OSError("failure")))
    assert client.post("/api/export/job_audit/zip").status_code == 500
    assert list(temp_dir.iterdir()) == []


def test_concurrent_pack_uses_unique_temp_files(client, album_dir):
    from concurrent.futures import ThreadPoolExecutor
    from core.packer import CbzPacker
    make_image(album_dir / "1.png")
    barrier = threading.Barrier(2)
    target = album_dir / "test.cbz"
    def pack():
        return CbzPacker().pack(album_dir, target, lambda *_: barrier.wait(timeout=5))
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(pack) for _ in range(2)]
        for future in futures:
            assert future.result() == target
    with zipfile.ZipFile(target) as archive:
        assert archive.testzip() is None
    assert not list(album_dir.glob("*.tmp"))


def test_paused_job_cannot_be_deleted(client, album_dir):
    from core import database as db
    create_job(album_dir, "paused")
    assert client.delete("/api/jobs/job_audit").status_code == 400
    assert db.get_job("job_audit")["status"] == "paused"


class FakePhoto(list):
    photo_id = "456"
    name = "Chapter"
    page_arr = [1]


class FakeAlbum(list):
    name = "Test album"
    author = "Test author"
    tags = []


@pytest.mark.parametrize("outcome", ["partial", "canceled", "success"])
def test_download_terminal_state_and_safe_packing(client, album_dir, monkeypatch, outcome):
    from core import database as db, jm_service as service
    from core.settings import update_settings
    from core.progress import progress_manager
    photo = FakePhoto([object()])
    album = FakeAlbum([photo])
    mock_client = Mock()
    mock_client.get_album_detail.return_value = album
    monkeypatch.setattr(service, "get_client", lambda **kwargs: (mock_client, None))
    monkeypatch.setattr(service, "close_client", lambda _: None)
    monkeypatch.setattr(service, "DOWNLOAD_ROOT", album_dir.parent)
    update_settings({"auto_pack": True, "delete_originals": True, "pack_format": "zip"})
    create_job(album_dir, "running")
    tracker = progress_manager.create_tracker("job_audit")
    def chapter(*args):
        target = args[3]
        make_image(target / "page.png")
        (target / "notes.txt").write_text("keep", encoding="utf-8")
        if outcome == "canceled":
            db.update_job("job_audit", status="canceled")
        elif outcome == "success":
            args[6][0] = 1
        else:
            raise OSError("chapter failed before counting pages")
    monkeypatch.setattr(service, "_download_chapter", chapter)
    try:
        service.download_album_job("job_audit", "123", [])
        job = db.get_job("job_audit")
        assert job["status"] == {"partial": "failed", "canceled": "canceled", "success": "completed"}[outcome]
        output = Path(job["output_path"])
        assert output.is_dir()
        assert (output / "notes.txt").is_file()
        assert (output / "page.png").exists() == (outcome != "success")
        assert bool(list(output.glob("*.zip"))) == (outcome == "success")
    finally:
        progress_manager.remove_tracker("job_audit")


def test_failed_redownload_preserves_previous_image(client, album_dir):
    from core import jm_service as service
    from core.settings import update_settings
    create_job(album_dir, "running")
    update_settings({"skip_existing": False})
    path = album_dir / "00001.webp"
    make_image(path)
    original = path.read_bytes()
    mock_client = Mock()
    mock_client.download_image.side_effect = OSError("network failed")
    failed = []
    service._download_image_single_attempt(
        mock_client, "job_audit", "123", None, "", [1], 0, None,
        "https://invalid.test/image", path, None, [0], 1, failed, None,
        None, album_dir, 2, 10, threading.Lock(),
    )
    assert path.read_bytes() == original
    assert len(failed) == 1
    assert list(album_dir.iterdir()) == [path]


@pytest.mark.skipif(os.name != "nt", reason="Windows named mutex")
def test_single_instance_protects_startup_before_bind(client, tmp_path, monkeypatch):
    import uuid
    import app as application
    monkeypatch.setattr(application, "_MUTEX_NAME", "Local\\JMComic_MutexTest_" + uuid.uuid4().hex)
    monkeypatch.setattr(application, "_PID_FILE", tmp_path / "pid")
    monkeypatch.setattr(application, "_PORT_FILE", tmp_path / "port.json")
    application._acquire_lock()
    try:
        with pytest.raises(SystemExit):
            application._acquire_lock()
    finally:
        application._release_lock()
    application._acquire_lock()
    application._release_lock()


@pytest.mark.skipif(os.name != "nt", reason="Windows directory junction")
def test_export_skips_windows_junction(client, album_dir, tmp_path):
    import subprocess
    from core.file_tree import safe_files
    outside = tmp_path / "private-directory"
    outside.mkdir()
    make_image(outside / "private.png")
    make_image(album_dir / "safe.png")
    junction = album_dir / "linked-directory"
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
                            capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0
    try:
        assert safe_files(album_dir) == [album_dir / "safe.png"]
        create_job(album_dir)
        response = client.post("/api/export/job_audit/zip")
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            assert all("private" not in name for name in archive.namelist())
        response.close()
    finally:
        # Remove the test junction itself, not its target or contents.
        os.rmdir(junction)
    assert (outside / "private.png").is_file()


def test_same_album_worker_is_not_scheduled_twice(client):
    from core import database as db
    from core.job_manager import JobManager
    manager = JobManager()
    db.insert_job("job_first", "123", "same", [])
    db.update_job("job_first", status="canceled")
    manager._running_jobs["job_first"] = Mock()
    manager._running_albums["job_first"] = "123"
    db.delete_job("job_first")
    db.insert_job("job_second", "123", "same", [])
    manager.schedule_next()
    assert db.get_job("job_second")["status"] == "queued"
    assert set(manager._running_jobs) == {"job_first"}


def test_scheduler_failed_thread_start_releases_capacity(client, monkeypatch):
    from core import database as db
    from core.job_manager import JobManager
    from core.progress import progress_manager
    manager = JobManager()
    db.insert_job("job_thread", "123", "test", [])
    monkeypatch.setattr(threading.Thread, "start", Mock(side_effect=RuntimeError("no threads")))
    with pytest.raises(RuntimeError):
        manager.schedule_next()
    assert manager.get_running_count() == 0
    assert db.get_job("job_thread")["status"] == "failed"
    assert progress_manager.get_tracker("job_thread") is None


def test_shared_client_is_retired_after_last_user(client, monkeypatch):
    from core import jm_service as service
    first, _ = service.get_client()
    second, _ = service.get_client()
    assert first is second
    close = Mock()
    monkeypatch.setattr(first.get_root_postman().session, "close", close)
    service.invalidate_option_cache()
    close.assert_not_called()
    service.close_client(first)
    close.assert_not_called()
    service.close_client(second)
    close.assert_called_once()
    service.close_client(second)
    close.assert_called_once()


def test_concurrent_shared_acquisition_builds_one_client(client):
    from concurrent.futures import ThreadPoolExecutor
    from core import jm_service as service
    with ThreadPoolExecutor(max_workers=8) as pool:
        clients = list(pool.map(lambda _: service.get_client()[0], range(20)))
    assert len({id(item) for item in clients}) == 1
    for item in clients:
        service.close_client(item)
    service.invalidate_option_cache()


def test_retired_client_survives_timed_out_background_call(client, monkeypatch):
    from types import MethodType
    from core import jm_service as service
    shared, _ = service.get_client()
    unblock = threading.Event()
    disposed = threading.Event()
    def slow(self):
        assert unblock.wait(timeout=5)
    close = Mock(side_effect=disposed.set)
    monkeypatch.setattr(shared.get_root_postman().session, "close", close)
    try:
        with pytest.raises(TimeoutError):
            service._call_with_timeout("test", 0.01, "timeout", MethodType(slow, shared))
        service.invalidate_option_cache()
        service.close_client(shared)
        close.assert_not_called()
    finally:
        unblock.set()
    assert disposed.wait(timeout=3)
    close.assert_called_once()


def test_diagnose_counts_error_lines(client, tmp_path, monkeypatch):
    from routes import api_system
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "error.log").write_text("2026-09-08 ERROR test\n" * 23, encoding="utf-8")
    monkeypatch.setattr(api_system, "LOG_DIR", log_dir)
    data = client.get("/api/system/diagnose").get_json()
    assert data["error_count_24h"] == 23


def test_json_parse_error_and_plain_text_are_rejected(client):
    assert client.post("/api/jobs", data="{bad", content_type="application/json").status_code == 400
    assert client.post("/api/jobs", data='{"album_id":"123"}', content_type="text/plain").status_code == 415


def test_organize_does_not_nest_or_overwrite_existing_destination(client, album_dir):
    from core.jm_service import organize_download
    make_image(album_dir / "chapter" / "1.png")
    destination = album_dir.parent / "Author" / album_dir.name
    make_image(destination / "chapter" / "1.png", "red")
    original = (destination / "chapter/1.png").read_bytes()
    assert organize_download(str(album_dir), "by_author", SimpleNamespace(author="Author")) is None
    assert (album_dir / "chapter/1.png").is_file()
    assert (destination / "chapter/1.png").read_bytes() == original
    assert not (destination / "chapter/chapter").exists()
