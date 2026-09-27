"""The floating quick-nav in the bottom-right corner (templates/base.html #quick-nav, static/js/quick-nav.js).

The step-back logic runs in Node with a stand-in window/document (skipped when Node is missing; CI installs it);
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

// opts: path, search, title, scrollY, listTop, store (Map shared between "pages"), nav ({key, index, entries})
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
    history: {
      state: opts.state === undefined ? null : opts.state,
      back: () => calls.push(['back']),
      replaceState(state) { this.state = state; },
    },
    on: {},
    addEventListener(type, fn) { (this.on[type] = this.on[type] || []).push(fn); },
    scrollY: opts.scrollY || 0,
    navMemory: {
      MAX_AGE: 12 * HOUR,
      listTopNow: () => (typeof opts.listTop === 'number' ? opts.listTop : null),
      expectReturn: (url, y, listTop) => calls.push(['expect', url, y, listTop]),
    },
  };
  if (opts.nav) {
    win.navigation = {
      currentEntry: { key: opts.nav.key, index: opts.nav.index },
      entries: () => opts.nav.entries.map(([key, url]) => ({ key, url: ORIGIN + url })),
      traverseTo: key => { calls.push(['traverse', key]); return { committed: Promise.resolve(), finished: Promise.resolve() }; },
    };
  }
  const doc = {
    title: opts.title || '',
    getElementById: () => null,  // no #quick-nav: only the logic runs
    querySelector: sel => (sel === '#album-content .card-title' && opts.heading ? { textContent: opts.heading } : null),
  };
  new Function('window', 'document', 'Date', src)(win, doc, FakeDate);
  return { store, calls, qn: win.quickNav, win };
}
const stackOf = store => JSON.parse(store.get('jm-quick-nav-back-v1') || '[]');
const out = {};

// a jump remembers the address, the exact position, the list position and a readable page name
let shared = new Map();
let p = load({ path: '/library', search: '?q=a&page=2', title: '资源库 - JMComic 下载控制台', scrollY: 1234.6, listTop: 300,
               store: shared, nav: { key: 'k1', index: 3, entries: [] } });
p.qn.remember();
out.remembered = stackOf(shared)[0];
out.titles = [
  ['/settings', '', 'JMComic 下载控制台 - 设置'], ['/search', '?keyword=abc&page=2', 'JMComic 下载控制台 - 搜索'],
  ['/album/123', '', '123 - JMComic 下载控制台', '一部很长的漫画标题'], ['/album/7', '', '7 - JMComic 下载控制台'],
  ['/wishlist', '', 'x'.repeat(60) + ' - JMComic 下载控制台'],
].map(([path, search, title, heading]) => {
  const store = new Map(); load({ path, search, title, heading, store }).qn.remember(); return stackOf(store)[0].title;
});

// several jumps come back in reverse order, one step per 返回, then nothing is left
shared = new Map();
load({ path: '/search', search: '?keyword=a', scrollY: 500, store: shared }).qn.remember();   // 搜索 → 收藏
load({ path: '/wishlist', search: '?status=missing', scrollY: 700, store: shared }).qn.remember(); // 收藏 → 资源库
p = load({ path: '/library', store: shared });
out.firstBack = [p.qn.goBack(), p.calls];
p = load({ path: '/wishlist', search: '?status=missing', store: shared });
out.secondBack = [p.qn.goBack(), p.calls];
p = load({ path: '/search', search: '?keyword=a', store: shared });
out.nothingLeft = [p.qn.goBack(), p.calls, stackOf(shared)];

// the browser's Back is used when the previous history entry is exactly the page of that step
const backVia = (entries, index, stepKey, stepUrl) => {
  const store = new Map();
  store.set('jm-quick-nav-back-v1', JSON.stringify([{ url: stepUrl, y: 10, listTop: null, title: 't', key: stepKey, savedAt: now }]));
  const r = load({ path: '/downloads', store, nav: { key: 'kNow', index, entries } });
  r.qn.goBack(); return r.calls.map(c => c[0]);
};
out.browserBack = backVia([['kA', '/library?q=1'], ['kNow', '/downloads']], 1, 'kA', '/library?q=1');
out.previousIsOther = backVia([['kX', '/library?q=1'], ['kNow', '/downloads']], 1, 'kA', '/library?q=1');
out.previousChangedAddress = backVia([['kA', '/library?q=2'], ['kNow', '/downloads']], 1, 'kA', '/library?q=1');
out.firstEntry = backVia([['kNow', '/downloads']], 0, 'kA', '/library?q=1');
// a step further back in this tab's history: straight back to that entry (no new history entry)
out.traverseBack = (() => {
  const store = new Map();
  store.set('jm-quick-nav-back-v1', JSON.stringify([{ url: '/library?q=1', y: 10, listTop: null, title: 't', key: 'kA', savedAt: now }]));
  const r = load({ path: '/downloads', store, nav: { key: 'kNow', index: 2, entries: [['kA', '/library?q=1'], ['kB', '/settings'], ['kNow', '/downloads']] } });
  r.qn.goBack(); return r.calls.map(c => c.slice(0, 2));
})();
// jumped back past quick-nav steps with the history menu: steps now at or after this page are no longer "behind"
const skipped = current => {
  const store = new Map();
  store.set('jm-quick-nav-back-v1', JSON.stringify([
    { url: '/library?page=3', y: 0, title: 'L', key: 'kL', savedAt: now }, { url: '/settings', y: 0, title: 'S', key: 'kS', savedAt: now }]));
  const entries = [['kL', '/library?page=3'], ['kS', '/settings'], ['kD', '/downloads']];
  const [path, search] = [['/library', '?page=3'], ['/settings', ''], ['/downloads', '']][current];
  const r = load({ path, search, store, nav: { key: entries[current][0], index: current, entries } });
  const kept = r.qn.prune().map(e => e.url);
  return [kept, r.qn.goBack(), r.calls.map(c => c[0])];
};
out.jumpedBackPastSteps = skipped(0);
out.jumpedBackOneStep = skipped(1);
out.stepsStillBehind = skipped(2);
// without the Navigation API: every history entry of the tab gets an increasing number in history.state
out.seqAssigned = (() => {
  const store = new Map();
  const a = load({ path: '/library', store });
  const b = load({ path: '/settings', store });
  const again = load({ path: '/library', store, state: { jmQuickNavSeq: 1, jmSearch: 'kept' } });  // Back: state comes back
  return [a.win.history.state, b.win.history.state, again.win.history.state, store.get('jm-quick-nav-seq')];
})();
out.seqRemembered = (() => {
  const store = new Map();
  const r = load({ path: '/library', store, state: { jmQuickNavSeq: 7 } }); r.qn.remember();
  return JSON.parse(store.get('jm-quick-nav-back-v1'))[0].seq;
})();
const seqSkipped = currentSeq => {
  const store = new Map();
  store.set('jm-quick-nav-back-v1', JSON.stringify([
    { url: '/library?page=3', y: 0, title: 'L', key: null, seq: 1, savedAt: now },
    { url: '/settings', y: 0, title: 'S', key: null, seq: 2, savedAt: now }]));
  const [path, search] = { 1: ['/library', '?page=3'], 2: ['/settings', ''], 3: ['/downloads', ''] }[currentSeq];
  const r = load({ path, search, store, state: { jmQuickNavSeq: currentSeq } });
  return [r.qn.prune().map(e => e.url), r.qn.goBack(), r.calls.map(c => c[0])];
};
out.seqJumpedBackPastSteps = seqSkipped(1);
out.seqStepsStillBehind = seqSkipped(3);
// pages without the quick-nav (reader, preview) still drop the steps a history-menu jump has passed
out.readerPrunes = (() => {
  const store = new Map();
  store.set('jm-quick-nav-back-v1', JSON.stringify([{ url: '/downloads', y: 0, title: 'D', key: 'kD', savedAt: now }]));
  const r = load({ path: '/read/5', store, nav: { key: 'kR', index: 1, entries: [['kL', '/library'], ['kR', '/read/5'], ['kD', '/downloads']] } });
  const onLoad = JSON.parse(store.get('jm-quick-nav-back-v1'));
  return [onLoad, (r.win.on.pageshow || []).length];
})();
out.noNavigationApi = (() => {
  const store = new Map();
  store.set('jm-quick-nav-back-v1', JSON.stringify([{ url: '/library', y: 1, listTop: null, title: 't', key: 'kA', savedAt: now }]));
  const r = load({ path: '/downloads', store }); r.qn.goBack(); return r.calls.map(c => c[0]);
})();

// already back on that page by other means (browser Back, the top menu): the step is dropped
const pruned = (step, path, search, nav) => {
  const store = new Map();
  store.set('jm-quick-nav-back-v1', JSON.stringify([
    { url: '/', y: 0, listTop: null, title: '首页', key: 'k0', savedAt: now }, Object.assign({ y: 0, listTop: null, title: 't', savedAt: now }, step)]));
  const r = load({ path, search, store, nav }); return r.qn.prune().map(e => e.url);
};
out.prunedSameEntry = pruned({ url: '/library?q=1', key: 'kA' }, '/library', '?q=2', { key: 'kA', index: 0, entries: [] });
out.prunedSameAddress = pruned({ url: '/library?q=1', key: 'kA' }, '/library', '?q=1', { key: 'kB', index: 0, entries: [] });
out.keptElsewhere = pruned({ url: '/library?q=1', key: 'kA' }, '/downloads', '', { key: 'kB', index: 0, entries: [] });

// bounded: the last MAX_STACK steps, 12 hours, same-site addresses only
shared = new Map();
for (let i = 0; i < 13; i++) load({ path: '/library', search: '?page=' + i, store: shared }).qn.remember();
out.bounded = [stackOf(shared).length, stackOf(shared)[0].url, p.qn.MAX_STACK];
shared = new Map();
shared.set('jm-quick-nav-back-v1', JSON.stringify([
  { url: '/old', y: 0, title: 't', savedAt: now - 13 * HOUR }, { url: '/future', y: 0, title: 't', savedAt: now + HOUR },
  { url: '//evil.example/x', y: 0, title: 't', savedAt: now }, { url: 'http://evil.example/', y: 0, title: 't', savedAt: now },
  { url: '/a b', y: 0, title: 't', savedAt: now }, { url: '/a\\b', y: 0, title: 't', savedAt: now },
  { url: 42, y: 0, title: 't', savedAt: now }, null, { url: '/ok', y: 5, title: 't', savedAt: now - HOUR }]));
out.filtered = load({ path: '/downloads', store: shared }).qn.readStack().map(e => e.url);
shared = new Map([['jm-quick-nav-back-v1', '{oops']]);
out.broken = [load({ path: '/downloads', store: shared }).qn.readStack(), load({ path: '/downloads', store: shared }).qn.goBack()];
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


def test_a_jump_remembers_the_address_and_the_exact_position(harness):
    step = harness["remembered"]
    assert {k: step[k] for k in ("url", "y", "listTop", "title", "key")} == {
        "url": "/library?q=a&page=2", "y": 1235, "listTop": 300, "title": "资源库", "key": "k1"}


def test_return_names_the_page_it_goes_back_to(harness):
    long_title = "x" * 39 + "…"
    assert harness["titles"] == ["设置", "搜索“abc”", "详情“一部很长的漫画标题”", "详情 7", long_title]


def test_returns_step_back_in_reverse_order_without_looping(harness):
    ok, calls = harness["firstBack"]
    assert ok is True and calls == [["expect", "/wishlist?status=missing", 700, None], ["assign", "/wishlist?status=missing"]]
    ok, calls = harness["secondBack"]
    assert ok is True and calls == [["expect", "/search?keyword=a", 500, None], ["assign", "/search?keyword=a"]]
    ok, calls, stack = harness["nothingLeft"]
    assert ok is False and calls == [] and stack == []  # 返回 itself never adds a step


def test_return_uses_the_browser_back_only_for_the_matching_previous_entry(harness):
    assert harness["browserBack"] == ["expect", "back"]
    assert harness["previousIsOther"] == ["expect", "assign"]
    assert harness["previousChangedAddress"] == ["expect", "assign"]
    assert harness["firstEntry"] == ["expect", "assign"]
    assert harness["noNavigationApi"] == ["expect", "assign"]


def test_a_step_already_returned_to_by_other_means_is_dropped(harness):
    assert harness["prunedSameEntry"] == ["/"]
    assert harness["prunedSameAddress"] == ["/"]
    assert harness["keptElsewhere"] == ["/", "/library?q=1"]


def test_return_goes_straight_back_to_an_earlier_history_entry(harness):
    assert harness["traverseBack"] == [["expect", "/library?q=1"], ["traverse", "kA"]]


def test_jumping_back_past_steps_never_makes_return_go_forward(harness):
    # 资源库 → (quick) 设置 → (quick) 下载管理, then the Back menu straight to 资源库: nothing is behind any more
    assert harness["jumpedBackPastSteps"] == [[], False, []]
    # Back once to 设置: only the 资源库 step is still behind, reached with the browser's Back
    assert harness["jumpedBackOneStep"] == [["/library?page=3"], True, ["expect", "back"]]
    assert harness["stepsStillBehind"] == [["/library?page=3", "/settings"], True, ["expect", "back"]]


def test_without_the_navigation_api_history_entries_are_numbered(harness):
    first, second, again, counter = harness["seqAssigned"]
    assert first == {"jmQuickNavSeq": 1} and second == {"jmQuickNavSeq": 2}
    assert again == {"jmQuickNavSeq": 1, "jmSearch": "kept"}  # an entry keeps its number and the rest of its state
    assert counter == "2"
    assert harness["seqRemembered"] == 7


def test_without_the_navigation_api_jumping_back_past_steps_never_goes_forward(harness):
    assert harness["seqJumpedBackPastSteps"] == [[], False, []]
    assert harness["seqStepsStillBehind"] == [["/library?page=3", "/settings"], True, ["expect", "assign"]]


def test_reader_pages_without_the_quick_nav_still_drop_passed_steps(harness):
    stack, pageshow_listeners = harness["readerPrunes"]
    assert stack == [] and pageshow_listeners == 1


def test_steps_are_bounded_fresh_and_same_site(harness):
    assert harness["bounded"] == [10, "/library?page=3", 10]
    assert harness["filtered"] == ["/ok"]
    assert harness["broken"] == [[], False]
