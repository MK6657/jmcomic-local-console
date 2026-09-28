"""F1: “locally readable” never claims the whole comic is downloaded.

Every page says “已下载内容 · 可离线阅读” (downloaded content), and the detail page adds “部分章节已下载 · M/N 话”
only when the local chapter inventory proves it (core/chapter_inventory.py). Everything runs in pytest's tmp folders
with the network blocked (conftest): the real downloads/ and runtime/ are never touched, and nothing here starts a
download."""
import json
import re
import shutil
import subprocess
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC_JS = ROOT / "static" / "js"
TEMPLATES = ROOT / "templates"
NODE = shutil.which("node")

# the album the fake upstream serves: chapter 71 has 3 pages, 72 has 2, 73 has 4
CHAPTERS = [("71", "第1话", 3), ("72", "第2话", 2), ("73", "第3话", 4)]


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
def downloads(client, tmp_path, monkeypatch):
    from core import archive_pages, local_availability, path_guard
    from routes import api_export, api_preview
    root = tmp_path / "downloads"
    root.mkdir()
    monkeypatch.setattr(path_guard, "DOWNLOAD_ROOT", root)
    monkeypatch.setattr(api_preview, "DOWNLOAD_ROOT", root)
    monkeypatch.setattr(api_export, "DOWNLOAD_ROOT", root)
    archive_pages.clear_cache()
    with local_availability._cache_lock:
        local_availability._cache.clear()
    yield root
    archive_pages.clear_cache()


@pytest.fixture
def download(client, downloads, monkeypatch):
    """The real download_album_job; only fetching a chapter is replaced (it writes `written` pages of it, or all)."""
    from core import database as db, jm_service
    from core.progress import progress_manager
    monkeypatch.setattr(jm_service, "DOWNLOAD_ROOT", downloads)
    monkeypatch.setattr(jm_service, "close_client", lambda _client: None)
    album = _Album([_Photo(pid, name, pages) for pid, name, pages in CHAPTERS])
    monkeypatch.setattr(jm_service, "get_client", lambda shared=True: (_Client(album), None))

    def run(job_id, photo_ids, outcome="completed", written=None, pack=False, pack_format="cbz", organize="none"):
        from core.settings import update_settings
        update_settings({"auto_pack": "true" if pack else "false", "delete_originals": "true" if pack else "false",
                         "pack_format": pack_format, "organize_mode": organize})

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
        return db.get_job(job_id)["status"]
    return run


def _chapter_list(chapters=CHAPTERS, album_id="3001", age=None, **extra):
    """What the detail page leaves in album_detail_cache (the only local source of N)."""
    from core import database as db
    detail = {"album_id": album_id, "title": "Name", "chapter_count": len(chapters),
              "photos": [{"photo_id": pid, "title": name, "page_count": pages} for pid, name, pages in chapters]}
    detail.update(extra)
    db.set_cached_album_detail(album_id, json.dumps(detail, ensure_ascii=False))
    if age is not None:
        conn = db.get_db()
        try:
            conn.execute("UPDATE album_detail_cache SET cached_at=? WHERE album_id=?",
                         ((datetime.now() - age).isoformat(), album_id))
            conn.commit()
        finally:
            conn.close()


def _claim(client, album_id="3001"):
    from core import local_availability
    with local_availability._cache_lock:
        local_availability._cache.clear()
    data = client.get(f"/api/local-chapters/{album_id}").get_json()
    assert data["status"] == "ok"
    return data["partial"], data["reason"]


# ─── the four job shapes ──────────────────────────────────────────────────────


def test_a_selected_chapter_job_is_partial(client, download):
    assert download("ja", ["71"]) == "completed"
    _chapter_list()
    assert _claim(client) == ({"downloaded": 1, "total": 3}, "partial")


def test_a_full_job_makes_no_claim_about_being_complete(client, download):
    assert download("ja", []) == "completed"          # [] = every chapter upstream had at the time
    _chapter_list()
    assert _claim(client) == (None, "all_chapters")    # never "全部已下载": the label stays “已下载内容”
    # upstream then publishes a 4th chapter: the same files now prove only 3 of 4
    _chapter_list(CHAPTERS + [("74", "第4话", 5)])
    assert _claim(client) == ({"downloaded": 3, "total": 4}, "partial")


def test_multiple_jobs_add_up_and_a_broken_chapter_stops_the_count(client, download):
    assert download("ja", ["71"]) == "completed"
    assert download("jb", ["72"]) == "completed"
    _chapter_list()
    assert _claim(client) == ({"downloaded": 2, "total": 3}, "partial")
    # a later job for chapter 73 fails after one of its 4 pages: the album is still readable, but no count
    assert download("jc", ["73"], outcome="failed", written=1) == "failed"
    assert _claim(client) == (None, "incomplete_chapter")


def test_cbz_only_albums_are_counted_from_the_archive(client, download, downloads):
    assert download("ja", ["71"], pack=True) == "completed"
    folder = downloads / "Name_3001"
    assert not list(folder.rglob("*.webp")) and (folder / "Name_3001.cbz").exists()  # archive-only now
    _chapter_list()
    assert _claim(client) == ({"downloaded": 1, "total": 3}, "partial")
    assert download("jb", ["72"], pack=True) == "completed"    # merged into the archive
    assert _claim(client) == ({"downloaded": 2, "total": 3}, "partial")


# ─── when nothing may be claimed ──────────────────────────────────────────────


def test_no_count_without_a_fresh_chapter_list(client, download):
    assert download("ja", ["71"]) == "completed"
    assert _claim(client) == (None, "no_chapter_list")                   # never fetched here
    _chapter_list(age=timedelta(hours=2))
    assert _claim(client) == (None, "no_chapter_list")                   # stale: N may have changed
    _chapter_list(chapter_count=5)
    assert _claim(client) == (None, "no_chapter_list")                   # list and count disagree
    _chapter_list([("71", "第1话", 0), ("72", "第2话", 2), ("73", "第3话", 4)])
    assert _claim(client) == (None, "incomplete_chapter")                # 0 pages = unknown, proves nothing
    _chapter_list([("72", "第2话", 2), ("73", "第3话", 4)])
    assert _claim(client) == (None, "unknown_chapter")                   # upstream no longer lists chapter 71


@pytest.mark.parametrize("damage", ["flat", "legacy", "leftover", "marker", "twice", "unmarked", "unmarked_extra"])
def test_pages_that_cannot_be_attributed_stop_the_count(client, download, downloads, damage):
    assert download("ja", ["71"]) == "completed"
    folder = downloads / "Name_3001"
    chapter = next(p for p in folder.iterdir() if p.is_dir())
    if damage == "flat":          # organize_mode=flat puts pages at the album root
        (folder / "第1话_00001.webp").write_bytes(b"page")
    elif damage == "legacy":      # an old folder without marker or __<photo_id>
        (folder / "第1话").mkdir()
        (folder / "第1话" / "00001.webp").write_bytes(b"page")
    elif damage == "leftover":    # a killed download's temp file that got some bytes
        (chapter / "00002.q7w3e9rt.webp").write_bytes(b"part")
    elif damage == "marker":      # the marker names another chapter
        (chapter / ".jm-chapter.json").write_text(json.dumps({"photo_id": "72", "format": 2}), encoding="utf-8")
    elif damage == "unmarked":    # the chapter folder lost its marker
        (chapter / ".jm-chapter.json").unlink()
    elif damage == "unmarked_extra":  # a '<name>__<id>' folder the app never made (no marker)
        (folder / "第2话__72").mkdir()
        for n in (1, 2):
            (folder / "第2话__72" / f"{n:05d}.webp").write_bytes(b"page")
    else:                         # the same chapter in two folders
        twin = folder / (chapter.name + "_2")
        twin.mkdir()
        (twin / ".jm-chapter.json").write_text(json.dumps({"photo_id": "71", "format": 2}), encoding="utf-8")
        (twin / "00009.webp").write_bytes(b"page")
    _chapter_list()
    assert _claim(client) == (None, "unattributed")


def test_not_readable_albums_get_no_count(client, downloads):
    _chapter_list()
    assert _claim(client) == (None, "not_readable")          # never downloaded
    assert client.get("/api/local-chapters/12x").status_code == 400


def test_the_count_never_goes_online_or_starts_a_download(client, download, monkeypatch):
    from core import database as db, jm_service
    assert download("ja", ["71"]) == "completed"
    _chapter_list()

    def forbidden(*args, **kwargs):
        raise AssertionError("the chapter count must not fetch anything")
    monkeypatch.setattr(jm_service, "get_album_detail_cached", forbidden)
    monkeypatch.setattr(jm_service, "get_album_detail", forbidden)
    monkeypatch.setattr(jm_service, "get_client", forbidden)
    jobs = len(db.get_all_jobs())
    assert _claim(client) == ({"downloaded": 1, "total": 3}, "partial")
    assert len(db.get_all_jobs()) == jobs


# ─── wording on every page ────────────────────────────────────────────────────

PAGES = {
    "下载管理": ["static/js/downloads.js"],
    "收藏": ["static/js/wishlist.js", "templates/wishlist.html"],
    "资源库": ["static/js/library.js", "templates/library.html"],
    "搜索": ["static/js/search.js"],
    "详情": ["static/js/detail.js"],
}


def test_no_page_claims_the_whole_comic_is_downloaded():
    sources = {rel: (ROOT / rel).read_text(encoding="utf-8") for files in PAGES.values() for rel in files}
    sources["static/js/utils.js"] = (STATIC_JS / "utils.js").read_text(encoding="utf-8")
    sources["static/js/sse-client.js"] = (STATIC_JS / "sse-client.js").read_text(encoding="utf-8")
    for rel, text in sources.items():
        assert "本地文件完整" not in text, rel
        assert "已下载 · 可离线阅读" not in text and "已下载 ·'" not in text, rel
        assert "'已下载：" not in text and '"已下载：' not in text, rel
    for page, files in PAGES.items():
        assert any("已下载内容 · 可离线阅读" in sources[rel] or "已下载内容 ·" in sources[rel] for rel in files), page
    assert "下载任务完成" in sources["static/js/sse-client.js"]


@pytest.mark.parametrize("script", ["downloads.js", "wishlist.js", "library.js", "search.js", "detail.js"])
def test_every_readable_badge_says_it_may_not_be_the_whole_comic(script):
    source = (STATIC_JS / script).read_text(encoding="utf-8")
    assert "不一定是整部漫画" in source, script


def test_both_status_filters_use_the_same_wording(client):
    for page in ("/library", "/wishlist"):
        html = client.get(page).get_data(as_text=True)
        assert '<option value="readable">已下载内容 · 可离线阅读</option>' in html, page
    assert "'readable': '已下载内容 · 可离线阅读'" in (STATIC_JS / "wishlist.js").read_text(encoding="utf-8")


def test_partial_badge_uses_theme_tokens_only():
    css = (ROOT / "static" / "css" / "style.css").read_text(encoding="utf-8")
    body = css[css.index(".badge.status-badge-partial {"):]
    body = body[:body.index("}")]
    assert "var(--info-bg)" in body and "var(--info)" in body and "#" not in body


# ─── the detail page badge, run in Node with the real utils.js and detail.js ───

DETAIL_HARNESS = r"""
const fs = require('fs');
const [utilsPath, detailPath, scenarioJson] = process.argv.slice(1);
const scenario = JSON.parse(scenarioJson);
function textNode(v) { return { nodeType: 3, data: String(v), parentNode: null }; }
function el(tag) {
  const cls = new Set(), attrs = {};
  const e = {
    nodeType: 1, tagName: tag.toUpperCase(), children: [], parentNode: null, hidden: false, title: '',
    get className() { return Array.from(cls).join(' '); },
    set className(v) { cls.clear(); String(v).split(/\s+/).forEach(c => c && cls.add(c)); },
    classList: { add: (...n) => n.forEach(c => cls.add(c)), remove: (...n) => n.forEach(c => cls.delete(c)),
                 contains: c => cls.has(c) },
    setAttribute: (k, v) => { attrs[k] = String(v); }, getAttribute: k => (k in attrs ? attrs[k] : null),
    removeAttribute: k => { delete attrs[k]; },
    appendChild(c) { c.parentNode = e; e.children.push(c); return c; },
    remove() { if (e.parentNode) { e.parentNode.children.splice(e.parentNode.children.indexOf(e), 1); e.parentNode = null; } },
    get textContent() { return e.children.map(c => c.nodeType === 3 ? c.data : c.textContent).join(''); },
    set textContent(v) { e.children = []; if (v !== '') e.appendChild(textNode(v)); },
    all() { return e.children.filter(c => c.nodeType === 1).flatMap(c => [c, ...c.all()]); },
    querySelectorAll(sel) { const c = sel.slice(1); return e.all().filter(n => n.classList.contains(c)); },
    querySelector(sel) { return e.querySelectorAll(sel)[0] || null; },
  };
  return e;
}
global.window = global;
window.addEventListener = () => {};
global.location = { origin: 'http://x' };
const ids = {};
global.document = { createElement: el, createTextNode: textNode, getElementById: id => ids[id] || null };
eval(fs.readFileSync(utilsPath, 'utf8'));
const calls = [];
window.apiFetch = url => { calls.push(url); return Promise.resolve(url === '/api/preview/available' ? scenario.available : scenario.chapters); };
const src = fs.readFileSync(detailPath, 'utf8');
eval(src.slice(src.indexOf('function setOfflineStatus'), src.indexOf('function bindEvents')));
const marker = el('div'); marker.hidden = true;
const offline = el('span'); offline.className = 'offline-badge'; offline.hidden = true; offline.textContent = '已下载内容 · 可离线阅读';
marker.appendChild(offline); ids['album-offline-status'] = marker;
const btn = el('a'); btn.hidden = true; ids['local-read-btn'] = btn;
global.albumId = '3001';
(async () => {
  refreshOfflineStatus();
  for (let i = 0; i < 5; i++) await new Promise(r => setImmediate(r));
  const partial = marker.querySelector('.status-badge-partial');
  process.stdout.write(JSON.stringify({
    calls, shown: !marker.hidden,
    badges: marker.all().filter(b => (b.classList.contains('badge') || b.classList.contains('offline-badge')) && !b.hidden)
      .map(b => b.textContent.trim()),
    title: partial ? partial.title : null,
  }));
})().catch(err => { console.error(err && err.stack || err); process.exit(1); });
"""


def _detail(available, chapters):
    if NODE is None:
        pytest.skip("needs Node.js (CI installs it)")
    scenario = {"available": available, "chapters": chapters}
    result = subprocess.run([NODE, "-e", DETAIL_HARNESS, str(STATIC_JS / "utils.js"), str(STATIC_JS / "detail.js"),
                             json.dumps(scenario)], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


READABLE = {"status": "ok", "readable": ["3001"], "archives": {}, "unavailable": {}}


def test_detail_shows_the_partial_count_only_when_proven():
    out = _detail(READABLE, {"status": "ok", "partial": {"downloaded": 2, "total": 5}, "reason": "partial"})
    assert out["badges"] == ["已下载内容 · 可离线阅读", "部分章节已下载 · 2/5 话"]
    assert "本地完整地有 2 话" in out["title"] and "共 5 话" in out["title"]
    assert out["calls"] == ["/api/preview/available", "/api/local-chapters/3001"]


@pytest.mark.parametrize("partial", [None, {"downloaded": 5, "total": 5}, {"downloaded": 0, "total": 5}])
def test_detail_shows_no_count_otherwise(partial):
    out = _detail(READABLE, {"status": "ok", "partial": partial, "reason": "x"})
    assert out["badges"] == ["已下载内容 · 可离线阅读"]


def test_detail_does_not_ask_for_a_count_when_nothing_is_readable():
    out = _detail({"status": "ok", "readable": [], "archives": {}, "unavailable": {"3001": "deleted"}},
                  {"status": "ok", "partial": {"downloaded": 1, "total": 3}, "reason": "partial"})
    assert out["badges"] == ["文件已删除"] and out["calls"] == ["/api/preview/available"]


# ─── review round: other folders, busy archives, extra pages, one chapter, wording in the render code ───


def test_an_earlier_download_in_another_folder_stops_the_count(client, download, downloads):
    # organize_mode=by_author moves the first download to <author>/<title>_<id>; the next one cannot move there
    assert download("ja", ["71", "72"], organize="by_author") == "completed"
    assert (downloads / "someone" / "Name_3001").is_dir()
    assert download("jb", ["73"], organize="by_author") == "completed"
    assert (downloads / "Name_3001").is_dir()
    _chapter_list()
    assert _claim(client) == (None, "other_folders")     # never “1/3”: chapters 71 and 72 are on disk too


def test_a_renamed_album_stops_the_count(client, download, monkeypatch):
    assert download("ja", ["71", "72"]) == "completed"
    monkeypatch.setattr(_Album, "name", "Name v2")        # upstream renamed it: the next download gets a new folder
    assert download("jb", ["73"]) == "completed"
    _chapter_list()
    assert _claim(client) == (None, "other_folders")


def test_a_busy_preferred_archive_stops_the_count(client, download, monkeypatch):
    from core import archive_pages
    assert download("ja", ["71", "72"], pack=True, pack_format="zip") == "completed"  # Name_3001.zip: 71, 72
    assert download("jb", ["73"], pack=True, pack_format="cbz") == "completed"        # Name_3001.cbz: all three
    _chapter_list()
    assert _claim(client) == (None, "all_chapters")
    real = archive_pages.read_index
    monkeypatch.setattr(archive_pages, "read_index", lambda path: (
        archive_pages.ArchiveIndex(Path(path), "corrupt", transient=True) if str(path).endswith(".cbz") else real(path)))
    assert _claim(client) == (None, "unattributed")      # never “2/3” from the older zip


def test_a_chapter_with_extra_pages_is_not_counted(client, download, downloads):
    assert download("ja", ["71"]) == "completed"
    chapter = next(p for p in (downloads / "Name_3001").iterdir() if p.is_dir())
    (chapter / "00004.webp").write_bytes(b"page")        # upstream says chapter 71 has 3 pages
    _chapter_list()
    assert _claim(client) == (None, "incomplete_chapter")


def test_single_chapter_albums_get_no_count_and_no_scan(client, download, monkeypatch):
    from core import chapter_inventory
    assert download("ja", ["71"]) == "completed"
    _chapter_list([("71", "第1话", 3)])
    monkeypatch.setattr(chapter_inventory, "_local_chapters", lambda folder: pytest.fail("must not scan"))
    assert _claim(client) == (None, "single_chapter")


NEXT_OUTER = chr(10) + "  function "     # the next function in a 2-space-indented script
NEXT_INNER = chr(10) + "    function "   # the next function in a 4-space-indented script


def _function(source, name):
    """The source of one JS function: from its declaration to the next function at the same or outer level."""
    start = source.index(f"function {name}(")
    ends = [e for e in (source.find(NEXT_OUTER, start + 1), source.find(NEXT_INNER, start + 1)) if e > 0]
    return source[start:min(ends) if ends else len(source)]


def test_the_render_code_itself_uses_the_new_wording():
    js = {name: (STATIC_JS / name).read_text(encoding="utf-8") for name in
          ("detail.js", "library.js", "search.js", "downloads.js", "wishlist.js", "utils.js")}
    tip = "本地有已下载的内容，可以离线阅读；不一定是整部漫画"
    assert "</i> 已下载内容 · 可离线阅读</span>" in _function(js["detail.js"], "renderAlbum")
    assert f'title="{tip}"' in _function(js["detail.js"], "renderAlbum")
    badge = _function(js["library.js"], "offlineBadge")
    assert "el('span', 'text-nowrap', '已下载内容 ·')" in badge and f"b.title = '{tip}'" in badge
    cover = _function(js["search.js"], "renderResults")
    assert f'title="{tip}" hidden><i class="bi bi-check-circle-fill" aria-hidden="true"></i>已下载内容 · 可离线阅读</span>' in cover
    card = _function(js["downloads.js"], "renderCompletedCard")
    assert f'<span class="offline-badge" title="{tip}"><i class="bi bi-check-circle-fill" aria-hidden="true"></i>已下载内容 · 可离线阅读</span>' in card
    status = _function(js["wishlist.js"], "statusBadges")
    assert f"offline.title = '已下载内容 · 可离线阅读：{tip}'" in status
    local = re.search(r"local:\s*\{[^}]*title:\s*'([^']*)'", js["utils.js"])
    assert local and local.group(1) == "已下载内容：打开本地文件阅读，无需联网"


def test_readmes_use_the_new_wording():
    for name in ("README.md", "README.zh-CN.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "已下载内容 · 可离线阅读" in text and "已下载 · 可离线阅读" not in text, name
