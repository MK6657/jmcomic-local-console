"""Chapter collision, bookmark cleanup and complete-PDF regressions."""
import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image


@pytest.fixture
def output_dir(client, tmp_path, monkeypatch):
    from core import path_guard
    root = tmp_path / "downloads"
    album = root / "Album"
    album.mkdir(parents=True)
    monkeypatch.setattr(path_guard, "DOWNLOAD_ROOT", root)
    return album


class Photo(list):
    def __init__(self, name, photo_id):
        super().__init__([object()])
        self.name = name
        self.photo_id = photo_id


@pytest.mark.parametrize("names", [
    ("Same", "Same"), ("a/b", "a?b"), ("UPPER", "upper"),
    ("x" * 101, "x" * 100 + "y"),
])
def test_concurrent_chapters_never_share_images(client, output_dir, monkeypatch, names):
    from core import jm_service as service, database as db
    photos = [Photo(names[0], "123"), Photo(names[1], "456")]
    db.insert_job("job_chapters", "999", "test", [])
    db.update_job("job_chapters", status="running")
    def image_download(*args):
        photo, destination = args[4], args[5]
        color = "red" if photo.photo_id == "123" else "blue"
        Image.new("RGB", (8, 8), color).save(destination / "00001.png")
    monkeypatch.setattr(service, "_download_chapter_image", image_download)
    def run(photo):
        service._download_chapter("job_chapters", "999", photos, output_dir, photo, 2,
            [0], [], [], None, threading.Event(), threading.Lock(), 1, 10)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(run, photos))
    folders = [service._chapter_output_dir(output_dir, photo) for photo in photos]
    assert folders[0] != folders[1]
    assert len(list(output_dir.iterdir())) == 2
    assert (folders[0] / "00001.png").read_bytes() != (folders[1] / "00001.png").read_bytes()
    for photo, folder in zip(photos, folders):
        assert json.loads((folder / ".jm-chapter.json").read_text())["photo_id"] == photo.photo_id


def test_legacy_folders_preserved_and_retry_reuses_owned_folder(client, output_dir):
    from core.jm_service import _chapter_output_dir
    old = output_dir / "Chapter"
    conflicting = output_dir / "Chapter__123"
    for folder in (old, conflicting):
        folder.mkdir()
        (folder / "00001.png").write_bytes(b"old-content-do-not-touch")
    photo = SimpleNamespace(name="Chapter", photo_id="123")
    target = _chapter_output_dir(output_dir, photo)
    assert target not in (old, conflicting)
    assert _chapter_output_dir(output_dir, photo) == target
    for folder in (old, conflicting):
        assert (folder / "00001.png").read_bytes() == b"old-content-do-not-touch"


@pytest.mark.parametrize("removed,remaining,expected", [
    ("failed", "queued", "queued"), ("failed", "running", "downloading"),
    ("failed", "paused", "downloading"), ("failed", "completed", "completed"),
    ("completed", "queued", "queued"), ("canceled", "failed", "failed"),
    ("finished", "queued", "queued"), ("failed", None, "none"),
])
def test_bulk_cleanup_recalculates_from_remaining_jobs(client, output_dir, removed, remaining, expected):
    from core import database as db
    db.add_wishlist("123", "test", "author", "")
    db.add_wishlist("456", "unrelated", "author", "")
    db.update_wishlist_download_status("456", "queued")
    old_status = "failed" if removed == "finished" else removed
    db.insert_job("job_old", "123", "old", [])
    db.update_job("job_old", status=old_status, output_path=str(output_dir))
    if remaining:
        db.insert_job("job_keep", "123", "current", [])
        db.update_job("job_keep", status=remaining)
    note = output_dir / "keep.txt"
    note.write_text("user file", encoding="utf-8")
    response = client.post("/api/jobs/clear/" + removed)
    assert response.status_code == 200
    assert response.get_json()["deleted"] == 1
    assert db.get_job("job_old") is None
    assert db.get_wishlist("123")["download_status"] == expected
    assert db.get_wishlist("456")["download_status"] == "queued"
    assert note.read_text() == "user file"
    if remaining:
        assert db.get_job("job_keep")["status"] == remaining


def test_single_delete_preserves_other_queued_job_status(client):
    from core import database as db
    db.add_wishlist("123", "test", "author", "")
    for job in ("job_old", "job_keep"):
        db.insert_job(job, "123", "test", [])
    assert client.delete("/api/jobs/job_old").status_code == 200
    assert db.get_wishlist("123")["download_status"] == "queued"
    assert db.get_job("job_keep") is not None


def test_cleanup_rolls_back_if_bookmark_update_fails(client, monkeypatch):
    from core import database as db
    db.add_wishlist("123", "test", "author", "")
    db.update_wishlist_download_status("123", "failed")
    db.insert_job("job_old", "123", "old", [])
    db.update_job("job_old", status="failed")
    monkeypatch.setattr(db, "_refresh_wishlist_after_job_delete", Mock(side_effect=RuntimeError("rollback")))
    with pytest.raises(RuntimeError):
        db.clear_jobs_by_status("failed")
    assert db.get_job("job_old")["status"] == "failed"
    assert db.get_wishlist("123")["download_status"] == "failed"


def setup_export(output_dir):
    from core import database as db
    db.insert_job("job_pdf", "123", "pdf", [])
    db.update_job("job_pdf", status="completed", output_path=str(output_dir))
    Image.new("RGB", (8, 8), "white").save(output_dir / "good.png")


def test_pdf_refuses_corrupt_page_and_cleans_temp_file(client, output_dir, tmp_path, monkeypatch):
    from routes import api_export
    setup_export(output_dir)
    (output_dir / "broken.png").write_bytes(b"not an image")
    exports = tmp_path / "exports"
    exports.mkdir()
    monkeypatch.setattr(api_export.tempfile, "tempdir", str(exports))
    response = client.post("/api/export/job_pdf/pdf")
    assert response.status_code == 422
    assert response.is_json
    assert response.get_json()["failed_images"] == ["broken.png"]
    assert "取消" in response.get_json()["message"]
    assert "Content-Disposition" not in response.headers
    assert list(exports.iterdir()) == []
    assert (output_dir / "broken.png").read_bytes() == b"not an image"


def test_pdf_fallback_also_refuses_missing_pages(client, output_dir, monkeypatch):
    import img2pdf
    from routes import api_export
    setup_export(output_dir)
    Image.new("RGB", (8, 8), "red").save(output_dir / "bad-on-fallback.png")
    monkeypatch.setattr(api_export, "_needs_pillow_conversion", lambda path: False)
    convert = Mock(side_effect=ValueError("force fallback"))
    monkeypatch.setattr(img2pdf, "convert", convert)
    original = api_export._image_to_jpeg_bytes
    monkeypatch.setattr(api_export, "_image_to_jpeg_bytes",
        lambda path: None if path.name == "bad-on-fallback.png" else original(path))
    response = client.post("/api/export/job_pdf/pdf")
    assert response.status_code == 422
    assert response.get_json()["failed_images"] == ["bad-on-fallback.png"]
    assert convert.call_count == 1


def test_valid_pdf_preserves_all_pages_including_alpha(client, output_dir):
    import pikepdf
    setup_export(output_dir)
    Image.new("RGBA", (8, 8), (255, 0, 0, 128)).save(output_dir / "alpha.png")
    response = client.post("/api/export/job_pdf/pdf")
    assert response.status_code == 200
    with pikepdf.Pdf.open(io.BytesIO(response.data)) as pdf:
        assert len(pdf.pages) == 2
    response.close()
