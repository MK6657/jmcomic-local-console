"""Export resource/lifetime regressions; all data is disposable and offline."""
import io
import threading
import time
import zipfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from core import archive_pages, database as db, export_budget, path_guard
from core.export_budget import BudgetedWriter, ExportBudget, ExportLimit, ExportTimeout
from routes import api_export


def png(mode="RGB", size=(16, 16)):
    stream = io.BytesIO()
    with Image.new(mode, size, "white") as image:
        image.save(stream, "PNG")
    return stream.getvalue()


@pytest.fixture
def env(client, tmp_path, monkeypatch):
    root = tmp_path / "downloads"
    album = root / "Album"
    album.mkdir(parents=True)
    exports = tmp_path / "exports"
    exports.mkdir()
    monkeypatch.setattr(path_guard, "DOWNLOAD_ROOT", root)
    monkeypatch.setattr(api_export, "DOWNLOAD_ROOT", root)
    monkeypatch.setattr(api_export.tempfile, "tempdir", str(exports))
    monkeypatch.setattr(api_export, "_EXPORT_SLOT", threading.BoundedSemaphore(1))
    archive_pages.clear_cache()
    db.insert_job("capacity", "123", "capacity", [])
    db.transition_job_status("capacity", ["queued"], "completed", output_path=str(album))
    (album / "1.png").write_bytes(png())
    responses = []

    def post(fmt="pdf", target=None):
        response = (target or client).post(f"/api/export/capacity/{fmt}")
        responses.append(response)
        return response

    def limit(**changes):
        monkeypatch.setattr(export_budget, "DEFAULT_LIMITS", replace(export_budget.DEFAULT_LIMITS, **changes))

    def archive(entries, remove_loose=True):
        if remove_loose:
            (album / "1.png").unlink(missing_ok=True)
        target = album / "Album.cbz"
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, content in entries.items():
                zf.writestr(name, content)
        return target

    yield SimpleNamespace(client=client, album=album, exports=exports, post=post, limit=limit, archive=archive)
    for response in responses:
        response.close()
    assert list(exports.iterdir()) == []
    assert api_export._EXPORT_SLOT.acquire(blocking=False), "export lease leaked"
    api_export._EXPORT_SLOT.release()
    archive_pages.clear_cache()


def rejected(response, status, reason=None):
    assert response.status_code == status, response.get_data(as_text=True)
    assert response.is_json and "Content-Disposition" not in response.headers
    if reason:
        assert response.json["reason"] == reason


@pytest.mark.parametrize("fmt", ["zip", "pdf"])
def test_unclosed_response_retains_one_shared_lease(env, fmt):
    response = env.post(fmt)
    assert response.status_code == 200
    assert len(list(env.exports.iterdir())) == 1
    before = time.monotonic()
    busy = env.post("pdf" if fmt == "zip" else "zip")
    rejected(busy, 503, "export_busy")
    assert int(busy.headers["Retry-After"]) > 0
    assert time.monotonic() - before < 1
    response.close()
    response.close()  # close may happen both in the server and its wrapper
    assert list(env.exports.iterdir()) == []
    next_response = env.post("zip")
    assert next_response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(next_response.data)) as zf:
        assert zf.testzip() is None and len(zf.infolist()) == 1
    next_response.close()


def test_concurrent_generation_never_queues_an_http_worker(env, app, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    real = api_export._build_pdf
    result = []

    def blocked(*args):
        entered.set()
        assert release.wait(5)
        return real(*args)

    monkeypatch.setattr(api_export, "_build_pdf", blocked)

    def generate():
        with app.test_client() as other:
            result.append(other.post("/api/export/capacity/pdf"))

    worker = threading.Thread(target=generate)
    worker.start()
    try:
        assert entered.wait(3)
        before = time.monotonic()
        rejected(env.post("zip"), 503, "export_busy")
        assert time.monotonic() - before < 1
        assert worker.is_alive()
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and len(result) == 1
    assert result[0].status_code == 200
    result[0].close()


@pytest.mark.parametrize("fmt", ["zip", "pdf"])
@pytest.mark.parametrize("changes", [
    {"files": 1}, {"pages": 1}, {"source_bytes": 100}, {"page_bytes": 30},
])
def test_collection_limits_are_413_and_leave_no_output(env, fmt, changes):
    (env.album / "2.png").write_bytes(png())
    env.limit(**changes)
    rejected(env.post(fmt), 413, "export_limit")
    assert list(env.exports.iterdir()) == []


@pytest.mark.parametrize("fmt", ["zip", "pdf"])
def test_scan_stops_even_when_entries_will_not_be_exported(env, fmt):
    (env.album / "1.png").unlink()
    for number in range(12):
        (env.album / str(number)).mkdir()
    env.limit(scan_entries=3)
    rejected(env.post(fmt), 413, "export_limit")


@pytest.mark.parametrize("fmt", ["zip", "pdf"])
@pytest.mark.parametrize("failure", ["directory", "iterator", "entry"])
def test_unreadable_chapter_refuses_partial_export_then_recovers(env, monkeypatch, fmt, failure):
    first, second = env.album / "01", env.album / "02"
    first.mkdir()
    second.mkdir()
    (env.album / "1.png").rename(first / "1.png")
    (second / "1.png").write_bytes(png())
    scandir = api_export.os.scandir

    class BadEntry:
        def __init__(self, entry):
            self.entry = entry

        def __getattr__(self, name):
            return getattr(self.entry, name)

        def stat(self, **kwargs):
            raise PermissionError("cannot read page metadata")

    def unreadable(path):
        if Path(path) != second:
            return scandir(path)
        if failure == "directory":
            raise PermissionError("cannot open chapter")

        class Listing:
            def __enter__(self):
                self.entries = scandir(path)
                return self

            def __iter__(self):
                for entry in self.entries:
                    if failure == "iterator":
                        raise PermissionError("cannot finish listing chapter")
                    yield BadEntry(entry)

            def __exit__(self, *exc):
                self.entries.close()
        return Listing()

    with monkeypatch.context() as patch:
        patch.setattr(api_export.os, "scandir", unreadable)
        response = env.post(fmt)
        assert response.status_code == 503, f"expected refusal, got HTTP {response.status_code}"
        rejected(response, 503, "export_source_unreadable")
    assert list(env.exports.iterdir()) == []
    response = env.post(fmt)  # the failed scan released its export slot
    assert response.status_code == 200
    if fmt == "zip":
        with zipfile.ZipFile(io.BytesIO(response.data)) as zf:
            assert zf.namelist() == ["Album/01/1.png", "Album/02/1.png"] and zf.testzip() is None
    else:
        import pikepdf
        with pikepdf.Pdf.open(io.BytesIO(response.data)) as pdf:
            assert len(pdf.pages) == 2
    response.close()


def test_scan_checks_deadline_between_ignored_entries(env, monkeypatch):
    (env.album / "1.png").unlink()
    for number in range(10):
        (env.album / str(number)).mkdir()
    now = [0]
    budget = ExportBudget(clock=lambda: now[0])
    visit = budget.visit

    def advance():
        visit()
        now[0] += 100

    monkeypatch.setattr(budget, "visit", advance)
    monkeypatch.setattr(api_export, "ExportBudget", lambda: budget)
    rejected(env.post(), 503, "export_timeout")
    assert budget.scanned == 3


@pytest.mark.parametrize("fmt", ["zip", "pdf"])
def test_archive_pages_participate_in_the_merged_limits(env, fmt):
    env.archive({"1.png": png(), "2.png": png(), "3.png": png()}, remove_loose=False)
    env.limit(pages=2)
    rejected(env.post(fmt), 413, "export_limit")


@pytest.mark.parametrize("fmt", ["zip", "pdf"])
def test_duplicate_archive_page_does_not_spend_a_second_page(env, fmt):
    env.archive({"1.jpg": png(), "2.png": png()}, remove_loose=False)
    env.limit(files=2, pages=2)
    response = env.post(fmt)
    assert response.status_code == 200
    if fmt == "pdf":
        import pikepdf
        with pikepdf.Pdf.open(io.BytesIO(response.data)) as pdf:
            assert len(pdf.pages) == 2
    else:
        with zipfile.ZipFile(io.BytesIO(response.data)) as zf:
            assert zf.namelist() == ["Album/1.png", "Album/2.png"]
    response.close()


@pytest.mark.parametrize("fmt", ["zip", "pdf"])
@pytest.mark.parametrize("changes", [{"archive_entries": 1}, {"archive_metadata_bytes": 1}])
def test_archive_metadata_is_bounded_before_sdk_index_allocation(env, monkeypatch, fmt, changes):
    env.archive({"1.png": png(), "2.png": png()})
    env.limit(**changes)

    def forbidden(*args):
        pytest.fail("oversized metadata reached SDK index allocation")

    monkeypatch.setattr(archive_pages, "read_index", forbidden)
    rejected(env.post(fmt), 413, "export_limit")


def test_known_archive_source_bytes_are_checked_during_merge(tmp_path):
    page1 = archive_pages.ArchivePage("1.png", "1.png", "", 80)
    page2 = archive_pages.ArchivePage("2.png", "2.png", "", 80)
    index = archive_pages.ArchiveIndex(tmp_path / "x.cbz", "ok", (page1, page2))
    budget = ExportBudget(replace(export_budget.DEFAULT_LIMITS, source_bytes=100))
    with pytest.raises(ExportLimit):
        api_export._merge_export_pages(tmp_path, [], index, budget)
    assert budget.files == 1 and budget.source_bytes == 80


@pytest.mark.parametrize("fmt", ["zip", "pdf"])
def test_output_size_is_enforced_before_publication(env, fmt):
    env.limit(output_bytes=100)
    rejected(env.post(fmt), 413, "export_limit")
    assert list(env.exports.iterdir()) == []


def test_combined_extraction_and_conversion_share_the_staging_budget(env):
    alpha = png("RGBA")
    env.archive({"1.png": alpha})
    env.limit(staging_bytes=len(alpha) + 100)
    rejected(env.post(), 413, "export_limit")


def test_extraction_counts_each_page_in_staging_budget(env):
    data = png()
    env.archive({"1.png": data, "2.png": data})
    env.limit(staging_bytes=len(data) * 2 - 1)
    rejected(env.post(), 413, "export_limit")


def test_rgba_is_a_temporary_jpeg_path_and_real_pdf_is_complete(env, monkeypatch):
    import img2pdf
    import pikepdf
    (env.album / "2.png").write_bytes(png("RGBA"))
    real = img2pdf.convert
    paths = []

    def capture(pages, **kwargs):
        # Underlying values are paths; iteration supplies bounded per-page reads.
        paths.extend(pages[:])
        assert all(isinstance(path, str) and Path(path).is_file() for path in paths)
        assert len(pages) == 1  # the conversion library never sees a whole book
        if len(paths) == 2:
            assert Path(paths[1]).suffix == ".jpg" and "jm-pdf-jpeg-" in paths[1]
        return real(pages, **kwargs)

    monkeypatch.setattr(img2pdf, "convert", capture)
    response = env.post()
    assert response.status_code == 200
    with pikepdf.Pdf.open(io.BytesIO(response.data)) as pdf:
        assert len(pdf.pages) == 2
    assert not Path(paths[1]).exists()
    assert list(env.exports.glob("jm-pdf-*")) == []
    response.close()


def test_fallback_frees_old_jpegs_and_rewinds_partial_output(env, monkeypatch):
    import img2pdf
    import pikepdf
    (env.album / "1.png").write_bytes(png("RGBA"))
    (env.album / "2.png").write_bytes(png("RGBA"))
    env.limit(staging_bytes=12000)  # JPEGs and single-page PDF spools share this budget
    real, previous, calls = img2pdf.convert, [], []

    def fail_once(pages, **kwargs):
        calls.append(1)
        paths = pages[:]
        if len(calls) == 1:
            previous.extend(Path(paths[0]).parent.iterdir())
            kwargs["outputstream"].write(b"partial PDF")
            raise ValueError("force fallback")
        assert all(not Path(path).exists() for path in previous)
        assert kwargs["outputstream"].tell() == 0
        assert all(Path(path).is_file() for path in paths)
        return real(pages, **kwargs)

    monkeypatch.setattr(img2pdf, "convert", fail_once)
    response = env.post()
    assert response.status_code == 200 and len(calls) == 3
    with pikepdf.Pdf.open(io.BytesIO(response.data)) as pdf:
        assert len(pdf.pages) == 2
    response.close()


def test_fallback_refuses_any_missing_page_and_cleans_everything(env, monkeypatch):
    import img2pdf
    (env.album / "2.png").write_bytes(png())
    monkeypatch.setattr(api_export, "_needs_pillow_conversion", lambda *args: False)
    calls = []

    def fail(*args, **kwargs):
        calls.append(1)
        raise ValueError("force fallback")

    monkeypatch.setattr(img2pdf, "convert", fail)
    real = api_export._image_to_jpeg_file
    monkeypatch.setattr(api_export, "_image_to_jpeg_file",
                        lambda path, *args: None if path.name == "2.png" else real(path, *args))
    response = env.post()
    rejected(response, 422)
    assert response.json["failed_images"] == ["2.png"] and len(calls) == 1
    assert list(env.exports.iterdir()) == []


def test_real_bad_image_stays_422_with_no_missing_page_pdf(env):
    (env.album / "2.png").write_bytes(b"bad image")
    response = env.post()
    rejected(response, 422)
    assert response.json["failed_images"] == ["2.png"]


def test_pixel_budget_applies_before_decode(env):
    env.limit(page_pixels=100)
    rejected(env.post(), 413, "export_limit")


@pytest.mark.parametrize("stage", ["conversion", "output", "after_output"])
def test_deadline_cancels_every_generation_phase(env, monkeypatch, stage):
    import img2pdf
    now = [0]
    budget = ExportBudget(clock=lambda: now[0])
    monkeypatch.setattr(api_export, "ExportBudget", lambda: budget)
    if stage == "conversion":
        (env.album / "1.png").write_bytes(png("RGBA"))
        real = api_export._image_to_jpeg_file

        def late(*args):
            result = real(*args)
            now[0] = 301
            return result

        monkeypatch.setattr(api_export, "_image_to_jpeg_file", late)
    else:
        real = img2pdf.convert

        def late(pages, **kwargs):
            if stage == "output":
                now[0] = 301
                kwargs["outputstream"].write(b"late")
            else:
                real(pages, **kwargs)
                now[0] = 301

        monkeypatch.setattr(img2pdf, "convert", late)
    response = env.post()
    rejected(response, 503, "export_timeout")
    assert response.headers["Retry-After"] == "5"
    assert list(env.exports.iterdir()) == [] and budget.staging_bytes == 0


@pytest.mark.parametrize("fmt", ["zip", "pdf"])
def test_send_failure_cleans_output_and_releases_lease(env, monkeypatch, fmt):
    with monkeypatch.context() as patch:
        patch.setattr(api_export, "send_file", lambda *a, **k: (_ for _ in ()).throw(OSError(28, "disk full")))
        rejected(env.post(fmt), 500)
    assert list(env.exports.iterdir()) == []
    response = env.post(fmt)
    assert response.status_code == 200
    response.close()


@pytest.mark.parametrize("registration", [1, 2])
def test_callback_registration_failure_closes_created_response(env, monkeypatch, registration):
    real, calls = api_export._ExportResponse.call_on_close, []

    def fail(self, callback):
        calls.append(1)
        if len(calls) == registration:
            raise RuntimeError("callback registration failed")
        return real(self, callback)

    with monkeypatch.context() as patch:
        patch.setattr(api_export._ExportResponse, "call_on_close", fail)
        rejected(env.post(), 500)
    assert list(env.exports.iterdir()) == []
    response = env.post()
    assert response.status_code == 200
    response.close()


def test_wsgi_start_response_failure_does_not_leak_response_or_lease(env, app):
    from werkzeug.test import EnvironBuilder
    builder = EnvironBuilder(path="/api/export/capacity/pdf", method="POST")

    def broken_start(*args):
        raise RuntimeError("cannot send response")

    try:
        with pytest.raises(RuntimeError, match="cannot send response"):
            app.wsgi_app(builder.get_environ(), broken_start)
    finally:
        builder.close()
    assert list(env.exports.iterdir()) == []
    response = env.post()
    assert response.status_code == 200
    response.close()


def test_after_request_failure_cleans_unpublished_response(env, app):
    def fail(response):
        raise RuntimeError("after_request failed")

    app.after_request_funcs.setdefault(None, []).append(fail)
    try:
        with pytest.raises(RuntimeError, match="after_request failed"):
            env.post()
    finally:
        app.after_request_funcs[None].remove(fail)
    assert list(env.exports.iterdir()) == []
    response = env.post()
    assert response.status_code == 200
    response.close()


@pytest.mark.parametrize("fmt", ["zip", "pdf"])
def test_early_error_return_releases_lease(env, fmt):
    (env.album / "1.png").unlink()
    rejected(env.post(fmt), 404)
    (env.album / "1.png").write_bytes(png())
    response = env.post(fmt)
    assert response.status_code == 200
    response.close()


def test_collection_exception_cleanup_replaces_old_eager_scan_mock(env, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(api_export, "_collect_files", lambda *a, **k: (_ for _ in ()).throw(OSError("scan failed")))
        rejected(env.post("zip"), 500)
    assert list(env.exports.iterdir()) == []
    response = env.post("zip")
    assert response.status_code == 200
    response.close()


@pytest.mark.parametrize("fmt", ["zip", "pdf"])
def test_disk_full_during_managed_write_is_clear_and_does_not_fallback(env, monkeypatch, fmt):
    real = BudgetedWriter.write

    def disk_full(self, data):
        real(self, data[:2])
        raise OSError(28, "disk full")

    with monkeypatch.context() as patch:
        patch.setattr(BudgetedWriter, "write", disk_full)
        response = env.post(fmt)
        rejected(response, 500)
        assert "临时空间不足" in response.json["message"]
    assert list(env.exports.iterdir()) == []
    response = env.post(fmt)
    assert response.status_code == 200
    response.close()


def test_budgeted_writer_high_water_truncation_and_real_size():
    budget = ExportBudget(replace(export_budget.DEFAULT_LIMITS, output_bytes=6, staging_bytes=6))
    output = io.BytesIO()
    writer = BudgetedWriter(output, budget)
    writer.write(b"123456")
    writer.seek(0)
    writer.write(b"x")
    assert writer.size == 6
    writer.seek(6)
    with pytest.raises(ExportLimit):
        writer.write(b"7")
    assert output.getvalue() == b"x23456"
    staged = BudgetedWriter(io.BytesIO(), budget, "page")
    staged.write(b"123456")
    staged.seek(0)
    staged.truncate()
    assert budget.staging_bytes == 0
    staged.write(b"new")
    assert budget.staging_bytes == 3


def test_actual_archive_read_size_checks_budget_and_integrity():
    page = archive_pages.ArchivePage("1.png", "1.png", "", 2)
    budget = ExportBudget(replace(export_budget.DEFAULT_LIMITS, page_bytes=3))
    budget.add_source(page, 2, True)
    with pytest.raises(ExportLimit):
        api_export._read_archive_page(lambda p: b"1234", page, budget)
    with pytest.raises(archive_pages.ArchiveError):
        api_export._read_archive_page(lambda p: b"1", page, budget)


def test_pdf_input_is_bounded_when_original_grows(tmp_path):
    path = tmp_path / "page.png"
    path.write_bytes(b"12")
    budget = ExportBudget(replace(export_budget.DEFAULT_LIMITS, source_bytes=3, page_bytes=4))
    budget.add_source(path, 2, True)
    path.write_bytes(b"1234")
    with pytest.raises(ExportLimit):
        api_export._PdfInput(path, budget).read_bytes()


def test_build_pdf_keeps_two_argument_signature(tmp_path):
    import pikepdf
    path = tmp_path / "alpha.png"
    path.write_bytes(png("RGBA"))
    stream = io.BytesIO()
    assert api_export._build_pdf([path], stream) == 1
    with pikepdf.Pdf.open(io.BytesIO(stream.getvalue())) as pdf:
        assert len(pdf.pages) == 1


def test_default_deadline_and_capacity_values_are_explicit():
    limits = export_budget.DEFAULT_LIMITS
    assert (limits.files, limits.pages, limits.seconds) == (10_000, 2_000, 300)
    assert limits.source_bytes == limits.staging_bytes == limits.output_bytes == 2 * 1024 ** 3
    assert limits.page_bytes == 128 * 1024 ** 2
    now = [0]
    budget = ExportBudget(clock=lambda: now[0])
    now[0] = 300
    with pytest.raises(ExportTimeout):
        budget.check()


@pytest.fixture
def metadata_archive(env, monkeypatch):
    """Real central records with independently forged EOCD/ZIP64 counts."""
    def make(count, declared=1, variant="plain", duplicate_metadata=False):
        import struct

        (env.album / "1.png").unlink(missing_ok=True)
        stream = io.BytesIO()
        with monkeypatch.context() as patch:
            if "zip64" in variant:
                patch.setattr(zipfile, "ZIP_FILECOUNT_LIMIT", 1)
            with zipfile.ZipFile(stream, "w") as zf:
                zf.writestr("1.png", png())
                for number in range(1, count):
                    name = "metadata/entry.xml" if duplicate_metadata else f"metadata/{number}.xml"
                    zf.writestr(name, b"")
                if "comment" in variant:
                    zf.comment = b"c" * 65535
        data = bytearray(stream.getvalue())
        end = data.rfind(zipfile.stringEndArchive)
        struct.pack_into("<HH", data, end + 8, declared, declared)
        if "zip64" in variant:
            end64 = data.rfind(zipfile.stringEndArchive64)
            assert end64 >= 0
            struct.pack_into("<QQ", data, end64 + 24, declared, declared)
        prefix = b"MZ self-extracting stub\r\n" if "sfx" in variant else b""
        path = env.album / "Album.cbz"
        path.write_bytes(prefix + data)
        return path

    return make


@pytest.mark.parametrize("fmt", ["zip", "pdf"])
@pytest.mark.parametrize("declared", [0, 1])
def test_eocd_underreports_actual_metadata_entries(env, monkeypatch, metadata_archive, fmt, declared):
    limits = export_budget.DEFAULT_LIMITS
    assert limits.archive_entries == 10_000
    assert limits.archive_metadata_bytes == 8 * 1024 ** 2
    path = metadata_archive(10_001, declared=declared)
    with path.open("rb") as handle:
        end = zipfile._EndRecData(handle)
    assert end[zipfile._ECD_ENTRIES_TOTAL] == declared
    assert end[zipfile._ECD_SIZE] < limits.archive_metadata_bytes
    with zipfile.ZipFile(path) as zf:
        assert len(zf.infolist()) == 10_001

    def forbidden(*args):
        pytest.fail("forged EOCD count reached SDK index allocation")

    monkeypatch.setattr(archive_pages, "read_index", forbidden)
    rejected(env.post(fmt), 413, "export_limit")
    assert list(env.exports.iterdir()) == []


@pytest.mark.parametrize("fmt", ["zip", "pdf"])
@pytest.mark.parametrize("variant", ["comment", "sfx-comment", "zip64", "zip64-sfx-comment"])
def test_metadata_preflight_stops_at_first_excess_record(env, monkeypatch, metadata_archive, fmt, variant):
    metadata_archive(20, variant=variant)
    env.limit(archive_entries=2)
    real_info, parsed = zipfile.ZipInfo, []

    def counted_info(*args, **kwargs):
        info = real_info(*args, **kwargs)
        parsed.append(info.filename)
        return info

    def forbidden(*args):
        pytest.fail("excess central records reached SDK index allocation")

    monkeypatch.setattr(zipfile, "ZipInfo", counted_info)
    monkeypatch.setattr(archive_pages, "read_index", forbidden)
    rejected(env.post(fmt), 413, "export_limit")
    assert len(parsed) == 3  # bounded parser stops; it does not build all 20 records


@pytest.mark.parametrize("fmt", ["zip", "pdf"])
@pytest.mark.parametrize("variant", ["plain", "comment", "sfx-comment", "zip64", "zip64-sfx-comment"])
def test_metadata_preflight_accepts_exact_actual_entry_limit(env, metadata_archive, fmt, variant):
    metadata_archive(3, variant=variant)
    env.limit(archive_entries=3)
    response = env.post(fmt)
    assert response.status_code == 200
    if fmt == "pdf":
        import pikepdf
        with pikepdf.Pdf.open(io.BytesIO(response.data)) as pdf:
            assert len(pdf.pages) == 1
    else:
        with zipfile.ZipFile(io.BytesIO(response.data)) as zf:
            assert zf.namelist() == ["Album/1.png"]
    response.close()


@pytest.mark.parametrize("fmt", ["zip", "pdf"])
def test_metadata_preflight_counts_duplicate_nonimage_records(env, monkeypatch, metadata_archive, fmt):
    with pytest.warns(UserWarning, match="Duplicate name"):
        path = metadata_archive(3, duplicate_metadata=True)
    with zipfile.ZipFile(path) as zf:
        assert len(zf.infolist()) == 3 and len(zf.NameToInfo) == 2
    env.limit(archive_entries=2)

    def forbidden(*args):
        pytest.fail("duplicate metadata records reached SDK index allocation")

    monkeypatch.setattr(archive_pages, "read_index", forbidden)
    rejected(env.post(fmt), 413, "export_limit")


@pytest.mark.parametrize("fmt", ["zip", "pdf"])
def test_metadata_preflight_checks_8mib_before_allocating_records(env, monkeypatch, fmt):
    path = env.archive({"1.png": png()})
    with zipfile.ZipFile(path, "a") as zf:
        for number in range(128):
            info = zipfile.ZipInfo(f"metadata/{number}.xml")
            info.comment = b"c" * 65535
            zf.writestr(info, b"")
    with path.open("rb") as handle:
        end = zipfile._EndRecData(handle)
    assert end[zipfile._ECD_SIZE] > 8 * 1024 ** 2

    def forbidden(*args, **kwargs):
        pytest.fail("oversized directory reached record/index allocation")

    monkeypatch.setattr(zipfile, "ZipInfo", forbidden)
    monkeypatch.setattr(archive_pages, "read_index", forbidden)
    rejected(env.post(fmt), 413, "export_limit")


@pytest.mark.parametrize("fmt", ["zip", "pdf"])
def test_metadata_preflight_checks_deadline_between_records(env, monkeypatch, metadata_archive, fmt):
    metadata_archive(20)
    now = [0]
    budget = ExportBudget(clock=lambda: now[0])
    monkeypatch.setattr(api_export, "ExportBudget", lambda: budget)
    real_info, parsed = zipfile.ZipInfo, []

    def slow_info(*args, **kwargs):
        info = real_info(*args, **kwargs)
        parsed.append(info.filename)
        now[0] += 100
        return info

    def forbidden(*args):
        pytest.fail("expired metadata preflight reached SDK index allocation")

    monkeypatch.setattr(zipfile, "ZipInfo", slow_info)
    monkeypatch.setattr(archive_pages, "read_index", forbidden)
    response = env.post(fmt)
    rejected(response, 503, "export_timeout")
    assert response.headers["Retry-After"] == "5" and len(parsed) == 3


@pytest.mark.parametrize("fmt", ["zip", "pdf"])
@pytest.mark.parametrize("loose", [False, True])
@pytest.mark.parametrize("problem", ["corrupt", "busy"])
def test_metadata_preflight_preserves_archive_error_classification(
        env, monkeypatch, metadata_archive, fmt, loose, problem):
    path = metadata_archive(3)
    if loose:
        (env.album / "1.png").write_bytes(png())
    if problem == "corrupt":
        data = bytearray(path.read_bytes())
        start = data.index(zipfile.stringCentralDir)
        second = data.index(zipfile.stringCentralDir, start + 4)
        data[second:second + 4] = b"bad!"
        path.write_bytes(data)
    else:
        real_open = io.open

        def locked(file, *args, **kwargs):
            if str(file) == str(path):
                raise PermissionError(13, "archive in use")
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr(io, "open", locked)
    response = env.post(fmt)
    if problem == "busy":
        rejected(response, 503, "archive_busy")
    elif loose:
        assert response.status_code == 200
        response.close()
    else:
        rejected(response, 422, "archive_corrupt")
