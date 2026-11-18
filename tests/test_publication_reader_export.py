"""Read the published album, export the exact completed task, never serve work in progress."""
import builtins
import hashlib
import io
import json
from types import SimpleNamespace
import zipfile

import pikepdf
import pytest
from PIL import Image

from core import database as db, path_guard
from routes import api_export, api_preview


@pytest.fixture
def root(client, tmp_path, monkeypatch):
    folder = tmp_path / "downloads"
    folder.mkdir()
    for module in (path_guard, api_export, api_preview):
        monkeypatch.setattr(module, "DOWNLOAD_ROOT", folder)
    return folder


def _page(folder, chapter, color):
    chapter_dir = folder / f"Chapter__{chapter}"
    chapter_dir.mkdir(parents=True, exist_ok=True)
    (chapter_dir / ".jm-chapter.json").write_text(json.dumps({"photo_id": chapter}), encoding="utf-8")
    with Image.new("RGB", (24, 24), color) as image:
        image.save(chapter_dir / "00001.png")
    return chapter_dir / "00001.png"


def _job(job_id, folder, status="completed"):
    db.insert_job(job_id, "123", "Album", [])
    assert db.transition_job_status(job_id, ["queued"], status, output_path=str(folder))


def prepare_publication(root, job_id, green=0, *, chapters=("71",), mode="RGB"):
    from core import publication
    from core.download_layout import chapter_output_dir

    class Chapter(list):
        name, photo_id = "Chapter", "71"

    album = root / "Album"
    _job(job_id, album, "running")
    workspace = publication.Workspace(root, job_id, "123", album, SimpleNamespace(directory=None))
    photos = []
    for chapter_id in chapters:
        photo = Chapter([1, 2])
        photo.photo_id = chapter_id
        photos.append(photo)
        chapter = chapter_output_dir(workspace.work, photo)
        for number in photo:
            color = (number * 63, green, 255) + ((128,) if mode == "RGBA" else ())
            with Image.new(mode, (12, 12), color) as image:
                image.save(chapter / f"{number:05d}.webp", lossless=True)
    manifest = workspace.seal(photos, lambda: None)
    target, evidence = workspace.project(manifest, SimpleNamespace(author="Author"), {}, lambda: None)
    return workspace, target, evidence


def finish_publication(job_id, target, evidence):
    from core.task_commands import finish_download
    chapters = evidence[1]
    total = sum(len(chapter["files"]) for chapter in chapters)
    assert finish_download(job_id, "completed", photo_ids=[c["photo_id"] for c in chapters],
                           total_pages=total, done_pages=total, output_path=str(target), publication=evidence)


def _check_export(client, url, fmt, expected):
    with client.post(f"{url}/{fmt}") as response:
        assert response.status_code == 200, response.json
        assert f"Album.{fmt}" in response.headers["Content-Disposition"]
        with io.BytesIO(response.data) as content:
            if fmt == "zip":
                with zipfile.ZipFile(content) as archive:
                    images = [name for name in archive.namelist() if name.endswith(".webp")]
                    assert images == [f"Album/{page.parent.name}/{page.name}" for page in expected]
                    assert [archive.read(name) for name in images] == [page.read_bytes() for page in expected]
            else:
                with pikepdf.Pdf.open(content) as document:
                    assert len(document.pages) == len(expected)
                    for pdf_page, source in zip(document.pages, expected):
                        obj, = pdf_page.get_images().values()
                        with pikepdf.PdfImage(obj).as_pil_image() as exported, Image.open(source) as original:
                            assert exported.tobytes() == original.tobytes()


@pytest.mark.parametrize("fmt", ["zip", "pdf"])
def test_task_export_keeps_its_version_album_uses_published_pages(client, root, fmt):
    older, album, evidence = prepare_publication(root, "older")
    finish_publication("older", album, evidence)
    newer, album, evidence = prepare_publication(root, "newer", 255, chapters=("71", "72"))
    finish_publication("newer", album, evidence)
    _page(root / ".jmcomic-work" / "paused", "73", "purple")
    _job("paused", album, "paused")

    _check_export(client, "/api/export/older", fmt, sorted(older.snapshot.glob("*/*.webp")))
    assert client.post(f"/api/export/paused/{fmt}").status_code == 400
    assert client.post("/api/jobs/clear/completed").status_code == 200
    assert client.post(f"/api/export/older/{fmt}").status_code == 404
    current = sorted(newer.snapshot.glob("*/*.webp"))
    _check_export(client, "/api/library/123/export", fmt, current)
    pages = client.get("/api/preview/123").json["pages"]
    assert [page["photo_id"] for page in pages] == ["71", "71", "72", "72"]
    for page, source in zip(pages, current):
        with client.get(page["url"]) as response:
            assert response.status_code == 200 and response.data == source.read_bytes()
    db.set_cached_album_detail("123", json.dumps({
        "chapter_count": 3, "photos": [{"photo_id": chapter, "page_count": 2} for chapter in ("71", "72", "73")],
    }))
    assert client.get("/api/local-chapters/123").json["partial"] == {"downloaded": 2, "total": 3}


@pytest.mark.parametrize("fmt", ["zip", "pdf"])
def test_registered_unreadable_snapshot_never_exports_album(client, root, monkeypatch, fmt):
    album = root / "Album"
    _page(album, "71", "green")
    _job("older", album)

    def unreadable(job_id, download_root):
        raise OSError("registered snapshot unavailable")

    monkeypatch.setattr("core.publication.job_snapshot", unreadable)
    with client.post(f"/api/export/older/{fmt}") as response:
        assert response.status_code == 503 and response.json["reason"] == "export_source_unreadable"
        assert "Content-Disposition" not in response.headers


def test_guessed_private_preview_is_not_visible(client, root):
    pending = _page(root / ".jmcomic-work" / "paused", "73", "purple")
    url = "/api/preview-img/" + pending.relative_to(root).as_posix()
    with client.get(url) as response:
        assert response.status_code == 404 and response.is_json
    assert client.get(url.replace(".jmcomic-work", "%2Ejmcomic-work")).status_code == 404
    public = _page(root / ".jmcomic-work-art", "71", "green")
    public_url = "/api/preview-img/" + public.relative_to(root).as_posix()
    with client.get(public_url) as response:
        assert response.status_code == 200 and response.data == public.read_bytes()
    public.write_bytes(b"")
    assert client.get(public_url).status_code == 404


@pytest.mark.parametrize("fmt", ["zip", "pdf"])
def test_snapshot_survives_superseded_projection_but_legacy_export_does_not(client, root, fmt):
    workspace, album, evidence = prepare_publication(root, "registered")
    finish_publication("registered", album, evidence)
    _job("old", album)
    assert db.mark_completed_jobs_superseded("123", str(album), "replacement") == 2
    _check_export(client, "/api/export/registered", fmt, sorted(workspace.snapshot.glob("*/*.webp")))
    assert client.post(f"/api/export/old/{fmt}").status_code == 404
    assert client.post(f"/api/library/123/export/{fmt}").status_code == 404


@pytest.mark.parametrize("private", [True, False], ids=["workspace", "download-root"])
def test_catalog_cannot_turn_private_files_into_public_pages(client, root, private):
    workspace = root / ".jmcomic-work" / "paused"
    _page(workspace, "73", "purple")
    _job("legacy", workspace if private else root)
    assert client.get("/api/preview/123").status_code == 404
    assert client.get("/api/local-chapters/123").json["reason"] == "not_readable"
    for url in ("/api/export/legacy/zip", "/api/library/123/export/zip"):
        with client.post(url) as response:
            assert response.status_code == 403 and "Content-Disposition" not in response.headers


@pytest.mark.parametrize("fmt", ["zip", "pdf"])
def test_truncated_source_never_sends_a_partial_export(client, root, monkeypatch, fmt):
    album = root / "Album"
    page = _page(album, "71", "red")
    smaller = page.read_bytes()
    page.write_bytes(smaller + b"additional data at collection time")
    _job("legacy", album)
    collect = api_export._collect_files

    def truncate_after_scan(*args, **kwargs):
        files = collect(*args, **kwargs)
        page.write_bytes(smaller)
        return files

    monkeypatch.setattr(api_export, "_collect_files", truncate_after_scan)
    with client.post(f"/api/export/legacy/{fmt}") as response:
        assert response.status_code == 503 and response.json["reason"] == "export_source_unreadable"
        assert "Content-Disposition" not in response.headers


@pytest.mark.parametrize("versioned", [False, True], ids=["first-visible-folder", "generation-folder"])
def test_view_images_require_the_committed_catalog_mapping(client, root, versioned):
    from core import publication
    from core.publication_access import published_path
    if versioned:
        (root / "Album").mkdir()
    workspace, target, evidence = prepare_publication(root, "pending")
    relative = "Chapter__71/00001.webp"
    assert (target / relative).is_file()
    assert (".jmcomic-views" in target.parts) is versioned
    url = f"/api/preview-img/{target.relative_to(root).as_posix()}/{relative}"
    assert client.get(url).status_code == 404
    with db.transaction() as conn:
        assert db.transition_job_status("pending", ["running"], "completed", output_path=str(target), conn=conn)
        publication.record(conn, "pending", "123", evidence)
        assert published_path(target / relative, root) is None
    assert client.post("/api/jobs/clear/completed").status_code == 200
    with client.get(url) as response:
        assert response.status_code == 200 and response.data == (workspace.snapshot / relative).read_bytes()
    with db.transaction() as conn:
        conn.execute("UPDATE resource_catalog SET output_path=? WHERE job_id='pending'", (str(root / "Elsewhere"),))
    assert client.get(url).status_code == 404
    with db.transaction() as conn:
        conn.execute("UPDATE resource_catalog SET output_path=? WHERE job_id='pending'", (str(target),))
        conn.execute("DELETE FROM published_tasks WHERE job_id='pending'")
    assert client.get(url).status_code == 404
    if versioned:
        (target / ".jm-view.json").unlink()
        assert client.get(url).status_code == 404


@pytest.mark.parametrize("fmt", ["zip", "pdf"])
def test_one_missing_snapshot_page_cancels_whole_task_export(client, root, fmt):
    workspace, target, evidence = prepare_publication(root, "finished")
    finish_publication("finished", target, evidence)
    (workspace.snapshot / "Chapter__71" / "00002.webp").unlink()
    with client.post(f"/api/export/finished/{fmt}") as response:
        assert response.status_code == 503 and response.json["reason"] == "export_source_unreadable"
        assert "Content-Disposition" not in response.headers


@pytest.mark.parametrize(("fmt", "mode"), [("zip", "RGB"), ("pdf", "RGB"), ("pdf", "RGBA")])
def test_same_size_snapshot_substitution_refuses_attachment(client, root, tmp_path, monkeypatch, fmt, mode):
    workspace, album, evidence = prepare_publication(root, "sealed", mode=mode)
    finish_publication("sealed", album, evidence)
    first, second = sorted(workspace.snapshot.glob("*/*.webp"))
    original, replacement = second.read_bytes(), first.read_bytes()
    assert original != replacement and len(original) == len(replacement)
    exports = tmp_path / "exports"
    exports.mkdir()
    monkeypatch.setattr(api_export.tempfile, "tempdir", str(exports))

    second.write_bytes(replacement)
    with client.post(f"/api/export/sealed/{fmt}") as response:
        assert response.status_code == 503 and response.json["reason"] == "export_source_unreadable"
        assert "Content-Disposition" not in response.headers
    assert list(exports.iterdir()) == []
    # Rewriting the local seal to match the changed bytes cannot replace the committed record.
    marker = workspace.snapshot / ".jm-publication.json"
    manifest = json.loads(marker.read_bytes())
    manifest[0]["digests"][second.name] = hashlib.sha256(replacement).hexdigest()
    marker.write_text(json.dumps(manifest), encoding="utf-8")
    with client.post(f"/api/export/sealed/{fmt}") as response:
        assert response.status_code == 503 and "Content-Disposition" not in response.headers
    assert list(exports.iterdir()) == []


@pytest.mark.parametrize("fmt", ["zip", "pdf"])
def test_snapshot_page_count_cannot_hide_a_renamed_page(client, root, fmt):
    workspace, album, evidence = prepare_publication(root, "renamed")
    finish_publication("renamed", album, evidence)
    page = next(workspace.snapshot.glob("*/*.webp"))
    page.rename(page.with_name("replacement.webp"))
    with client.post(f"/api/export/renamed/{fmt}") as response:
        assert response.status_code == 503 and response.json["reason"] == "export_source_unreadable"
        assert "Content-Disposition" not in response.headers


@pytest.mark.parametrize(("fmt", "mode"), [("zip", "RGB"), ("pdf", "RGB"), ("pdf", "RGBA")])
def test_sealed_export_reads_each_page_once(client, root, monkeypatch, fmt, mode):
    workspace, album, evidence = prepare_publication(root, "sealed", mode=mode)
    finish_publication("sealed", album, evidence)
    opens = {str(page): 0 for page in workspace.snapshot.glob("*/*.webp")}

    def track(opener):
        def counted(path, *args, **kwargs):
            if str(path) in opens:
                opens[str(path)] += 1
            return opener(path, *args, **kwargs)
        return counted

    # pathlib uses io.open; Pillow opens path arguments with builtins.open.
    monkeypatch.setattr(io, "open", track(io.open))
    monkeypatch.setattr(builtins, "open", track(builtins.open))
    with client.post(f"/api/export/sealed/{fmt}") as response:
        assert response.status_code == 200, response.json
        if fmt == "pdf":
            with pikepdf.Pdf.open(io.BytesIO(response.data)) as doc:
                assert len(doc.pages) == 2
                for number, page in enumerate(doc.pages, 1):
                    obj, = page.get_images().values()
                    with pikepdf.PdfImage(obj).as_pil_image() as image:
                        assert image.getpixel((0, 0)) == pytest.approx((number * 63, 0, 255), abs=2)
        else:
            with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
                assert archive.testzip() is None
    assert list(opens.values()) == [1, 1]


def test_pdf_fallback_rechecks_snapshot_when_it_rereads(client, root, monkeypatch):
    import img2pdf
    workspace, album, evidence = prepare_publication(root, "fallback")
    finish_publication("fallback", album, evidence)
    first, second = sorted(workspace.snapshot.glob("*/*.webp"))
    real = img2pdf.convert
    calls = 0

    def retry_after_substitution(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:  # Both original pages passed verification during the first attempt.
            second.write_bytes(first.read_bytes())
            raise ValueError("retry as JPEG")
        return real(*args, **kwargs)

    monkeypatch.setattr(img2pdf, "convert", retry_after_substitution)
    with client.post("/api/export/fallback/pdf") as response:
        assert response.status_code == 503 and response.json["reason"] == "export_source_unreadable"
        assert "Content-Disposition" not in response.headers
    assert calls == 3  # Only the intact first page reached the fallback encoder.


@pytest.mark.parametrize("damage", ["missing-seal", "missing-digest", "duplicate-page"])
def test_registered_snapshot_requires_complete_seal(client, root, damage):
    workspace, album, evidence = prepare_publication(root, "sealed")
    finish_publication("sealed", album, evidence)
    marker = workspace.snapshot / ".jm-publication.json"
    if damage == "missing-seal":
        marker.unlink()
    else:
        manifest = json.loads(marker.read_bytes())
        if damage == "missing-digest":
            del manifest[0]["digests"]["00002.webp"]
        else:
            manifest[0]["files"].append("00002.webp")
        text = json.dumps(manifest)
        marker.write_text(text, encoding="utf-8")
        with db.transaction() as conn:
            conn.execute("UPDATE published_tasks SET manifest=? WHERE job_id='sealed'", (text,))
    for fmt in ("zip", "pdf"):
        with client.post(f"/api/export/sealed/{fmt}") as response:
            assert response.status_code == 503 and response.json["reason"] == "export_source_unreadable"
            assert "Content-Disposition" not in response.headers


def test_private_image_is_not_public_through_a_directory_link(client, root):
    from test_storage import _directory_link, _remove_directory_link
    private = root / ".jmcomic-work" / "unfinished"
    page = _page(private, "73", "purple")
    alias = root / "public-alias"
    _directory_link(private, alias)
    try:
        with client.get("/api/preview-img/public-alias/" + page.relative_to(private).as_posix()) as response:
            assert response.status_code == 404
    finally:
        _remove_directory_link(alias)


def test_first_view_appearing_during_a_legacy_url_lookup_stays_hidden(client, root, monkeypatch):
    from core import publication_access
    workspace, target, _ = prepare_publication(root, "pending")
    before_rename = workspace.base / "prepared-first-view"
    target.rename(before_rename)
    view_job = publication_access._view_job

    def rename_after_marker_probe(marker):
        job = view_job(marker)
        if marker.parent == target and before_rename.exists():
            before_rename.rename(target)
        return job

    monkeypatch.setattr(publication_access, "_view_job", rename_after_marker_probe)
    with client.get("/api/preview-img/Album/Chapter__71/00001.webp") as response:
        assert response.status_code == 404
    assert target.is_dir() and not before_rename.exists()
