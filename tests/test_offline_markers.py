"""Offline-readable markers (search / detail / downloads) and the inline-handler cleanup.

POST /api/preview/available answers "which of these albums can be read offline" with the shared
rule in core.local_availability, so every page shows the same 已下载 / 阅读 state.
"""
import re
import shutil
from pathlib import Path
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC_JS = ROOT / "static" / "js"
TEMPLATES = ROOT / "templates"
URL = "/api/preview/available"

# on<event>= inside HTML that a script builds or a template ships (onclick="...", onkeydown='...', onclick=\'...)
EVENTS = (r"click|dblclick|key\w*|mouse\w*|pointer\w*|touch\w*|change|input|submit|focus\w*|blur"
          r"|load|error|scroll|wheel|drag\w*|drop|contextmenu|toggle|animation\w*|transition\w*")
INLINE_HANDLER = re.compile(r"""\bon(?:%s)\s*=\s*\\?["']""" % EVENTS, re.IGNORECASE)


@pytest.fixture
def downloads(client, tmp_path, monkeypatch):
    from core import path_guard
    root = tmp_path / "downloads"
    root.mkdir()
    monkeypatch.setattr(path_guard, "DOWNLOAD_ROOT", root)
    return root


def _album(folder):
    """A downloaded album folder with one page: an empty folder is not readable offline."""
    folder.mkdir(parents=True)
    (folder / "001.jpg").write_bytes(b"page")
    return folder


def _job(job_id, album_id, status, output_path):
    from core import database as db
    db.insert_job(job_id, album_id, "t", [])
    db.update_job(job_id, status=status, output_path=str(output_path) if output_path else None)


def _post(client, body):
    return client.post(URL, json=body)


# ── endpoint: results ─────────────────────────────────────────────

def test_available_lists_only_readable_albums_in_request_order(client, downloads):
    for name in ("A", "B"):
        _album(downloads / name)
    _job("j1", "111", "completed", downloads / "A")
    _job("j2", "222", "completed", downloads / "B")
    _job("j3", "333", "running", downloads / "A")          # still downloading
    _job("j4", "444", "completed", downloads / "gone")     # folder deleted
    _job("j5", "555", "failed", downloads / "A")
    response = _post(client, {"album_ids": ["222", "333", "444", "555", "666", "111", "222"]})
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok", "readable": ["222", "111"]}


def test_available_matches_the_shared_rule(client, downloads):
    import time
    from core.local_availability import readable_album_ids
    _album(downloads / "old")
    _job("old", "777", "completed", downloads / "old")
    time.sleep(0.01)
    _job("new", "777", "completed", downloads / "missing")  # latest completed job lost its folder
    _album(downloads / "C")
    _job("c", "888", "completed", downloads / "C")
    ids = ["777", "888", "999"]
    readable = _post(client, {"album_ids": ids}).get_json()["readable"]
    assert set(readable) == readable_album_ids(ids) == {"888"}


def test_available_accepts_integer_ids_and_empty_list(client, downloads):
    _album(downloads / "D")
    _job("d", "123", "completed", downloads / "D")
    assert _post(client, {"album_ids": [123, 456]}).get_json() == {"status": "ok", "readable": ["123"]}
    assert _post(client, {"album_ids": []}).get_json() == {"status": "ok", "readable": []}


def test_available_accepts_the_maximum_batch(client, downloads):
    from core.local_availability import MAX_IDS
    _album(downloads / "E")
    _job("e", "100000", "completed", downloads / "E")
    ids = [str(100000 + n) for n in range(MAX_IDS)]
    response = _post(client, {"album_ids": ids})
    assert response.status_code == 200
    assert response.get_json()["readable"] == ["100000"]


# ── endpoint: validation ──────────────────────────────────────────

def test_available_rejects_too_many_ids(client):
    from core.local_availability import MAX_IDS
    response = _post(client, {"album_ids": [str(n) for n in range(MAX_IDS + 1)]})
    assert response.status_code == 400
    assert response.get_json()["status"] == "error"


@pytest.mark.parametrize("bad", ["abc", "12a", "", " 12", "1 OR 1=1", "１２３", "1" * 21, True, -5, 1.5, None, [1], {"a": 1}])
def test_available_rejects_non_numeric_ids(client, bad):
    response = _post(client, {"album_ids": ["123", bad]})
    assert response.status_code == 400
    assert response.get_json()["status"] == "error"


@pytest.mark.parametrize("body", [{}, {"album_ids": "123"}, {"album_ids": None}, {"album_ids": {"0": "123"}}, {"ids": ["1"]}])
def test_available_requires_an_album_ids_list(client, body):
    response = _post(client, body)
    assert response.status_code == 400
    assert response.get_json()["status"] == "error"


def test_available_requires_a_json_object(client):
    assert client.post(URL, json=["123"]).status_code == 400
    assert client.post(URL, json="123").status_code == 400
    assert client.post(URL).status_code == 400  # no body
    assert client.post(URL, data="album_ids=1", content_type="application/x-www-form-urlencoded").status_code in (400, 415)
    assert client.get(URL).status_code != 200   # read-only question, but only answered via POST


def test_available_creates_no_jobs(client):
    from core import database as db
    before = len(db.get_all_jobs())
    _post(client, {"album_ids": ["123", "456"]})
    assert len(db.get_all_jobs()) == before


# ── pages: no inline handlers with data, markers wired up ────────

@pytest.mark.parametrize("script", ["detail.js", "search.js", "downloads.js"])
def test_scripts_build_no_inline_event_handlers(script):
    source = (STATIC_JS / script).read_text(encoding="utf-8")
    assert not INLINE_HANDLER.search(source), f"{script} still builds an inline on*= handler"
    # the old entry points that inline handlers called must be gone too
    for name in ("removeAlbumTag", "addCustomTag", "syncAlbumTags", "cancelJob", "retryJob", "deleteJob"):
        assert "window." + name not in source


@pytest.mark.parametrize("template", ["detail.html", "search.html", "downloads.html", "index.html"])
def test_templates_have_no_inline_event_handlers(template):
    assert not INLINE_HANDLER.search((TEMPLATES / template).read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", ["/album/123", "/search", "/downloads", "/"])
def test_rendered_pages_have_no_inline_event_handlers(client, downloads, path):
    _album(downloads / "H")
    _job("job_0123456789ab", "321", "completed", downloads / "H")  # home page lists it under 最近下载
    html = client.get(path).get_data(as_text=True)
    assert not re.search(r"\son[a-z]+\s*=", html, re.IGNORECASE)


def test_home_open_folder_is_a_delegated_button(client, downloads):
    # tojson inside onclick="..." broke the attribute at the JSON string's own quotes: the button did nothing
    _album(downloads / "H")
    _job("job_0123456789ab", "321", "completed", downloads / "H")
    html = client.get("/").get_data(as_text=True)
    button = re.search(r'<button[^>]*data-action="open-folder"[^>]*>', html)
    assert button, "open-folder must be a real <button data-action>"
    tag = button.group(0)
    assert 'data-job-id="job_0123456789ab"' in tag and 'type="button"' in tag and 'aria-label="打开文件夹"' in tag
    assert "bi-folder2-open" in html and "bi-folder-open" not in html  # bi-folder-open is not a Bootstrap icon
    home = (STATIC_JS / "home.js").read_text(encoding="utf-8")
    assert 'data-action="open-folder"' in home and "window.openFolder(" in home and "dataset.jobId" in home
    assert "hero-download-btn" in home  # the 车号下载 button's former inline handler lives here too


def test_escape_html_attr_is_not_documented_for_inline_handlers():
    source = (STATIC_JS / "utils.js").read_text(encoding="utf-8")
    head = source[:source.index("window.escapeHtmlAttr")]
    doc = head[head.rindex("/**"):]
    # the HTML parser decodes the entities back before an inline handler runs: never a safe context
    assert "onclick" not in doc and "内联" in doc


def test_detail_tag_editor_uses_data_attributes_and_real_buttons():
    source = (STATIC_JS / "detail.js").read_text(encoding="utf-8")
    # tag names travel only through dataset / textContent, never through HTML or JS strings
    assert "dataset.tag = name" in source
    assert "createEl('button', 'tag-edit-btn')" in source and "remove.type = 'button'" in source
    assert "setAttribute('aria-label', '删除标签 ' + name)" in source
    assert "escapeHtmlAttr(tag.tag)" not in source
    # one delegated listener handles remove / add / sync and the Enter key
    for action in ("remove-tag", "add-tag", "sync-tags"):
        assert "'" + action + "'" in source


def test_detail_favourite_star_loads_in_the_clicked_state():
    # a favourite used to load with btn-outline-warning AND btn-warning: the star was drawn in --warning on a
    # --warning background (invisible), while clicking produced the correct button
    source = (STATIC_JS / "detail.js").read_text(encoding="utf-8")
    setter = source[source.index("function setWishlistButton"):source.index("function initWishlist")]
    assert "classList.toggle('btn-warning', starred)" in setter
    assert "classList.toggle('btn-outline-warning', !starred)" in setter
    assert "btn.title = " in setter and "setAttribute('aria-label'" in setter and "setAttribute('aria-pressed'" in setter
    init = source[source.index("function initWishlist"):source.index("//  标签管理功能")]
    # loaded-as-favourite, starred by a click and unstarred by a click all go through the one setter
    assert init.count("setWishlistButton(") == 3 and "setWishlistButton(b, true)" in init
    assert "classList.add(" not in init and "classList.remove(" not in init and "icon.className" not in init
    # the rendered button gets the .wishlist-btn rules, like the star on search results
    render = source[source.index("function renderAlbum"):source.index("function setWishlistButton")]
    assert 'id="wishlist-toggle-btn" class="btn btn-sm wishlist-btn btn-outline-warning' in render
    assert 'aria-label="收藏"' in render
    css = (ROOT / "static" / "css" / "style.css").read_text(encoding="utf-8")
    # the icon follows the button text colour (it was pinned to --warning: invisible on the hovered outline star)
    assert re.search(r"\.wishlist-btn\.btn-outline-warning i,\s*\.wishlist-btn\.btn-warning i\s*\{\s*color:\s*inherit;", css)
    # a hovered favourite keeps --warning instead of Bootstrap's bright yellow under a white star
    hover = re.search(r"\.wishlist-btn\.btn-warning:hover,[^{]*\{([^}]*)\}", css)
    assert hover and "var(--warning)" in hover.group(1) and "var(--text-on-accent)" in hover.group(1)


def test_detail_page_offers_local_reading_and_plain_back(client):
    html = client.get("/album/123").get_data(as_text=True)
    assert html.index("js/reading-nav.js") < html.index("js/detail.js")
    source = (STATIC_JS / "detail.js").read_text(encoding="utf-8")
    assert "'/api/preview/available'" in source
    assert 'id="local-read-btn"' in source and "'/read/' + encodeURIComponent(album.album_id)" in source
    assert "已下载 · 可离线阅读" in source
    assert "返回搜索" not in source and "readingNav.bindBack" in source


def test_search_and_downloads_ask_the_shared_endpoint():
    search = (STATIC_JS / "search.js").read_text(encoding="utf-8")
    assert "'/api/preview/available'" in search and "offline-badge--cover" in search
    assert "refreshReadable();" in search  # re-run after every render, including snapshot/bfcache restores
    downloads = (STATIC_JS / "downloads.js").read_text(encoding="utf-8")
    assert "'/api/preview/available'" in downloads
    assert "window.readLink.html(job.album_id" in downloads and "href=\"/preview/' + albumPath" in downloads


def test_search_cover_badge_uses_the_offline_wording():
    # a bare "已下载" contradicted the library/favourites, where a comic whose files are gone is not downloaded
    search = (STATIC_JS / "search.js").read_text(encoding="utf-8")
    assert "已下载 · 可离线阅读</span>" in search
    assert "> 已下载</span>" not in search


def test_downloads_completed_cards_show_the_shared_marker():
    source = (STATIC_JS / "downloads.js").read_text(encoding="utf-8")
    card = source[source.index("function renderCompletedCard"):source.index("function renderFailedCard")]
    # readable → the same marker as every other page; checked and not readable → 文件已删除
    assert "offline-badge" in card and "已下载 · 可离线阅读" in card
    assert "status-badge-muted" in card and "文件已删除" in card
    # 预览 reads the same local files as 阅读: offered only when the album is readable
    preview = card.index("href=\"/preview/' + albumPath")
    assert re.search(r"readable && job\._path \? '<a[^']*$", card[:preview])
    # the status badges sit with the title, so they don't shift sideways with the number of buttons
    assert card.index("job-card-status") < card.index("job-card-actions")


# ── 阅读: offered for every comic; /read decides local vs online when it is clicked ──

def _location(response):
    parts = urlsplit(response.headers["Location"])
    return parts.path + ("?" + parts.query if parts.query else "")


def test_read_opens_the_local_reader_when_downloaded(client, downloads):
    _job("job_read_ok", "123", "completed", _album(downloads / "Album_123"))
    response = client.get("/read/123")
    assert response.status_code == 200
    assert 'data-source="local"' in response.get_data(as_text=True)


@pytest.mark.parametrize("query, target", [
    ("", "/online/123"), ("?page=5", "/online/123?page=5"),
    ("?page=0", "/online/123"), ("?page=-2", "/online/123"), ("?page=abc", "/online/123"),
])
def test_read_goes_online_when_not_downloaded(client, downloads, query, target):
    from core import database as db
    before = len(db.get_all_jobs())
    response = client.get("/read/123" + query)
    assert response.status_code == 302 and _location(response) == target
    assert len(db.get_all_jobs()) == before  # reading never starts a download
    followed = client.get("/read/123" + query, follow_redirects=True).get_data(as_text=True)
    assert 'data-source="online"' in followed


@pytest.mark.parametrize("status", ["queued", "running", "failed"])
def test_read_goes_online_while_nothing_is_downloaded_yet(client, downloads, status):
    _job("job_read_" + status, "123", status, None)
    assert _location(client.get("/read/123")) == "/online/123"


def test_read_follows_the_files_after_the_page_was_rendered(client, downloads):
    folder = _album(downloads / "Album_123")
    _job("job_read_gone", "123", "completed", folder)
    assert client.get("/read/123").status_code == 200
    shutil.rmtree(folder)  # files deleted: the same button now reads online
    assert _location(client.get("/read/123")) == "/online/123"
    _album(folder)
    _job("job_read_again", "123", "completed", folder)  # downloaded again: back to the local files
    assert client.get("/read/123").status_code == 200


def test_read_button_helper_picks_the_look_not_the_destination():
    source = (STATIC_JS / "utils.js").read_text(encoding="utf-8")
    helper = source[source.index("“阅读”按钮（搜索"):]
    assert "link.href = '/read/' + encodeURIComponent(id);" in helper
    assert "'/online/" not in helper  # the server decides when the button is clicked
    assert "icon: 'bi-book'" in helper and "icon: 'bi-globe2'" in helper and "label: '阅读（在线）'" in helper
    # no visually-hidden text: it is position:absolute and widened the favourites table's scroll container
    assert "'visually-hidden'" not in helper
    assert "/^[0-9]{1,20}$/.test(id)" in helper  # /read rejects anything else
    assert "innerHTML" not in helper and "textContent" in helper


@pytest.mark.parametrize("script, call", [
    # the created button is appended whenever the id is valid, whatever item.readable says
    ("library.js", "        var read = window.readLink.create(albumId, item.readable === true, 'btn-sm');\n"
                   "        if (read) buttons.appendChild(read);\n"),
    ("wishlist.js", "    var read = window.readLink.create(albumId, item.readable === true, 'btn-sm');\n"
                    "    if (read) actions.appendChild(read);\n"),
    ("search.js", "\n      html += window.readLink.html(item.album_id, undefined, 'btn-sm flex-fill reader-link');\n"),
])
def test_every_list_always_offers_read(script, call):
    source = (STATIC_JS / script).read_text(encoding="utf-8").replace("\r\n", "\n")
    assert call in source
    assert "if (item.readable)" not in source  # no longer only for downloaded comics
    assert "'/read/' +" not in source  # the link is built by the shared helper only


def test_search_updates_the_read_button_when_the_check_returns():
    source = (STATIC_JS / "search.js").read_text(encoding="utf-8")
    setter = source[source.index("function setCardReadable"):source.index("function refreshReadable")]
    assert "window.readLink.apply(link, readable)" in setter


def test_downloads_offer_read_on_completed_and_failed_cards():
    source = (STATIC_JS / "downloads.js").read_text(encoding="utf-8")
    completed = source[source.index("function renderCompletedCard"):source.index("function renderFailedCard")]
    failed = source[source.index("function renderFailedCard"):source.index("// ── SSE 事件回调")]
    for card in (completed, failed):
        assert "window.readLink.html(job.album_id, readableState(job.album_id), 'btn-sm')" in card
    # appended unconditionally (only 预览 depends on the files being there)
    assert "\n      + readBtn\n" in completed.replace("\r\n", "\n")
    assert "\n      + window.readLink.html(job.album_id, readableState(job.album_id), 'btn-sm')\n" in failed.replace("\r\n", "\n")
    # failed albums are checked too (an earlier download may still be readable) and re-rendered with the result
    assert "refreshReadable(groups.completed.concat(groups.failed));" in source
    assert "renderSection('failed', lastFailed, renderFailedCard);" in source


# Runs the real helper from utils.js in Node with a tiny DOM stand-in and checks what it renders.
READ_LINK_HARNESS = r"""
const fs = require('fs');
class El {
  constructor(tag) { this.tag = tag; this.attrs = {}; this.children = []; this.className = ''; this._text = null; }
  get classList() {
    const el = this;
    const list = () => el.className.split(/\s+/).filter(Boolean);
    return {
      add: (...c) => { el.className = [...new Set([...list(), ...c])].join(' '); },
      remove: (...c) => { el.className = list().filter(x => !c.includes(x)).join(' '); },
    };
  }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  removeAttribute(k) { delete this.attrs[k]; }
  set href(v) { this.attrs.href = v; }
  set title(v) { this.attrs.title = v; }
  set textContent(v) { this.children = []; if (v) this.children.push({ text: String(v) }); }
  get textContent() { return this.children.map(c => c.text !== undefined ? c.text : c.textContent).join(''); }
  appendChild(c) { this.children.push(c); return c; }
  get outerHTML() {
    const attrs = Object.entries(this.className ? { class: this.className, ...this.attrs } : this.attrs)
      .map(([k, v]) => ` ${k}="${v}"`).join('');
    const inner = this.children.map(c => c.text !== undefined ? c.text : c.outerHTML).join('');
    return `<${this.tag}${attrs}>${inner}</${this.tag}>`;
  }
}
globalThis.window = globalThis;
window.addEventListener = () => {};
globalThis.document = { createElement: t => new El(t), createTextNode: t => ({ text: t }) };
eval(fs.readFileSync(process.argv[1], 'utf8'));
const out = {};
for (const [name, readable] of [['local', true], ['online', false], ['unknown', undefined]]) {
  const a = window.readLink.create('123', readable, 'btn-sm');
  out[name] = { cls: a.className, href: a.attrs.href, state: a.attrs['data-read-state'], label: a.getAttribute('aria-label'),
                text: a.textContent, icon: a.children[0].className, html: window.readLink.html('123', readable, 'btn-sm') };
}
const updated = window.readLink.apply(window.readLink.create('7', undefined, 'btn-sm'), false);
out.applied = { cls: updated.className, label: updated.getAttribute('aria-label'), icon: updated.children[0].className,
                back: window.readLink.apply(updated, true).getAttribute('aria-label') };
out.invalid = [window.readLink.create('12a', true), window.readLink.create(null, true), window.readLink.html('', false)];
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js (CI installs it)")
def test_read_link_helper_renders_each_state():
    import json
    import subprocess
    result = subprocess.run([shutil.which("node"), "-e", READ_LINK_HARNESS, str(STATIC_JS / "utils.js")],
                            capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    for state in ("local", "online", "unknown"):
        assert out[state]["href"] == "/read/123" and out[state]["state"] == state  # the server picks the destination
        assert out[state]["text"] == " 阅读" and "btn-sm" in out[state]["cls"].split()
        assert out[state]["html"].startswith("<a ") and 'href="/read/123"' in out[state]["html"]
    assert out["local"]["cls"].split() == ["btn", "btn-sm", "btn-primary"] and out["local"]["icon"] == "bi bi-book"
    assert out["online"]["cls"].split() == ["btn", "btn-sm", "btn-outline-primary"] and out["online"]["icon"] == "bi bi-globe2"
    assert out["unknown"]["cls"].split() == ["btn", "btn-sm", "btn-outline-primary"] and out["unknown"]["icon"] == "bi bi-book"
    assert out["online"]["label"] == "阅读（在线）" and out["local"]["label"] is None and out["unknown"]["label"] is None
    # updating an existing button swaps the look (no duplicated variants) and drops the online name again
    assert out["applied"]["cls"].split() == ["btn", "btn-sm", "btn-outline-primary"] and out["applied"]["icon"] == "bi bi-globe2"
    assert out["applied"]["label"] == "阅读（在线）" and out["applied"]["back"] is None
    assert out["invalid"] == [None, None, ""]
