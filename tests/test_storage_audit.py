"""Offline storage audit regressions using ordinary archives and disposable instances."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from io import BytesIO
import os
from pathlib import Path
import threading
import zipfile

from PIL import Image
import pytest

from core import album_maintenance, archive_pages, backup, database as db, local_availability, path_guard, restore, thumb_cache
from core.export_budget import DEFAULT_LIMITS, ExportBudget, ExportLimit
from routes import api_export, api_preview
from test_archive_reading import _job, _reader, downloads, png, write_zip
from test_publication_reader_export import finish_publication, prepare_publication


def _uncompressed_png(color):
    stream = BytesIO()
    with Image.new("RGB", (240, 280), color) as image:
        image.save(stream, "PNG", compress_level=0)
    return stream.getvalue()


def _assert_thumb_color(client, album_id, channel):
    with client.get(f"/api/cover/{album_id}/portrait?w=180") as response:
        assert response.status_code == 200 and response.mimetype == "image/webp"
        with Image.open(BytesIO(response.data)) as thumb:
            assert thumb.size == (180, 210)
            pixel = thumb.getpixel((0, 0))
            assert pixel[channel] > 220 and all(value < 30 for index, value in enumerate(pixel) if index != channel)
        return response.headers["ETag"]


def test_same_size_and_mtime_allow_distinct_archive_generations(client, downloads, tmp_path, monkeypatch):
    monkeypatch.setattr(thumb_cache, "get_cache_dir", lambda: tmp_path / "thumb-cache")
    red, blue = _uncompressed_png("red"), _uncompressed_png("blue")
    first = write_zip(downloads / "A" / "A.cbz", {"1.png": red}, zipfile.ZIP_STORED)
    second = write_zip(downloads / "B" / "B.cbz", {"1.png": blue}, zipfile.ZIP_STORED)
    timestamp = 1_700_000_000_000_000_000
    for path in first, second:
        os.utime(path, ns=(timestamp, timestamp))
    assert archive_pages._signature(first)[:2] == archive_pages._signature(second)[:2]
    _job("red", "101", first.parent)
    _job("blue", "102", second.parent)
    first_etag = _assert_thumb_color(client, "101", 0)
    assert _assert_thumb_color(client, "102", 2) != first_etag

    old_index = archive_pages.read_index(first)
    old_url = client.get("/api/preview/101").json["pages"][0]["url"]
    with client.get(old_url) as served:
        assert served.status_code == 200 and served.data == red
        old_etag = served.headers["ETag"]
    replacement = write_zip(tmp_path / "replacement.cbz", {"1.png": blue}, zipfile.ZIP_STORED)
    os.utime(replacement, ns=(timestamp, timestamp))
    assert archive_pages._signature(replacement)[:2] == old_index.signature[:2]
    os.replace(replacement, first)
    new_index = archive_pages.read_index(first)
    assert new_index.signature[2:4] != old_index.signature[2:4]
    assert archive_pages.version(old_index) != archive_pages.version(new_index)
    with pytest.raises(archive_pages.ArchiveError):
        archive_pages.read_page(old_index, old_index.reader_pages[0])
    with client.get(old_url, headers={"If-None-Match": old_etag}) as served:
        assert served.status_code == 200 and served.data == blue
        assert served.headers["ETag"] != old_etag
    assert _assert_thumb_color(client, "101", 2) != first_etag


def test_offline_status_detects_archive_replacement_without_directory_mtime_change(client, downloads, tmp_path):
    album = downloads / "Book"
    image = png("red")
    path = write_zip(album / "Book.cbz", {"1.txt": b" " * len(image)}, zipfile.ZIP_STORED)
    replacement = write_zip(tmp_path / "new.cbz", {"1.png": image}, zipfile.ZIP_STORED)
    timestamp = 1_700_000_000_000_000_000
    for file in path, replacement, album:
        os.utime(file, ns=(timestamp, timestamp))
    assert archive_pages._signature(path)[:2] == archive_pages._signature(replacement)[:2]
    _job("changed", "123", album)
    assert local_availability.local_state("123").state == "archive_empty"
    os.replace(replacement, path)
    os.utime(album, ns=(timestamp, timestamp))
    assert local_availability.local_state("123").state == "archive"


def test_local_states_uses_latest_record_even_if_superseded(client, downloads, monkeypatch):
    older = downloads / "Older"
    older.mkdir()
    (older / "1.png").write_bytes(png("red"))
    _job("older", "123", older)
    _job("newer", "123", downloads / "Newer")
    with db.transaction() as connection:
        connection.execute("UPDATE resource_catalog SET superseded_at='2026-11-20' WHERE job_id='newer'")
    original = db.resource_records
    queries = []

    def observed(album_ids, *, conn=None, latest_only=False):
        rows = original(album_ids, conn=conn, latest_only=latest_only)
        queries.append((latest_only, [row["job_id"] for row in rows]))
        return rows

    monkeypatch.setattr(db, "resource_records", observed)
    assert local_availability.local_states(["123", "123", "124"]) == {"123": local_availability.MISSING}
    assert queries == [(True, ["newer"])]


@pytest.mark.parametrize("source", ["files", "archive", "mixed"])
def test_flat_pages_keep_original_chapter_and_gapped_page_offsets(client, downloads, source):
    album = downloads / "Flat"
    album.mkdir()
    pages = {"Chapter2__71_p00003.png": png("red"), "Chapter2__71_p00007~2.png": png("blue"),
             "Chapter10__72_p00005.png": png("purple"), "unmarked.png": png("green")}
    identities = {"Chapter2__71_p00003.png": ("71", 2), "Chapter2__71_p00007~2.png": ("71", 6),
                  "Chapter10__72_p00005.png": ("72", 4), "unmarked.png": (None, 3)}
    archived = {}
    for index, (name, data) in enumerate(pages.items()):
        if source == "files" or source == "mixed" and index % 2 == 0:
            (album / name).write_bytes(data)
        else:
            archived[name] = data
    if archived:
        write_zip(album / "Flat.cbz", archived)
    _job("flat", "101", album)

    response = client.get("/api/preview/101")
    assert response.status_code == 200 and response.json["source"] == source
    assert len(response.json["pages"]) == len(pages)
    for page, name in zip(response.json["pages"], pages):
        assert (page["photo_id"], page["offset"]) == identities[name]
        with client.get(page["url"]) as image:
            assert image.status_code == 200 and image.data == pages[name]


@pytest.mark.parametrize("script", ["reader.js", "preview.js"])
def test_flat_archive_online_link_uses_the_original_page_in_both_readers(client, downloads, script):
    album = downloads / "Flat"
    write_zip(album / "Flat.cbz", {"Chapter__71_p00007~2.png": png("red")})
    _job("flat", "101", album)
    pages = client.get("/api/preview/101").json["pages"]
    rendered = _reader(script, "http://x/read/101", 422, {"reason": "archive_corrupt", "message": "读图失败"},
                       1, pages=pages)
    assert rendered["error"]["controls"] == [["A", "在线阅读这一页", "/online/101?chapter=71&offset=6"]]


@pytest.mark.parametrize("source", ["files", "archive-first", "loose-duplicate"])
def test_local_cover_matches_reader_first_page(client, downloads, source):
    album = downloads / "Book"
    later, earlier = album / "Chapter10__72/00001.png", album / "Chapter2__71/00001.png"
    later.parent.mkdir(parents=True)
    later.write_bytes(png("red"))
    if source != "archive-first":
        earlier.parent.mkdir()
        earlier.write_bytes(png("blue"))
    saved = BytesIO()
    with Image.new("RGB", (8, 8), "yellow") as yellow:
        yellow.save(saved, "JPEG")
    if source != "files":
        write_zip(album / "Book.cbz", {"Chapter2__71/00001.jpg": saved.getvalue()})
    _job("ordered", "123", album)

    first = client.get("/api/preview/123").json["pages"][0]
    assert first["photo_id"] == "71"
    with client.get(first["url"]) as page, client.get("/api/cover/123") as cover:
        expected = saved.getvalue() if source == "archive-first" else png("blue")
        assert cover.status_code == page.status_code == 200
        assert cover.data == page.data == expected


def test_warm_archive_reuses_index_for_parallel_pages_and_verified_304(client, downloads, monkeypatch):
    album = downloads / "Warm"
    entries = {f"{number:05d}.png": png("blue", (number + 8, 8)) for number in range(32)}
    path = write_zip(album / "Warm.cbz", entries)
    _job("warm", "101", album)
    url = client.get("/api/preview/101").json["pages"][-1]["url"]
    index = archive_pages.read_index(path)
    calls, member_read = [], archive_pages._read_member

    def counted(archive, page):
        calls.append(page.name)
        return member_read(archive, page)

    def no_reparse(*args):
        pytest.fail("the warm archive parsed its central directory again")

    monkeypatch.setattr(archive_pages, "_read_member", counted)
    monkeypatch.setattr(zipfile.ZipFile, "_RealGetContents", no_reparse)
    with client.get(url) as first:
        assert first.status_code == 200 and first.data == entries[index.reader_pages[-1].name]
        etag = first.headers["ETag"]
    with client.get(url, headers={"If-None-Match": f"W/{etag}"}) as cached:
        assert cached.status_code == 304 and not cached.data and cached.headers["ETag"] == etag
    assert calls == [index.reader_pages[-1].name]
    with client.get(url, headers={"If-None-Match": "*"}) as wildcard:
        assert wildcard.status_code == 304
    assert calls == [index.reader_pages[-1].name] * 2

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(lambda page: archive_pages.read_page(index, page), index.reader_pages)) == list(entries.values())
    assert len(calls) == len(entries) + 2

    replacement = write_zip(downloads / "replacement.cbz", {"00000.png": png("red")})
    file_open = Path.open

    def replace_at_open(file, mode="r", *args, **kwargs):
        if file == path and mode == "rb":
            os.replace(replacement, path)
        return file_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", replace_at_open)
    with pytest.raises(archive_pages.ArchiveBusy):
        archive_pages.read_page(index, index.reader_pages[0])
    assert len(calls) == len(entries) + 2


@pytest.mark.parametrize("constraint", ["entries", "directory"])
def test_ordinary_zip64_reader_accepts_exact_limit_and_checks_before_allocation(tmp_path, monkeypatch, constraint):
    path = tmp_path / "ordinary.cbz"
    with monkeypatch.context() as writer:
        writer.setattr(zipfile, "ZIP_FILECOUNT_LIMIT", 1)
        with zipfile.ZipFile(path, "w") as archive:
            archive.comment = b"A legal ZIP64 archive"
            archive.writestr("1.png", png("red"))
            archive.writestr("content.xml", "<test/>")
    with path.open("rb") as handle:
        end = zipfile._EndRecData(handle)
    assert end[0] == zipfile.stringEndArchive64
    limit, count = (("MAX_ENTRIES", 2) if constraint == "entries"
                    else ("MAX_DIRECTORY_BYTES", end[zipfile._ECD_SIZE]))
    export_limit = "archive_entries" if constraint == "entries" else "archive_metadata_bytes"
    with path.open("rb") as handle:
        api_export._check_export_archive_metadata(handle, ExportBudget(replace(DEFAULT_LIMITS, **{export_limit: count})))
    monkeypatch.setattr(archive_pages, limit, count)
    archive_pages.clear_cache()
    index = archive_pages.read_index(path)
    assert index.status == "ok" and archive_pages.read_page(index, index.reader_pages[0]) == png("red")

    def forbidden(*args, **kwargs):
        pytest.fail("over-budget ZIP reached member allocation")

    monkeypatch.setattr(archive_pages, limit, count - 1)
    monkeypatch.setattr(zipfile, "ZipInfo", forbidden)
    archive_pages.clear_cache()
    assert archive_pages.read_index(path).status == "corrupt"
    archive_pages.clear_cache()
    with path.open("rb") as handle, pytest.raises(ExportLimit):
        api_export._check_export_archive_metadata(handle, ExportBudget(replace(DEFAULT_LIMITS, **{export_limit: count - 1})))


def _full_backup(downloads, destination):
    return backup.create_backup(db.DB_PATH, downloads, destination, include_downloads=True, service_stopped=True)


@pytest.mark.parametrize("constraint", ["entries", "directory"])
def test_backup_limits_check_before_allocation(client, downloads, tmp_path, monkeypatch, constraint):
    (downloads / "1.png").write_bytes(png("red"))
    path = tmp_path / "backup.zip"
    with monkeypatch.context() as writer:
        writer.setattr(zipfile, "ZIP_FILECOUNT_LIMIT", 1)
        _full_backup(downloads, path)
    with path.open("rb") as handle:
        end = zipfile._EndRecData(handle)
        assert end[0] == zipfile.stringEndArchive64
        monkeypatch.setattr(backup, "MAX_FILES", 1)
        monkeypatch.setattr(backup, "MAX_DIRECTORY_BYTES", end[zipfile._ECD_SIZE])
        assert backup.inspect_backup(handle).includes_downloads and not handle.closed

        def forbidden(*args, **kwargs):
            pytest.fail("over-budget backup reached member allocation")

        limit, value = ("MAX_FILES", 0) if constraint == "entries" else ("MAX_DIRECTORY_BYTES", end[zipfile._ECD_SIZE] - 1)
        monkeypatch.setattr(backup, limit, value)
        monkeypatch.setattr(zipfile, "ZipInfo", forbidden)
        with pytest.raises(backup.BackupError, match="元数据超过上限"):
            backup.inspect_backup(handle)
        assert not handle.closed


class _Interrupted(BaseException):
    pass


@pytest.mark.parametrize(("phase", "by_author"), [
    ("at_source", False), ("old_retained", False), ("at_target", False), ("at_target", True),
])
def test_full_restore_recovers_an_interrupted_organize_move(client, downloads, tmp_path, monkeypatch, phase, by_author):
    if not by_author:
        older, shelf, evidence = prepare_publication(downloads, "older")
        finish_publication("older", shelf, evidence)
        older_first = (older.snapshot / "Chapter__71/00001.webp").read_bytes()
        (shelf / "old-version.txt").write_text("keep this", encoding="utf-8")
    newer, published, evidence = prepare_publication(downloads, "newer", 255)
    if by_author:
        evidence[2].update(organize_mode="by_author", author="Writer", auto_pack="false")
    finish_publication("newer", published, evidence)
    first_page = (newer.snapshot / "Chapter__71/00001.webp").read_bytes()
    shelf = downloads / ("Writer/Album" if by_author else "Album")
    staged_relative = published.relative_to(downloads)
    shelf_relative = shelf.relative_to(downloads)
    work = album_maintenance.claim()
    assert work and work["album_id"] == "123"
    rename = Path.rename

    def interrupted(path, destination):
        if phase == "at_source":
            raise _Interrupted()
        result = rename(path, destination)
        if phase == "old_retained" or path == published:
            raise _Interrupted()
        return result

    with monkeypatch.context() as fault:
        fault.setattr(Path, "rename", interrupted)
        with pytest.raises(_Interrupted):
            album_maintenance.run(work, threading.Event())

    if by_author:
        legacy = tmp_path / "legacy-backup.zip"
        with monkeypatch.context() as old_writer:
            old_writer.setattr(backup, "_maintenance_moves", lambda *args: ())
            assert _full_backup(downloads, legacy).manifest["version"] == 1
        with pytest.raises(restore.RestoreError) as rejected:
            restore.restore_backup(legacy, tmp_path / "unverified")
        assert rejected.value.reason == "missing_move_evidence"
        assert not (tmp_path / "unverified").exists()

    archive = tmp_path / "backup.zip"
    info = _full_backup(downloads, archive)
    assert info.manifest["resources"]["maintenance_moves"] == {"123": phase}
    assert db.get_job("newer")["output_path"] == str(published)
    downloads.rename(tmp_path / "original-offline")
    result = restore.restore_backup(archive, tmp_path / "recovered")
    assert result.restored_files == len(info.manifest["resources"]["files"]) and not result.warnings
    monkeypatch.setattr(db, "DB_PATH", result.database)
    monkeypatch.setattr(db, "DB_DIR", result.database.parent)
    for module in path_guard, api_export, api_preview:
        monkeypatch.setattr(module, "DOWNLOAD_ROOT", result.downloads)

    expected = result.downloads / (shelf_relative if phase == "at_target" else staged_relative)
    assert db.get_job("newer")["output_path"] == db.get_completed_job_by_album_id("123")["output_path"] == str(expected)
    assert album_maintenance.public_directory("123") == expected
    first = client.get("/api/preview/123").json["pages"][0]
    with client.get(first["url"]) as image:
        assert image.status_code == 200 and image.data == first_page
    exports = [("/api/library/123/export/cbz", first_page), ("/api/export/newer/cbz", first_page)]
    if not by_author:
        exports.append(("/api/export/older/cbz", older_first))
        old_copies = list(result.downloads.rglob("old-version.txt"))
        assert len(old_copies) == 1 and old_copies[0].read_text(encoding="utf-8") == "keep this"
        if phase != "at_target":
            assert old_copies[0].parent == result.downloads / shelf_relative
    for endpoint, original in exports:
        with client.post(endpoint) as response:
            assert response.status_code == 200, response.json
            with zipfile.ZipFile(BytesIO(response.data)) as output:
                assert output.read("Chapter__71/00001.webp") == original
    album_maintenance.recover()
    resumed = album_maintenance.claim()
    assert resumed is not None
    album_maintenance.run(resumed, threading.Event())
    assert album_maintenance.statuses(["123"])["123"]["state"] == "completed"
    assert album_maintenance.public_directory("123") == result.downloads / shelf_relative


def test_restore_directory_checks_scale_with_current_ancestor_chain(client, tmp_path, monkeypatch):
    checks = []
    for count in (32, 64):
        root = tmp_path / f"source-{count}"
        for number in range(count):
            folder = root / f"book-{number:03}" / "chapter"
            folder.mkdir(parents=True)
            (folder / "1.png").write_bytes(png("white"))
        pack = tmp_path / f"backup-{count}.zip"
        _full_backup(root, pack)
        visited, original = [], restore._plain_directory

        def counted(path):
            visited.append(path)
            return original(path)

        with monkeypatch.context() as trace:
            trace.setattr(restore, "_plain_directory", counted)
            result = restore.restore_backup(pack, tmp_path / f"restored-{count}")
        assert result.restored_files == count
        checks.append(len(visited))
    assert checks[0] < checks[1] < checks[0] * 2.1


def test_restore_rechecks_completed_sibling_directories_before_publish(client, downloads, tmp_path, monkeypatch):
    for book in ("A", "B"):
        path = downloads / book / "1.png"
        path.parent.mkdir()
        path.write_bytes(png("white"))
    pack = tmp_path / "complete.zip"
    _full_backup(downloads, pack)
    copy_member = restore._copy_member
    replacements = []

    def replace_sibling(archive, name, target, expected, deadline):
        copy_member(archive, name, target, expected, deadline)
        if name == "downloads/B/1.png":
            sibling = target.parent.with_name("A")
            sibling.rename(sibling.with_name("A.original"))
            sibling.mkdir()
            replacement = sibling / "keeper.txt"
            replacement.write_text("separate directory", encoding="utf-8")
            replacements.append(replacement)

    monkeypatch.setattr(restore, "_copy_member", replace_sibling)
    target = tmp_path / "recovered"
    with pytest.raises(restore.RestoreError) as rejected:
        restore.restore_backup(pack, target)
    assert rejected.value.reason == "unsafe_target" and not target.exists()
    assert replacements[0].read_text(encoding="utf-8") == "separate directory"


def test_archive_signature_uses_birthtime_when_python312_fstat_ctime_differs(downloads, monkeypatch):
    from types import SimpleNamespace
    from core.packer import CbzPacker

    album = downloads / "Py312Book"
    album.mkdir()
    page_bytes = png("green")
    (album / "001.png").write_bytes(page_bytes)

    orig_lstat = archive_pages.os.lstat
    orig_fstat = archive_pages.os.fstat

    def py312_lstat(path):
        st = orig_lstat(path)
        return SimpleNamespace(
            st_mode=st.st_mode,
            st_file_attributes=getattr(st, "st_file_attributes", 0),
            st_size=st.st_size,
            st_mtime_ns=st.st_mtime_ns,
            st_dev=st.st_dev,
            st_ino=st.st_ino,
            st_birthtime_ns=st.st_ctime_ns,
            st_ctime_ns=st.st_ctime_ns,
        )

    def py312_fstat(fd):
        st = orig_fstat(fd)
        return SimpleNamespace(
            st_mode=st.st_mode,
            st_file_attributes=getattr(st, "st_file_attributes", 0),
            st_size=st.st_size,
            st_mtime_ns=st.st_mtime_ns,
            st_dev=st.st_dev,
            st_ino=st.st_ino,
            st_birthtime_ns=st.st_ctime_ns,
            # Simulate CPython 3.12 Windows gh-112840: fstat().st_ctime_ns is ChangeTime != CreationTime
            st_ctime_ns=st.st_mtime_ns + 8_038_400,
        )

    monkeypatch.setattr(archive_pages.os, "lstat", py312_lstat)
    monkeypatch.setattr(archive_pages.os, "fstat", py312_fstat)
    archive_pages.clear_cache()

    packed = CbzPacker().pack(album, album / "Py312Book.cbz")
    index = archive_pages.read_index(packed)
    assert index.status == "ok" and not index.transient
    assert len(index.reader_pages) == 1
    assert archive_pages.read_page(index, index.reader_pages[0]) == page_bytes

