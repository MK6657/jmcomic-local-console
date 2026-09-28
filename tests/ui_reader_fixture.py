"""Explicit offline UI fixture. Synthetic pages only; never loads the user's database.

Run with the project Python: python tests/ui_reader_fixture.py --port 5010
检查新章节 never goes online here (an offline stub answers 立即检查; see /test/update-metrics). --fast-updates also runs
the background check loop against that stub with short delays.
"""
import argparse
import json
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


def add_partial_sample(downloads, db, Image, ImageDraw):
    """900010: 3 chapters upstream, only chapter 1 (3 pages) downloaded by a selected-chapter job. The detail page
    shows “部分章节已下载 · 1/3 话” (its detail request writes the chapter list the count is checked against)."""
    folder = downloads / "Partial chapters sample"
    chapter = folder / "第1话__91001"
    chapter.mkdir(parents=True)
    (chapter / ".jm-chapter.json").write_text(json.dumps({"photo_id": "91001", "format": 2}), encoding="utf-8")
    for page in range(1, 4):
        img = Image.new("RGB", (600, 800), "#eeeae1")
        ImageDraw.Draw(img).text((200, 350), f"PARTIAL ch1 p{page}", fill="#2B2A27")
        img.save(chapter / f"{page:05d}.webp")
    db.add_wishlist("900010", "Partial chapters sample")
    db.insert_job("job_ui_900010", "900010", "Partial chapters sample", ["91001"])
    db.update_job("job_ui_900010", status="completed", output_path=str(folder))
    return {"album_id": "900010", "title": "Partial chapters sample", "author": "UI fixture",
            "cover": "/static/images/no-cover.svg", "tags": [], "chapter_count": 3,
            "photos": [{"photo_id": "91001", "title": "第1话", "page_count": 3},
                       {"photo_id": "91002", "title": "第2话", "page_count": 4},
                       {"photo_id": "91003", "title": "第3话", "page_count": 2}]}


def _episodes(*photo_ids, titles=None):
    """[(photo_id, index, title)] in upstream order (what a check sees)."""
    titles = titles or {}
    return [(str(pid), n, titles.get(str(pid), f"第{n}话")) for n, pid in enumerate(photo_ids, 1)]


# 立即检查 in this fixture never goes online: update_checker.fetch_upstream_episodes is replaced by this stub.
# album_id → the upstream chapter list it answers; anything else fails like a network error.
UPDATE_UPSTREAM = {
    "900001": _episodes("900001"),                                       # never → 已记下上游现有的 1 话
    "900002": _episodes("92001", "92002", "92003"),                      # no_update
    "900004": _episodes(*(str(94000 + n) for n in range(1, 13))),        # baseline 12 话
    "900009": _episodes("99001", "99002", "99004", titles={"99004": "第3话（重新上传）"}),  # changed
    "900010": _episodes("91001", "91002", "91003"),                      # never → no_update
    "900011": _episodes("91101", "91102", "91103", "91104"),             # new: 第4话
    "900013": _episodes("93001", "93002"),                               # slow (3 s) → no_update
    "900500": _episodes("95001", "95002"),                               # new
}
SLOW_UPDATE = {"900013": 3.0}


def add_update_samples(downloads, db, Image, ImageDraw):
    """检查新章节 samples. Rows are written only through core.update_store with explicit times (no check runs):
    900001 no row (还没检查过) · 900002 no_update, checked 2 h ago · 900003 failed (network) twice, retry in 1 h,
    last success yesterday · 900004 baseline 12 话 (legacy first check) · 900009 changed (1 new, 1 gone) ·
    900010 download baseline 91001-91003, never checked (the partial badge stays 1/3) · 900011 'Update sample': a
    job with chapters 91101 + 91102 (baseline 91101-91103), 第4话 91104 confirmed yesterday (its detail serves 4
    chapters: 新 mark, 选中这些章节, 部分章节已下载 · 2/4 话) · 900012 readable, the stub fails (network) ·
    900013 'Slow check sample': the stub answers after 3 s (检查中… visible) · 900500 new (the chip only on the
    newer of its two completed cards) · 900700 files deleted with a stale 'new' row: shows nothing anywhere.
    Returns the detail the fixture serves for 900011."""
    from datetime import datetime, timedelta
    from core import update_store
    now = datetime.now().replace(microsecond=0)

    def page(path, label):
        img = Image.new("RGB", (600, 800), "#eeeae1")
        ImageDraw.Draw(img).text((200, 350), label, fill="#2B2A27")
        path.parent.mkdir(parents=True, exist_ok=True)
        img.save(path)

    def completed(job_id, album_id, title, folder, photo_ids=()):
        db.insert_job(job_id, album_id, title, list(photo_ids))
        db.update_job(job_id, status="completed", output_path=str(folder))

    def downloaded(album_id, episodes, at):
        update_store.note_download_started(album_id, episodes, had_local_content=False, now=at,
                                           next_at=at + timedelta(hours=24))

    def checked(album_id, episodes, at, trigger="auto"):
        update_store.begin_check(album_id, trigger, at)
        update_store.record_success(album_id, episodes, trigger, at, at, 1, at + timedelta(hours=24))

    def failed(album_id, at, retry_in):
        update_store.begin_check(album_id, "auto", at)
        update_store.record_failure(album_id, "network", "ConnectionError", "auto", at, at, 1,
                                    lambda _fail_count: retry_in)

    # 900002 CBZ only: no new chapters, checked 2 hours ago
    downloaded("900002", UPDATE_UPSTREAM["900002"], now - timedelta(days=3))
    checked("900002", UPDATE_UPSTREAM["900002"], now - timedelta(hours=2))
    # 900003 ZIP only: checked yesterday, then two network failures; the next try is in 1 hour
    downloaded("900003", _episodes("93101", "93102"), now - timedelta(days=3))
    checked("900003", _episodes("93101", "93102"), now - timedelta(days=1))
    failed("900003", now - timedelta(hours=3), 1800)
    failed("900003", now, 3600)
    # 900004 downloaded before the feature: the first check only recorded the 12 chapters upstream has
    checked("900004", UPDATE_UPSTREAM["900004"], now - timedelta(hours=5))
    # 900009 changed: 99004 appeared while 99003 vanished (re-upload?)
    downloaded("900009", _episodes("99001", "99002", "99003"), now - timedelta(days=3))
    checked("900009", UPDATE_UPSTREAM["900009"], now - timedelta(hours=1))
    # 900010 partial download: the download recorded the full list, never checked since
    downloaded("900010", UPDATE_UPSTREAM["900010"], now - timedelta(days=2))

    # 900011 Update sample: chapters 1-2 downloaded (marked folders), 3 not selected, 4 new since
    folder = downloads / "Update sample"
    pages = {"91101": 3, "91102": 2}
    for n, (photo_id, count) in enumerate(pages.items(), 1):
        chapter = folder / f"第{n}话__{photo_id}"
        chapter.mkdir(parents=True)
        (chapter / ".jm-chapter.json").write_text(json.dumps({"photo_id": photo_id, "format": 2}), encoding="utf-8")
        for number in range(1, count + 1):
            page(chapter / f"{number:05d}.webp", f"UPDATE ch{n} p{number}")
    db.add_wishlist("900011", "Update sample")
    completed("job_ui_900011", "900011", "Update sample", folder, pages)
    downloaded("900011", _episodes("91101", "91102", "91103"), now - timedelta(days=3))
    checked("900011", UPDATE_UPSTREAM["900011"], now - timedelta(days=1))

    # 900012 readable, its check fails (network); 900013 readable, its check takes 3 s
    for album_id, title in (("900012", "Network failure sample"), ("900013", "Slow check sample")):
        target = downloads / title
        page(target / "001.png", title)
        completed(f"job_ui_{album_id}", album_id, title, target)
    downloaded("900013", UPDATE_UPSTREAM["900013"], now - timedelta(days=1))

    # 900500 (two completed jobs): new chapter 95002
    downloaded("900500", _episodes("95001"), now - timedelta(days=2))
    checked("900500", UPDATE_UPSTREAM["900500"], now - timedelta(minutes=30))
    # 900700 files deleted: a stale 'new' row that must never show
    downloaded("900700", _episodes("97001"), now - timedelta(days=3))
    checked("900700", _episodes("97001", "97002"), now - timedelta(hours=2))

    return {"album_id": "900011", "title": "Update sample", "author": "UI fixture",
            "cover": "/static/images/no-cover.svg", "tags": [], "chapter_count": 4,
            "photos": [{"photo_id": "91101", "title": "第1话", "page_count": 3},
                       {"photo_id": "91102", "title": "第2话", "page_count": 2},
                       {"photo_id": "91103", "title": "第3话", "page_count": 4},
                       {"photo_id": "91104", "title": "第4话", "page_count": 5}]}


def install_update_stub(update_checker):
    """Replace the only network call of 检查新章节 with canned answers (UPDATE_UPSTREAM); unknown ids fail like a
    network error. Returns the metrics the stub keeps (calls, in flight, max in flight)."""
    import threading
    import time
    metrics = {"calls": [], "in_flight": 0, "max_in_flight": 0}
    lock = threading.Lock()

    def fake_fetch(album_id):
        album_id = str(album_id)
        with lock:
            metrics["calls"].append(album_id)
            metrics["in_flight"] += 1
            metrics["max_in_flight"] = max(metrics["max_in_flight"], metrics["in_flight"])
        try:
            if album_id in SLOW_UPDATE:
                time.sleep(SLOW_UPDATE[album_id])
            if album_id not in UPDATE_UPSTREAM:
                raise update_checker.CheckFailed("network", "fixture offline", 1)
            return list(UPDATE_UPSTREAM[album_id]), 1
        finally:
            with lock:
                metrics["in_flight"] -= 1

    update_checker.fetch_upstream_episodes = fake_fetch
    metrics["fake"] = fake_fetch
    return metrics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=5010)
    parser.add_argument("--fast-updates", action="store_true",
                        help="also start the background 检查新章节 loop with short delays (still the offline stub)")
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
    partial_detail = add_partial_sample(sandbox / "downloads", db, Image, ImageDraw)
    update_detail = add_update_samples(sandbox / "downloads", db, Image, ImageDraw)
    # 检查新章节: the only network call is replaced by the offline stub; the background loop stays off
    # (create_app never arms it) unless --fast-updates starts it below, after the stub is checked
    from core import update_checker
    update_metrics = install_update_stub(update_checker)
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
        served = {"900010": partial_detail, "900011": update_detail}.get(album_id)
        if served:
            # like the real /api/album: the detail page leaves a fresh chapter list in album_detail_cache
            db.set_cached_album_detail(album_id, json.dumps(served, ensure_ascii=False))
            return jsonify(status="ok", data=served)
        return jsonify(status="ok", data={"album_id": album_id, "title": "Sample detail", "author": "UI fixture",
            "cover": "/static/images/no-cover.svg", "photos": [], "tags": [], "chapter_count": 0})
    app.view_functions["api_search.search"] = search
    app.view_functions["api_album.album_detail"] = detail
    app.view_functions["api_library.album_tags_sync"] = lambda album_id: jsonify(status="ok", synced=0, total_tags=0)
    # 未下载的漫画点“阅读”会转到在线阅读：在线接口换成离线桩，从不访问上游
    app.view_functions["api_online.online_album"] = lambda album_id: (
        jsonify(status="error", message="在线加载失败，请稍后重试"), 502)
    app.add_url_rule("/test/metrics", "metrics", lambda: jsonify(calls))

    def update_metrics_view():
        conn = db.get_db()
        try:
            jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        finally:
            conn.close()
        stubbed = update_checker.fetch_upstream_episodes is update_metrics["fake"]
        return jsonify(fetch="stub" if stubbed else "REAL", calls=list(update_metrics["calls"]),
                       max_in_flight=update_metrics["max_in_flight"], jobs=jobs)
    app.add_url_rule("/test/update-metrics", "update_metrics", update_metrics_view)
    # the fixture's network is NOT blocked: never serve with the real fetch or an armed background loop
    assert update_checker.fetch_upstream_episodes is update_metrics["fake"], "检查新章节 must use the offline stub"
    assert not update_checker.runtime_status()["armed"], "the background check loop must not run in the fixture"
    if args.fast_updates:
        update_checker.STARTUP_DELAY, update_checker.STARTUP_JITTER = 5, 0
        update_checker.GAP, update_checker.GAP_JITTER = 10, 0
        update_checker.BACKOFF = (60,) + tuple(update_checker.BACKOFF[1:])
        update_checker.start()   # only after the stub assertion above
    print(f"Fixture: http://127.0.0.1:{args.port}; data: {sandbox}", flush=True)
    serve(app, host="127.0.0.1", port=args.port, threads=8)


if __name__ == "__main__":
    main()
