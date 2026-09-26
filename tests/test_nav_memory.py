"""Top navigation returns to the last search / favourites / library state of this tab (static/js/nav-memory.js).

The logic is exercised in Node with a stand-in window/document (skipped when Node is missing; CI installs it),
the wiring through rendered pages and script sources.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC_JS = ROOT / "static" / "js"
NODE = shutil.which("node")

HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
const HOUR = 3600 * 1000;
let now = 1_000_000_000_000;
const FakeDate = { now: () => now };

// opts: path, search, scrollY, store (Map shared between "pages"), links, navType, listTop (null = no list element)
function page(opts) {
  opts = opts || {};
  const store = opts.store || new Map();
  const on = { window: {}, document: {} };
  const timers = [];
  const add = (where) => (type, fn) => { (on[where][type] = on[where][type] || []).push(fn); };
  const links = (opts.links || []).map(p => ({
    attrs: { 'data-nav-memory': p, href: p },
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    setAttribute(k, v) { this.attrs[k] = String(v); },
  }));
  const win = {
    sessionStorage: {
      getItem: k => store.has(k) ? store.get(k) : null,
      setItem: (k, v) => store.set(k, String(v)),
      removeItem: k => store.delete(k),
    },
    location: { pathname: opts.path || '/', search: opts.search || '' },
    performance: { getEntriesByType: () => [{ type: opts.navType || 'navigate' }] },
    scrollY: opts.scrollY || 0,
    requestAnimationFrame: fn => fn(),
    scrollTo: o => { win.scrollY = o.top; win.lastBehavior = o.behavior; },
    addEventListener: add('window'),
  };
  // the list sits at listTop in the document (its viewport top moves with the scroll)
  const list = { top: opts.listTop, getBoundingClientRect() { return { top: this.top - win.scrollY }; } };
  const doc = {
    visibilityState: 'visible', addEventListener: add('document'), querySelectorAll: () => links,
    querySelector: sel => sel === '[data-nav-memory-list]' && typeof opts.listTop === 'number' ? list : null,
  };
  const setT = (fn, ms) => { timers.push({ fn, ms }); return timers.length; };
  const clearT = id => { if (timers[id - 1]) timers[id - 1].fn = null; };
  new Function('window', 'document', 'setTimeout', 'clearTimeout', 'Date', src)(win, doc, setT, clearT, FakeDate);
  const fire = (where, type, event) => (on[where][type] || []).forEach(fn => fn(event || {}));
  const target = (nav) => ({ closest: sel => sel === '.navbar' ? (nav ? {} : null) : null });
  return {
    win, doc, store, links, list, nm: win.navMemory,
    scroll(y) { win.scrollY = y; fire('window', 'scroll'); },
    rest() { timers.splice(0).forEach(t => t.fn && t.fn()); },
    click(nav) { fire('window', 'click', { target: target(nav) }); },
    type() { fire('window', 'input', { target: target(false) }); },
    key() { fire('window', 'keydown', { target: target(false) }); },
    hoverLink(i) { fire('document', 'pointerover', { target: { closest: () => links[i] } }); return links[i].attrs.href; },
    clickLink(i, mods) { fire('document', 'click', Object.assign({ type: 'click', target: { closest: () => links[i] } }, mods)); return links[i].attrs.href; },
    leave() { fire('window', 'pagehide'); },
    hide() { doc.visibilityState = 'hidden'; fire('document', 'visibilitychange'); },
    pageshow(persisted) { fire('window', 'pageshow', { persisted }); },
    wheel() { fire('window', 'wheel'); },
  };
}

const out = {};
const snap = (url, age) => JSON.stringify({ url, data: { items: [] }, scrollY: 10, savedAt: now - age });
const memory = (entry) => new Map([['jm-nav-memory-v1', JSON.stringify({ '/library': Object.assign({ savedAt: now }, entry) })]]);
const arrivedAt = (store, path, age) => { store.set('jm-nav-arrival-v1', JSON.stringify({ path, at: now - (age || 0) })); return store; };

// nothing remembered yet
let p = page({ links: ['/search', '/library', '/wishlist'] });
out.empty = [p.nm.lastUrl('/search'), p.nm.lastUrl('/library'), p.hoverLink(0), p.hoverLink(1)];
out.other = p.nm.lastUrl('/downloads');

// the search snapshot written by search.js
p = page({ store: new Map([['jm-search-state-v1', snap('/search?keyword=a&page=2', HOUR)]]), links: ['/search'] });
out.fresh = [p.nm.lastUrl('/search'), p.hoverLink(0)];
out.expired = page({ store: new Map([['jm-search-state-v1', snap('/search?keyword=a', 13 * HOUR)]]) }).nm.lastUrl('/search');
out.future = page({ store: new Map([['jm-search-state-v1', snap('/search?keyword=a', -HOUR)]]) }).nm.lastUrl('/search');
out.rejected = ['//evil/search?x', '/searchx?y', '/search?x y', '/search?x#h', '/search\\x', 'javascript:1', 42]
  .map(u => page({ store: new Map([['jm-search-state-v1', snap(u, 0)]]) }).nm.lastUrl('/search'));
out.broken = page({ store: new Map([['jm-search-state-v1', '{oops']]) }).nm.lastUrl('/search');

// on the page itself: the current address wins over what was remembered
out.current = page({ path: '/library', search: '?sort=author', store: memory({ url: '/library?q=old', scrollY: 1 }) }).nm.lastUrl('/library');

// favourites: leaving records the address, the place, the raw position and where the list was
let shared = new Map();
p = page({ path: '/wishlist', search: '?status=none', scrollY: 700, store: shared, listTop: 300 });
p.leave();
const saved = JSON.parse(shared.get('jm-nav-memory-v1'))['/wishlist'];
out.saved = saved;
p = page({ store: shared, links: ['/wishlist', '/search'] });
out.savedLink = p.clickLink(0);
out.arrivalFlag = JSON.parse(shared.get('jm-nav-arrival-v1') || 'null');
shared.delete('jm-nav-arrival-v1');
p.clickLink(1);
out.noArrivalForSearch = shared.has('jm-nav-arrival-v1');
p.clickLink(0, { ctrlKey: true }); p.clickLink(0, { button: 1 }); p.clickLink(0, { shiftKey: true });
out.noArrivalForNewTab = shared.has('jm-nav-arrival-v1');  // opened elsewhere: this tab does not navigate
out.wrongKey = page({ store: new Map([['jm-nav-memory-v1', JSON.stringify({ '/library': { url: '/wishlist?x', scrollY: 1, savedAt: now } })]]) }).nm.lastUrl('/library');

// the place: scrolling back up to reach the menu does not count, resting or acting somewhere does
p = page({ path: '/library' });
p.scroll(1500); p.rest();              // rested at 1500
p.scroll(0);                           // quick trip to the top to click the menu
out.placeAfterTrip = p.nm.place().y;
p.rest();                              // ...but staying at the top on purpose
out.placeAfterRestAtTop = p.nm.place().y;
p.scroll(900); p.click(false);         // clicked something at 900
p.scroll(40);
out.placeAfterClick = p.nm.place().y;
p.scroll(900); p.rest(); p.scroll(30); p.click(true); // clicking the menu itself is not "acting here"
out.placeNavClick = p.nm.place().y;
p.scroll(1800); p.rest(); p.scroll(1000); p.key(); p.scroll(200); p.key(); p.scroll(0); // PageUp, PageUp, PageUp
out.placeAfterScrollKeys = p.nm.place().y;
p.scroll(1300); p.type(); p.scroll(10);  // typed into a field at 1300
out.placeAfterTyping = p.nm.place().y;
p.scroll(2400);
out.placeInside = p.nm.place().y;       // left from inside the list: exactly there
p = page({ path: '/library', scrollY: 60 });
out.placeNoHistory = p.nm.place().y;
// the place belongs to the address: clicking page 2 / a filter at the bottom, then the list changes
p = page({ path: '/library', search: '?page=1' });
p.scroll(2500); p.click(false);
p.win.location.search = '?page=2'; p.win.scrollY = 0;
out.placeAfterPaging = p.nm.place().y;
shared = new Map();
p = page({ path: '/library', search: '?q=1', store: shared });
p.scroll(1200); p.rest(); p.scroll(0); p.leave();
const left = JSON.parse(shared.get('jm-nav-memory-v1'))['/library'];
out.leaveUsesPlace = [left.scrollY, left.rawScrollY, left.listTop];
// on a phone the opened menu pushes the page down before the user taps 收藏: the place keeps the list position
// measured when the place was taken, so the same spot of the list comes back once the menu is closed again
shared = new Map();
p = page({ path: '/library', search: '?q=1', store: shared, listTop: 300 });
p.scroll(2500); p.rest();
p.list.top = 520; p.scroll(0); p.leave();   // menu open: everything 220 px lower
const phone = JSON.parse(shared.get('jm-nav-memory-v1'))['/library'];
out.phoneSaved = [phone.scrollY, phone.listTop, phone.rawScrollY, phone.rawListTop];
arrivedAt(shared, '/library');
p = page({ path: '/library', search: '?q=1', store: shared, listTop: 300 });
p.nm.restoreScroll();
out.phoneRestored = p.win.scrollY;

// restoring the scroll position after the list is drawn
p = page({ path: '/library', search: '?q=1', store: arrivedAt(memory({ url: '/library?q=1', scrollY: 900 }), '/library') });
p.nm.restoreScroll();
out.restored = [p.win.scrollY, p.win.lastBehavior, p.store.has('jm-nav-arrival-v1')];
p.win.scrollY = 5; p.nm.restoreScroll();
out.restoredOnce = p.win.scrollY;
p = page({ path: '/library', search: '?q=1', store: memory({ url: '/library?q=1', scrollY: 900 }) });
p.nm.restoreScroll();
out.typedAddress = p.win.scrollY;          // no nav link, not Back / reload: the initial page
out.backForward = (() => { const q = page({ path: '/library', search: '?q=1', navType: 'back_forward', store: memory({ url: '/library?q=1', scrollY: 900 }) }); q.nm.restoreScroll(); return q.win.scrollY; })();
out.reload = (() => { const q = page({ path: '/library', search: '?q=1', navType: 'reload', store: memory({ url: '/library?q=1', scrollY: 900, rawScrollY: 0 }) }); q.nm.restoreScroll(); return q.win.scrollY; })();
out.reloadMid = (() => { const q = page({ path: '/library', search: '?q=1', navType: 'reload', store: memory({ url: '/library?q=1', scrollY: 900, rawScrollY: 400 }) }); q.nm.restoreScroll(); return q.win.scrollY; })();
out.reloadUsesRawList = (() => { const q = page({ path: '/library', search: '?q=1', navType: 'reload', listTop: 350, store: memory({ url: '/library?q=1', scrollY: 900, listTop: 100, rawScrollY: 400, rawListTop: 300 }) }); q.nm.restoreScroll(); return q.win.scrollY; })();
out.staleArrival = (() => { const q = page({ path: '/library', search: '?q=1', store: arrivedAt(memory({ url: '/library?q=1', scrollY: 900 }), '/library', 60000) }); q.nm.restoreScroll(); return q.win.scrollY; })();
out.otherArrival = (() => { const q = page({ path: '/library', search: '?q=1', store: arrivedAt(memory({ url: '/library?q=1', scrollY: 900 }), '/wishlist') }); q.nm.restoreScroll(); return q.win.scrollY; })();
out.otherFilters = (() => { const q = page({ path: '/library', search: '?q=2', store: arrivedAt(memory({ url: '/library?q=1', scrollY: 900 }), '/library') }); q.nm.restoreScroll(); return q.win.scrollY; })();
out.userScrolledFirst = (() => { const q = page({ path: '/library', search: '?q=1', store: arrivedAt(memory({ url: '/library?q=1', scrollY: 900 }), '/library') }); q.wheel(); q.nm.restoreScroll(); return q.win.scrollY; })();
out.restoreExpired = (() => { const q = page({ path: '/library', search: '?q=1', store: arrivedAt(memory({ url: '/library?q=1', scrollY: 900, savedAt: now - 13 * HOUR }), '/library') }); q.nm.restoreScroll(); return q.win.scrollY; })();
// content above the list loaded later / earlier: the same spot of the list comes back
out.listMovedDown = (() => { const q = page({ path: '/library', search: '?q=1', listTop: 500, store: arrivedAt(memory({ url: '/library?q=1', scrollY: 900, listTop: 300 }), '/library') }); q.nm.restoreScroll(); return q.win.scrollY; })();
out.listMovedUp = (() => { const q = page({ path: '/library', search: '?q=1', listTop: 111, store: arrivedAt(memory({ url: '/library?q=1', scrollY: 900, listTop: 300 }), '/library') }); q.nm.restoreScroll(); return q.win.scrollY; })();
out.aboveTheList = (() => { const q = page({ path: '/library', search: '?q=1', listTop: 500, store: arrivedAt(memory({ url: '/library?q=1', scrollY: 200, listTop: 300 }), '/library') }); q.nm.restoreScroll(); return q.win.scrollY; })();
out.restoredIsThePlace = (() => { const q = page({ path: '/library', search: '?q=1', store: arrivedAt(memory({ url: '/library?q=1', scrollY: 900 }), '/library') }); q.nm.restoreScroll(); q.scroll(0); return q.nm.place().y; })();

p = page(); p.nm.markPlace(640); out.markPlace = p.nm.place().y;

// a rest timer running when the page goes into the back/forward cache must not finish after it comes back
p = page({ path: '/library' });
p.scroll(1500); p.rest(); p.scroll(0); p.leave(); p.rest();
out.timerStoppedOnLeave = p.nm.place().y;

// Back / Forward from the back/forward cache: the page is where it was left (at the top, to reach the menu)
const bf = (entry, y, persisted) => {
  const q = page({ path: '/library', search: '?q=1', scrollY: y, store: memory(Object.assign({ url: '/library?q=1' }, entry)) });
  q.pageshow(persisted); return q.win.scrollY;
};
out.bfcache = [bf({ scrollY: 1400 }, 0, true), bf({ scrollY: 1400 }, 0, false), bf({ scrollY: 1400 }, 600, true),
               bf({ scrollY: 80 }, 0, true), bf({ scrollY: 1400, url: '/library?q=2' }, 0, true)];

// leaving (or switching away) before the list was drawn keeps the remembered place instead of the page top
shared = memory({ url: '/library?q=1', scrollY: 1300, listTop: 250, savedAt: now - HOUR });
p = page({ path: '/library', search: '?q=1', store: shared });
p.leave();
let kept = JSON.parse(shared.get('jm-nav-memory-v1'))['/library'];
out.keptBeforeDrawn = [kept.scrollY, kept.listTop, kept.savedAt === now];
p.hide();
kept = JSON.parse(shared.get('jm-nav-memory-v1'))['/library'];
out.keptWhenHidden = kept.scrollY;
p.nm.restoreScroll();          // drawn (typed address: no jump), from now on leaving records the page as it is
p.scroll(500); p.leave();
out.savedAfterDrawn = JSON.parse(shared.get('jm-nav-memory-v1'))['/library'].scrollY;
p = page({ path: '/library', search: '?q=9', store: shared });
p.scroll(300); p.leave();       // another address: nothing to keep
out.otherUrlBeforeDrawn = JSON.parse(shared.get('jm-nav-memory-v1'))['/library'];
out.navType = [page({ navType: 'reload' }).nm.navigationType(), page({ navType: 'back_forward' }).nm.navigationType()];
out.maxAgeHours = p.nm.MAX_AGE / HOUR;
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def harness():
    if NODE is None:
        pytest.skip("needs Node.js (CI installs it)")
    result = subprocess.run([NODE, "-e", HARNESS, str(STATIC_JS / "nav-memory.js")],
                            capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_nothing_remembered_keeps_the_plain_links(harness):
    assert harness["empty"] == [None, None, "/search", "/library"]
    assert harness["other"] is None  # only 搜索 / 收藏 / 资源库 are remembered


def test_nav_search_returns_to_the_last_search_of_this_tab(harness):
    assert harness["fresh"] == ["/search?keyword=a&page=2", "/search?keyword=a&page=2"]
    assert harness["maxAgeHours"] == 12
    assert harness["expired"] is None and harness["future"] is None


def test_only_addresses_of_the_same_page_are_used(harness):
    assert harness["rejected"] == [None] * 7
    assert harness["broken"] is None and harness["wrongKey"] is None


def test_on_the_page_itself_the_current_address_wins(harness):
    assert harness["current"] == "/library?sort=author"


def test_leaving_a_list_page_remembers_its_address_and_place(harness):
    saved = harness["saved"]
    assert saved["url"] == "/wishlist?status=none" and saved["scrollY"] == 700 and saved["rawScrollY"] == 700
    assert saved["listTop"] == 300 and saved["rawListTop"] == 300 and isinstance(saved["savedAt"], int)
    assert harness["savedLink"] == "/wishlist?status=none"
    # clicking a remembered list link marks the arrival; the search page restores from its own snapshot
    assert harness["arrivalFlag"]["path"] == "/wishlist" and harness["noArrivalForSearch"] is False
    assert harness["noArrivalForNewTab"] is False  # Ctrl / Shift / middle click open another tab


def test_back_forward_cache_returns_to_the_place(harness):
    # restored at the top → the place; not persisted, already scrolled, place near the top or other filters → untouched
    assert harness["bfcache"] == [1400, 0, 600, 0, 0]
    assert harness["timerStoppedOnLeave"] == 1500


def test_leaving_before_the_list_is_drawn_keeps_the_place(harness):
    assert harness["keptBeforeDrawn"] == [1300, 250, True]
    assert harness["keptWhenHidden"] == 1300
    assert harness["savedAfterDrawn"] == 500
    assert harness["otherUrlBeforeDrawn"]["url"] == "/library?q=9" and harness["otherUrlBeforeDrawn"]["scrollY"] == 300


def test_the_place_is_where_the_user_was_not_the_trip_to_the_menu(harness):
    assert harness["placeAfterTrip"] == 1500       # scrolled up just to reach the navbar
    assert harness["placeAfterRestAtTop"] == 0     # stayed at the top on purpose
    assert harness["placeAfterClick"] == 900       # last did something at 900
    assert harness["placeNavClick"] == 900         # pressing the menu itself doesn't move the place
    assert harness["placeAfterScrollKeys"] == 1800  # PageUp / arrow keys only scroll: not "doing something here"
    assert harness["placeAfterTyping"] == 1300
    assert harness["placeInside"] == 2400          # left from inside the list: exactly there
    assert harness["placeNoHistory"] == 60
    assert harness["placeAfterPaging"] == 0        # the place of page 1 does not carry over to page 2
    assert harness["leaveUsesPlace"] == [1200, 0, None]
    assert harness["markPlace"] == 640


def test_scroll_is_restored_once_after_arriving_through_the_menu(harness):
    assert harness["restored"] == [900, "instant", False]  # no animation; the arrival mark is used up
    assert harness["restoredOnce"] == 5
    assert harness["restoredIsThePlace"] == 900


def test_typed_addresses_open_the_initial_page_but_back_and_reload_restore(harness):
    assert harness["typedAddress"] == 0
    assert harness["staleArrival"] == 0 and harness["otherArrival"] == 0
    assert harness["backForward"] == 900
    assert harness["reload"] == 0 and harness["reloadMid"] == 400  # a reload stays where the user actually was
    assert harness["reloadUsesRawList"] == 450
    assert harness["navType"] == ["reload", "back_forward"]


def test_scroll_is_restored_only_for_the_same_filters_and_a_still_user(harness):
    assert harness["otherFilters"] == 0
    assert harness["userScrolledFirst"] == 0       # never yank a page the user already moved
    assert harness["restoreExpired"] == 0


def test_restore_follows_the_list_when_content_above_it_changed_height(harness):
    assert harness["listMovedDown"] == 1100        # 200 px more above the list than when leaving
    assert harness["listMovedUp"] == 711
    assert harness["aboveTheList"] == 200          # a place above the list is kept as it was


def test_place_keeps_the_list_position_it_was_taken_with(harness):
    assert harness["phoneSaved"] == [2500, 300, 0, 520]
    assert harness["phoneRestored"] == 2500


# ── wiring ──

def test_nav_links_carry_the_memory_marker(client):
    html = client.get("/downloads").get_data(as_text=True)
    for path in ("/search", "/wishlist", "/library"):
        assert re.search(r'<a class="nav-link[^"]*" href="%s" data-nav-memory="%s">' % (path, path), html), path
    for path in ("/", "/downloads", "/settings"):
        assert re.search(r'<a class="nav-link[^"]*" href="%s">' % path, html), path


@pytest.mark.parametrize("url, script", [
    ("/", "home.js"), ("/search", "search.js"), ("/library", "library.js"), ("/wishlist", "wishlist.js"),
    ("/downloads", "downloads.js"), ("/album/123", "detail.js"), ("/online/123", "reader.js"),
])
def test_nav_memory_loads_before_the_page_script(client, url, script):
    html = client.get(url).get_data(as_text=True)
    assert "js/nav-memory.js" in html
    assert html.index("js/nav-memory.js") < html.index("js/" + script)


@pytest.mark.parametrize("url, marker", [
    ("/search", '<div id="search-results" data-nav-memory-list>'),
    ("/library", '<div id="library-grid" class="library-grid" data-nav-memory-list>'),
    ("/wishlist", 'id="wishlist-table" data-nav-memory-list>'),
])
def test_list_pages_mark_the_list_used_for_restoring(client, url, marker):
    assert marker in client.get(url).get_data(as_text=True)


def test_local_reader_back_falls_back_to_the_last_search(client, monkeypatch):
    from core import local_availability
    monkeypatch.setattr(local_availability, "is_readable", lambda album_id: True)
    local = client.get("/read/123").get_data(as_text=True)
    assert re.search(r'href="/search" data-nav-memory="/search" id="reader-back"', local)
    online = client.get("/online/123").get_data(as_text=True)
    assert re.search(r'<a [^>]*href="/album/123" id="reader-back"', online)
    assert not re.search(r'<a [^>]*data-nav-memory[^>]*id="reader-back"', online)


def test_empty_library_links_to_the_last_search():
    source = (STATIC_JS / "library.js").read_text(encoding="utf-8")
    assert '<a href="/search" data-nav-memory="/search">搜索页面</a>' in source


def test_search_snapshot_follows_the_navigation_memory():
    source = (STATIC_JS / "search.js").read_text(encoding="utf-8").replace("\r\n", "\n")
    assert "SNAPSHOT_MAX_AGE = (window.navMemory && window.navMemory.MAX_AGE)" in source
    assert "RELOAD_MAX_AGE = 30 * 60 * 1000" in source  # F5 still searches again after 30 minutes
    assert "age < (reload ? RELOAD_MAX_AGE : SNAPSHOT_MAX_AGE)" in source
    assert "var fetchedAt = saved ? (saved.fetchedAt || saved.savedAt) : 0;" in source  # data age, not time left
    # only leaving the page records "where the user was"; loading / paging records where the page is
    assert "var seen = keep ? { y: keep.scrollY || 0, listTop: keep.resultsTop } : (leaving && nm ? nm.place() : here);" in source
    # until a restore has scrolled back, saves keep the snapshot's place instead of the page top
    assert "var keep = restoring;" in source and "if (restoring !== saved) return;" in source
    assert "if (!restore) restoring = null;" in source
    assert "scrollY: seen.y, resultsTop: seen.listTop," in source
    assert "rawScrollY: here.y, rawResultsTop: here.listTop," in source
    pagehide = source[source.index("window.addEventListener('pagehide'"):][:120]
    assert "saveSearchState(true);" in pagehide
    assert source.count("saveSearchState(true)") == 1
    # Back / Forward use the history entry's own snapshot; only a reload takes the newer of the two copies
    # (a reload drops the pagehide replaceState)
    assert "function snapshotForHere(reload)" in source and "var saved = snapshotForHere(reload);" in source
    assert "if (own && session && reload) return (session.savedAt || 0) > (own.savedAt || 0) ? session : own;" in source
    assert "return own || session;" in source
    # restoring goes through the shared helper, relative to the results
    assert "window.navMemory.scrollBack(y, listTop);" in source
    assert "var listTop = raw ? saved.rawResultsTop : saved.resultsTop;" in source
    assert "if (restore) scrollToSaved(restore.saved, restore.reload);" in source
    assert "restoreSearchState(true);" in source and "restoreSearchState(false);" in source


def test_detail_back_uses_the_same_memory():
    source = (STATIC_JS / "detail.js").read_text(encoding="utf-8")
    assert "window.navMemory.lastUrl('/search')" in source
    assert "jm-search-state-v1" not in source  # one owner of the snapshot format


@pytest.mark.parametrize("script", ["library.js", "wishlist.js"])
def test_list_pages_restore_the_place_after_drawing(script):
    source = (STATIC_JS / script).read_text(encoding="utf-8")
    assert "if (window.navMemory) window.navMemory.restoreScroll();" in source
