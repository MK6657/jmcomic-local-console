"""The floating quick-nav in the bottom-right corner (templates/base.html #quick-nav, static/js/quick-nav.js).

The 返回 (previous page) logic runs in Node with a stand-in window/document (skipped when Node is missing; CI installs it);
the wiring is checked through rendered pages and the stylesheet.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
NODE = shutil.which("node")

DESTINATIONS = [  # same six places, order and icons as the top navigation
    ("/", "bi-house-door", "首页"), ("/search", "bi-search", "搜索"), ("/downloads", "bi-list-ul", "下载管理"),
    ("/wishlist", "bi-bookmark-heart", "收藏"), ("/library", "bi-collection", "资源库"), ("/settings", "bi-gear", "设置"),
]
REMEMBERED = {"/search", "/wishlist", "/library"}  # carry data-nav-memory like the top links


def _quick_nav(html):
    match = re.search(r'<div class="quick-nav" id="quick-nav">(.*?)\n</div>\n', html, re.S)
    return match.group(1) if match else None


@pytest.mark.parametrize("url", ["/", "/search", "/downloads", "/wishlist", "/library", "/settings", "/album/123"])
def test_ordinary_pages_have_the_quick_nav(client, url):
    block = _quick_nav(client.get(url).get_data(as_text=True))
    assert block is not None, url
    links = re.findall(r'<a class="quick-nav-item[^"]*" href="([^"]+)"\s+data-quick-dest="([^"]+)"([^>]*)>\s*'
                       r'<i class="bi ([^"]+)" aria-hidden="true"></i><span>([^<]+)</span>', block)
    assert [(href, icon, label) for href, _, _, icon, label in links] == DESTINATIONS
    for href, dest, rest, _, _ in links:
        assert href == dest
        assert ('data-nav-memory="%s"' % href in rest) == (href in REMEMBERED), href
        assert ('aria-current="page"' in rest) == (href == url), href  # the page you are on is marked
    for action, label in (("back", "返回"), ("top", "到顶"), ("bottom", "到底")):
        assert re.search(r'<button type="button" class="quick-nav-item" data-quick-action="%s"[^>]*>\s*'
                         r'<i class="bi [^"]+" aria-hidden="true"></i><span>%s</span>' % (action, label), block), action
    # nothing to go back to until a quick-nav jump: announced as unavailable, still focusable
    assert re.search(r'data-quick-action="back" aria-disabled="true"', block)
    assert re.search(r'<button type="button" class="quick-nav-toggle" aria-expanded="false" '
                     r'aria-controls="quick-nav-panel"\s+title="快捷导航" aria-label="快捷导航">', block)
    assert 'id="quick-nav-panel"' in block


@pytest.mark.parametrize("url", ["/online/123", "/preview/123"])
def test_reader_pages_keep_their_own_bottom_tools_only(client, url):
    html = client.get(url).get_data(as_text=True)
    assert 'id="quick-nav"' not in html and "quick-nav-item" not in html


def test_local_reader_has_no_quick_nav(client, monkeypatch):
    from core import local_availability
    monkeypatch.setattr(local_availability, "is_readable", lambda album_id: True)
    assert 'id="quick-nav"' not in client.get("/read/123").get_data(as_text=True)


@pytest.mark.parametrize("url, script", [
    ("/", "home.js"), ("/search", "search.js"), ("/library", "library.js"), ("/wishlist", "wishlist.js"),
    ("/downloads", "downloads.js"), ("/album/123", "detail.js"), ("/settings", "settings.js"),
])
def test_quick_nav_script_loads_after_the_navigation_memory(client, url, script):
    html = client.get(url).get_data(as_text=True)
    tag = '<script src="/static/js/%s?v='
    assert html.index(tag % "nav-memory.js") < html.index(tag % "quick-nav.js") < html.index(tag % script)


def test_quick_nav_styles_use_only_the_theme_tokens():
    css = (STATIC / "css" / "style.css").read_text(encoding="utf-8")
    start = css.index("右下角快捷导航（templates/base.html #quick-nav")
    block = css[start:]
    rules = re.sub(r"/\*.*?\*/", "", block, flags=re.S)
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(", rules)  # colours only through var(--…)
    assert "z-index: 1035" in rules  # above the page, below Bootstrap modals (1050+) and toasts (1080)
    assert re.search(r"body:has\(\.quick-nav\) #toast-container \{ bottom: calc\(16px \+ 44px\) !important; \}", rules)
    # while open, toasts move above the panel but never off the screen (vh, then dvh where supported)
    lifted = re.search(r"body:has\(\.quick-nav\.is-open\) #toast-container \{(.*?)\}", rules, re.S).group(1)
    for unit in ("100vh", "100dvh"):
        assert ("bottom: max(calc(16px + 44px), min(calc(16px + 44px + 8px + var(--quick-nav-panel-h, 195px)), "
                "calc(%s - 96px))) !important;" % unit) in lifted
    assert re.search(r"@media print \{\s*\.quick-nav \{ display: none !important; \}", rules)
    durations = re.findall(r"(\d+)ms", rules)
    assert durations and all(150 <= int(ms) <= 200 for ms in durations if ms != "0")


HARNESS = r"""
const src = require('fs').readFileSync(process.argv[1], 'utf8');
const HOUR = 3600 * 1000;
let now = 1_000_000_000_000;
const FakeDate = { now: () => now };
const ORIGIN = 'http://127.0.0.1:5000';
const PAGES = 'jm-quick-nav-pages-v1';

// opts: path, search, title, heading, place ({y, listTop} from nav-memory), store (Map shared between "pages"),
// nav ({key, index, entries: [[key, url]]} — the Navigation API), referrer, historyLength
function load(opts) {
  const store = opts.store || new Map();
  const calls = [];
  const win = {
    sessionStorage: {
      getItem: k => store.has(k) ? store.get(k) : null,
      setItem: (k, v) => store.set(k, String(v)),
      removeItem: k => store.delete(k),
    },
    location: {
      pathname: opts.path, search: opts.search || '', origin: ORIGIN,
      href: ORIGIN + opts.path + (opts.search || ''), assign: url => calls.push(['assign', url]),
    },
    history: { length: opts.historyLength || 1, back: () => calls.push(['back']) },
    on: {},
    addEventListener(type, fn) { (this.on[type] = this.on[type] || []).push(fn); },
    scrollY: 0,
    navMemory: {
      MAX_AGE: 12 * HOUR,
      place: () => opts.place || { y: 0, listTop: null },
      expectReturn: (url, y, listTop) => calls.push(['expect', url, y, listTop]),
      noteBackClick: () => calls.push(['note']),
    },
  };
  if (opts.nav) {
    win.navigation = {
      currentEntry: { key: opts.nav.key, index: opts.nav.index },
      entries: () => (opts.nav.entries || []).map(([key, url]) => ({ key, url: ORIGIN + url })),
    };
  }
  const doc = {
    title: opts.title || '', referrer: opts.referrer || '',
    getElementById: () => null,  // no #quick-nav: only the logic runs
    // the page heading with the album title: detail, reader (#reader-title) and preview (#album-title)
    querySelector: sel => (['#album-content .card-title', '#reader-title', '#album-title'].includes(sel) && opts.heading
      ? { textContent: opts.heading } : null),
  };
  new Function('window', 'document', 'Date', src)(win, doc, FakeDate);
  return {
    store, calls, qn: win.quickNav, win,
    leave: () => (win.on.pagehide || []).forEach(fn => fn({ persisted: false })),
  };
}
const pagesOf = store => JSON.parse(store.get(PAGES) || '{}');
const saved = (url, title, y, listTop, age) => ({ url, title, y, listTop: listTop === undefined ? null : listTop, savedAt: now - (age || 0) });
const withPages = pages => new Map([[PAGES, JSON.stringify(pages)]]);
const out = {};

// leaving a page records its address, name and the place nav-memory says was being looked at, by history entry
let shared = new Map();
let p = load({ path: '/library', search: '?q=a&page=2', title: '资源库 - JMComic 下载控制台', place: { y: 1234.6, listTop: 300 },
               store: shared, nav: { key: 'k1', index: 3 } });
out.beforeLeaving = pagesOf(shared);
p.leave();
out.recorded = pagesOf(shared).k1;
out.pagehideListeners = (p.win.on.pagehide || []).length;
out.titles = [
  ['/settings', '', 'JMComic 下载控制台 - 设置'], ['/search', '?keyword=abc&page=2', 'JMComic 下载控制台 - 搜索'],
  ['/album/123', '', '123 - JMComic 下载控制台', '一部很长的漫画标题'], ['/album/7', '', '7 - JMComic 下载控制台'],
  ['/wishlist', '', 'x'.repeat(60) + ' - JMComic 下载控制台'], ['/read/5', '', 'JMComic 下载控制台 - 连续阅读', '连续阅读'],
  // reader.js / preview.js retitle the page "<album> - JMComic" / "<album> - JMComic 图片预览"
  ['/read/5', '', 'Sample - JMComic', 'Sample'], ['/online/5', '', 'Sample - JMComic', 'Sample'],
  ['/preview/5', '', 'Sample - JMComic 图片预览', 'Sample'], ['/preview/5', '', 'JMComic 下载控制台 - 图片预览', '加载中...'],
  ['/downloads', '', 'Sample - JMComic'],
].map(([path, search, title, heading]) => {
  const store = new Map(); load({ path, search, title, heading, store, nav: { key: 'k', index: 0 } }).leave();
  return pagesOf(store).k.title;
});
out.noKeyNoRecord = (() => { const s = new Map(); load({ path: '/library', store: s }).leave(); return s.has(PAGES); })();

// 返回 = the previous page of this tab: its name, then the browser's Back to it, landing where it was left
p = load({ path: '/downloads', store: withPages({ kA: saved('/library?q=1', '资源库', 900, 250) }),
           nav: { key: 'kNow', index: 1, entries: [['kA', '/library?q=1'], ['kNow', '/downloads']] } });
out.previous = p.qn.previousEntry();
out.back = [p.qn.goBack(), p.calls];
// reached by any link (top bar, a result, 首页's 查看全部): the entry before this one, whatever it was
p = load({ path: '/album/900001', store: withPages({ kS: saved('/search?keyword=sample', '搜索“sample”', 1800, 226),
                                                      kD: saved('/downloads', '下载管理', 0) }),
           nav: { key: 'kNow', index: 2, entries: [['kD', '/downloads'], ['kS', '/search?keyword=sample'], ['kNow', '/album/900001']] } });
out.mixed = [p.qn.previousEntry().title, p.qn.goBack(), p.calls];
// the entry's address changed after it was recorded (filters written into the address bar): its address wins
p = load({ path: '/downloads', store: withPages({ kA: saved('/library?q=1', '资源库', 900) }),
           nav: { key: 'kNow', index: 1, entries: [['kA', '/library?q=2'], ['kNow', '/downloads']] } });
out.addressChanged = [p.qn.goBack(), p.calls];
// nothing recorded for the previous entry (left before the page script ran, expired…): plain Back, generic name
p = load({ path: '/downloads', nav: { key: 'kNow', index: 1, entries: [['kX', '/settings'], ['kNow', '/downloads']] } });
out.unrecorded = [p.qn.previousEntry(), p.qn.goBack(), p.calls];
// the first page of this app in the tab (typed in, a new tab, came from another site): nothing to go back to
p = load({ path: '/downloads', store: withPages({ kA: saved('/library', '资源库', 5) }),
           nav: { key: 'kNow', index: 0, entries: [['kNow', '/downloads']] } });
out.first = [p.qn.previousEntry(), p.qn.goBack(), p.calls];
// a later entry exists (went back once already): 返回 names and targets the entry BEFORE this one, never a later one
const forward = current => {
  const store = withPages({ kA: saved('/library?q=1', '资源库', 700, 200, 3000), kB: saved('/downloads', '下载管理', 40, null, 2000),
                            kC: saved('/settings', '设置', 90, null, 1000) });
  const entries = [['kA', '/library?q=1'], ['kB', '/downloads'], ['kC', '/settings']];
  const [path, search] = [['/library', '?q=1'], ['/downloads', ''], ['/settings', '']][current];
  const r = load({ path, search, store, nav: { key: entries[current][0], index: current, entries } });
  return [r.qn.previousEntry(), r.qn.goBack(), r.calls];
};
out.forwardMiddle = forward(1);
out.forwardFirst = forward(0);
// a same-origin address that is not a page of this app (typed /api/… JSON, a /static file): not a page to return to
out.notAppPages = ['/api/jobs', '/static/js/app.js', '/favicon.ico', '/apiary'].map(prevUrl => {
  const r = load({ path: '/settings', nav: { key: 'kNow', index: 1, entries: [['kP', prevUrl], ['kNow', '/settings']] } });
  const previous = r.qn.previousEntry();
  return previous && previous.url;
});
// no Navigation API: nothing to go by (history.length counts later entries too, the referrer survives Back)
const noApi = (referrer, historyLength) => {
  const r = load({ path: '/downloads', referrer, historyLength });
  return [r.qn.previousEntry(), r.qn.goBack(), r.calls];
};
out.noApiSameSite = noApi(ORIGIN + '/library?q=1', 3);
out.noApiOtherSite = noApi('https://example.com/', 3);
out.noApiNoReferrer = noApi('', 3);
out.noApiFirstEntry = noApi(ORIGIN + '/library', 1);

// bounded (the latest MAX_PAGES), 12 hours, same-site addresses and well-formed entries only
shared = new Map();
for (let i = 0; i < 55; i++) {
  now += 1000;
  load({ path: '/library', search: '?page=' + i, title: '资源库 - JMComic 下载控制台', store: shared, nav: { key: 'k' + i, index: i } }).leave();
}
const kept = pagesOf(shared);
out.bounded = [Object.keys(kept).length, 'k4' in kept, 'k5' in kept, 'k54' in kept, p.qn.MAX_PAGES];
shared = withPages({
  old: saved('/old', 't', 0, null, 13 * HOUR), future: saved('/future', 't', 0, null, -HOUR),
  evil: saved('//evil.example/x', 't', 0), abs: saved('http://evil.example/', 't', 0), space: saved('/a b', 't', 0),
  slash: saved('/a\\b', 't', 0), notitle: { url: '/x', y: 0, savedAt: now }, noy: { url: '/x', title: 't', savedAt: now },
  nul: null, ok: saved('/ok', 't', 5, null, HOUR),
});
out.filtered = Object.keys(load({ path: '/downloads', store: shared }).qn.readPages());
out.broken = [load({ path: '/downloads', store: new Map([[PAGES, '{oops']]) }).qn.readPages(),
              load({ path: '/downloads', store: new Map([[PAGES, '[1,2]']]) }).qn.readPages()];
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def harness():
    if NODE is None:
        pytest.skip("needs Node.js (CI installs it)")
    result = subprocess.run([NODE, "-e", HARNESS, str(STATIC / "js" / "quick-nav.js")],
                            capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_leaving_a_page_records_its_name_and_place(harness):
    assert harness["beforeLeaving"] == {}
    page = harness["recorded"]
    assert {k: page[k] for k in ("url", "title", "y", "listTop")} == {
        "url": "/library?q=a&page=2", "title": "资源库", "y": 1235, "listTop": 300}
    assert harness["pagehideListeners"] == 1
    assert harness["noKeyNoRecord"] is False  # without the Navigation API there is no entry to file it under


def test_return_names_the_previous_page(harness):
    long_title = "x" * 39 + "…"
    assert harness["titles"] == ["设置", "搜索“abc”", "详情“一部很长的漫画标题”", "详情 7", long_title, "连续阅读",
                                 "阅读“Sample”", "在线阅读“Sample”", "预览“Sample”", "图片预览", "Sample"]


def test_return_goes_to_the_previous_page_where_it_was_left(harness):
    assert harness["previous"] == {"url": "/library?q=1", "title": "资源库", "y": 900, "listTop": 250}
    assert harness["back"] == [True, [["expect", "/library?q=1", 900, 250], ["note"], ["back"]]]


def test_return_after_an_ordinary_link_goes_to_that_page_not_an_older_one(harness):
    # 下载管理 → (quick) 搜索 → a result's link → detail: 返回 goes to the search results, not 下载管理
    title, ok, calls = harness["mixed"]
    assert title == "搜索“sample”"
    assert ok is True and calls == [["expect", "/search?keyword=sample", 1800, 226], ["note"], ["back"]]


def test_the_previous_entry_address_wins_over_the_recorded_one(harness):
    assert harness["addressChanged"] == [True, [["expect", "/library?q=2", 900, None], ["note"], ["back"]]]


def test_an_unrecorded_previous_page_is_still_reachable(harness):
    previous, ok, calls = harness["unrecorded"]
    assert previous == {"url": "/settings", "title": None, "y": None, "listTop": None}
    assert ok is True and calls == [["note"], ["back"]]


def test_nothing_to_return_to_on_the_first_page_of_the_tab(harness):
    assert harness["first"] == [None, False, []]


def test_without_the_navigation_api_return_is_unavailable(harness):
    # a same-site referrer and history.length > 1 do not prove an earlier page of this app is right behind
    for case in ("noApiSameSite", "noApiOtherSite", "noApiNoReferrer", "noApiFirstEntry"):
        assert harness[case] == [None, False, []], case


def test_return_never_names_or_targets_a_later_entry(harness):
    previous, ok, calls = harness["forwardMiddle"]
    assert previous == {"url": "/library?q=1", "title": "资源库", "y": 700, "listTop": 200}
    assert ok is True and calls == [["expect", "/library?q=1", 700, 200], ["note"], ["back"]]
    assert harness["forwardFirst"] == [None, False, []]  # the first entry, with later ones: nothing behind


def test_addresses_that_are_not_app_pages_are_not_returned_to(harness):
    assert harness["notAppPages"] == [None, None, None, "/apiary"]


def test_recorded_pages_are_bounded_fresh_and_same_site(harness):
    assert harness["bounded"] == [50, False, True, True, 50]  # the 50 most recent survive
    assert harness["filtered"] == ["ok"]
    assert harness["broken"] == [{}, {}]
