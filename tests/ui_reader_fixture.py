"""Explicit offline UI fixture. Synthetic pages only; never loads the user's database.

Run with the project Python: python tests/ui_reader_fixture.py --port 5010
"""
import argparse
import tempfile
from pathlib import Path
import sys


def add_archive_samples(downloads, db, Image, ImageDraw):
    """Archive-only reading samples (ids 900002-900009, all favourites, on the first search page):
    900002 CBZ only, 3 chapters (auto-packed, then the originals deleted) · 900003 the app's ZIP only, 2 chapters ·
    900004 a CBZ of 12 pages + only pages 1-5 still loose (like a re-download that stopped half way): the reader shows
    12 pages, 1-5 from the loose files, 6-12 from the CBZ · 900005 corrupt CBZ · 900006 CBZ without pages ·
    900007 files deleted · 900008 never downloaded · 900009 CBZ whose page 3 is damaged."""
    import shutil
    import zipfile
    from core.packer import CbzPacker

    def draw(path, label, colour):
        img = Image.new("RGB", (600, 800), "#eeeae1")
        pen = ImageDraw.Draw(img)
        pen.rectangle((30, 30, 570, 770), outline=colour, width=6)
        pen.text((200, 350), label, fill="#2B2A27")
        path.parent.mkdir(parents=True, exist_ok=True)
        img.save(path)

    def chapters(folder, prefix, counts, colour):
        for chapter, count in enumerate(counts, start=1):
            for page in range(1, count + 1):
                draw(folder / f"ch{chapter}" / f"{page:03d}.png", f"{prefix} ch{chapter} p{page}", colour)

    def completed(job_id, album_id, title, folder):
        db.add_wishlist(album_id, title)
        db.insert_job(job_id, album_id, title, [])
        db.update_job(job_id, status="completed", output_path=str(folder))

    for album_id, title, suffix, counts, colour in (
        ("900002", "CBZ only sample", ".cbz", (6, 7, 5), "#2B6CB0"),
        ("900003", "ZIP only sample", ".zip", (4, 4), "#2F855A"),
    ):
        folder = downloads / title
        chapters(folder, suffix[1:].upper(), counts, colour)
        CbzPacker().pack(folder, folder / f"{title}{suffix}")   # auto-pack ...
        for chapter in list(folder.iterdir()):
            if chapter.is_dir():
                shutil.rmtree(chapter)                             # ... then the originals were deleted
        completed(f"job_ui_{album_id}", album_id, title, folder)

    both = downloads / "Loose and CBZ sample"
    chapters(both, "LOOSE", (12,), "#B7791F")
    CbzPacker().pack(both, both / "Loose and CBZ sample.cbz")   # the archive holds 12 pages ...
    for extra in range(6, 13):
        (both / "ch1" / f"{extra:03d}.png").unlink()              # ... the loose folder only 1-5: 6-12 from the CBZ
    completed("job_ui_900004", "900004", "Loose and CBZ sample", both)

    corrupt = downloads / "Corrupt CBZ sample"
    corrupt.mkdir()
    (corrupt / "Corrupt CBZ sample.cbz").write_bytes(b"PK\x03\x04 not really a zip")
    completed("job_ui_900005", "900005", "Corrupt CBZ sample", corrupt)

    empty = downloads / "Empty CBZ sample"
    empty.mkdir()
    with zipfile.ZipFile(empty / "Empty CBZ sample.cbz", "w") as zf:
        zf.writestr("ComicInfo.xml", "<ComicInfo/>")
        zf.writestr("readme.txt", "no pages here")
    completed("job_ui_900006", "900006", "Empty CBZ sample", empty)

    completed("job_ui_900007", "900007", "Deleted files sample 2", downloads / "gone-900007")
    db.add_wishlist("900008", "Never downloaded sample 2")

    half = downloads / "Damaged page sample"
    chapters(half, "HALF", (5,), "#C53030")
    target = half / "Damaged page sample.cbz"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_STORED) as zf:   # stored: one flipped byte breaks one page
        for page in sorted((half / "ch1").iterdir()):
            zf.write(page, f"ch1/{page.name}")
    third = (half / "ch1" / "003.png").read_bytes()
    shutil.rmtree(half / "ch1")
    data = bytearray(target.read_bytes())
    data[data.index(third) + len(third) // 2] ^= 0xFF
    target.write_bytes(bytes(data))
    completed("job_ui_900009", "900009", "Damaged page sample", half)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=5010)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import core.path_utils as paths
    sandbox = Path(tempfile.mkdtemp(prefix="jm-reader-ui-"))
    paths.get_app_root = lambda: sandbox
    from app import create_app
    from core import database as db
    from flask import jsonify, request
    from PIL import Image, ImageDraw
    from waitress import serve
    app = create_app()
    output = sandbox / "downloads" / "Reader sample"
    output.mkdir(parents=True)
    for number in range(1, 46):
        img = Image.new("RGB", (600, 800), "#eeeae1")
        draw = ImageDraw.Draw(img)
        draw.rectangle((30, 30, 570, 770), outline="#686256", width=3)
        draw.text((240, 350), f"TEST PAGE {number}", fill="#2B2A27")
        img.save(output / f"{number:03d}.png")
    db.insert_job("job_ui", "900001", "Reader sample", [])
    db.update_job("job_ui", status="completed", output_path=str(output))
    # Same album, two distinct completed folders. Preview must use the newest one;
    # exporting each job must still use that job's own folder.
    for job_id, folder_name, colour in (
        ("job_ui_old", "Old title 900500", "#b7791f"),
        ("job_ui_new", "New title 900500", "#2f855a"),
    ):
        folder = sandbox / "downloads" / folder_name
        folder.mkdir()
        page = Image.new("RGB", (320, 420), "#eeeae1")
        ImageDraw.Draw(page).text((35, 190), job_id, fill=colour)
        page.save(folder / f"{job_id}.png")
        db.insert_job(job_id, "900500", folder_name, [])
        db.update_job(job_id, status="completed", output_path=str(folder))
    db.insert_job("job_ui_failed", "900600", "Failed download sample", [])
    db.update_job("job_ui_failed", status="failed", error_message="Synthetic offline failure")
    db.add_wishlist("900700", "Deleted files sample")
    db.insert_job("job_ui_missing", "900700", "Deleted files sample", [])
    db.update_job("job_ui_missing", status="completed", output_path=str(sandbox / "downloads" / "gone"))
    db.add_wishlist("900701", "Never downloaded sample")
    add_archive_samples(sandbox / "downloads", db, Image, ImageDraw)
    calls = {"search": 0}
    def search():
        calls["search"] += 1
        page = max(1, request.args.get("page", 1, type=int))
        page_size = min(100, max(1, request.args.get("page_size", 20, type=int)))
        start = (page - 1) * page_size
        return jsonify(status="ok", total=120, page=page, page_size=page_size, items=[{
            "album_id": str(900001 + index), "title": f"Sample comic {index + 1}",
            "author": "UI fixture", "cover_url": "/static/images/no-cover.svg", "tags": ["sample"],
        } for index in range(start, min(start + page_size, 120))])
    def detail(album_id):
        return jsonify(status="ok", data={"album_id": album_id, "title": "Sample detail", "author": "UI fixture",
            "cover": "/static/images/no-cover.svg", "photos": [], "tags": [], "chapter_count": 0})
    app.view_functions["api_search.search"] = search
    app.view_functions["api_album.album_detail"] = detail
    app.view_functions["api_library.album_tags_sync"] = lambda album_id: jsonify(status="ok", synced=0, total_tags=0)
    # 未下载的漫画点“阅读”会转到在线阅读：在线接口换成离线桩，从不访问上游
    app.view_functions["api_online.online_album"] = lambda album_id: (
        jsonify(status="error", message="在线加载失败，请稍后重试"), 502)
    app.add_url_rule("/test/metrics", "metrics", lambda: jsonify(calls))
    print(f"Fixture: http://127.0.0.1:{args.port}; data: {sandbox}", flush=True)
    serve(app, host="127.0.0.1", port=args.port, threads=8)


if __name__ == "__main__":
    main()
