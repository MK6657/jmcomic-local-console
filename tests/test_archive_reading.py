"""Archive-only offline reading: CBZ / app-packed ZIP as the page source for the readers, the shared
"readable offline" rule, the lists' reasons and both exports. Everything happens in pytest's tmp folders:
the real downloads/ and runtime/ are never touched."""
import io
import os
import re
import shutil
import warnings
import zipfile
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
STATIC_JS = ROOT / "static" / "js"


def png(color, size=(8, 8)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


# Chapter-relative names as CbzPacker writes them. Natural order puts 2 before 10 and 第2话 before 第10话;
# each page has its own size so the order can be checked in the PDF too.
ORDER = ["第1话/1.png", "第1话/2.png", "第1话/10.png", "第2话/1.png", "第10话/1.png"]
PAGES = {name: png(color, (8 + n, 8 + n))
         for n, (name, color) in enumerate(zip(ORDER, ("red", "green", "blue", "yellow", "purple")))}


def write_zip(path, entries, compression=zipfile.ZIP_DEFLATED):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return path


def comic(folder, entries=None, suffix=".cbz", compression=zipfile.ZIP_DEFLATED):
    """An album folder holding only <folder name>.cbz / .zip with a ComicInfo.xml, as the auto-packer leaves it
    after the original images were deleted."""
    entries = dict(PAGES if entries is None else entries)
    entries.setdefault("ComicInfo.xml", "<ComicInfo/>")
    folder.mkdir(parents=True, exist_ok=True)
    return write_zip(folder / (folder.name + suffix), entries, compression)


def loose(folder, names=("第1话/1.png", "第1话/2.png")):
    for name in names:
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(PAGES.get(name, png("white")))
    return folder


def junk_archive(folder):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (folder.name + ".cbz")
    path.write_bytes(b"PK\x03\x04 this is not really a zip")
    return path


def damage_page(path, page_bytes):
    """Flip one byte inside a stored (uncompressed) page so only its CRC check fails."""
    data = bytearray(path.read_bytes())
    at = data.index(page_bytes) + len(page_bytes) // 2
    data[at] ^= 0xFF
    path.write_bytes(bytes(data))


def listing(folder):
    return sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*"))


def _job(job_id, album_id, output_path, status="completed"):
    from core import database as db
    db.insert_job(job_id, album_id, "t", [])
    db.update_job(job_id, status=status, output_path=str(output_path) if output_path else None)


@pytest.fixture
def downloads(client, tmp_path, monkeypatch):
    from core import archive_pages, local_availability, path_guard
    from routes import api_export, api_jobs, api_preview
    root = tmp_path / "downloads"
    root.mkdir()
    monkeypatch.setattr(path_guard, "DOWNLOAD_ROOT", root)
    monkeypatch.setattr(api_preview, "DOWNLOAD_ROOT", root)
    monkeypatch.setattr(api_export, "DOWNLOAD_ROOT", root)
    monkeypatch.setattr(api_jobs, "_cbz_cache", {})
    archive_pages.clear_cache()
    with local_availability._cache_lock:
        local_availability._cache.clear()
    yield root
    archive_pages.clear_cache()


@pytest.fixture
def exports_tmp(tmp_path, monkeypatch):
    """Where the exports may put temporary files (checked to be empty afterwards)."""
    from routes import api_export
    folder = tmp_path / "exports"
    folder.mkdir()
    monkeypatch.setattr(api_export.tempfile, "tempdir", str(folder))
    return folder


# ─── the archive page source (core.archive_pages) ─────────────────────────────


def test_pages_come_in_natural_chapter_order_without_metadata(downloads):
    from core import archive_pages
    index = archive_pages.read_index(comic(downloads / "A"))
    assert index.status == "ok" and index.format == "cbz"
    assert [p.name for p in index.pages] == ORDER  # ComicInfo.xml is not a page
    assert [p.chapter for p in index.pages] == ["第1话"] * 3 + ["第2话", "第10话"]
    assert [archive_pages.read_page(index, p) for p in index.pages] == [PAGES[n] for n in ORDER]


def test_unsafe_and_non_page_entries_are_ignored(downloads):
    from core import archive_pages
    page = png("red")
    path = downloads / "B" / "B.cbz"
    path.parent.mkdir()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in ("../escape.png", "/abs.png", "C:/drive.png", "a/../b.png", "./dot.png",
                     "__MACOSX/ch/1.png", "ch/._1.png", "notes.txt", "ComicInfo.xml", "folder/", "empty.png"):
            zf.writestr(name, b"" if name in ("folder/", "empty.png") else page)
        zf.writestr("002.png", page)
        zf.writestr("第1话/001.png", page)
        zf.writestr(".hack 第1话/001.png", page)  # chapter titles may start with "." (safe_dirname keeps it)
        for method in (zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA):  # zipfile can't cap their output: skipped
            zf.writestr(zipfile.ZipInfo(f"m{method}.png"), page, compress_type=method)
    index = archive_pages.read_index(path)
    assert index.status == "ok"
    assert sorted(p.name for p in index.pages) == [".hack 第1话/001.png", "002.png", "第1话/001.png"]
    # zipfile clears the flag when writing, so check the encrypted-entry rule on the entry itself
    encrypted = zipfile.ZipInfo("secret.png")
    encrypted.flag_bits |= 0x1
    encrypted.file_size, encrypted.compress_size = 100, 90
    plain = zipfile.ZipInfo("plain.png")
    plain.file_size, plain.compress_size = 100, 90
    assert archive_pages._page_name(encrypted) is None and archive_pages._page_name(plain) == "plain.png"


def test_backslash_names_from_windows_tools_are_chapter_folders(downloads):
    from core import archive_pages
    info = zipfile.ZipInfo("x")
    path = downloads / "W" / "W.cbz"
    path.parent.mkdir()
    with zipfile.ZipFile(path, "w") as zf:
        info.filename = "第1话\\001.png"  # stored as-is (zipfile only rewrites os.sep when it builds the name)
        zf.writestr(info, png("red"))
    index = archive_pages.read_index(path)
    assert [(p.chapter, p.name) for p in index.pages] == [("第1话", "第1话/001.png")]
    assert archive_pages.read_page(index, index.pages[0]) == png("red")


def test_a_repeated_entry_is_one_page_the_last_one(downloads):
    from core import archive_pages
    path = downloads / "D" / "D.cbz"
    path.parent.mkdir()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # zipfile warns about the duplicate name
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("001.png", png("red"))
            zf.writestr("001.png", png("blue", (20, 20)))
    index = archive_pages.read_index(path)
    assert index.status == "ok" and [p.name for p in index.pages] == ["001.png"]
    assert archive_pages.read_page(index, index.pages[0]) == png("blue", (20, 20))


def test_find_archive_prefers_the_packers_own_file(downloads):
    from core.archive_pages import find_archive
    folder = downloads / "Album"
    folder.mkdir()
    write_zip(folder / "other.zip", PAGES)
    write_zip(folder / "nested" / "nested.cbz", PAGES)
    assert find_archive(folder) is None  # a random .zip may be anything; archives in sub folders don't count
    write_zip(folder / "b.cbz", PAGES)
    write_zip(folder / "a10.cbz", PAGES)
    write_zip(folder / "a9.cbz", PAGES)
    assert find_archive(folder).name == "a9.cbz"  # other .cbz: natural order
    write_zip(folder / "ALBUM.zip", PAGES)
    assert find_archive(folder).name == "ALBUM.zip"  # pack_format=zip writes <folder>.zip (any case)
    write_zip(folder / "Album.cbz", PAGES)
    assert find_archive(folder).name == "Album.cbz"


@pytest.mark.parametrize("damage", ["not_a_zip", "truncated", "bad_crc"])
def test_damaged_archives_are_corrupt(downloads, damage):
    from core import archive_pages
    first = png("red")
    path = comic(downloads / "C", {"001.png": first, "002.png": png("blue")}, compression=zipfile.ZIP_STORED)
    if damage == "not_a_zip":
        path.write_bytes(b"not a zip at all")
    elif damage == "truncated":
        path.write_bytes(path.read_bytes()[:200])
    else:
        damage_page(path, first)
    assert archive_pages.read_index(path).status == "corrupt"


def test_archives_without_reader_pages_are_empty(downloads):
    from core import archive_pages
    assert archive_pages.read_index(comic(downloads / "E1", {})).status == "empty"
    bmp = io.BytesIO()
    Image.new("RGB", (4, 4)).save(bmp, "BMP")
    index = archive_pages.read_index(comic(downloads / "E2", {"001.bmp": bmp.getvalue()}))
    assert index.status == "empty"  # the reader can't show BMP (same rule as loose files)


def test_zip_bombs_and_oversized_archives_are_refused(downloads, monkeypatch):
    import tracemalloc
    from core import archive_pages
    # BZIP2 / LZMA inflate a whole chunk at once (a few KB can become GBs): such entries are never opened
    for method in (zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA):
        folder = downloads / f"B{method}"
        folder.mkdir()
        with zipfile.ZipFile(folder / f"B{method}.cbz", "w") as zf:
            zf.writestr(zipfile.ZipInfo("001.png"), b"\0" * (8 * 1024 * 1024), compress_type=method)
        tracemalloc.start()
        status = archive_pages.read_index(folder / f"B{method}.cbz").status
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        assert status == "empty" and peak < 4 * 1024 * 1024, (method, peak)
    # a page bigger than the page limit is skipped (and DEFLATE output never exceeds the declared size)
    monkeypatch.setattr(archive_pages, "MAX_PAGE_BYTES", 1024)
    assert archive_pages.read_index(comic(downloads / "P", {"001.png": b"\0" * 4096})).status == "empty"
    monkeypatch.setattr(archive_pages, "MAX_PAGE_BYTES", 64 * 1024 * 1024)
    monkeypatch.setattr(archive_pages, "MAX_ENTRIES", 3)
    assert archive_pages.read_index(comic(downloads / "M")).status == "corrupt"
    monkeypatch.setattr(archive_pages, "MAX_ENTRIES", 20000)
    monkeypatch.setattr(archive_pages, "MAX_TOTAL_BYTES", 100)
    assert archive_pages.read_index(comic(downloads / "T")).status == "corrupt"


def test_the_index_is_reused_until_the_archive_changes(downloads, monkeypatch):
    from core import archive_pages
    path = comic(downloads / "K")
    calls = []
    build = archive_pages._build_index
    monkeypatch.setattr(archive_pages, "_build_index", lambda *a: calls.append(a) or build(*a))
    assert archive_pages.read_index(path) is archive_pages.read_index(path)
    assert len(calls) == 1
    write_zip(path, {"1.png": png("red")})
    assert [p.name for p in archive_pages.read_index(path).pages] == ["1.png"] and len(calls) == 2


# ─── the shared "readable offline" rule (core.local_availability) ────────────


def _cases(downloads):
    """One album per case; returns album_id → expected (state, archive format, problem)."""
    loose(downloads / "Both")
    comic(downloads / "Both")
    _job("j1", "101", downloads / "Both")                 # loose + archive → loose wins
    comic(downloads / "Cbz")
    _job("j2", "102", downloads / "Cbz")                  # CBZ only
    comic(downloads / "Zip", suffix=".zip")
    _job("j3", "103", downloads / "Zip")                  # the app's ZIP only
    junk_archive(downloads / "Bad")
    _job("j4", "104", downloads / "Bad")                  # corrupt archive
    comic(downloads / "Empty", {})
    _job("j5", "105", downloads / "Empty")                # archive without pages
    _job("j6", "106", downloads / "Gone")                 # nothing left
    (downloads / "Shell").mkdir()
    _job("j7", "107", downloads / "Shell")                # empty folder
    return {
        "101": ("loose", None, None), "102": ("archive", "cbz", None), "103": ("archive", "zip", None),
        "104": ("archive_corrupt", "cbz", "archive_corrupt"), "105": ("archive_empty", "cbz", "archive_empty"),
        "106": ("missing", None, "deleted"), "107": ("missing", None, "deleted"),
    }


def test_local_states_cover_every_case(downloads):
    from core.local_availability import local_states, readable_album_ids
    expected = _cases(downloads)
    ids = list(expected) + ["108"]  # 108 was never downloaded
    states = local_states(ids)
    assert {a: (s.state, s.archive, s.problem) for a, s in states.items()} == expected
    assert readable_album_ids(ids) == {"101", "102", "103"}


def test_unchanged_archive_folders_are_not_opened_again(downloads, monkeypatch):
    from core.local_availability import local_states
    expected = _cases(downloads)
    first = local_states(list(expected))

    def boom(*args, **kwargs):
        raise AssertionError("an unchanged folder / archive was scanned again")
    with monkeypatch.context() as patch:
        patch.setattr(os, "scandir", boom)
        patch.setattr(os.path, "realpath", boom)
        patch.setattr(zipfile, "ZipFile", boom)
        assert local_states(list(expected)) == first


def test_repairing_or_breaking_an_archive_is_noticed(downloads):
    from core.local_availability import local_state
    path = junk_archive(downloads / "Fix")
    _job("jf", "201", downloads / "Fix")
    assert local_state("201").state == "archive_corrupt"
    comic(downloads / "Fix")  # replaced in place (the folder's own timestamp may not change)
    assert local_state("201").state == "archive"
    path.write_bytes(path.read_bytes()[:100])
    assert local_state("201").state == "archive_corrupt"


def test_an_archive_only_folder_is_not_an_empty_shell(downloads):
    # jm_service marks earlier downloads superseded when the folder has nothing left to read
    from core.local_availability import has_local_pages
    assert has_local_pages(comic(downloads / "A").parent)
    assert has_local_pages(loose(downloads / "L"))
    assert not has_local_pages(junk_archive(downloads / "X").parent)
    assert not has_local_pages(comic(downloads / "E", {}).parent)
    source = (ROOT / "core" / "jm_service.py").read_text(encoding="utf-8")
    assert "recreated = not has_local_pages(album_dir)" in source


def test_auto_pack_then_deleting_the_originals_keeps_the_same_pages(client, downloads):
    import shutil
    from core.packer import CbzPacker
    folder = loose(downloads / "Packed", ORDER)
    _job("jp", "301", folder)
    before = [p["chapter"] for p in client.get("/api/preview/301").get_json()["pages"]]
    CbzPacker().pack(folder, folder / "Packed.cbz")
    for chapter in ("第1话", "第2话", "第10话"):
        shutil.rmtree(folder / chapter)
    data = client.get("/api/preview/301").get_json()
    assert data["source"] == "archive" and [p["chapter"] for p in data["pages"]] == before
    assert [client.get(p["url"]).data for p in data["pages"]] == [PAGES[n] for n in ORDER]


# ─── readers: /api/preview, the archive page endpoint, /read ──────────────────


@pytest.mark.parametrize("suffix, fmt", [(".cbz", "cbz"), (".zip", "zip")])
def test_an_archive_only_album_opens_from_the_archive(client, downloads, suffix, fmt):
    folder = downloads / "Album #1"
    comic(folder, suffix=suffix)
    _job("j1", "101", folder)
    before = listing(folder)
    data = client.get("/api/preview/101").get_json()
    assert (data["status"], data["source"], data["archive"], data["total_pages"]) == ("ok", "archive", fmt, 5)
    assert [p["page"] for p in data["pages"]] == [1, 2, 3, 4, 5]
    assert [p["chapter"] for p in data["pages"]] == ["第1话"] * 3 + ["第2话", "第10话"]
    assert [(p["photo_id"], p["offset"]) for p in data["pages"]] == [(None, 0), (None, 1), (None, 2), (None, 0), (None, 0)]
    for page, name in zip(data["pages"], ORDER):
        assert re.fullmatch(r"/api/preview-archive/101/\d+\?v=[0-9a-f]+-[0-9a-f]+&p=[^&]+", page["url"])
        response = client.get(page["url"])
        assert response.status_code == 200 and response.data == PAGES[name]
        assert response.mimetype == "image/png" and response.headers["Cache-Control"] == "no-cache"
        again = client.get(page["url"], headers={"If-None-Match": response.headers["ETag"]})
        assert again.status_code == 304
    assert listing(folder) == before  # nothing was extracted into the download folder


def test_loose_images_win_and_the_archive_only_fills_in(client, downloads):
    folder = loose(downloads / "Both")   # 第1话/1.png and 第1话/2.png are also in the archive
    comic(folder)
    _job("j1", "101", folder)
    data = client.get("/api/preview/101").get_json()
    assert (data["source"], data["archive"], data["total_pages"]) == ("mixed", "cbz", 5)  # never twice
    urls = [p["url"] for p in data["pages"]]
    assert all(u.startswith("/api/preview-img/") for u in urls[:2])       # 第1话/1, 2: the loose files
    assert all(u.startswith("/api/preview-archive/") for u in urls[2:])   # 第1话/10, 第2话, 第10话: archive
    assert [client.get(u).data for u in urls] == [PAGES[n] for n in ORDER]
    # all pages loose (auto-pack without deleting the originals): only the loose files, nothing from the archive
    loose(folder, ORDER)
    data = client.get("/api/preview/101").get_json()
    assert (data["source"], data["total_pages"]) == ("files", 5)


@pytest.mark.parametrize("kind, reason, words", [
    ("corrupt", "archive_corrupt", "本地压缩包已损坏"),
    ("empty", "archive_empty", "本地压缩包里没有可阅读的图片"),
])
def test_an_unusable_archive_is_explained_not_shown_as_offline(client, downloads, kind, reason, words):
    folder = downloads / "Bad"
    junk_archive(folder) if kind == "corrupt" else comic(folder, {})
    _job("j1", "101", folder)
    response = client.get("/api/preview/101")
    body = response.get_json()
    assert response.status_code == 422 and body["reason"] == reason
    assert words in body["message"] and "Bad.cbz" in body["message"] and "在线阅读" in body["message"]
    assert client.get("/api/preview-archive/101/1").status_code == 422
    # the local reader and the single-page preview open and say why, with a way to read online instead
    read = client.get("/read/101")
    assert read.status_code == 200 and 'data-source="local"' in read.get_data(as_text=True)
    assert 'id="reader-go-online"' in read.get_data(as_text=True)
    assert 'id="preview-go-online"' in client.get("/preview/101").get_data(as_text=True)


def test_a_damaged_later_page_is_reported_not_served(client, downloads):
    second = png("blue", (30, 30))
    path = comic(downloads / "Half", {"001.png": png("red"), "002.png": second}, compression=zipfile.ZIP_STORED)
    damage_page(path, second)
    _job("j1", "101", downloads / "Half")
    assert client.get("/api/preview/101").get_json()["total_pages"] == 2  # the first page checks out
    assert client.get("/api/preview-archive/101/1").status_code == 200
    response = client.get("/api/preview-archive/101/2")
    assert response.status_code == 422 and response.get_json()["reason"] == "archive_corrupt"
    for bad in ("/api/preview-archive/101/3", "/api/preview-archive/101/0", "/api/preview-archive/999/1"):
        assert client.get(bad).status_code == 404, bad
    assert client.get("/api/preview-archive/1a/1").status_code == 400


def test_read_opens_archives_locally_and_goes_online_only_when_nothing_is_left(client, downloads):
    comic(downloads / "Cbz")
    _job("j1", "101", downloads / "Cbz")
    _job("j2", "102", downloads / "Gone")
    local = client.get("/read/101")
    assert local.status_code == 200 and 'data-source="local"' in local.get_data(as_text=True)
    online = client.get("/read/102?page=3")
    assert online.status_code == 302 and online.headers["Location"].endswith("/online/102?page=3")
    reader = (STATIC_JS / "reader.js").read_text(encoding="utf-8")
    assert "'/api/preview-archive/'" in reader  # the local reader accepts pages read from the archive


# ─── one answer for every page: /api/preview/available, lists, download manager ─


def test_available_reports_archives_and_reasons(client, downloads):
    _cases(downloads)
    data = client.post("/api/preview/available",
                       json={"album_ids": ["101", "102", "103", "104", "105", "106", "107", "108"]}).get_json()
    assert data["readable"] == ["101", "102", "103"]
    assert data["archives"] == {"102": "cbz", "103": "zip"}
    assert data["unavailable"] == {"104": "archive_corrupt", "105": "archive_empty", "106": "deleted",
                                   "107": "deleted"}
    assert "108" not in data["local"] and data["local"]["104"] == {"state": "archive_corrupt", "archive": "cbz"}


def _favourite_everything():
    from core import database as db
    for album_id in ("101", "102", "103", "104", "105", "106", "107", "108"):
        db.add_wishlist(album_id, f"Comic {album_id}", "", "")


def test_lists_share_the_reasons_and_the_unavailable_filter(client, downloads):
    _cases(downloads)
    _favourite_everything()
    for url, missing in (("/api/wishlist", "missing"), ("/api/library", "missing")):
        items = {i["album_id"]: i for i in client.get(url, query_string={"page_size": 100}).get_json()["items"]}
        got = {a: (i["status_group"], i["files_missing"], i["archive"], i["local_problem"]) for a, i in items.items()}
        assert got == {
            "101": ("readable", False, None, None), "102": ("readable", False, "cbz", None),
            "103": ("readable", False, "zip", None),
            "104": ("none", True, None, "archive_corrupt"), "105": ("none", True, None, "archive_empty"),
            "106": ("none", True, None, "deleted"), "107": ("none", True, None, "deleted"),
            "108": ("none", False, None, None),
        }, url

        def ids(status):
            data = client.get(url, query_string={"status": status, "page_size": 100}).get_json()
            return {i["album_id"] for i in data["items"]}
        # one filter for "downloaded before, local files unusable" whatever the reason; never-downloaded stays out
        assert ids(missing) == {"104", "105", "106", "107"}, url
        assert ids("readable") == {"101", "102", "103"}, url
    counts = client.get("/api/wishlist").get_json()["group_counts"]
    assert counts["missing"] == 4 and counts["readable"] == 3 and counts["none"] == 1


def test_download_manager_labels_the_archive_format(client, downloads):
    comic(downloads / "Cbz")
    _job("j1", "101", downloads / "Cbz")
    comic(downloads / "Zip", suffix=".zip")
    _job("j2", "102", downloads / "Zip")
    loose(downloads / "Loose")
    _job("j3", "103", downloads / "Loose")
    jobs = {j["job_id"]: j for j in client.get("/api/jobs").get_json()["jobs"]}
    assert {k: (j["has_cbz"], j["archive_format"]) for k, j in jobs.items()} == {
        "j1": (True, "cbz"), "j2": (True, "zip"), "j3": (False, None)}
    assert client.get("/api/jobs/j2").get_json()["job"]["archive_format"] == "zip"


def test_pages_use_the_shared_badges_and_the_renamed_filter(client):
    for page in ("/library", "/wishlist"):
        html = client.get(page).get_data(as_text=True)
        assert '<option value="missing">下载过 · 本地文件不可用</option>' in html, page
        assert "文件已删除</option>" not in html, page
    utils = (STATIC_JS / "utils.js").read_text(encoding="utf-8")
    for text in ("'文件已删除'", "'压缩包损坏'", "'压缩包无可阅读图片'", "window.localBadges", "stateFor"):
        assert text in utils, text
    for script in ("library.js", "wishlist.js", "search.js", "detail.js", "downloads.js"):
        assert "window.localBadges." in (STATIC_JS / script).read_text(encoding="utf-8"), script
    assert "'下载过 · 本地文件不可用'" in (STATIC_JS / "wishlist.js").read_text(encoding="utf-8")
    css = (ROOT / "static" / "css" / "style.css").read_text(encoding="utf-8")
    for rule in (".badge.status-badge-warning", ".badge.status-badge-archive"):
        body = css[css.index(rule + " {"):]
        body = body[:body.index("}")]
        assert "#" not in body and "rgb" not in body, rule  # eye-care tokens only


# ─── exports ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("suffix", [".cbz", ".zip"])
def test_zip_export_of_an_archive_only_album_holds_the_pages(client, downloads, exports_tmp, suffix):
    folder = downloads / "Album"
    comic(folder, suffix=suffix)
    _job("jz", "101", folder)
    before = listing(folder)
    response = client.post("/api/export/jz/zip")
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.data)) as zf:
        assert zf.namelist() == [f"Album/{name}" for name in ORDER]  # chapter paths and page order kept
        assert [zf.read(f"Album/{name}") for name in ORDER] == [PAGES[name] for name in ORDER]
    response.close()
    assert listing(folder) == before  # nothing restored into the download folder
    assert list(exports_tmp.iterdir()) == []


def test_zip_export_never_wraps_the_archive(client, downloads, exports_tmp):
    folder = loose(downloads / "Both")
    comic(folder)
    _job("jz", "101", folder)
    response = client.post("/api/export/jz/zip")
    with zipfile.ZipFile(io.BytesIO(response.data)) as zf:
        # loose pages + the archive's other pages, each once; never Both.cbz itself
        assert zf.namelist() == [f"Both/{name}" for name in ORDER]
        assert [zf.read(f"Both/{name}") for name in ORDER] == [PAGES[name] for name in ORDER]
    response.close()


def test_pdf_export_of_an_archive_only_album(client, downloads, exports_tmp):
    import pikepdf
    folder = downloads / "Album"
    comic(folder)
    _job("jp", "101", folder)
    before = listing(folder)
    response = client.post("/api/export/jp/pdf")
    assert response.status_code == 200
    with pikepdf.Pdf.open(io.BytesIO(response.data)) as pdf:
        widths = [float(page.mediabox[2]) for page in pdf.pages]
    response.close()
    assert len(widths) == 5 and widths == sorted(widths) and len(set(widths)) == 5  # every page, in order
    assert listing(folder) == before
    assert list(exports_tmp.iterdir()) == []  # the pages extracted for the PDF are gone too


@pytest.mark.parametrize("fmt", ["zip", "pdf"])
@pytest.mark.parametrize("kind, reason, words", [
    ("corrupt", "archive_corrupt", "本地压缩包已损坏"),
    ("empty", "archive_empty", "本地压缩包里没有可导出的图片"),
])
def test_exports_of_an_unusable_archive_explain_why(client, downloads, exports_tmp, fmt, kind, reason, words):
    folder = downloads / "Bad"
    junk_archive(folder) if kind == "corrupt" else comic(folder, {})
    _job("je", "101", folder)
    response = client.post(f"/api/export/je/{fmt}")
    body = response.get_json()
    assert response.status_code == 422 and body["reason"] == reason
    assert words in body["message"] and "Bad.cbz" in body["message"]
    assert list(exports_tmp.iterdir()) == []


@pytest.mark.parametrize("fmt", ["zip", "pdf"])
def test_exports_stop_at_a_damaged_later_page(client, downloads, exports_tmp, fmt):
    second = png("blue", (30, 30))
    folder = downloads / "Half"
    damage_page(comic(folder, {"001.png": png("red"), "002.png": second}, compression=zipfile.ZIP_STORED), second)
    _job("je", "101", folder)
    before = listing(folder)
    response = client.post(f"/api/export/je/{fmt}")
    assert response.status_code == 422 and response.get_json()["reason"] == "archive_corrupt"
    assert "Content-Disposition" not in response.headers
    assert list(exports_tmp.iterdir()) == [] and listing(folder) == before


def test_export_buttons_never_save_an_empty_file():
    # With a download manager (e.g. IDM) installed, the browser gets an empty 204 while the manager saves the file;
    # the page used to save that as a 0-byte download.zip and say the export was complete.
    source = (STATIC_JS / "downloads.js").read_text(encoding="utf-8")
    export = source[source.index("function downloadExport"):source.index("// ── 批量清理")]
    assert "r.status === 204" in export and "已交给下载工具" in export
    assert "if (!blob.size) throw new Error(" in export


def test_preview_error_only_enlarges_its_own_icon():
    css = (ROOT / "static" / "css" / "preview.css").read_text(encoding="utf-8")
    assert ".preview-error > i {" in css and ".preview-error i {" not in css  # button icons stay button-sized
    assert ".preview-image-hint > i {" in css and ".preview-image-hint i {" not in css
    html = (ROOT / "templates" / "preview.html").read_text(encoding="utf-8")
    actions = html[html.index('class="preview-error-actions"'):]
    assert actions.index('href="/downloads"') < actions.index('id="preview-go-online"') < actions.index("</div>")


# ─── review round 1: merging, pinned entries, failures, packer, exports, one fact for 阅读 ───────────


def test_merge_matches_pages_by_path_whatever_the_format(downloads):
    # a re-download may bring a page back as .webp where the archive holds .jpg: still the same page
    from core import archive_pages
    index = archive_pages.read_index(comic(downloads / "M", {"ch1/001.jpg": png("red"), "ch1/002.jpg": png("blue")}))
    page = downloads / "M" / "ch1" / "001.webp"
    merged = archive_pages.merge_pages([("ch1/001.webp", page)], index)
    assert [(name, type(source).__name__) for name, source in merged] == [
        ("ch1/001.webp", "WindowsPath" if os.name == "nt" else "PosixPath"), ("ch1/002.jpg", "ArchivePage")]
    assert archive_pages.merge_pages([("ch1/001.webp", page)], None) == [("ch1/001.webp", page)]


def test_a_rejected_duplicate_is_never_read_in_place_of_the_checked_entry(downloads):
    # zipfile looks names up to the LAST entry; a later same-name entry that failed the checks must not be read
    from core import archive_pages
    good = png("red")
    path = downloads / "Dup" / "Dup.cbz"
    path.parent.mkdir()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as zf:
            zf.writestr("001.png", good)
            zf.writestr(zipfile.ZipInfo("001.png"), b"\0" * 4096, compress_type=zipfile.ZIP_BZIP2)
    index = archive_pages.read_index(path)
    assert index.status == "ok" and archive_pages.read_page(index, index.pages[0]) == good


def test_any_failure_inside_an_archive_is_explained_never_a_500(client, downloads, exports_tmp, monkeypatch):
    from core import archive_pages
    client.application.config["PROPAGATE_EXCEPTIONS"] = False
    try:
        comic(downloads / "Odd")
        _job("j1", "101", downloads / "Odd")
        loose(downloads / "Fine")
        _job("j2", "102", downloads / "Fine")
        from core import database as db
        db.add_wishlist("101", "Odd", "", "")
        db.add_wishlist("102", "Fine", "", "")
        real = archive_pages._read_member

        def odd(zf, page):
            raise KeyError("no item named " + page.name)  # e.g. the archive was swapped underneath
        monkeypatch.setattr(archive_pages, "_read_member", odd)
        # while indexing: the archive counts as corrupt; the other albums and whole lists keep working
        assert client.get("/api/wishlist").status_code == 200
        data = client.post("/api/preview/available", json={"album_ids": ["101", "102"]}).get_json()
        assert data["readable"] == ["102"] and data["unavailable"] == {"101": "archive_corrupt"}
        assert client.get("/api/preview/101").status_code == 422
        # while reading a page (index built before): 422 from the page, both exports, no temp files left
        monkeypatch.setattr(archive_pages, "_read_member", real)
        archive_pages.clear_cache()
        assert client.get("/api/preview/101").status_code == 200
        monkeypatch.setattr(archive_pages, "_read_member", odd)
        response = client.get("/api/preview-archive/101/1")
        assert response.status_code == 422 and response.get_json()["reason"] == "archive_corrupt"
        for fmt in ("zip", "pdf"):
            response = client.post(f"/api/export/j1/{fmt}")
            assert response.status_code == 422 and response.get_json()["reason"] == "archive_corrupt", fmt
        assert list(exports_tmp.iterdir()) == []
    finally:
        client.application.config["PROPAGATE_EXCEPTIONS"] = None


def test_the_newest_app_archive_wins_and_a_broken_one_falls_back(client, downloads):
    from core import archive_pages
    folder = downloads / "Two"
    old = comic(folder, {"ch1/001.png": png("red")})                      # Two.cbz: 1 page (older)
    new = comic(folder, suffix=".zip")                                      # Two.zip: 5 pages (newer)
    os.utime(old, ns=(1_000_000_000, 1_000_000_000))
    _job("j1", "101", folder)
    assert archive_pages.select(folder).path == new  # format switched, album downloaded again: newer is complete
    assert client.get("/api/preview/101").get_json()["total_pages"] == 5
    os.utime(new, ns=(500_000_000, 500_000_000))
    old.write_bytes(b"broken")                                              # now the newest one is broken
    index = archive_pages.select(folder)
    assert index.path == new and index.status == "ok"
    data = client.post("/api/preview/available", json={"album_ids": ["101"]}).get_json()
    assert data["readable"] == ["101"] and data["archives"] == {"101": "zip"}


def test_packing_keeps_the_archived_pages_that_are_not_loose(downloads):
    from core import archive_pages
    from core.packer import CbzPacker
    folder = downloads / "Album"
    comic(folder, {"第1话/1.png": png("red"), "第1话/2.png": png("green")})
    (folder / "第1话" / "2.webp").parent.mkdir(parents=True, exist_ok=True)
    (folder / "第1话" / "2.webp").write_bytes(b"new page 2")  # the same page downloaded again: it wins
    (folder / "第2话").mkdir()
    (folder / "第2话" / "1.png").write_bytes(png("blue"))    # a new chapter
    CbzPacker().pack(folder, folder / "Album.cbz", base=archive_pages.select(folder))
    archive_pages.clear_cache()
    index = archive_pages.read_index(folder / "Album.cbz")
    got = {p.name: archive_pages.read_page(index, p) for p in index.pages}
    assert got == {"第1话/1.png": png("red"), "第1话/2.webp": b"new page 2", "第2话/1.png": png("blue")}


class _Photo(list):
    def __init__(self, photo_id, name, pages):
        super().__init__(range(pages))
        self.photo_id = photo_id
        self.name = name
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
    """The real download_album_job with auto-pack + delete originals; only fetching a chapter is replaced
    (it writes `written` pages of it, or all of them, then the job ends as `outcome` says)."""
    from core import database as db, jm_service
    from core.progress import progress_manager
    from core.settings import update_settings
    monkeypatch.setattr(jm_service, "DOWNLOAD_ROOT", downloads)
    monkeypatch.setattr(jm_service, "close_client", lambda _client: None)
    update_settings({"auto_pack": "true", "delete_originals": "true", "pack_format": "cbz"})
    album = _Album([_Photo("71", "第1话", 3), _Photo("72", "第2话", 2)])
    monkeypatch.setattr(jm_service, "get_client", lambda shared=True: (_Client(album), None))

    def run(job_id, photo_ids, outcome="completed", written=None):
        def chapter(job_id, album_id, album, album_dir, photo, total_pages, done_pages, pending_images,
                    failed_pages, *rest):
            folder = jm_service._chapter_output_dir(album_dir, photo)
            for n in range(1, (written or len(photo)) + 1):
                (folder / f"{n:05d}.webp").write_bytes(f"{photo.photo_id}-{n}".encode())
                done_pages[0] += 1
            if outcome == "failed":
                failed_pages.append("下载失败")
        monkeypatch.setattr(jm_service, "_download_chapter", chapter)
        db.insert_job(job_id, "3001", "Name", photo_ids)
        db.update_job(job_id, status="running")
        progress_manager.create_tracker(job_id)
        try:
            jm_service.download_album_job(job_id, "3001", photo_ids)
        finally:
            progress_manager.remove_tracker(job_id)
        return db.get_job(job_id)
    return run


def _archived(downloads):
    from core import archive_pages
    archive_pages.clear_cache()
    index = archive_pages.read_index(downloads / "Name_3001" / "Name_3001.cbz")
    return {p.name.split("__")[1]: archive_pages.read_page(index, p).decode() for p in index.pages}


def test_a_chapter_only_update_keeps_the_archived_chapters(client, downloads, download):
    assert download("ja", ["71"])["status"] == "completed"
    assert _archived(downloads) == {"71/00001.webp": "71-1", "71/00002.webp": "71-2", "71/00003.webp": "71-3"}
    assert not list((downloads / "Name_3001").rglob("*.webp"))  # originals deleted
    assert download("jb", ["72"])["status"] == "completed"      # later: only the new chapter
    assert _archived(downloads) == {"71/00001.webp": "71-1", "71/00002.webp": "71-2", "71/00003.webp": "71-3",
                                    "72/00001.webp": "72-1", "72/00002.webp": "72-2"}
    data = client.get("/api/preview/3001").get_json()
    assert (data["source"], data["total_pages"]) == ("archive", 5)


def test_a_failed_redownload_never_hides_the_archive(client, downloads, download):
    from core import database as db
    assert download("ja", ["71"])["status"] == "completed"
    assert download("jb", ["71"], outcome="failed", written=1)["status"] == "failed"
    assert db.get_job("ja")["superseded_at"] is None   # the archive still holds the whole earlier download
    data = client.get("/api/preview/3001").get_json()
    assert (data["source"], data["total_pages"]) == ("mixed", 3)  # page 1 loose, pages 2-3 from the archive
    response = client.post("/api/export/ja/zip")
    with zipfile.ZipFile(io.BytesIO(response.data)) as zf:
        assert len([n for n in zf.namelist() if n.endswith(".webp")]) == 3
    response.close()


def test_read_button_look_follows_the_same_fact_as_read(client, downloads):
    # the latest job failed (or is queued) but the last completed download left only a broken archive:
    # /read opens the local page that explains it, so every page must draw 阅读 that way (not "online")
    import time
    from core import database as db
    junk_archive(downloads / "Bad")
    _job("j1", "101", downloads / "Bad")
    time.sleep(0.01)
    _job("j2", "101", None, status="failed")
    db.add_wishlist("101", "Bad", "", "")
    for url in ("/api/wishlist", "/api/library"):
        item = client.get(url).get_json()["items"][0]
        assert (item["status_group"], item["local_problem"], item["archive_problem"]) == \
            ("failed", None, "archive_corrupt"), url
    data = client.post("/api/preview/available", json={"album_ids": ["101"]}).get_json()
    assert data["unavailable"] == {} and data["archive_problems"] == {"101": "archive_corrupt"}
    assert 'data-source="local"' in client.get("/read/101").get_data(as_text=True)
    for script, call in (("library.js", "stateFor(item.readable, item.archive_problem)"),
                         ("wishlist.js", "stateFor(item.readable, item.archive_problem)"),
                         ("search.js", "stateFor(readable, archiveProblem)"),
                         ("downloads.js", "stateFor(readableAlbums[key], (localInfo[key] || {}).state)")):
        assert call in (STATIC_JS / script).read_text(encoding="utf-8"), script


@pytest.mark.parametrize("layout", ["empty", "foreign_zip", "nested_cbz"])
def test_exports_without_a_single_page_say_so(client, downloads, exports_tmp, layout):
    folder = downloads / "Album"
    folder.mkdir()
    if layout == "foreign_zip":
        write_zip(folder / "other.zip", PAGES)          # not the app's archive: not a page source
    elif layout == "nested_cbz":
        write_zip(folder / "ch1" / "ch1.cbz", PAGES)    # archives in sub folders are not used either
    _job("je", "101", folder)
    for fmt in ("zip", "pdf"):
        response = client.post(f"/api/export/je/{fmt}")
        assert response.status_code == 404 and response.get_json()["message"] == "下载目录中没有图片", fmt
    assert list(exports_tmp.iterdir()) == []


def test_pdf_of_loose_plus_archive_has_every_page_once(client, downloads, exports_tmp):
    import pikepdf
    folder = loose(downloads / "Both")
    comic(folder)
    _job("jp", "101", folder)
    response = client.post("/api/export/jp/pdf")
    with pikepdf.Pdf.open(io.BytesIO(response.data)) as pdf:
        widths = [float(page.mediabox[2]) for page in pdf.pages]
    response.close()
    assert len(widths) == 5 and widths == sorted(widths) and len(set(widths)) == 5
    assert list(exports_tmp.iterdir()) == []


def test_pdf_names_an_undecodable_archive_page_by_its_archive_name(client, downloads, exports_tmp):
    comic(downloads / "Album", {"第1话/1.png": png("red"), "第1话/2.png": b"not an image"})
    _job("jp", "101", downloads / "Album")
    response = client.post("/api/export/jp/pdf")
    assert response.status_code == 422 and response.get_json()["failed_images"] == ["第1话/2.png"]
    assert list(exports_tmp.iterdir()) == []


def test_pdf_temp_write_failure_is_not_called_a_corrupt_archive(client, downloads, exports_tmp, monkeypatch):
    comic(downloads / "Album")
    _job("jp", "101", downloads / "Album")
    real = Path.write_bytes

    def full(self, data):
        if "jm-pdf-pages-" in str(self):
            raise OSError(28, "No space left on device")
        return real(self, data)
    monkeypatch.setattr(Path, "write_bytes", full)
    response = client.post("/api/export/jp/pdf")
    body = response.get_json()
    assert response.status_code == 500 and "reason" not in body and "临时空间不足" in body["message"]
    assert list(exports_tmp.iterdir()) == []


def test_pdf_cleans_the_extracted_pages_after_any_error(client, downloads, exports_tmp, monkeypatch):
    from routes import api_export
    comic(downloads / "Album")
    _job("jp", "101", downloads / "Album")
    monkeypatch.setattr(api_export, "_build_pdf", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    assert client.post("/api/export/jp/pdf").status_code == 500
    assert list(exports_tmp.iterdir()) == []


def test_download_manager_packed_badge_needs_confirmed_loose_pages():
    source = (STATIC_JS / "downloads.js").read_text(encoding="utf-8")
    card = source[source.index("function renderCompletedCard"):source.index("function renderFailedCard")]
    assert "var cbzBadge = readable && local.state === 'loose' && packFormat" in card


def test_damaged_archive_pages_are_explained_in_both_readers():
    nav = (STATIC_JS / "reading-nav.js").read_text(encoding="utf-8")
    assert "archivePageProblem: function (url)" in nav and "'/api/preview-archive/'" in nav
    for script in ("reader.js", "preview.js"):
        source = (STATIC_JS / script).read_text(encoding="utf-8")
        assert "nav.archivePageProblem(" in source and "在线阅读这一页" in source, script


# ─── the shared badge / 阅读 helpers and the search + detail setters, run in Node ─────────────────

NODE = shutil.which("node")
BADGE_HARNESS = r"""
const fs = require('fs');
const [utilsPath, navPath, searchPath, detailPath] = process.argv.slice(1);
function textNode(v) { return { nodeType: 3, data: String(v), parentNode: null }; }
function el(tag) {
  const cls = new Set(), attrs = {};
  const e = {
    nodeType: 1, tagName: tag.toUpperCase(), children: [], parentNode: null, hidden: false, title: '', href: '',
    get className() { return Array.from(cls).join(' '); },
    set className(v) { cls.clear(); String(v).split(/\s+/).forEach(c => c && cls.add(c)); },
    classList: { add: (...n) => n.forEach(c => cls.add(c)), remove: (...n) => n.forEach(c => cls.delete(c)),
                 contains: c => cls.has(c) },
    setAttribute: (k, v) => { attrs[k] = String(v); }, getAttribute: k => (k in attrs ? attrs[k] : null),
    removeAttribute: k => { delete attrs[k]; },
    appendChild(c) { if (c.parentNode) c.parentNode.children.splice(c.parentNode.children.indexOf(c), 1);
                     c.parentNode = e; e.children.push(c); return c; },
    append(...cs) { cs.forEach(c => e.appendChild(typeof c === 'string' ? textNode(c) : c)); },
    remove() { if (e.parentNode) { e.parentNode.children.splice(e.parentNode.children.indexOf(e), 1); e.parentNode = null; } },
    get textContent() { return e.children.map(c => c.nodeType === 3 ? c.data : c.textContent).join(''); },
    set textContent(v) { e.children = []; if (v !== '') e.appendChild(textNode(v)); },
    all() { return e.children.filter(c => c.nodeType === 1).flatMap(c => [c, ...c.all()]); },
    querySelectorAll(sel) { const c = sel.slice(1); return e.all().filter(n => n.classList.contains(c)); },
    querySelector(sel) { return e.querySelectorAll(sel)[0] || null; },
    get outerHTML() { return '<' + tag + ' class="' + e.className + '">' + e.textContent + '</' + tag + '>'; },
  };
  return e;
}
global.window = global;
window.addEventListener = () => {};
global.location = { origin: 'http://x' };
const ids = {};
global.document = { createElement: el, createTextNode: textNode, getElementById: id => ids[id] || null };
eval(fs.readFileSync(utilsPath, 'utf8'));
eval(fs.readFileSync(navPath, 'utf8'));
const pick = (path, from, to) => { const s = fs.readFileSync(path, 'utf8'); return s.slice(s.indexOf(from), s.indexOf(to)); };
eval(pick(searchPath, 'function setCardReadable', 'function refreshReadable'));
eval(pick(detailPath, 'function setOfflineStatus', 'function refreshOfflineStatus'));
const visible = box => box.all().filter(b => (b.classList.contains('badge') || b.classList.contains('offline-badge')) && !b.hidden)
  .map(b => b.textContent.trim());
const out = { stateFor: [], badges: {}, search: [], detail: [], nav: [] };
for (const args of [[true, 'archive_corrupt'], [false, 'archive_corrupt'], [false, 'archive_empty'], [false, 'deleted'],
                    [false, undefined], [undefined, undefined]]) out.stateFor.push(window.readLink.stateFor(...args));
const link = window.readLink.create('101', 'archive_problem', 'btn-sm');
out.badges.link = [link.getAttribute('data-read-state'), link.className, link.children[0].className, link.title];
for (const r of ['deleted', 'archive_corrupt', 'archive_empty', 'bogus']) {
  const b = window.localBadges.problem(r); out.badges[r] = b && [b.className, b.textContent.trim()];
}
for (const f of ['cbz', 'zip', 'rar']) { const b = window.localBadges.archive(f); out.badges[f] = b && [b.className, b.textContent.trim()]; }
// search card: (readable, archive format, unavailable reason, archive problem) as refreshReadable passes them
const card = el('div'); const box = el('div'); box.className = 'cover-badges';
const cover = el('span'); cover.className = 'offline-badge offline-badge--cover'; cover.hidden = true; cover.textContent = '已下载内容 · 可离线阅读';
box.appendChild(cover); card.appendChild(box);
const read = window.readLink.create('101', undefined, 'reader-link'); read.classList.add('reader-link'); card.appendChild(read);
for (const args of [[true, 'cbz'], [false, undefined, 'archive_corrupt', 'archive_corrupt'],
                    [false, undefined, undefined, 'archive_empty'], [false, undefined, 'deleted'], [true, 'zip'], [false]]) {
  setCardReadable(card, ...args); out.search.push([visible(box), read.getAttribute('data-read-state')]);
}
// detail: (readable, archive format, unavailable reason)
const marker = el('div'); marker.hidden = true; const offline = el('span'); offline.className = 'offline-badge'; offline.hidden = true;
offline.textContent = '已下载内容 · 可离线阅读'; marker.appendChild(offline); ids['album-offline-status'] = marker;
const btn = el('a'); btn.hidden = true; ids['local-read-btn'] = btn;
for (const args of [[true, 'cbz'], [false, undefined, 'archive_empty'], [false, undefined, 'deleted'], [true], [false]]) {
  setOfflineStatus(...args); out.detail.push([marker.hidden, visible(marker), btn.hidden]);
}
// damaged archive page: 422 archive_corrupt → the server's message; anything else → null
const replies = { '/api/preview-archive/1/3': { ok: false, status: 422, json: async () => ({ reason: 'archive_corrupt', message: '坏页' }) },
                  '/api/preview-archive/1/4': { ok: false, status: 404, json: async () => ({}) } };
global.fetch = async url => replies[url] || { ok: true, status: 200 };
(async () => {
  for (const url of ['/api/preview-archive/1/3', '/api/preview-archive/1/4', '/api/preview-img/a/1.png', '/api/preview-archive/1/1'])
    out.nav.push(await window.readingNav.archivePageProblem(url));
  process.stdout.write(JSON.stringify(out));
})().catch(err => { console.error(err && err.stack || err); process.exit(1); });
"""


@pytest.fixture(scope="module")
def badge_harness():
    import json
    import subprocess
    if NODE is None:
        pytest.skip("needs Node.js (CI installs it)")
    result = subprocess.run([NODE, "-e", BADGE_HARNESS] + [str(STATIC_JS / name) for name in
                            ("utils.js", "reading-nav.js", "search.js", "detail.js")],
                            capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_read_look_helper(badge_harness):
    assert badge_harness["stateFor"] == [True, "archive_problem", "archive_problem", False, False, False]
    state, classes, icon, title = badge_harness["badges"]["link"]
    assert state == "archive_problem" and "btn-outline-primary" in classes and "bi-book" in icon and "压缩包" in title


def test_reason_and_archive_badges(badge_harness):
    b = badge_harness["badges"]
    assert b["deleted"] == ["badge status-badge-muted", "文件已删除"]
    assert b["archive_corrupt"] == ["badge status-badge-warning", "压缩包损坏"]
    assert b["archive_empty"] == ["badge status-badge-warning", "压缩包无可阅读图片"]
    assert b["cbz"] == ["badge status-badge-archive", "CBZ"] and b["zip"] == ["badge status-badge-archive", "ZIP"]
    assert b["bogus"] is None and b["rar"] is None


def test_search_cards_show_the_shared_badges_and_drop_stale_ones(badge_harness):
    assert badge_harness["search"] == [
        [["已下载内容 · 可离线阅读", "CBZ"], "local"],
        [["压缩包损坏"], "archive_problem"],
        [[], "archive_problem"],          # failed / queued group: no reason badge, but 阅读 explains the archive
        [["文件已删除"], "online"],
        [["已下载内容 · 可离线阅读", "ZIP"], "local"],
        [[], "online"],
    ]


def test_detail_shows_the_shared_badges(badge_harness):
    assert badge_harness["detail"] == [
        [False, ["已下载内容 · 可离线阅读", "CBZ"], False],
        [False, ["压缩包无可阅读图片"], True],
        [False, ["文件已删除"], True],
        [False, ["已下载内容 · 可离线阅读"], False],
        [True, [], True],
    ]


def test_damaged_page_helper_reads_the_servers_reason(badge_harness):
    assert badge_harness["nav"] == [{"reason": "archive_corrupt", "message": "坏页。"}, None, None, None]


# ─── review round 2: repacks, the pack guard, transient errors, order, single open, wiring ────────


def test_a_repacked_archive_serves_the_same_page_by_name_or_says_it_changed(client, downloads):
    folder = downloads / "Rep"
    comic(folder, {"ch2__22/001.png": png("red"), "ch2__22/002.png": png("blue")})
    _job("j1", "101", folder)
    old = client.get("/api/preview/101").get_json()["pages"]
    assert [(p["chapter"], p["photo_id"], p["offset"]) for p in old] == [("ch2__22", "22", 0), ("ch2__22", "22", 1)]
    # repacked with an earlier chapter in front: page numbers shift, the old URLs still find their own pages
    comic(folder, {"ch1__11/001.png": png("green"), "ch2__22/001.png": png("red"), "ch2__22/002.png": png("blue")})
    os.utime(folder / "Rep.cbz", ns=(2_000_000_000_000_000_000, 2_000_000_000_000_000_000))
    assert [client.get(p["url"]).data for p in old] == [png("red"), png("blue")]
    # a page that is no longer in the archive: 409, never another page
    comic(folder, {"ch1__11/001.png": png("green")})
    os.utime(folder / "Rep.cbz", ns=(3_000_000_000_000_000_000, 3_000_000_000_000_000_000))
    response = client.get(old[1]["url"])
    assert response.status_code == 409 and response.get_json()["reason"] == "archive_changed"
    assert client.get("/api/preview-archive/101/1").data == png("green")  # without v: plain page number


def _flip_first_page(path, first):
    damage_page(path, first)


def test_auto_pack_never_overwrites_an_archive_it_cannot_fully_read(client, downloads, download):
    from core import archive_pages
    assert download("ja", ["71"])["status"] == "completed"
    target = downloads / "Name_3001" / "Name_3001.cbz"
    # rewrite the archive stored (uncompressed) so one page can be damaged without touching the others
    archive_pages.clear_cache()
    index = archive_pages.read_index(target)
    pages = {p.name: archive_pages.read_page(index, p) for p in index.pages}
    write_zip(target, pages, zipfile.ZIP_STORED)
    damage_page(target, pages[sorted(pages)[0]])       # page 1 damaged, pages 2-3 intact
    before = target.read_bytes()
    assert download("jb", ["72"])["status"] == "completed"
    assert target.read_bytes() == before                 # not replaced: pages 2-3 exist only in there
    assert sorted(p.name for p in (downloads / "Name_3001").rglob("*.webp")) == ["00001.webp", "00002.webp"]


def test_auto_pack_never_drops_archive_entries_it_had_to_skip(client, downloads, download):
    from core import archive_pages
    assert download("ja", ["71"])["status"] == "completed"
    target = downloads / "Name_3001" / "Name_3001.cbz"
    with zipfile.ZipFile(target, "a") as zf:  # an image entry the reader skips (BZIP2): it could not be carried over
        zf.writestr(zipfile.ZipInfo("extra/001.png"), png("red"), compress_type=zipfile.ZIP_BZIP2)
    archive_pages.clear_cache()
    assert archive_pages.read_index(target).skipped == 1
    before = target.read_bytes()
    assert download("jb", ["72"])["status"] == "completed"
    assert target.read_bytes() == before


def test_a_passing_read_error_never_supersedes_the_earlier_download(client, downloads, download, monkeypatch):
    from core import archive_pages, database as db, local_availability
    assert download("ja", ["71"])["status"] == "completed"
    archive_pages.clear_cache()
    with local_availability._cache_lock:
        local_availability._cache.clear()
    real, calls = archive_pages.zipfile.ZipFile, []

    def locked_once(*a, **k):
        calls.append(a)
        if len(calls) == 1:
            raise PermissionError(13, "locked")
        return real(*a, **k)
    monkeypatch.setattr(archive_pages.zipfile, "ZipFile", locked_once)
    assert download("jb", ["71"], outcome="failed", written=1)["status"] == "failed"
    monkeypatch.setattr(archive_pages.zipfile, "ZipFile", real)
    assert db.get_job("ja")["superseded_at"] is None
    assert client.get("/api/preview/3001").get_json()["total_pages"] == 3


@pytest.mark.parametrize("loose_name", ["第10话/1.png", "第1话/10.png"])
def test_merged_pages_keep_natural_order(client, downloads, exports_tmp, loose_name):
    import pikepdf
    folder = downloads / "Ord"
    comic(folder)
    loose(folder, [loose_name])
    _job("jo", "101", folder)
    data = client.get("/api/preview/101").get_json()
    assert data["source"] == "mixed" and [client.get(p["url"]).data for p in data["pages"]] == [PAGES[n] for n in ORDER]
    response = client.post("/api/export/jo/zip")
    with zipfile.ZipFile(io.BytesIO(response.data)) as zf:
        assert zf.namelist() == [f"Ord/{n}" for n in ORDER]
    response.close()
    response = client.post("/api/export/jo/pdf")
    with pikepdf.Pdf.open(io.BytesIO(response.data)) as pdf:
        widths = [float(page.mediabox[2]) for page in pdf.pages]
    response.close()
    assert widths == sorted(widths) and len(set(widths)) == 5


@pytest.mark.parametrize("fmt", ["zip", "pdf"])
def test_exports_open_the_archive_once(client, downloads, exports_tmp, monkeypatch, fmt):
    from core import archive_pages
    folder = downloads / "Long"
    comic(folder, {f"ch1/{n:03d}.png": png("white", (8 + n % 5, 8)) for n in range(1, 61)})
    _job("jl", "101", folder)
    archive_pages.select(folder)  # index warm, as after the reader opened it
    real, opens = zipfile.ZipFile, []

    class Counting(real):
        def __init__(self, file, mode="r", *a, **k):
            if mode == "r":
                opens.append(file)
            super().__init__(file, mode, *a, **k)
    monkeypatch.setattr(archive_pages.zipfile, "ZipFile", Counting)
    response = client.post(f"/api/export/jl/{fmt}")
    assert response.status_code == 200 and len(opens) == 1, opens
    response.close()


def test_availability_survives_more_archives_than_the_index_cache_holds(client, downloads, monkeypatch):
    from core import archive_pages, local_availability
    monkeypatch.setattr(archive_pages, "_CACHE_MAX", 2)
    for n in range(6):
        comic(downloads / f"A{n}")
        _job(f"j{n}", str(200 + n), downloads / f"A{n}")
    ids = [str(200 + n) for n in range(6)]
    assert local_availability.readable_album_ids(ids) == set(ids)
    assert len(archive_pages._cache) <= 2
    builds = []
    build = archive_pages._build_index
    monkeypatch.setattr(archive_pages, "_build_index", lambda *a: builds.append(a) or build(*a))
    with local_availability._cache_lock:
        local_availability._cache.clear()   # as when every verdict's TTL ran out
    assert local_availability.readable_album_ids(ids) == set(ids) and builds == []  # no archive reopened


def test_download_manager_labels_only_readable_archives(client, downloads):
    loose(downloads / "LooseBad")
    junk_archive(downloads / "LooseBad")                    # loose pages + a broken CBZ: not "packed"
    _job("j1", "101", downloads / "LooseBad")
    comic(downloads / "Empty", {})
    _job("j2", "102", downloads / "Empty")
    loose(downloads / "Packed")
    comic(downloads / "Packed")
    _job("j3", "103", downloads / "Packed")
    jobs = {j["job_id"]: (j["has_cbz"], j["archive_format"]) for j in client.get("/api/jobs").get_json()["jobs"]}
    assert jobs == {"j1": (False, None), "j2": (False, None), "j3": (True, "cbz")}


def test_online_page_link_uses_the_chapter_and_the_offset():
    reader = (STATIC_JS / "reader.js").read_text(encoding="utf-8")
    assert "url.searchParams.delete('offset')" in reader and "parseInt(params.get('offset'), 10)" in reader
    for script in ("reader.js", "preview.js"):
        assert "nav.onlinePageUrl(albumId, " in (STATIC_JS / script).read_text(encoding="utf-8"), script


# Node: the full downloads.js, and search / detail from the API answer to the badges (real utils.js)
WIRING_HARNESS = r"""
const fs = require('fs');
const [utilsPath, downloadsPath, searchPath, detailPath, dataPath] = process.argv.slice(1);
const data = JSON.parse(fs.readFileSync(dataPath, 'utf8'));
const esc = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
function textNode(v) { return { nodeType: 3, data: String(v), parentNode: null }; }
function el(tag) {
  const cls = new Set(), attrs = {};
  let raw = null;
  const e = {
    nodeType: 1, tagName: tag.toUpperCase(), children: [], parentNode: null, hidden: false, title: '', href: '',
    dataset: {},
    get className() { return Array.from(cls).join(' '); },
    set className(v) { cls.clear(); String(v).split(/\s+/).forEach(c => c && cls.add(c)); },
    classList: { add: (...n) => n.forEach(c => cls.add(c)), remove: (...n) => n.forEach(c => cls.delete(c)),
                 contains: c => cls.has(c), toggle: (c, f) => (f ? cls.add(c) : cls.delete(c)) },
    setAttribute: (k, v) => { attrs[k] = String(v); }, getAttribute: k => (k in attrs ? attrs[k] : null),
    removeAttribute: k => { delete attrs[k]; },
    addEventListener: () => {},
    appendChild(c) { if (c.parentNode) c.parentNode.children.splice(c.parentNode.children.indexOf(c), 1);
                     c.parentNode = e; e.children.push(c); raw = null; return c; },
    append(...cs) { cs.forEach(c => e.appendChild(typeof c === 'string' ? textNode(c) : c)); },
    remove() { if (e.parentNode) { e.parentNode.children.splice(e.parentNode.children.indexOf(e), 1); e.parentNode = null; } },
    get textContent() { return e.children.map(c => c.nodeType === 3 ? c.data : c.textContent).join(''); },
    set textContent(v) { e.children = []; raw = null; if (v !== '') e.appendChild(textNode(v)); },
    get innerHTML() { return raw !== null ? raw : e.children.map(c => c.nodeType === 3 ? esc(c.data) : c.outerHTML).join(''); },
    set innerHTML(v) { e.children = []; raw = String(v); },
    get outerHTML() {
      const a = Object.assign({}, attrs);
      if (cls.size) a['class'] = e.className;
      if (e.title) a.title = e.title;
      if (e.href) a.href = e.href;
      const s = Object.keys(a).map(k => ' ' + k + '="' + String(a[k]).replace(/"/g, '&quot;') + '"').join('');
      return '<' + tag + s + '>' + e.innerHTML + '</' + tag + '>';
    },
    all() { return e.children.filter(c => c.nodeType === 1).flatMap(c => [c, ...c.all()]); },
    querySelectorAll(sel) { const c = sel.slice(1); return e.all().filter(n => n.classList.contains(c)); },
    querySelector(sel) { return e.querySelectorAll(sel)[0] || null; },
    contains: () => true,
  };
  return e;
}
const ids = {};
const loaded = [];
global.window = global;
window.addEventListener = () => {};
global.location = { origin: 'http://x', pathname: '/downloads', search: '' };
global.document = {
  createElement: el, createTextNode: textNode, body: el('body'), visibilityState: 'visible',
  getElementById: id => (ids[id] = ids[id] || el('div')),
  addEventListener: (type, fn) => { if (type === 'DOMContentLoaded') loaded.push(fn); },
};
global.setInterval = () => 1; global.clearInterval = () => {};
eval(fs.readFileSync(utilsPath, 'utf8'));
window.apiFetch = url => Promise.resolve(url === '/api/jobs' ? data.jobs : data.available);
window.showToast = () => {}; window._jobTitleMap = {};
window.setSSECallbacks = () => {}; window.connectSSE = () => {}; window.disconnectAllSSE = () => {};
eval(fs.readFileSync(downloadsPath, 'utf8'));
const pick = (path, from, to) => { const s = fs.readFileSync(path, 'utf8'); return s.slice(s.indexOf(from), s.indexOf(to)); };
const flush = () => new Promise(resolve => setImmediate(resolve));
const visible = box => box.all().filter(b => (b.classList.contains('badge') || b.classList.contains('offline-badge')) && !b.hidden)
  .map(b => b.textContent.trim());
(async () => {
  loaded.forEach(fn => fn());
  for (let i = 0; i < 5; i++) await flush();
  const out = { downloads: {}, search: {}, detail: {} };
  for (const section of ['completed', 'failed']) {
    const html = ids[section + '-section'].innerHTML;
    for (const part of html.split('<div class="card job-card').slice(1)) {
      const job = /data-job-id="([^"]+)"/.exec(part)[1];
      const status = (/job-card-status">([\s\S]*?)<\/div>/.exec(part) || [, ''])[1];
      const badges = Array.from(status.matchAll(/<span class="(?:badge [^"]*|offline-badge)"[^>]*>([\s\S]*?)<\/span>/g))
        .map(m => m[1].replace(/<[^>]+>/g, '').trim());
      out.downloads[section + ':' + job] = [badges, (/data-read-state="([^"]+)"/.exec(part) || [])[1] || null];
    }
  }
  // search: refreshReadable over one card per album
  eval(pick(searchPath, 'function setCardReadable', 'function pageButton'));
  const cards = data.ids.map(id => {
    const card = el('div'); card.setAttribute('data-album-id', id);
    const box = el('div'); box.className = 'cover-badges';
    const cover = el('span'); cover.className = 'offline-badge offline-badge--cover'; cover.hidden = true;
    cover.textContent = '已下载内容 · 可离线阅读'; box.appendChild(cover); card.appendChild(box);
    const link = window.readLink.create(id, undefined, 'reader-link'); link.classList.add('reader-link'); card.appendChild(link);
    return card;
  });
  global.resultsDiv = { querySelectorAll: () => cards, contains: () => true };
  refreshReadable();
  for (let i = 0; i < 3; i++) await flush();
  cards.forEach(card => {
    out.search[card.getAttribute('data-album-id')] = [visible(card.querySelector('.cover-badges')),
      card.querySelector('.reader-link').getAttribute('data-read-state')];
  });
  // detail: refreshOfflineStatus for each album
  eval(pick(detailPath, 'function setOfflineStatus', 'function bindEvents'));
  for (const id of data.ids) {
    const marker = el('div'); marker.hidden = true; const offline = el('span'); offline.className = 'offline-badge';
    offline.hidden = true; offline.textContent = '已下载内容 · 可离线阅读'; marker.appendChild(offline);
    ids['album-offline-status'] = marker; const btn = el('a'); btn.hidden = true; ids['local-read-btn'] = btn;
    global.albumId = id;
    refreshOfflineStatus();
    for (let i = 0; i < 3; i++) await flush();
    out.detail[id] = [marker.hidden ? null : visible(marker), !btn.hidden];
  }
  process.stdout.write(JSON.stringify(out));
})().catch(err => { console.error(err && err.stack || err); process.exit(1); });
"""


@pytest.fixture
def wiring(client, downloads, tmp_path):
    import json
    import subprocess
    import time
    from core import database as db
    if NODE is None:
        pytest.skip("needs Node.js (CI installs it)")
    _cases(downloads)
    junk_archive(downloads / "Later")                     # 109: broken archive, then a failed re-download
    _job("j9", "109", downloads / "Later")
    time.sleep(0.01)
    _job("j9b", "109", None, status="failed")
    ids = [str(n) for n in range(101, 110)]
    payload = {"ids": ids, "jobs": client.get("/api/jobs").get_json(),
               "available": client.post("/api/preview/available", json={"album_ids": ids}).get_json()}
    data = tmp_path / "wiring.json"
    data.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    result = subprocess.run([NODE, "-e", WIRING_HARNESS] + [str(STATIC_JS / name) for name in
                            ("utils.js", "downloads.js", "search.js", "detail.js")] + [str(data)],
                            capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


OFFLINE = "已下载内容 · 可离线阅读"


def test_download_manager_cards_follow_the_api(wiring):
    assert wiring["downloads"] == {
        "completed:j1": [["CBZ", "已完成", OFFLINE], "local"],        # loose pages + a good CBZ: "packed"
        "completed:j2": [["已完成", OFFLINE, "CBZ"], "local"],
        "completed:j3": [["已完成", OFFLINE, "ZIP"], "local"],
        "completed:j4": [["已完成", "压缩包损坏"], "archive_problem"],
        "completed:j5": [["已完成", "压缩包无可阅读图片"], "archive_problem"],
        "completed:j6": [["已完成", "文件已删除"], "online"],
        "completed:j7": [["已完成", "文件已删除"], "online"],
        "completed:j9": [["已完成", "压缩包损坏"], "archive_problem"],
        "failed:j9b": [[], "archive_problem"],
    }


def test_search_cards_follow_the_api(wiring):
    assert wiring["search"] == {
        "101": [[OFFLINE], "local"], "102": [[OFFLINE, "CBZ"], "local"], "103": [[OFFLINE, "ZIP"], "local"],
        "104": [["压缩包损坏"], "archive_problem"], "105": [["压缩包无可阅读图片"], "archive_problem"],
        "106": [["文件已删除"], "online"], "107": [["文件已删除"], "online"], "108": [[], "online"],
        "109": [[], "archive_problem"],   # latest job failed: no reason badge, but 阅读 opens the explanation
    }


def test_detail_follows_the_api(wiring):
    assert wiring["detail"] == {
        "101": [[OFFLINE], True], "102": [[OFFLINE, "CBZ"], True], "103": [[OFFLINE, "ZIP"], True],
        "104": [["压缩包损坏"], False], "105": [["压缩包无可阅读图片"], False],
        "106": [["文件已删除"], False], "107": [["文件已删除"], False], "108": [None, False], "109": [None, False],
    }


# ─── review round 3: every archive in the folder, empty files, busy archives, packed-away pages, readers ───


def _pack_format(fmt):
    from core.settings import update_settings
    update_settings({"pack_format": fmt})


def test_switching_the_pack_format_keeps_every_chapter(client, downloads, download):
    assert download("ja", ["71"])["status"] == "completed"               # Name_3001.cbz: 71
    _pack_format("zip")
    assert download("jb", ["72"])["status"] == "completed"               # Name_3001.zip: 71 + 72
    data = client.get("/api/preview/3001").get_json()
    assert (data["archive"], data["total_pages"]) == ("zip", 5)
    _pack_format("cbz")
    assert download("jc", ["71"])["status"] == "completed"               # the old cbz is rebuilt from both
    data = client.get("/api/preview/3001").get_json()
    assert (data["archive"], data["total_pages"]) == ("cbz", 5)
    assert _archived(downloads) == {"71/00001.webp": "71-1", "71/00002.webp": "71-2", "71/00003.webp": "71-3",
                                    "72/00001.webp": "72-1", "72/00002.webp": "72-2"}


@pytest.mark.parametrize("trouble", ["busy", "skipped"])
def test_an_archive_that_cannot_be_merged_blocks_packing(client, downloads, download, monkeypatch, trouble):
    from core import archive_pages
    assert download("ja", ["71"])["status"] == "completed"               # Name_3001.cbz: 71
    old = downloads / "Name_3001" / "Name_3001.cbz"
    if trouble == "skipped":
        with zipfile.ZipFile(old, "a") as zf:
            zf.writestr(zipfile.ZipInfo("extra/001.png"), png("red"), compress_type=zipfile.ZIP_BZIP2)
    archive_pages.clear_cache()
    before = old.read_bytes()
    _pack_format("zip")                                                   # the cbz would not be overwritten ...
    with monkeypatch.context() as patch:
        if trouble == "busy":
            _lock(patch, ".cbz")
        assert download("jb", ["72"])["status"] == "completed"
    # ... but a new zip would hide it: no pack, originals kept, the cbz untouched
    assert not (downloads / "Name_3001" / "Name_3001.zip").exists()
    assert old.read_bytes() == before
    assert len(list((downloads / "Name_3001").rglob("*.webp"))) == 2


def test_empty_leftover_files_are_neither_packed_nor_blocking(client, downloads, download):
    from core import archive_pages
    chapter = downloads / "Name_3001" / "第1话__71"
    chapter.mkdir(parents=True)
    (chapter / "00002.q7w3e9rt.webp").write_bytes(b"")                   # left by a download that was killed
    assert download("ja", ["71"])["status"] == "completed"
    archive_pages.clear_cache()
    index = archive_pages.read_index(downloads / "Name_3001" / "Name_3001.cbz")
    assert index.skipped == 0 and len(index.pages) == 3
    # the real pages were packed and deleted; the empty leftover is neither packed nor deleted ...
    assert sorted(p.name for p in (downloads / "Name_3001").rglob("*.webp")) == ["00002.q7w3e9rt.webp"]
    # ... and is not a page anywhere: the album is archive-only (CBZ badge), reader and PDF use the archive
    from core.local_availability import local_state
    assert local_state("3001").state == "archive"
    data = client.get("/api/preview/3001").get_json()
    assert (data["source"], data["total_pages"]) == ("archive", 3)
    response = client.post("/api/export/ja/zip")
    with zipfile.ZipFile(io.BytesIO(response.data)) as zf:
        pages = [n for n in zf.namelist() if n.endswith(".webp")]
    response.close()
    assert len(pages) == 3 and not any("q7w3e9rt" in n for n in pages)
    assert download("jb", ["72"])["status"] == "completed"               # later packs still work
    assert len(_archived(downloads)) == 5


def test_originals_are_kept_when_the_new_archive_does_not_check_out(client, downloads, download, monkeypatch):
    from core.packer import CbzPacker
    real = CbzPacker.pack

    def short(self, *a, **k):
        out = real(self, *a, **k)
        self.packed_count += 1   # as if a page went missing on the way into the archive
        return out
    monkeypatch.setattr(CbzPacker, "pack", short)
    assert download("ja", ["71"])["status"] == "completed"
    assert len(list((downloads / "Name_3001").rglob("*.webp"))) == 3     # nothing deleted


def _lock(monkeypatch, name):
    from core import archive_pages
    real = archive_pages.zipfile.ZipFile

    def locked(file, *a, **k):
        if str(file).endswith(name):
            raise PermissionError(32, "in use by another program")
        return real(file, *a, **k)
    monkeypatch.setattr(archive_pages.zipfile, "ZipFile", locked)


def test_a_busy_archive_is_never_called_corrupt(client, downloads, exports_tmp, monkeypatch):
    from core import archive_pages, local_availability
    comic(downloads / "Busy")
    _job("j1", "101", downloads / "Busy")
    assert client.get("/api/preview/101").status_code == 200
    pages = client.get("/api/preview/101").get_json()["pages"]
    archive_pages.clear_cache()
    with local_availability._cache_lock:
        local_availability._cache.clear()
    with monkeypatch.context() as patch:
        _lock(patch, "Busy.cbz")
        data = client.post("/api/preview/available", json={"album_ids": ["101"]}).get_json()
        assert data["readable"] == ["101"] and data["unavailable"] == {} and data["archive_problems"] == {}
        for response in (client.get("/api/preview/101"), client.get(pages[0]["url"]),
                         client.post("/api/export/j1/zip"), client.post("/api/export/j1/pdf")):
            body = response.get_json()
            assert response.status_code == 503 and body["reason"] == "archive_busy" and "暂时打不开" in body["message"]
        assert list(exports_tmp.iterdir()) == []
    assert client.get("/api/preview/101").status_code == 200              # nothing was remembered as corrupt


def test_a_locked_second_archive_keeps_the_folder_counted(client, downloads, monkeypatch):
    from core import archive_pages
    from core.local_availability import has_local_pages
    folder = downloads / "Two"
    good = comic(folder, suffix=".zip")
    os.utime(good, ns=(1_000_000_000, 1_000_000_000))
    junk_archive(folder)                                                  # Two.cbz: newer, broken
    _job("j1", "101", folder)
    _lock(monkeypatch, "Two.zip")                                          # Two.zip: good, but busy right now
    assert archive_pages.select_status(folder)[3] is True
    assert has_local_pages(folder)       # never "nothing left" (that would supersede the earlier download)
    assert client.post("/api/preview/available", json={"album_ids": ["101"]}).get_json()["readable"] == ["101"]
    for response in (client.get("/api/preview/101"), client.post("/api/export/j1/zip")):
        assert response.status_code == 503 and response.get_json()["reason"] == "archive_busy"  # not "Two.cbz 损坏"


def test_a_page_packed_away_after_the_reader_opened_says_refresh(client, downloads, download):
    import shutil
    folder = downloads / "Name_3001"
    assert download("ja", ["71"])["status"] == "completed"
    loose(folder, ["第2话__72/00001.webp"])
    url = next(p["url"] for p in client.get("/api/preview/3001").get_json()["pages"] if "__72" in p["url"])
    shutil.rmtree(folder / "第2话__72")
    comic(folder, {**{f"第1话__71/{n:05d}.webp": f"71-{n}".encode() for n in (1, 2, 3)},
                   "第2话__72/00001.webp": b"72-1"}, suffix=".cbz")        # auto-pack moved it into the archive
    os.utime(folder / "Name_3001.cbz", ns=(2_000_000_000_000_000_000, 2_000_000_000_000_000_000))
    response = client.get(url)
    assert response.status_code == 409 and response.get_json()["reason"] == "archive_changed"
    assert client.get("/api/preview-img/Name_3001/no-such/00009.webp").status_code == 404  # really gone: 404


def test_repack_details(client, downloads):
    folder = downloads / "Rep2"
    comic(folder, {"ch__5/001.png": png("red"), "ch__5/002.png": png("blue")})
    _job("j1", "101", folder)
    old = client.get("/api/preview/101").get_json()["pages"]
    comic(folder, {"ch__5/001.webp": b"webp one", "ch__5/002.webp": b"webp two"})  # same pages, now .webp
    os.utime(folder / "Rep2.cbz", ns=(2_000_000_000_000_000_000, 2_000_000_000_000_000_000))
    assert [client.get(p["url"]).data for p in old] == [b"webp one", b"webp two"]
    comic(folder, {"other__6/001.png": png("green")})
    os.utime(folder / "Rep2.cbz", ns=(3_000_000_000_000_000_000, 3_000_000_000_000_000_000))
    response = client.get(old[0]["url"])
    assert response.status_code == 409 and response.headers["Cache-Control"] == "no-store"


def test_a_failed_redownload_is_seen_at_once(client, downloads, download):
    from core.local_availability import local_state
    assert download("ja", ["71"])["status"] == "completed"
    assert local_state("3001").state == "archive"
    assert download("jb", ["71"], outcome="failed", written=1)["status"] == "failed"
    assert local_state("3001").state == "loose"     # forgotten at the end of the job, not after the TTL


# Node: the real reading-nav.js + reader.js / preview.js (fake DOM), adapted from the round-3 reviewer's harness
READER_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const JS = process.argv[1];
const scenario = JSON.parse(process.argv[2]);
function textNode(v) { return { nodeType: 3, data: String(v), parentNode: null }; }
function el(tag) {
  const cls = new Set(), attrs = {}, listeners = {};
  const e = {
    nodeType: 1, tagName: String(tag).toUpperCase(), children: [], parentNode: null, hidden: false, disabled: false,
    dataset: {}, style: {}, value: '', href: '', src: '', alt: '', loading: '', decoding: '', id: '', type: '', max: 0,
    get className() { return Array.from(cls).join(' '); },
    set className(v) { cls.clear(); String(v).split(/\s+/).forEach(c => c && cls.add(c)); },
    classList: { add: (...n) => n.forEach(c => cls.add(c)), remove: (...n) => n.forEach(c => cls.delete(c)),
                 contains: c => cls.has(c), toggle: (c, f) => ((f === undefined ? !cls.has(c) : f) ? cls.add(c) : cls.delete(c)) },
    setAttribute: (k, v) => { attrs[k] = String(v); }, getAttribute: k => (k in attrs ? attrs[k] : null),
    removeAttribute: k => { delete attrs[k]; },
    addEventListener: (t, fn) => { (listeners[t] = listeners[t] || []).push(fn); },
    fire: (t, ev) => (listeners[t] || []).forEach(fn => fn(ev || {})),
    appendChild(c) {
      if (c.isFragment) { c.children.slice().forEach(x => e.appendChild(x)); c.children = []; return c; }
      if (c.parentNode) c.parentNode.children.splice(c.parentNode.children.indexOf(c), 1);
      c.parentNode = e; e.children.push(c); return c;
    },
    append(...cs) { cs.forEach(c => e.appendChild(typeof c === 'string' ? textNode(c) : c)); },
    replaceChildren(...cs) { e.children.forEach(c => { c.parentNode = null; }); e.children = []; e.append(...cs); },
    replaceWith(n) { const p = e.parentNode; const i = p.children.indexOf(e); if (n.parentNode) n.parentNode.children.splice(n.parentNode.children.indexOf(n), 1); p.children[i] = n; n.parentNode = p; e.parentNode = null; },
    remove() { if (e.parentNode) { e.parentNode.children.splice(e.parentNode.children.indexOf(e), 1); e.parentNode = null; } },
    get textContent() { return e.children.map(c => c.nodeType === 3 ? c.data : c.textContent).join(''); },
    set textContent(v) { e.children = []; if (v !== '') e.appendChild(textNode(v)); },
    set innerHTML(v) { e.children = []; e.appendChild(textNode(String(v).replace(/<[^>]+>/g, ''))); },
    all() { return e.children.filter(c => c.nodeType === 1).flatMap(c => [c, ...c.all()]); },
    querySelectorAll(sel) {
      if (sel === 'img') return e.all().filter(n => n.tagName === 'IMG');
      if (sel.startsWith('.')) { const c = sel.slice(1).split(/[\s\[]/)[0]; return e.all().filter(n => n.classList.contains(c)); }
      return [];
    },
    querySelector(sel) { return e.querySelectorAll(sel)[0] || null; },
    contains: () => false, focus: () => {}, closest: () => null, matches: () => false, insertAdjacentHTML: () => {},
    scrollIntoView: () => {}, scrollTo: () => {}, scrollBy: () => {},
    getBoundingClientRect: () => ({ top: 0, bottom: 0, left: 0, width: 0 }),
  };
  return e;
}
const ids = {};
global.window = global;
const url0 = new URL(scenario.href);
const replaced = [];
global.location = { origin: url0.origin, href: url0.href, pathname: url0.pathname, search: url0.search, reload: () => replaced.push('RELOAD') };
global.history = { length: 1, state: null, replaceState: (s, t, u) => { replaced.push(u); const n = new URL(u, url0.origin); location.href = n.href; location.search = n.search; } };
global.sessionStorage = { getItem: () => null, setItem: () => {} };
global.requestAnimationFrame = fn => { setImmediate(fn); return 1; };
global.scrollY = 0; window.scrollTo = () => {};
window.addEventListener = () => {};
window.getSelection = () => ({ toString: () => '' });
const root = el('div');
root.setAttribute('data-album-id', '101');
if (scenario.online) root.setAttribute('data-source', 'online');
global.document = {
  createElement: el, createTextNode: textNode,
  createDocumentFragment: () => { const f = el('fragment'); f.isFragment = true; return f; },
  querySelector: sel => (sel === '.continuous-reader' && scenario.script === 'reader.js') ? root
                      : (sel === '.preview-container' && scenario.script === 'preview.js') ? root : null,
  getElementById: id => { if (ids['reader-pages']) { const f = ids['reader-pages'].all().find(n => n.id === id); if (f) return f; }
    return (ids[id] = ids[id] || Object.assign(el('div'), { id })); },
  addEventListener: () => {}, activeElement: null,
};
window.escapeHtmlAttr = s => String(s);
global.Image = function () { return el('img'); };
global.setTimeout = () => 0; global.clearTimeout = () => {};
window.apiFetch = () => scenario.loadError
  ? Promise.reject(Object.assign(new Error(scenario.loadError.message), { status: scenario.loadError.status }))
  : Promise.resolve(scenario.data);
global.fetch = async () => ({ ok: false, status: scenario.status, json: async () => scenario.reply });
eval(fs.readFileSync(path.join(JS, 'reading-nav.js'), 'utf8'));
const out = {};
out.onlinePageUrl = scenario.data.pages.map(p => window.readingNav.onlinePageUrl('101', p));
eval(fs.readFileSync(path.join(JS, scenario.script), 'utf8'));
const flush = () => new Promise(r => setImmediate(r));
(async () => {
  for (let i = 0; i < 8; i++) await flush();
  out.replaced = replaced.slice();
  if (scenario.loadError) {
    out.previewError = { message: ids['error-message'].textContent, retryHidden: ids['preview-retry'].classList.contains('d-none'),
                         heading: ids['album-title'].textContent, title: document.title };
    process.stdout.write(JSON.stringify(out)); return;
  }
  if (!scenario.failPage) { process.stdout.write(JSON.stringify(out)); return; }
  if (scenario.script === 'reader.js') {
    const fig = ids['reader-pages'].all().find(n => n.id === 'reader-page-' + scenario.failPage);
    fig.querySelector('img').onerror();
    for (let i = 0; i < 8; i++) await flush();
    const box = fig.querySelector('.reader-page-error');
    out.error = { text: box.children[0].textContent, controls: box.children.slice(1).map(c => [c.tagName, c.textContent.trim(), c.href || null]) };
  } else {
    const img = ids['preview-image'];
    if (scenario.failPage > 1) { ids['page-jump-input'].value = scenario.failPage; ids['page-jump'].fire('submit', { preventDefault() {} }); }
    img.setAttribute('src', scenario.data.pages[scenario.failPage - 1].url);
    img.fire('error');
    if (scenario.nextBeforeAnswer) ids['btn-next'].fire('click');   // the reader moves on before the answer comes
    for (let i = 0; i < 8; i++) await flush();
    const hint = ids['no-image-hint'];
    out.hintHidden = hint.classList.contains('d-none');
    out.error = { text: hint.children[1] && hint.children[1].textContent, controls: hint.children.slice(2).map(c => [c.tagName, c.textContent.trim(), c.href || null]) };
  }
  process.stdout.write(JSON.stringify(out));
})().catch(err => { console.error(err && err.stack || err); process.exit(1); });
"""

_READER_PAGES = [{"page": n, "url": f"/api/preview-archive/101/{n}?v=a-b&p=x{n}", "chapter": c, "photo_id": pid,
                  "offset": off}
                 for n, (c, pid, off) in enumerate([("第1话__11", "11", 0), ("第1话__11", "11", 1), ("第1话__11", "11", 2),
                                                    ("第2话__22", "22", 0), ("第2话__22", "22", 1), ("旧目录", None, 0)],
                                                   start=1)]


def _reader(script, href, status=None, reply=None, fail=None, online=False, urls=None, load_error=None,
            next_before_answer=False):
    import json
    import subprocess
    if NODE is None:
        pytest.skip("needs Node.js (CI installs it)")
    pages = [dict(p, url=(urls or {}).get(p["page"], p["url"])) for p in _READER_PAGES]
    scenario = {"script": script, "href": href, "online": online, "status": status, "reply": reply, "failPage": fail,
                "loadError": load_error, "nextBeforeAnswer": next_before_answer,
                "data": {"status": "ok", "title": "t", "pages": pages, "total_pages": len(pages)}}
    result = subprocess.run([NODE, "-e", READER_HARNESS, str(STATIC_JS), json.dumps(scenario)],
                            capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_online_page_url_uses_chapter_and_offset():
    out = _reader("reader.js", "http://x/read/101")
    assert out["onlinePageUrl"] == [
        {"url": "/online/101?chapter=11&offset=0", "exact": True}, {"url": "/online/101?chapter=11&offset=1", "exact": True},
        {"url": "/online/101?chapter=11&offset=2", "exact": True}, {"url": "/online/101?chapter=22&offset=0", "exact": True},
        {"url": "/online/101?chapter=22&offset=1", "exact": True},
        {"url": "/online/101", "exact": False}]  # folder without "__<photo_id>": online reading from the start


@pytest.mark.parametrize("offset, page", [("1", 5), ("99", 5), ("-5", 4), ("abc", 4)])
def test_online_reader_opens_the_chapter_at_the_offset(offset, page):
    out = _reader("reader.js", f"http://x/online/101?chapter=22&offset={offset}", online=True)
    assert out["replaced"][0] == f"/online/101?page={page}"


@pytest.mark.parametrize("script", ["reader.js", "preview.js"])
def test_damaged_page_offers_the_same_page_online(script):
    out = _reader(script, "http://x/read/101", 422, {"reason": "archive_corrupt", "message": "压缩包里这一页读不出来"}, 5)
    assert out["error"]["text"] == "压缩包里这一页读不出来。"
    assert out["error"]["controls"] == [["A", "在线阅读这一页", "/online/101?chapter=22&offset=1"]]  # no useless retry


@pytest.mark.parametrize("script", ["reader.js", "preview.js"])
def test_page_without_chapter_id_offers_online_reading_from_the_start(script):
    out = _reader(script, "http://x/read/101", 422, {"reason": "archive_corrupt", "message": "坏了。"}, 6)
    assert out["error"]["text"] == "坏了。" and out["error"]["controls"] == [["A", "在线阅读", "/online/101"]]


@pytest.mark.parametrize("script", ["reader.js", "preview.js"])
def test_changed_archive_offers_refresh(script):
    out = _reader(script, "http://x/read/101", 409, {"reason": "archive_changed", "message": "本地压缩包已更新，请刷新页面后再读"}, 2)
    assert out["error"]["text"] == "本地压缩包已更新，请刷新页面后再读。"
    assert out["error"]["controls"] == [["BUTTON", "刷新页面", None]]


@pytest.mark.parametrize("status, reply", [(503, {"reason": "archive_busy", "message": "暂时打不开"}),
                                           (422, {"reason": "archive_empty", "message": "x"}), (404, {})])
def test_other_failures_keep_the_retry(status, reply):
    out = _reader("reader.js", "http://x/read/101", status, reply, 3)
    assert out["error"]["controls"] == [["BUTTON", "重新加载图片", None]]


# ─── review round 4 ───────────────────────────────────────────────────────────


def test_a_zero_byte_entry_in_an_old_archive_does_not_block_packing(client, downloads, download):
    from core import archive_pages
    assert download("ja", ["71"])["status"] == "completed"
    cbz = downloads / "Name_3001" / "Name_3001.cbz"
    with zipfile.ZipFile(cbz, "a") as zf:  # what the old packer did with a killed download's leftover
        zf.writestr("第1话__71/00002.q7w3e9rt.webp", b"")
    archive_pages.clear_cache()
    assert archive_pages.read_index(cbz).skipped == 0
    assert download("jb", ["72"])["status"] == "completed"
    assert len(_archived(downloads)) == 5 and not list((downloads / "Name_3001").rglob("*.webp"))


def test_packing_merges_every_archive_newest_first(client, downloads, download):
    folder = downloads / "Name_3001"
    old = comic(folder, {f"第1话__71/{n:05d}.webp": f"71-{n}".encode() for n in (1, 2, 3)}, suffix=".cbz")
    os.utime(old, ns=(10**18, 10**18))                      # cbz: chapter 71 only (made before a format switch)
    new = comic(folder, {"第2话__72/00001.webp": b"x", "第1话__71/00001.webp": b"71-newer"}, suffix=".zip")
    os.utime(new, ns=(2 * 10**18, 2 * 10**18))              # zip: newer, never merged with the cbz
    assert download("ja", ["72"])["status"] == "completed"  # packs as cbz (overwrites the old cbz)
    assert _archived(downloads) == {"71/00001.webp": "71-newer", "71/00002.webp": "71-2", "71/00003.webp": "71-3",
                                    "72/00001.webp": "72-1", "72/00002.webp": "72-2"}


def test_a_busy_archive_after_its_index_was_cached(client, downloads, exports_tmp, monkeypatch):
    comic(downloads / "Busy")
    _job("j1", "101", downloads / "Busy")
    pages = client.get("/api/preview/101").get_json()["pages"]   # index cached, as in normal use
    with monkeypatch.context() as patch:
        _lock(patch, "Busy.cbz")
        for response in (client.get(pages[0]["url"]), client.post("/api/export/j1/zip"),
                         client.post("/api/export/j1/pdf")):
            body = response.get_json()
            assert response.status_code == 503 and body["reason"] == "archive_busy", body
    assert list(exports_tmp.iterdir()) == []
    assert client.get(pages[0]["url"]).status_code == 200


def test_a_front_truncated_archive_is_corrupt_not_busy(client, downloads):
    from core import archive_pages
    from core.local_availability import local_state
    path = comic(downloads / "Cut", compression=zipfile.ZIP_STORED)
    path.write_bytes(path.read_bytes()[10:])   # offsets now point before the start of the file
    _job("j1", "101", downloads / "Cut")
    index = archive_pages.read_index(path)
    assert index.status == "corrupt" and not index.transient
    assert local_state("101").state == "archive_corrupt"
    response = client.get("/api/preview/101")
    assert response.status_code == 422 and response.get_json()["reason"] == "archive_corrupt"


def test_the_preview_error_offers_retry_only_when_the_archive_is_busy():
    html = (ROOT / "templates" / "preview.html").read_text(encoding="utf-8")
    assert 'id="preview-retry"' in html and 'class="btn btn-outline-primary d-none" id="preview-retry"' in html


@pytest.mark.parametrize("status, hidden", [(503, False), (422, True), (404, True)])
def test_preview_load_error_retry(status, hidden):
    out = _reader("preview.js", "http://x/preview/101", load_error={"status": status, "message": "说明"})
    # the heading no longer stays on “加载中...”; the reason is in the error panel
    assert out["previewError"] == {"message": "说明", "retryHidden": hidden, "heading": "无法预览本地文件",
                                   "title": "无法预览本地文件 - JMComic 图片预览"}


@pytest.mark.parametrize("script", ["reader.js", "preview.js"])
def test_a_loose_page_packed_away_offers_refresh(script):
    out = _reader(script, "http://x/read/101", 409, {"reason": "archive_changed", "message": "这一页已打包进本地压缩包，请刷新页面后再读"},
                  2, urls={2: "/api/preview-img/A/ch__11/002.png"})
    assert out["error"] == {"text": "这一页已打包进本地压缩包，请刷新页面后再读。", "controls": [["BUTTON", "刷新页面", None]]}


@pytest.mark.parametrize("script", ["reader.js", "preview.js"])
def test_a_busy_page_says_why_and_keeps_retry(script):
    out = _reader(script, "http://x/read/101", 503, {"reason": "archive_busy", "message": "本地压缩包暂时打不开"}, 3)
    retry = "重新加载图片" if script == "reader.js" else "重试"
    assert out["error"]["text"] == "本地压缩包暂时打不开。" and out["error"]["controls"] == [["BUTTON", retry, None]]


def test_a_late_answer_never_covers_the_next_page():
    out = _reader("preview.js", "http://x/preview/101", 422, {"reason": "archive_corrupt", "message": "坏页"}, 3,
                  next_before_answer=True)
    assert out["hintHidden"] and out["error"]["text"] == "图片加载失败"
