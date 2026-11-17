"""Unfinished writes never become readable pages or exported content."""
import io
import os
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from PIL import Image


@pytest.mark.parametrize("name", [
    ".jmcomic-staging/attempt-1/page.webp", "chapter/.JMCOMIC-STAGING/raw.gif",
    r"chapter\.jmcomic-staging\page.webp", "00001.abcdefgh.webp", "100001.a0_b2c3d.gif",
    "00001.tmp-abcdefgh.webp",
])
def test_known_unfinished_paths_are_hidden(name):
    from core.storage import is_temporary_path
    assert is_temporary_path(name)


@pytest.mark.parametrize("name", [
    "00001.webp", "00001.foo.webp", "00001.abcdefg.webp", "00001.abcdefghi.webp",
    "00001.abcdefgh.png", "0001.abcdefgh.webp", "00001.FINALDOC.webp", "cover.draft.webp",
    ".jmcomic-staging-book/00001.webp", ".chapter/00001.webp", "00001.tmp-note.webp",
])
def test_normal_dotted_image_names_remain_content(name):
    from core.storage import is_temporary_path
    assert not is_temporary_path(name)


def test_temp_like_chapter_directory_names_are_not_treated_as_temp_files(tmp_path):
    from core.file_tree import safe_files
    chapter = tmp_path / "00001.abcdefgh.webp"
    chapter.mkdir()
    page = chapter / "00001.webp"
    page.write_bytes(b"complete image")
    assert safe_files(tmp_path) == [page]
    assert safe_files(chapter) == [page]


def test_page_is_hidden_until_validated_publish_and_cleanup_is_local(tmp_path):
    from core.file_tree import safe_files
    from core.storage import STAGING_DIRECTORY, staged_file
    target = tmp_path / "00001.webp"
    target.write_bytes(b"old complete image")
    with staged_file(target) as staged:
        assert staged.path.suffix == ".webp"
        assert staged.path.parents[1] == tmp_path / STAGING_DIRECTORY
        assert staged.path.stat().st_dev == target.stat().st_dev
        staged.path.write_bytes(b"new complete image")
        assert safe_files(tmp_path) == [target]
        assert target.read_bytes() == b"old complete image"
        assert staged.publish(lambda p: p.read_bytes() == b"new complete image") == target
        assert target.read_bytes() == b"new complete image"
    assert not staged.path.exists()
    assert not (tmp_path / STAGING_DIRECTORY).exists()


@pytest.mark.parametrize("failure", ["download", "validation", "publication"])
def test_failures_preserve_previous_image_and_clean_attempt(tmp_path, monkeypatch, failure):
    from core import storage
    target = tmp_path / "00001.webp"
    target.write_bytes(b"old image")
    with pytest.raises((RuntimeError, IOError)):
        with storage.staged_file(target) as staged:
            staged.path.write_bytes(b"partial new image")
            if failure == "download":
                raise RuntimeError("synthetic download failure")
            if failure == "publication":
                def fail(*args):
                    raise PermissionError("synthetic locked target")
                monkeypatch.setattr(storage.os, "replace", fail)
            staged.publish(lambda p: failure != "validation")
    assert target.read_bytes() == b"old image"
    assert not staged.path.exists()
    assert not staged.path.parent.exists()


def test_empty_or_changed_payload_is_not_published(tmp_path):
    from core.storage import staged_file
    target = tmp_path / "00001.webp"
    target.write_bytes(b"old image")
    with staged_file(target) as staged:
        with pytest.raises(IOError):
            staged.publish(lambda p: True)
        staged.path.write_bytes(b"first")
        def change_during_validation(path):
            path.write_bytes(b"different")
            return True
        with pytest.raises(IOError, match="校验期间"):
            staged.publish(change_during_validation)
    assert target.read_bytes() == b"old image"


def test_raw_gif_has_its_suffix_and_is_discarded_separately(tmp_path):
    from core.file_tree import safe_files
    from core.storage import staged_file
    target = tmp_path / "00001.webp"
    with staged_file(target) as image:
        with staged_file(target, suffix=".gif") as raw:
            assert raw.path.suffix == ".gif"
            raw.path.write_bytes(b"raw animation")
            image.path.write_bytes(b"converted animation")
            assert safe_files(tmp_path) == []
        assert not raw.path.exists()
        image.publish(lambda p: p.read_bytes() == b"converted animation")
    assert target.read_bytes() == b"converted animation"


def test_concurrent_attempts_keep_the_shared_root_until_the_last_cleanup(tmp_path):
    from core.file_tree import safe_files
    from core.storage import STAGING_DIRECTORY, staged_file
    ready = threading.Barrier(4)
    def write(index):
        target = tmp_path / f"{index:05}.webp"
        with staged_file(target) as staged:
            staged.path.write_bytes(f"page {index}".encode())
            ready.wait(timeout=5)
            staged.publish(lambda p: p.read_bytes() == f"page {index}".encode())
        return target
    with ThreadPoolExecutor(max_workers=4) as pool:
        targets = list(pool.map(write, range(1, 5)))
    assert safe_files(tmp_path) == targets
    assert not (tmp_path / STAGING_DIRECTORY).exists()


def test_existing_directory_cannot_be_a_publication_target(tmp_path):
    from core.storage import STAGING_DIRECTORY, staged_file
    target = tmp_path / "00001.webp"
    target.mkdir()
    with pytest.raises(ValueError):
        with staged_file(target):
            pytest.fail("directory destination must be rejected before writing")
    assert target.is_dir()
    assert not (tmp_path / STAGING_DIRECTORY).exists()


def test_publication_lock_does_not_cover_slow_payload_validation(tmp_path):
    from core.storage import staged_file
    lock = threading.Lock()
    with staged_file(tmp_path / "Album.cbz") as staged:
        staged.path.write_bytes(b"complete archive")
        def validate(path):
            assert not lock.locked()
            return path.read_bytes() == b"complete archive"
        staged.publish(validate, lock=lock)
    assert not lock.locked()


def _directory_link(target, link):
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def _remove_directory_link(link):
    os.rmdir(link) if os.name == "nt" else link.unlink()


def test_existing_staging_junction_is_rejected_without_touching_target(tmp_path):
    from core.storage import STAGING_DIRECTORY, staged_file
    album, outside = tmp_path / "album", tmp_path / "outside"
    album.mkdir()
    outside.mkdir()
    sentinel = outside / "keep.webp"
    sentinel.write_bytes(b"do not touch")
    link = album / STAGING_DIRECTORY
    _directory_link(outside, link)
    try:
        with pytest.raises(ValueError, match="符号链接或目录联接"):
            with staged_file(album / "00001.webp"):
                pytest.fail("must reject before handing a path to the SDK")
        assert list(outside.iterdir()) == [sentinel]
        assert sentinel.read_bytes() == b"do not touch"
    finally:
        _remove_directory_link(link)


def test_replaced_attempt_directory_is_not_followed_during_publish_or_cleanup(tmp_path):
    from core.storage import staged_file
    target = tmp_path / "00001.webp"
    target.write_bytes(b"old image")
    outside = tmp_path / "outside"
    outside.mkdir()
    with staged_file(target) as staged:
        staged.path.write_bytes(b"new image")
        attempt = staged.path.parent
        moved = attempt.with_name(attempt.name + "-moved")
        attempt.rename(moved)
        _directory_link(outside, attempt)
        foreign = outside / staged.path.name
        foreign.write_bytes(b"foreign image")
        try:
            with pytest.raises(ValueError):
                staged.publish(lambda p: True)
            staged.discard()
            assert foreign.read_bytes() == b"foreign image"
            assert target.read_bytes() == b"old image"
        finally:
            _remove_directory_link(attempt)


def test_destination_parent_junction_is_rejected(tmp_path):
    from core.storage import staged_file
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "linked-chapter"
    _directory_link(outside, link)
    try:
        with pytest.raises(ValueError):
            with staged_file(link / "00001.webp"):
                pytest.fail("linked parent must not receive temporary files")
        assert list(outside.iterdir()) == []
    finally:
        _remove_directory_link(link)


def test_scanners_and_exports_ignore_staging_and_old_page_temps(client, tmp_path, monkeypatch):
    from core import database as db, local_availability, path_guard
    from core.file_tree import safe_files
    from core.packer import CbzPacker
    from core.storage import STAGING_DIRECTORY, staged_file
    from routes import api_export, api_preview
    downloads = tmp_path / "downloads"
    album = downloads / "Album"
    album.mkdir(parents=True)
    monkeypatch.setattr(path_guard, "DOWNLOAD_ROOT", downloads)
    monkeypatch.setattr(api_export, "DOWNLOAD_ROOT", downloads)
    monkeypatch.setattr(api_preview, "DOWNLOAD_ROOT", downloads)
    for name in ("00001.abcdefgh.webp", "00001.a0_b2c3d.gif"):
        (album / name).write_bytes(b"legacy unfinished image")
    target = album / "00001.webp"
    with staged_file(target) as staged:
        Image.new("RGB", (8, 8), "red").save(staged.path)
        assert safe_files(album) == []
        assert not local_availability.has_page_image(album)
        assert api_preview._scan_pages(album) == []
        assert api_export._collect_images(album) == []
        assert CbzPacker.packable_images(album) == []
        staged.publish(lambda p: p.stat().st_size > 0)
    ordinary = album / "00002.foo.webp"
    Image.new("RGB", (8, 8), "blue").save(ordinary)
    expected = [target, ordinary]
    assert safe_files(album) == expected
    assert local_availability.has_page_image(album)
    assert [path for _, path in api_preview._scan_pages(album)] == expected
    assert api_export._collect_images(album) == expected
    assert CbzPacker.packable_images(album) == expected
    db.insert_job("storage-job", "123", "fixture", [])
    db.transition_job_status(
        "storage-job", ["queued", "running", "paused", "completed", "failed", "canceled"], "completed",
        output_path=str(album),
    )
    response = client.post("/api/export/storage-job/zip")
    try:
        assert response.status_code == 200
        with zipfile.ZipFile(io.BytesIO(response.data)) as exported:
            assert exported.namelist() == ["Album/00001.webp", "Album/00002.foo.webp"]
    finally:
        response.close()
    # Recognizing an old temp never deletes it; only our private staging is cleaned.
    assert (album / "00001.abcdefgh.webp").exists()
    assert not (album / STAGING_DIRECTORY).exists()


def test_packer_publishes_only_complete_archives_and_preserves_old_on_crc_failure(tmp_path, monkeypatch):
    from core.packer import CbzPacker
    from core.storage import STAGING_DIRECTORY
    image = tmp_path / "00001.png"
    Image.new("RGB", (8, 8), "red").save(image)
    output = tmp_path / "Album.cbz"
    packer = CbzPacker()
    assert packer.pack(tmp_path, output) == output
    with zipfile.ZipFile(output) as archive:
        assert archive.namelist() == ["ComicInfo.xml", "00001.png"]
        assert archive.testzip() is None
    previous = output.read_bytes()
    monkeypatch.setattr(zipfile.ZipFile, "testzip", lambda self: "00001.png")
    with pytest.raises(IOError, match="校验失败"):
        packer.pack(tmp_path, output)
    assert output.read_bytes() == previous
    assert not (tmp_path / STAGING_DIRECTORY).exists()
