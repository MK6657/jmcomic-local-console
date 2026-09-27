"""资源库 card blank-space click: the 点击资源库卡片空白处打开详情 setting in static/js/library.js.

The grid click handler and the re-reading of the setting run in Node with a stand-in window/document
(skipped when Node is missing; CI installs it). The server side, the rendered data-card-click and the
settings switch are covered by tests/test_list_options.py.
"""
import json
import shutil
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parent.parent
LIBRARY_JS = ROOT / "static" / "js" / "library.js"
NODE = shutil.which("node")

HARNESS = r"""
const src = require('fs').readFileSync(process.argv[1], 'utf8');
let now = 1_000_000_000_000;
const FakeDate = { now: () => now };
// lets settled promise callbacks run (no real waiting)
const flush = () => new Promise(resolve => setImmediate(resolve));
const ok = value => ({ status: 'ok', settings: { library_card_click: value } });

// the selectors library.js asks closest() about on the card grid
function matchOne(node, sel) {
  if (sel === '[data-action]') return node.dataset.action !== undefined;
  if (sel[0] === '.') return node.classList.contains(sel.slice(1));
  if (/^[a-z]+$/.test(sel)) return node.tagName === sel.toUpperCase();
  throw new Error('selector not modelled by the harness: ' + sel);
}

// generic stand-in element: enough for everything library.js does at load and when it draws cards
function makeEl(tag) {
  const classes = new Set(), attrs = {}, handlers = {};
  let text = '';
  const detach = () => { e.children.forEach(k => { if (k && typeof k === 'object') k.parentNode = null; }); e.children = []; };
  const e = {
    tagName: String(tag).toUpperCase(), parentNode: null, children: [], handlers,
    // like the browser's DOMStringMap: every value is stored as a string
    dataset: new Proxy({}, { set: (t, k, v) => { t[k] = String(v); return true; } }),
    style: {}, hidden: false, value: '', options: [], focusCount: 0,
    get className() { return Array.from(classes).join(' '); },
    set className(v) { classes.clear(); String(v).split(/\s+/).forEach(c => { if (c) classes.add(c); }); },
    classList: {
      add: (...names) => names.forEach(n => classes.add(n)),
      remove: (...names) => names.forEach(n => classes.delete(n)),
      toggle: (n, force) => { const want = force === undefined ? !classes.has(n) : !!force; if (want) classes.add(n); else classes.delete(n); return want; },
      contains: n => classes.has(n),
    },
    get textContent() { return text; },
    set textContent(v) { detach(); text = String(v); },
    get innerHTML() { return text; },
    set innerHTML(v) { detach(); text = String(v); },
    setAttribute: (k, v) => { attrs[k] = String(v); },
    getAttribute: k => (k in attrs ? attrs[k] : null),
    removeAttribute: k => { delete attrs[k]; },
    addEventListener: (type, fn) => { (handlers[type] = handlers[type] || []).push(fn); },
    appendChild(child) {
      const kids = child && child.tagName === '#FRAGMENT' ? child.children.splice(0) : [child];
      kids.forEach(k => { if (k && typeof k === 'object') k.parentNode = e; e.children.push(k); });
      return child;
    },
    querySelector: () => null,
    querySelectorAll: () => [],
    contains(node) { for (let x = node; x; x = x.parentNode) if (x === e) return true; return false; },
    matches: sel => matchOne(e, sel),
    closest(sel) {
      const parts = sel.split(',').map(s => s.trim());
      for (let x = e; x; x = x.parentNode) if (x.matches && parts.some(s => matchOne(x, s))) return x;
      return null;
    },
    focus() { e.focusCount++; },
    getBoundingClientRect: () => ({ top: 0 }),
    scrollIntoView() {},
  };
  return e;
}

const find = (root, test) => {
  for (const k of (root && root.children) || []) {
    if (k && k.classList && test(k)) return k;
    const deeper = find(k, test);
    if (deeper) return deeper;
  }
  return null;
};
const hasClass = name => x => x.classList.contains(name);

// opts: cardClick (data-card-click, undefined = no attribute), navType, navMemory (false = not loaded), search
function page(opts) {
  opts = opts || {};
  const on = { window: {}, document: {} };
  const fetches = [], opens = [], confirms = [], toasts = [], timers = [], intervals = [];
  const els = {};
  const win = {
    location: { pathname: '/library', search: opts.search || '', hash: '' },
    history: {
      state: null,
      replaceState(state, title, url) { const i = url.indexOf('?'); win.location.search = i === -1 ? '' : url.slice(i); },
    },
    // every request stays pending until the scenario answers it; like utils.js apiFetch, a new request
    // with the same abortKey aborts the older one still in flight
    apiFetch: (url, o) => new Promise((resolve, reject) => {
      o = o || {};
      if (o.abortKey) fetches.forEach(f => {
        if (f.done || f.opts.abortKey !== o.abortKey) return;
        const err = new Error('aborted'); err.name = 'AbortError';
        f.done = true; f.aborted = true; f.reject(err);
      });
      fetches.push({ url, opts: o, resolve, reject, done: false, aborted: false });
    }),
    // utils.js readLink.create: a link with an icon (the 'read-link' class only lets the harness find it)
    readLink: { create: () => { const a = makeEl('a'); a.className = 'btn btn-sm read-link'; a.appendChild(makeEl('i')); return a; } },
    open: (...args) => { opens.push(args); return null; },
    requestAnimationFrame: fn => fn(),
    performance: { getEntriesByType: () => [{ type: opts.navType || 'navigate' }] },
    addEventListener: (type, fn) => { (on.window[type] = on.window[type] || []).push(fn); },
  };
  if (opts.navMemory !== false) win.navMemory = { navigationType: () => opts.navType || 'navigate', restoreScroll: () => {} };
  const doc = {
    hidden: false, visibilityState: 'visible', activeElement: null,
    getElementById: id => els[id] || (els[id] = makeEl('div')),
    createElement: tag => makeEl(tag),
    createDocumentFragment: () => makeEl('#fragment'),
    createTextNode: text => ({ nodeType: 3, textContent: String(text) }),
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener: (type, fn) => { (on.document[type] = on.document[type] || []).push(fn); },
  };
  const grid = els['library-grid'] = makeEl('div');
  grid.className = 'library-grid' + (opts.cardClick === 'true' ? ' library-grid--card-click' : '');
  if (opts.cardClick !== undefined) grid.dataset.cardClick = opts.cardClick;
  // fake timers: collected, never run by themselves
  const setT = (fn, ms) => timers.push({ fn, ms });
  const clearT = id => { if (timers[id - 1]) timers[id - 1].fn = null; };
  const setI = (fn, ms) => intervals.push({ fn, ms });
  const clearI = id => { if (intervals[id - 1]) intervals[id - 1].fn = null; };
  const confirmStub = message => { confirms.push(message); return true; };
  const showToast = (message, type) => toasts.push([message, type]);
  new Function('window', 'document', 'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'confirm', 'showToast', 'Date', src)(
    win, doc, setT, clearT, setI, clearI, confirmStub, showToast, FakeDate);

  const settle = (url, how) => {
    let n = 0;
    for (const f of fetches) {
      if (f.done || !(f.url === url || (url.endsWith('?') && f.url.startsWith(url)))) continue;
      f.done = true; how(f); n++;
    }
    return n;
  };
  const p = {
    win, doc, grid, fetches, opens, confirms, timers, intervals,
    fire(where, type, event) { (on[where][type] || []).forEach(fn => fn(Object.assign({ type }, event))); },
    // a click somewhere in the grid; returns the window.open calls it made
    click(target) {
      if (!target) return 'no target';
      const before = opens.length;
      // a plain left click with the mouse
      const ev = { type: 'click', target, detail: 1, button: 0, ctrlKey: false, metaKey: false, shiftKey: false, altKey: false,
                   defaultPrevented: false, stopPropagation() {}, preventDefault() { this.defaultPrevented = true; } };
      (grid.handlers.click || []).forEach(fn => fn(ev));
      return opens.slice(before);
    },
    // a card shaped like buildCard's, plus form controls a card might grow later
    card(albumId) {
      const n = (parent, tag, cls, data, attrs) => {
        const x = makeEl(tag);
        if (cls) x.className = cls;
        Object.assign(x.dataset, data || {});
        Object.entries(attrs || {}).forEach(([k, v]) => x.setAttribute(k, v));
        parent.appendChild(x);
        return x;
      };
      const c = {};
      c.card = n(grid, 'div', 'library-card', { albumId });
      c.coverLink = n(c.card, 'a', 'card-cover-link');
      c.coverImg = n(c.coverLink, 'img', 'card-cover thumb-img');
      c.body = n(c.card, 'div', 'card-body');
      c.title = n(c.body, 'div', 'card-title');
      c.titleLink = n(c.title, 'a', 'text-decoration-none text-dark');
      c.author = n(c.body, 'div', 'card-author');
      c.authorIcon = n(c.author, 'i', 'bi bi-person');
      c.authorBtn = n(c.author, 'button', 'library-author-link', { action: 'filter-author', author: 'Alice' });
      c.tags = n(c.body, 'div', 'card-tags');
      c.tagBadge = n(c.tags, 'span', 'tag-badge tag-source-user');
      c.tagRemove = n(c.tagBadge, 'button', 'tag-remove', { action: 'remove-tag', tag: 'foo' });
      c.actions = n(c.body, 'div', 'card-actions');
      c.statusBadge = n(c.actions, 'span', 'badge bg-secondary');
      c.buttons = n(c.actions, 'div', 'card-action-buttons');
      c.info = n(c.buttons, 'a', 'btn btn-outline-info btn-sm');
      c.infoIcon = n(c.info, 'i', 'bi bi-info-circle');
      c.star = n(c.buttons, 'button', 'btn btn-sm btn-outline-warning', { action: 'toggle-wishlist' }, { 'aria-pressed': 'false' });
      c.starIcon = n(c.star, 'i', 'bi bi-star');
      c.read = n(c.buttons, 'a', 'btn btn-sm read-link');
      c.plainButton = n(c.buttons, 'button', 'btn btn-sm');
      c.input = n(c.body, 'input', 'form-control');
      c.select = n(c.body, 'select', 'form-select');
      c.textarea = n(c.body, 'textarea', 'form-control');
      return c;
    },
    respond: (url, value) => settle(url, f => f.resolve(value)),
    fail: (url, err) => settle(url, f => f.reject(err)),
    settingsRequests: () => fetches.filter(f => f.url === '/api/settings').map(f => f.opts),
    newRequests: from => fetches.slice(from).map(f => ({
      url: f.url, method: f.opts.method || 'GET', body: f.opts.body || null, abortKey: f.opts.abortKey || null })),
    // what the grid says and what a click on blank card text does right now
    state: () => ({
      attr: grid.dataset.cardClick === undefined ? null : grid.dataset.cardClick,
      cls: grid.classList.contains('library-grid--card-click'),
      opens: p.click(p.card('123').body),
    }),
  };
  return p;
}

const BLANK = ['body', 'title', 'author', 'authorIcon', 'tags', 'tagBadge', 'actions', 'statusBadge', 'buttons'];
const CONTROLS = ['titleLink', 'coverLink', 'coverImg', 'info', 'infoIcon', 'read', 'plainButton', 'input', 'select', 'textarea'];
const TRIGGERS = {
  visibility: p => { p.doc.visibilityState = 'visible'; p.doc.hidden = false; p.fire('document', 'visibilitychange'); },
  // same, but keyboard focus is still on a card link (the list skips its reload then; the setting must not)
  visibilityFocusInGrid: p => { p.doc.activeElement = p.card('9').titleLink; TRIGGERS.visibility(p); },
  focus: p => p.fire('window', 'focus'),
  pageshow: p => p.fire('window', 'pageshow', { persisted: true }),
};
const clickEach = (p, names) => Object.fromEntries(names.map(k => [k, p.click(p.card('123')[k])]));
const listReply = albumId => ({ status: 'ok', total: 1, items: [{
  album_id: albumId, title: 'T', author: 'Alice', cover_url: '/c.jpg', tags: [{ tag: 'foo', source: 'user' }],
  status_group: 'none', is_wishlisted: false, readable: false }] });
const cardIds = p => p.grid.children.filter(k => k.classList && k.classList.contains('library-card')).map(k => k.dataset.albumId);

(async () => {
  const out = {};

  // the setting as rendered into the page (data-card-click on #library-grid)
  out.initial = {};
  for (const [name, value] of [['true', 'true'], ['false', 'false'], ['missing', undefined], ['empty', '']]) {
    out.initial[name] = page({ cardClick: value }).state();
  }

  // blank card text, links / buttons / form controls, and clicks outside a card body; setting off and on
  for (const [name, value] of [['off', 'false'], ['on', 'true']]) {
    const p = page({ cardClick: value });
    const r = out[name] = {};
    const before = p.fetches.length;
    r.blank = clickEach(p, BLANK);
    r.controls = clickEach(p, CONTROLS);
    r.outside = { gridGap: p.click(p.grid), cardPadding: p.click(p.card('123').card) };
    r.requests = p.newRequests(before);
    // the card's own action buttons (the author filter last: it redraws the grid)
    const c = p.card('123');
    let from = p.fetches.length;
    r.wishlist = { opens: p.click(c.starIcon), requests: p.newRequests(from) };
    from = p.fetches.length;
    r.removeTag = { opens: p.click(c.tagRemove), confirms: p.confirms.slice(), requests: p.newRequests(from) };
    from = p.fetches.length;
    r.author = { opens: p.click(c.authorBtn), requests: p.newRequests(from), search: p.win.location.search };
  }

  // coming back to the page re-reads the setting: on, then off again
  out.refresh = {};
  for (const [name, trigger] of Object.entries(TRIGGERS)) {
    const p = page({ cardClick: 'false' });
    const r = out.refresh[name] = {};
    trigger(p);
    r.requests = p.settingsRequests();
    r.answered = p.respond('/api/settings', ok('true'));
    await flush();
    r.on = p.state();
    trigger(p);
    p.respond('/api/settings', ok('false'));
    await flush();
    r.off = p.state();
    r.count = p.settingsRequests().length;
  }
  {
    const p = page({ cardClick: 'false' });
    p.doc.visibilityState = 'hidden'; p.doc.hidden = true; p.fire('document', 'visibilitychange');
    p.fire('window', 'pageshow', { persisted: false });   // a normal load, not the back/forward cache
    out.noRefresh = p.settingsRequests().length;
  }

  // a re-read of the setting and a list load in flight never cancel each other
  {
    const p = page({ cardClick: 'false' });
    const r = out.inFlight = {};
    TRIGGERS.focus(p);                                     // while the first list load is still out
    r.listAnswered = p.respond('/api/library?', listReply(456));
    await flush();
    r.cards = cardIds(p);
    const from = p.fetches.length;
    p.intervals.forEach(t => t.fn && t.fn());              // the 15 s auto refresh reloads the list meanwhile
    r.reload = p.newRequests(from).map(q => q.url.split('?')[0]);
    r.settingsAnswered = p.respond('/api/settings', ok('true'));
    await flush();
    r.state = p.state();
  }

  // an empty filtered list: the 清除筛选 button in the grid still clears, the message itself opens nothing
  out.empty = {};
  for (const [name, value] of [['off', 'false'], ['on', 'true']]) {
    const p = page({ cardClick: value, search: '?q=x' });
    const r = out.empty[name] = {};
    p.respond('/api/library?', { status: 'ok', total: 0, items: [] });
    await flush();
    const box = find(p.grid, hasClass('empty-library'));
    const clear = box && find(box, x => x.dataset.action === 'clear-filters');
    r.found = [!!box, !!clear];
    r.opens = [box, box && box.children.find(k => k.tagName === 'P')].map(t => p.click(t));
    const from = p.fetches.length;
    r.clearOpens = p.click(clear);
    r.requests = p.newRequests(from).map(q => q.method + ' ' + q.url);
    r.search = p.win.location.search;
  }

  // failed or unusable answers keep whatever the page had
  out.failures = {};
  for (const [name, value] of [['on', 'true'], ['off', 'false']]) {
    const p = page({ cardClick: value });
    const flip = value === 'true' ? 'false' : 'true';
    const r = out.failures[name] = {};
    TRIGGERS.visibility(p);
    p.fail('/api/settings', new Error('offline'));
    await flush();
    r.rejected = p.state();
    TRIGGERS.focus(p);
    const abort = new Error('aborted'); abort.name = 'AbortError';
    p.fail('/api/settings', abort);
    await flush();
    r.aborted = p.state();
    TRIGGERS.pageshow(p);
    p.respond('/api/settings', { status: 'error', message: 'x', settings: { library_card_click: flip } });
    await flush();
    r.notOk = p.state();
    TRIGGERS.visibility(p);
    p.respond('/api/settings', { status: 'ok' });
    await flush();
    r.noSettings = p.state();
    r.requests = p.settingsRequests().length;
  }

  // startup: Back / Forward without the back/forward cache may show an old copy of the page
  out.startup = {};
  for (const [name, navType, value, answer] of [
    ['navigate', 'navigate', 'false', 'true'], ['reload', 'reload', 'false', 'true'],
    ['backForward', 'back_forward', 'false', 'true'], ['backForwardOff', 'back_forward', 'true', 'false']]) {
    const p = page({ cardClick: value, navType });
    const r = out.startup[name] = { requests: p.settingsRequests().length };
    p.respond('/api/settings', ok(answer));
    await flush();
    r.state = p.state();
  }
  out.startup.noNavMemory = page({ cardClick: 'false', navMemory: false }).settingsRequests().length;

  // the card library.js itself draws behaves like the stand-in card
  {
    const p = page({ cardClick: 'true' });
    p.respond('/api/library?', listReply(456));
    await flush();
    const card = find(p.grid, hasClass('library-card'));
    const body = find(card, hasClass('card-body'));
    const title = find(body, hasClass('card-title'));
    const r = out.rendered = { card: card ? [card.className, card.dataset.albumId, p.grid.contains(card)] : null };
    r.blank = [body, title, find(body, hasClass('card-author')), find(body, hasClass('card-tags')),
               find(body, hasClass('tag-badge')), find(body, hasClass('card-actions'))].map(t => p.click(t));
    const read = find(body, hasClass('read-link'));
    r.links = [title && title.children[0], find(card, hasClass('card-cover-link')), find(card, hasClass('card-cover')),
               find(body, hasClass('btn-outline-info')), read, read && read.children[0]].map(t => p.click(t));
    const from = p.fetches.length;
    r.actions = ['toggle-wishlist', 'remove-tag', 'filter-author'].map(a => p.click(find(body, x => x.dataset.action === a)));
    r.actionRequests = p.newRequests(from).map(q => q.method + ' ' + q.url);
  }

  console.log(JSON.stringify(out));
})().catch(err => { console.error(err && err.stack || err); process.exit(1); });
"""


def run_harness(script):
    """Run HARNESS against a library.js (the real one, or a scratch copy for mutation checks)."""
    result = subprocess.run([NODE, "-e", HARNESS, str(script)],
                            capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def harness():
    if NODE is None:
        pytest.skip("needs Node.js (CI installs it)")
    return run_harness(LIBRARY_JS)


OPEN = [["/album/123", "_blank", "noopener"]]  # one new tab with the detail page, no opener
ON = {"attr": "true", "cls": True, "opens": OPEN}
OFF = {"attr": "false", "cls": False, "opens": []}
BLANK = ["body", "title", "author", "authorIcon", "tags", "tagBadge", "actions", "statusBadge", "buttons"]
CONTROLS = ["titleLink", "coverLink", "coverImg", "info", "infoIcon", "read", "plainButton", "input", "select", "textarea"]


def _query(url):
    parts = urlsplit(url)
    return parts.path, parse_qs(parts.query)


def test_the_initial_state_comes_from_data_card_click(harness):
    assert harness["initial"]["true"] == ON
    assert harness["initial"]["false"] == OFF
    # anything but "true" keeps the default (off)
    assert harness["initial"]["missing"] == {"attr": None, "cls": False, "opens": []}
    assert harness["initial"]["empty"] == {"attr": "", "cls": False, "opens": []}


def test_blank_space_opens_nothing_when_the_setting_is_off(harness):
    assert harness["off"]["blank"] == {name: [] for name in BLANK}


def test_blank_space_opens_the_detail_page_once_when_the_setting_is_on(harness):
    assert harness["on"]["blank"] == {name: OPEN for name in BLANK}


@pytest.mark.parametrize("state", ["off", "on"])
def test_links_buttons_and_form_controls_never_open_from_blank_space(harness, state):
    # they do their own thing (a link opens its own tab): never a second tab from the card
    assert harness[state]["controls"] == {name: [] for name in CONTROLS}
    assert harness[state]["requests"] == []  # and none of these clicks is an action


@pytest.mark.parametrize("state", ["off", "on"])
def test_clicks_outside_a_card_body_open_nothing(harness, state):
    assert harness[state]["outside"] == {"gridGap": [], "cardPadding": []}


@pytest.mark.parametrize("state", ["off", "on"])
def test_card_action_buttons_still_run_their_own_action(harness, state):
    result = harness[state]
    # 作者: the list reloads filtered by that author and the address keeps it
    author = result["author"]
    assert author["opens"] == []
    assert len(author["requests"]) == 1
    path, query = _query(author["requests"][0]["url"])
    assert path == "/api/library" and query["author"] == ["Alice"] and query["page"] == ["1"]
    assert author["search"] == "?author=Alice"
    # tag ×: asks first, then deletes that tag from that album
    remove = result["removeTag"]
    assert remove["opens"] == []
    assert len(remove["confirms"]) == 1 and '"foo"' in remove["confirms"][0]
    assert [(r["url"], r["method"], json.loads(r["body"])) for r in remove["requests"]] == [
        ("/api/library/123/tags", "DELETE", {"tags": ["foo"]})]
    # star (clicked on its icon): adds to favourites
    wishlist = result["wishlist"]
    assert wishlist["opens"] == []
    assert [(r["url"], r["method"]) for r in wishlist["requests"]] == [("/api/wishlist", "POST")]
    assert json.loads(wishlist["requests"][0]["body"])["album_id"] == "123"


@pytest.mark.parametrize("trigger", ["visibility", "visibilityFocusInGrid", "focus", "pageshow"])
def test_coming_back_to_the_page_rereads_the_setting(harness, trigger):
    # visibilitychange to visible (also while keyboard focus is on a card link), a window focus,
    # a pageshow from the back/forward cache
    result = harness["refresh"][trigger]
    # answered == 1: the request was still in flight (not aborted by the list reload that may follow)
    assert len(result["requests"]) == 1 and result["answered"] == 1
    assert result["requests"][0].get("abortKey") != "library-list"  # never shares the list request's key
    assert result["on"] == ON
    assert result["off"] == OFF
    assert result["count"] == 2


def test_a_hidden_tab_or_a_fresh_pageshow_does_not_reread_the_setting(harness):
    assert harness["noRefresh"] == 0


def test_rereading_the_setting_and_loading_the_list_do_not_cancel_each_other(harness):
    result = harness["inFlight"]
    # the re-read went out while the first list load was pending: that list still arrives and is drawn
    assert result["listAnswered"] == 1 and result["cards"] == ["456"]
    # the auto refresh then reloads the list while the re-read is pending: the re-read still lands
    assert result["reload"] == ["/api/library"]
    assert result["settingsAnswered"] == 1 and result["state"] == ON


@pytest.mark.parametrize("state", ["off", "on"])
def test_the_empty_list_message_opens_nothing_and_its_clear_button_still_works(harness, state):
    result = harness["empty"][state]
    assert result["found"] == [True, True]
    assert result["opens"] == [[], []]  # the message box and its text
    # 清除筛选: no tab, the list reloads without the search and the address drops it
    assert result["clearOpens"] == []
    assert len(result["requests"]) == 1 and result["requests"][0].startswith("GET /api/library?")
    _, query = _query(result["requests"][0].split(" ", 1)[1])
    assert "q" not in query and query["page"] == ["1"]
    assert result["search"] == ""


@pytest.mark.parametrize("start", ["on", "off"])
def test_a_failed_or_unusable_answer_keeps_the_state(harness, start):
    result = harness["failures"][start]
    assert result["requests"] == 4  # each case really asked the server
    expected = ON if start == "on" else OFF
    for case in ("rejected", "aborted", "notOk", "noSettings"):
        assert result[case] == expected, case


def test_back_forward_startup_rereads_the_setting(harness):
    startup = harness["startup"]
    assert startup["backForward"] == {"requests": 1, "state": ON}
    assert startup["backForwardOff"] == {"requests": 1, "state": OFF}
    # an ordinary load or a reload already has the current page from the server
    assert startup["navigate"] == {"requests": 0, "state": OFF}
    assert startup["reload"] == {"requests": 0, "state": OFF}
    assert startup["noNavMemory"] == 0


def test_the_card_library_js_draws_behaves_like_the_stand_in_card(harness):
    rendered = harness["rendered"]
    assert rendered["card"] == ["library-card", "456", True]
    opened = [["/album/456", "_blank", "noopener"]]
    assert rendered["blank"] == [opened] * 6
    assert rendered["links"] == [[]] * 6  # title, cover link, cover image, 详情, 阅读 and its icon
    assert rendered["actions"] == [[]] * 3
    requests = rendered["actionRequests"]
    assert requests[:2] == ["POST /api/wishlist", "DELETE /api/library/456/tags"]
    assert len(requests) == 3 and requests[2].startswith("GET /api/library?") and "author=Alice" in requests[2]
