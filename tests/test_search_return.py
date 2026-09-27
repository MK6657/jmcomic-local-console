"""The search page lands where the floating quick-nav 返回 left it (static/js/search.js with window.navMemory.takeReturn).

search.js runs in Node with a stand-in window/document, an apiFetch answered by hand and a stubbed navMemory
(skipped when Node is missing; CI installs it). The stub hands out the pending 返回 once, and only within
15 s of the page opening, like nav-memory.js.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC_JS = ROOT / "static" / "js"
NODE = shutil.which("node")

HARNESS = r"""
const src = require('fs').readFileSync(process.argv[1], 'utf8');
const HOUR = 3600 * 1000;
const RETURN_MS = 15000;   // nav-memory.js: a 返回 target older than this is gone
let now = 1_000_000_000_000;
const FakeDate = { now: () => now };
const ORIGIN = 'http://127.0.0.1:5000';
const KEY = 'jm-search-state-v1';
const flush = () => new Promise(resolve => setImmediate(resolve));  // let pending .then callbacks run

// a generic element: value / innerHTML, event handlers kept for fire(), no children
function element() {
  const classes = new Set();
  const on = {};
  return {
    value: '', innerHTML: '', textContent: '', disabled: false, dataset: {}, style: {},
    classList: {
      add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c),
      toggle: (c, force) => ((force === undefined ? !classes.has(c) : force) ? classes.add(c) : classes.delete(c)),
    },
    addEventListener(type, fn) { (on[type] = on[type] || []).push(fn); },
    fire(type, event) { (on[type] || []).forEach(fn => fn(event || {})); },
    querySelector: () => null, querySelectorAll: () => [], contains: () => true,
    getBoundingClientRect: () => ({ top: 0 }), scrollIntoView() {}, focus() {},
  };
}
const IDS = ['search-form', 'search-input', 'search-btn', 'sort-select', 'page-size-select', 'search-results',
             'pagination', 'search-status', 'global-alert-container', 'search-history-container', 'search-history-tags'];

// opts: search (location.search of /search), store (sessionStorage Map), back (the pending 返回 { y, listTop }),
// navType, scrollY, holdFrames (animation frames wait for runFrames() instead of running at once)
function load(opts) {
  opts = opts || {};
  const store = opts.store || new Map();
  let startedAt = now;          // the 返回 was clicked right before this page opened
  let pending = opts.back || null;
  let takes = 0;
  let moved = false;            // nav-memory's "the user already scrolled / pressed a key on this page"
  const fetches = [], scrolls = [], timers = [], frames = [];
  const els = {};
  IDS.forEach(id => { els[id] = element(); });
  const winOn = {};
  const loc = {
    pathname: '/search', search: opts.search || '',
    get href() { return ORIGIN + this.pathname + this.search; },
  };
  const hist = {
    state: null,
    replaceState(state, title, url) {   // like the browser: the address bar follows
      hist.state = state;
      if (url) { const u = new URL(url, ORIGIN); loc.pathname = u.pathname; loc.search = u.search; }
    },
  };
  const session = {
    getItem: k => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: k => store.delete(k),
  };
  const win = {
    location: loc, sessionStorage: session, scrollY: opts.scrollY || 0,
    addEventListener: (type, fn) => { (winOn[type] = winOn[type] || []).push(fn); },
    scrollTo: o => { win.scrollY = o.top; },
    // every request stays open until the test answers it
    apiFetch: url => new Promise((resolve, reject) => { fetches.push({ url, resolve, reject }); }),
    readLink: { html: id => '<a class="reader-link" href="/online/' + id + '">阅读</a>', apply() {} },
    escapeHtml: s => String(s == null ? '' : s),
    escapeHtmlAttr: s => String(s == null ? '' : s),
    navMemory: {
      MAX_AGE: 12 * HOUR,
      navigationType: () => opts.navType || 'navigate',
      listTopNow: () => 300,
      place: () => ({ y: Math.round(win.scrollY), listTop: 300 }),
      scrollBack: (y, listTop) => { scrolls.push([y, listTop]); win.scrollY = y; },
      // handed out once, and only within RETURN_MS of the 返回 (≈ this page opening)
      takeReturn: () => {
        takes += 1;
        const back = pending && now - startedAt < RETURN_MS ? pending : null;
        pending = null;
        return back;
      },
      hasReturn: () => !!pending && now - startedAt < RETURN_MS,
      userMoved: () => moved,
    },
  };
  const doc = { visibilityState: 'visible', getElementById: id => els[id] || null, addEventListener() {} };
  const raf = fn => { if (opts.holdFrames) frames.push(fn); else fn(); return 0; };
  const setT = (fn, ms) => { timers.push({ fn, ms }); return timers.length; };
  new Function('window', 'document', 'history', 'location', 'sessionStorage', 'requestAnimationFrame', 'setTimeout', 'Date', src)
    (win, doc, hist, loc, session, raf, setT, FakeDate);
  const searches = () => fetches.filter(f => f.url.indexOf('/api/search?') === 0);
  return {
    win, els, scrolls,
    takes: () => takes,
    hasReturn: () => win.navMemory.hasReturn(),
    searches: () => searches().map(f => f.url),
    historyRequested: () => fetches.some(f => f.url === '/api/search-history'),
    async respond(i, data) { searches()[i].resolve(data); await flush(); },
    drawn: text => els['search-results'].innerHTML.indexOf(text) >= 0,
    saved: () => { const s = JSON.parse(store.get(KEY) || 'null'); return s && [s.url, s.scrollY, s.resultsTop, s.rawScrollY, s.rawResultsTop]; },
    clickPage(n) {
      const button = { disabled: false, getAttribute: () => String(n) };
      els.pagination.fire('click', { target: { closest: sel => (sel === 'button[data-page]' ? button : null) } });
    },
    newSearch(q) { els['search-input'].value = q; els['search-btn'].fire('click'); },
    sortBy(v) { els['sort-select'].value = v; els['sort-select'].fire('change'); },
    leave() { (winOn.pagehide || []).forEach(fn => fn({})); },
    expectReturn(back) { pending = back; startedAt = now; },
    userScrolls(y) { win.scrollY = y; moved = true; },
    pageshow(persisted) { (winOn.pageshow || []).forEach(fn => fn({ persisted })); },
    runFrames() { while (frames.length) frames.shift()(); },   // held frames, including ones they request
  };
}

const out = {};
const RET = { y: 2600, listTop: 300 };                            // where 返回 wants to land
const HERE = '?keyword=abc&sort=latest&page_size=20&page=1';      // the address search.js keeps in the bar
const OTHER = '?keyword=other&sort=latest&page_size=20&page=1';
const item = (id, title) => ({ album_id: id, title, author: 'someone', tags: [], cover_url: '' });
const OK = { status: 'ok', items: [item('42', 'Fresh result')], total: 1 };
const LATE = { status: 'ok', items: [item('9', 'Late result')], total: 1 };   // an answer the user moved on from
// a results snapshot as search.js writes it: seen at 900 (results at 250), fetched an hour ago
const snapStore = (search, extra) => new Map([[KEY, JSON.stringify(Object.assign({
  url: '/search' + search, query: 'abc', sort: 'latest', pageSize: 20, page: 1,
  data: { status: 'ok', items: [item('7', 'Snapshot result')], total: 1 },
  fetchedAt: now - HOUR, savedAt: now - HOUR,
  scrollY: 900, resultsTop: 250, rawScrollY: 900, rawResultsTop: 250,
}, extra))]]);

(async () => {
  // (a) a snapshot for this address + 返回: redrawn from it (no upstream request), lands at the 返回 place
  let p = load({ search: HERE, store: snapStore(HERE), back: RET });
  out.snapshotReturn = { searches: p.searches(), drawn: p.drawn('Snapshot result'), scrolls: p.scrolls.slice(),
                         takes: p.takes(), history: p.historyRequested() };
  // (c) paging / searching afterwards starts fresh: the 返回 is not used again
  p.clickPage(2); await p.respond(0, OK);
  p.newSearch('xyz'); await p.respond(1, OK);
  out.snapshotReturnThenMore = { searches: p.searches(), scrolls: p.scrolls.slice(), takes: p.takes() };

  // (b) no snapshot for this address (the tab searched something else since): search again, then land in place
  p = load({ search: HERE, store: snapStore(OTHER), back: RET });
  out.researchBefore = { searches: p.searches(), scrolls: p.scrolls.slice(), takes: p.takes(), left: p.hasReturn(),
                         saved: p.saved(), spinner: p.drawn('搜索中') };
  await p.respond(0, OK);
  out.researchAfter = { drawn: p.drawn('Fresh result'), scrolls: p.scrolls.slice(), takes: p.takes() };
  p.clickPage(2); await p.respond(1, OK);
  p.newSearch('xyz'); await p.respond(2, OK);
  out.researchThenMore = { searches: p.searches().length, scrolls: p.scrolls.slice(), takes: p.takes() };

  // a slow upstream: the answer comes 20 s later, past nav-memory's 15 s for the 返回 (taken at page start)
  p = load({ search: HERE, store: snapStore(OTHER), back: RET });
  now += 20000;
  await p.respond(0, OK);
  out.slowResearch = { scrolls: p.scrolls.slice(), takes: p.takes() };
  // this address has a snapshot, but its results are too old to reuse (> 12 h): searched again, 返回 still wins
  p = load({ search: HERE, store: snapStore(HERE, { fetchedAt: now - 13 * HOUR, savedAt: now - 13 * HOUR }), back: RET });
  const staleSearches = p.searches().length;
  now += 20000;
  await p.respond(0, OK);
  out.slowStaleSnapshot = { searches: staleSearches, scrolls: p.scrolls.slice(), takes: p.takes() };
  // the user scrolls (or presses 到顶 / 到底) while the slow search runs: stays where they went
  p = load({ search: HERE, store: snapStore(OTHER), back: RET });
  p.userScrolls(1200);
  now += 20000;
  await p.respond(0, OK);
  out.movedWhileSearching = { scrolls: p.scrolls.slice(), y: p.win.scrollY, drawn: p.drawn('Fresh result') };
  // leaving before the answer: the snapshot keeps the 返回 place instead of the page top
  p = load({ search: HERE, store: snapStore(OTHER), back: RET });
  p.leave();
  out.leftWhileSearching = p.saved();
  // 返回 to the very top of the results (y 0) lands at the top, not at the snapshot's own 900
  out.topReturn = load({ search: HERE, store: snapStore(HERE), back: { y: 0, listTop: 300 } }).scrolls;
  // once landed, the page is the user's again: scrolling on and leaving records the new place
  p = load({ search: HERE, store: snapStore(HERE), back: RET });
  p.win.scrollY = 3100;
  p.leave();
  out.movedOnAfterSnapshotReturn = p.saved();
  p = load({ search: HERE, store: snapStore(OTHER), back: RET });
  await p.respond(0, OK);
  p.win.scrollY = 3100;
  p.leave();
  out.movedOnAfterResearchReturn = p.saved();

  // (d) no 返回: the snapshot's own place as before; a fresh search does not scroll
  p = load({ search: HERE, store: snapStore(HERE) });
  out.snapshotNoReturn = { searches: p.searches(), drawn: p.drawn('Snapshot result'), scrolls: p.scrolls.slice(), takes: p.takes() };
  p = load({ search: HERE, store: snapStore(OTHER) });
  await p.respond(0, OK);
  out.freshSearch = { searches: p.searches().length, drawn: p.drawn('Fresh result'), scrolls: p.scrolls.slice() };
  // F5: the position right before the reload, unless 返回 brought the page here
  const recent = { fetchedAt: now - 5 * 60 * 1000, savedAt: now - 5 * 60 * 1000, rawScrollY: 400, rawResultsTop: 280 };
  out.reloadNoReturn = load({ search: HERE, store: snapStore(HERE, recent), navType: 'reload' }).scrolls;
  out.reloadReturn = load({ search: HERE, store: snapStore(HERE, recent), navType: 'reload', back: RET }).scrolls;

  // (e) the user moves on before the slow answer arrives: the 返回 place is dropped
  p = load({ search: HERE, store: snapStore(OTHER), back: RET });
  p.clickPage(2);
  const pagedSaved = p.saved();
  await p.respond(1, OK); await p.respond(0, LATE);        // new page answers first, the old one after
  out.pagedBeforeAnswer = { searches: p.searches(), scrolls: p.scrolls.slice(), saved: pagedSaved,
                            drawn: p.drawn('Fresh result'), late: p.drawn('Late result') };
  // paging after the results are drawn but before the landing frames run: the landing is dropped
  p = load({ search: HERE, store: snapStore(OTHER), back: RET, holdFrames: true });
  await p.respond(0, OK);
  p.clickPage(2);
  p.runFrames();
  await p.respond(1, OK); p.runFrames();
  out.pagedBeforeLanding = { searches: p.searches().length, scrolls: p.scrolls.slice() };
  // a new search / a sort change before the slow answer
  p = load({ search: HERE, store: snapStore(OTHER), back: RET });
  p.newSearch('xyz');
  const searchedSaved = p.saved();
  now += 20000;
  await p.respond(0, OK); await p.respond(1, OK);          // the old answer first, then the new one
  out.searchedBeforeAnswer = { searches: p.searches(), scrolls: p.scrolls.slice(), saved: searchedSaved };
  p = load({ search: HERE, store: snapStore(OTHER), back: RET });
  p.sortBy('views');
  await p.respond(0, OK); await p.respond(1, OK);
  out.sortedBeforeAnswer = { searches: p.searches().length, scrolls: p.scrolls.slice() };

  // (f) no keyword: search.js leaves the 返回 to nav-memory's generic handler
  p = load({ search: '', back: RET });
  out.noKeyword = { takes: p.takes(), left: p.hasReturn(), searches: p.searches() };
  p.newSearch('abc'); await p.respond(0, OK);
  out.noKeywordThenSearch = { takes: p.takes(), scrolls: p.scrolls.slice() };
  out.noKeywordOtherParams = ['?sort=views&page=2', '?keyword=&sort=latest'].map(search => {
    const r = load({ search, back: RET }); return [r.takes(), r.hasReturn(), r.searches().length];
  });

  // back/forward cache: 返回 through the browser's Back shows the page again; search.js takes the new 返回 then
  p = load({ search: HERE, store: snapStore(HERE) });
  p.leave();
  p.expectReturn({ y: 1800, listTop: 320 });
  p.pageshow(true);
  out.bfcacheReturn = { searches: p.searches(), scrolls: p.scrolls.slice(), takes: p.takes() };

  console.log(JSON.stringify(out));
})().catch(err => { console.error(err && err.stack || err); process.exit(1); });
"""


def run_harness(script):
    """Run the harness against the search.js at `script` (a mutation check passes a scratch copy)."""
    result = subprocess.run([NODE, "-e", HARNESS, str(script)],
                            capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def harness():
    if NODE is None:
        pytest.skip("needs Node.js (CI installs it)")
    return run_harness(STATIC_JS / "search.js")


SEARCH_HERE = "/api/search?q=abc&page=1&page_size=20&sort=latest"


def test_a_snapshot_is_redrawn_at_the_return_place_without_searching(harness):
    r = harness["snapshotReturn"]
    assert r["searches"] == [] and r["drawn"] is True   # the snapshot's results, upstream not asked again
    assert r["scrolls"] == [[2600, 300]]                # where 返回 left off, not the snapshot's own 900 / 250
    assert r["takes"] == 1
    assert r["history"] is True                         # the search history is a separate request


def test_without_a_snapshot_the_search_runs_again_then_lands_in_place(harness):
    before = harness["researchBefore"]
    assert before["searches"] == [SEARCH_HERE] and before["spinner"] is True
    assert before["scrolls"] == []                      # nothing to land on until the results are drawn
    assert before["takes"] == 1 and before["left"] is False  # claimed as soon as the page opened
    after = harness["researchAfter"]
    assert after["drawn"] is True and after["scrolls"] == [[2600, 300]] and after["takes"] == 1


def test_a_slow_search_still_lands_at_the_return_place(harness):
    # the answer arrives 20 s later, when the 返回 in nav-memory would long be gone
    assert harness["slowResearch"] == {"scrolls": [[2600, 300]], "takes": 1}
    stale = harness["slowStaleSnapshot"]
    assert stale["searches"] == 1                       # results older than 12 h are not reused
    assert stale["scrolls"] == [[2600, 300]]            # ...but the 返回 beats the old snapshot's 900 / 250
    assert stale["takes"] == 1


def test_scrolling_while_the_search_runs_keeps_the_user_where_they_went(harness):
    assert harness["movedWhileSearching"] == {"scrolls": [], "y": 1200, "drawn": True}


def test_leaving_before_the_results_arrive_keeps_the_return_place(harness):
    assert harness["leftWhileSearching"] == ["/search?keyword=abc&sort=latest&page_size=20&page=1", 2600, 300, 2600, 300]


def test_a_return_to_the_top_lands_at_the_top(harness):
    # y 0 is a real place (the user was at the top), not "no return"
    assert harness["topReturn"] == [[0, 300]]


def test_after_landing_leaving_records_where_the_user_went(harness):
    # the 返回 place is not kept once the page has landed there
    moved = ["/search?keyword=abc&sort=latest&page_size=20&page=1", 3100, 300, 3100, 300]
    assert harness["movedOnAfterSnapshotReturn"] == moved
    assert harness["movedOnAfterResearchReturn"] == moved


def test_the_return_is_used_once(harness):
    # later pages and searches start at the top of their results
    after_snapshot = harness["snapshotReturnThenMore"]
    assert len(after_snapshot["searches"]) == 2 and "page=2" in after_snapshot["searches"][0]
    assert after_snapshot["scrolls"] == [[2600, 300]] and after_snapshot["takes"] == 1
    assert harness["researchThenMore"] == {"searches": 3, "scrolls": [[2600, 300]], "takes": 1}


def test_without_a_return_the_snapshot_place_is_used_as_before(harness):
    r = harness["snapshotNoReturn"]
    assert r["searches"] == [] and r["drawn"] is True
    assert r["scrolls"] == [[900, 250]]                 # the place seen when leaving, relative to the results
    fresh = harness["freshSearch"]
    assert fresh["searches"] == 1 and fresh["drawn"] is True
    assert fresh["scrolls"] == []                       # a plain search stays where the page is


def test_a_reload_lands_at_the_return_place_only_when_there_is_one(harness):
    assert harness["reloadNoReturn"] == [[400, 280]]    # F5: where the user was right before it
    assert harness["reloadReturn"] == [[2600, 300]]


def test_a_new_search_or_page_before_the_answer_cancels_the_return(harness):
    paged = harness["pagedBeforeAnswer"]
    assert len(paged["searches"]) == 2 and "page=2" in paged["searches"][1]
    assert paged["scrolls"] == [] and paged["drawn"] is True
    assert paged["late"] is False                       # the old page's late answer does not replace page 2
    assert paged["saved"][1:3] == [0, 300]              # page 2 remembers where the page is, not the 返回 place
    # a page change right after the results are drawn also beats the landing still waiting for its frames
    assert harness["pagedBeforeLanding"] == {"searches": 2, "scrolls": []}
    searched = harness["searchedBeforeAnswer"]
    assert searched["searches"][1].startswith("/api/search?q=xyz&page=1")
    assert searched["scrolls"] == []
    assert searched["saved"][:3] == ["/search?keyword=xyz&sort=latest&page_size=20&page=1", 0, 300]
    assert harness["sortedBeforeAnswer"] == {"searches": 2, "scrolls": []}


def test_pages_without_a_keyword_leave_the_return_to_nav_memory(harness):
    r = harness["noKeyword"]
    assert r["takes"] == 0 and r["left"] is True and r["searches"] == []
    # searching on that page afterwards does not pick it up either
    assert harness["noKeywordThenSearch"] == {"takes": 0, "scrolls": []}
    assert harness["noKeywordOtherParams"] == [[0, True, 0], [0, True, 0]]


def test_back_forward_cache_takes_the_new_return(harness):
    r = harness["bfcacheReturn"]
    assert r["searches"] == []                          # redrawn from the snapshot kept when leaving
    assert r["scrolls"] == [[900, 250], [1800, 320]]    # first visit: snapshot place; shown again by 返回: its place
    assert r["takes"] == 2                              # once per showing of the page
