"""How the floating quick-nav behaves on the page (the UI half of static/js/quick-nav.js, after "// ── 界面 ──").

Runs in Node with a stand-in #quick-nav, document and window (skipped when Node is missing; CI installs it).
Timers, Date.now and animation frames are fake, so nothing waits on real time. The step-back logic itself is
covered by tests/test_quick_nav.py.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC_JS = ROOT / "static" / "js"
NODE = shutil.which("node")

OPEN = [True, "true"]     # [root has .is-open, toggle aria-expanded]
CLOSED = [False, "false"]
NO_BACK_LABEL = "返回（还没有用快捷导航跳转过）"

HARNESS = r"""
const src = require('fs').readFileSync(process.argv[1], 'utf8');
const HOUR = 3600 * 1000;
let now = 1_000_000_000_000;
const FakeDate = { now: () => now };
const ORIGIN = 'http://127.0.0.1:5000';
const KEY = 'jm-quick-nav-back-v1';
const DESTS = ['/', '/search', '/downloads', '/wishlist', '/library', '/settings'];
const REMEMBERED = ['/search', '/wishlist', '/library'];  // carry data-nav-memory like the top links
const step = (url, title, y) => ({ url, y: y || 0, listTop: null, title, key: null, savedAt: now });

// only the two selectors quick-nav.js asks closest() for
const matches = (n, sel) => (sel === 'a[data-quick-dest]' && n.tag === 'a' && 'data-quick-dest' in n.attrs)
  || (sel === 'button[data-quick-action]' && n.tag === 'button' && 'data-quick-action' in n.attrs);

function node(tag, attrs, parent) {
  return {
    tag, attrs: Object.assign({}, attrs), parent: parent || null, title: '', on: {},
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    addEventListener(type, fn) { (this.on[type] = this.on[type] || []).push(fn); },
    closest(sel) { for (let n = this; n; n = n.parent) if (matches(n, sel)) return n; return null; },
  };
}

// opts: path, search, title, scrollY, innerHeight, scrollHeight, panelHeight, stack (steps to seed), resizeObserver
function page(opts) {
  opts = opts || {};
  const store = new Map();
  if (opts.stack) store.set(KEY, JSON.stringify(opts.stack));
  const calls = [];     // scrollTo / expectReturn / assign / history.back, in order
  const cssVars = [];   // document.documentElement.style.setProperty(...)
  const focused = [];   // toggle.focus()
  let timers = [];
  let seq = 0;
  const setT = (fn, ms) => { const id = ++seq; timers.push({ id, fn, due: now + (ms || 0) }); return id; };
  const clearT = id => { timers = timers.filter(t => t.id !== id); };

  // #quick-nav: toggle + panel (返回 / 到顶 / 到底, then the six destinations)
  const body = node('body', {});
  const root = node('div', { id: 'quick-nav' }, body);
  const classes = new Set(['quick-nav']);
  root.classList = {
    contains: c => classes.has(c),
    toggle: (c, force) => { const on = force === undefined ? !classes.has(c) : !!force; if (on) classes.add(c); else classes.delete(c); return on; },
  };
  root.contains = x => { for (let n = x; n; n = n.parent) if (n === root) return true; return false; };
  const toggle = node('button', { class: 'quick-nav-toggle', 'aria-expanded': 'false' }, root);
  toggle.focus = () => focused.push('toggle');
  const panel = node('div', { id: 'quick-nav-panel' }, root);
  panel.offsetHeight = opts.panelHeight || 236;
  const buttons = {};
  ['back', 'top', 'bottom'].forEach(a => {
    buttons[a] = node('button', Object.assign({ 'data-quick-action': a }, a === 'back' ? { 'aria-disabled': 'true' } : {}), panel);
  });
  const bottomIcon = node('i', {}, buttons.bottom);
  const links = {};
  const linkLabels = {};  // <a …><i class="bi …"></i><span>资源库</span></a>: a mouse click usually lands on these
  DESTS.forEach(d => {
    links[d] = node('a', Object.assign({ href: d, 'data-quick-dest': d }, REMEMBERED.includes(d) ? { 'data-nav-memory': d } : {}), panel);
    node('i', {}, links[d]);
    linkLabels[d] = node('span', {}, links[d]);
  });
  const outside = node('button', {}, body);
  // the page's #toast-container (base.html) with one toast's close button
  const toastBox = node('div', { id: 'toast-container' }, body);
  toastBox.contains = x => { for (let n = x; n; n = n.parent) if (n === toastBox) return true; return false; };
  const toastClose = node('button', {}, node('div', {}, toastBox));
  const state = { focusVisible: null };  // the element matching :focus-visible inside #quick-nav, if any
  const found = new Map([
    ['.quick-nav-toggle', toggle], ['.quick-nav-panel', panel], ['[data-quick-action="back"]', buttons.back],
    ['[data-quick-action="top"]', buttons.top], ['[data-quick-action="bottom"]', buttons.bottom],
  ]);
  root.querySelector = sel => (sel === ':focus-visible' ? state.focusVisible : found.get(sel) || null);

  const doc = {
    title: opts.title || '下载管理 - JMComic 下载控制台', body, activeElement: body, on: {},
    documentElement: { scrollHeight: opts.scrollHeight || 0, style: { setProperty: (k, v) => cssVars.push([k, v]) } },
    getElementById: id => (id === 'quick-nav' ? root : (id === 'toast-container' ? toastBox : null)),
    querySelector: () => null,
    addEventListener(type, fn) { (this.on[type] = this.on[type] || []).push(fn); },
  };
  const path = opts.path || '/downloads';
  const win = {
    on: {}, scrollY: opts.scrollY || 0, innerHeight: opts.innerHeight || 800,
    addEventListener(type, fn) { (this.on[type] = this.on[type] || []).push(fn); },
    scrollTo: o => { calls.push(['scrollTo', o.top, o.behavior]); win.scrollY = o.top; },
    requestAnimationFrame: fn => fn(),
    sessionStorage: {
      getItem: k => store.has(k) ? store.get(k) : null,
      setItem: (k, v) => store.set(k, String(v)),
      removeItem: k => store.delete(k),
    },
    location: {
      pathname: path, search: opts.search || '', origin: ORIGIN, href: ORIGIN + path + (opts.search || ''),
      assign: url => calls.push(['assign', url]),
    },
    history: { back: () => calls.push(['back']) },
    navMemory: {
      MAX_AGE: 12 * HOUR,
      listTopNow: () => null,
      expectReturn: (url, y, listTop) => calls.push(['expect', url, y, listTop]),
    },
  };
  let observer = null;
  if (opts.resizeObserver) win.ResizeObserver = function (cb) { this.observe = target => { observer = { cb, target }; }; };

  new Function('window', 'document', 'setTimeout', 'clearTimeout', 'Date', src)(win, doc, setT, clearT, FakeDate);

  const fire = (target, type, event) => (target.on[type] || []).slice().forEach(fn => fn(event || {}));
  return {
    win, doc, calls, focused, root, toggle, panel, buttons, bottomIcon, links, linkLabels, outside, body, state,
    shown: () => [classes.has('is-open'), toggle.getAttribute('aria-expanded')],
    disabled: () => ['back', 'top', 'bottom'].map(a => buttons[a].getAttribute('aria-disabled')),
    backLabel: () => [classes.has('has-back'), buttons.back.getAttribute('aria-label'), buttons.back.title],
    stack: () => JSON.parse(store.get(KEY) || '[]'),
    // the last --quick-nav-panel-h written (style.css only reads it while the panel is open)
    panelVar: () => { const w = cssVars.filter(c => c[0] === '--quick-nav-panel-h').pop(); return w ? w[1] : null; },
    // move the clock forward, running every timer that falls due on the way, in order
    advance(ms) {
      const end = now + ms;
      for (;;) {
        const due = timers.filter(t => t.due <= end).sort((a, b) => a.due - b.due || a.id - b.id)[0];
        if (!due) break;
        timers = timers.filter(t => t !== due);
        now = due.due;
        due.fn();
      }
      now = end;
    },
    enter: type => fire(root, 'pointerenter', { pointerType: type || 'mouse' }),
    leave: type => fire(root, 'pointerleave', { pointerType: type || 'mouse' }),
    // the mouse leaves #quick-nav onto `to` (a toast, the page…)
    leaveTo: to => fire(root, 'pointerleave', { pointerType: 'mouse', relatedTarget: to }),
    toastEnter: () => fire(toastBox, 'pointerenter', { pointerType: 'mouse' }),
    toastLeaveTo: to => fire(toastBox, 'pointerleave', { pointerType: 'mouse', relatedTarget: to }),
    toastClose,
    // clicks bubble: the toggle / panel first, then #quick-nav itself
    clickToggle() { const e = { target: toggle, button: 0 }; fire(toggle, 'click', e); fire(root, 'click', e); },
    click(target, mods) {
      const e = Object.assign({ target, button: 0, ctrlKey: false, shiftKey: false, metaKey: false, altKey: false }, mods);
      fire(panel, 'click', e); fire(root, 'click', e);
    },
    key: k => fire(doc, 'keydown', { key: k }),
    // pointerdown / pointerup reach the document's capture listeners first, then #quick-nav when they happened inside
    press(target, type) {
      type = type || 'pointerdown';
      fire(doc, type, { target });
      if (root.contains(target)) fire(root, type, { target });
    },
    focusOut: next => fire(root, 'focusout', { relatedTarget: next }),
    scroll(y) { win.scrollY = y; fire(win, 'scroll'); },
    resize: () => fire(win, 'resize'),
    // watching <body> or <html> both see the page grow
    observed: () => !!observer && (observer.target === body || observer.target === doc.documentElement),
    bodyResized: () => observer && observer.cb([]),
    pagehide: () => fire(win, 'pagehide', { persisted: true }),
    pageshow: () => fire(win, 'pageshow', { persisted: true }),
  };
}

const out = {};
let p;

// a fresh page: closed, nothing to go back to, nothing to scroll
p = page();
out.initial = [p.shown(), p.disabled(), p.backLabel()];

// mouse hover: opens on enter, closes a moment after leaving; coming back in time keeps it open
p = page();
p.enter();
out.hoverOpen = p.shown();
p.leave();
out.hoverJustLeft = p.shown();
p.advance(299);
out.hoverAlmost = p.shown();
p.advance(1);
out.hoverClosed = p.shown();
p.enter(); p.leave(); p.advance(200); p.enter(); p.advance(1000);
out.hoverReentered = p.shown();

// keyboard focus inside a hovered panel keeps it open when the mouse leaves
p = page();
p.enter(); p.state.focusVisible = p.links['/library']; p.leave(); p.advance(1000);
out.keyboardKeepsOpen = p.shown();
p.state.focusVisible = null; p.leave(); p.advance(300);
out.keyboardGone = p.shown();

// touch and pen do not open on enter (their tap is the toggle's click)
p = page();
p.enter('touch');
out.touchEnter = p.shown();
p.enter('pen');
out.penEnter = p.shown();

// the toggle opens and keeps it open; the mouse leaving does not close it; a second click closes
p = page();
p.clickToggle();
out.clickOpen = p.shown();
p.leave(); p.advance(1000);
out.clickSurvivesLeave = p.shown();
p.clickToggle();
out.clickClosed = p.shown();

// clicking while hover-opened pins it instead of closing
p = page();
p.enter(); p.clickToggle();
out.pinned = p.shown();
p.leave(); p.advance(1000);
out.pinnedAfterLeave = p.shown();

// Escape closes; focus goes back to the toggle only when it was inside
p = page();
p.clickToggle(); p.doc.activeElement = p.body; p.key('Escape');
out.escapeOutside = [p.shown(), p.focused.length];
p.clickToggle(); p.doc.activeElement = p.links['/settings']; p.key('Enter');
out.otherKey = p.shown();
p.key('Escape');
out.escapeInside = [p.shown(), p.focused.slice()];
p.key('Escape');
out.escapeWhenClosed = p.focused.length;

// pressing elsewhere closes, pressing on the panel does not
p = page();
p.clickToggle(); p.press(p.links['/']); p.press(p.panel);
out.pressInside = p.shown();
p.press(p.outside);
out.pressOutside = p.shown();

// focus leaving: to an element outside closes, within the quick-nav does not
p = page();
p.clickToggle(); p.focusOut(p.links['/search']);
out.focusToInside = p.shown();
p.focusOut(p.outside);
out.focusToOutside = p.shown();
// focus lost to nowhere (Tab out of the page, another window): closes, unless it happens during a press on the
// quick-nav itself (a click on the panel's blank space moves focus to <body>: with a mouse on press, on touch after lifting)
p.clickToggle(); p.focusOut(null);
out.focusLostNoPress = p.shown();
p.clickToggle(); p.press(p.panel); p.advance(1500); p.focusOut(null);   // mouse held down on blank panel space
out.focusLostAfterPress = p.shown();
p.press(p.panel, 'pointerup'); p.click(p.panel); p.advance(1); p.focusOut(null);  // that click is over: Tab out
out.focusLostAfterGrace = p.shown();
p.clickToggle(); p.press(p.links['/']); p.press(p.links['/'], 'pointerup'); p.focusOut(null);  // touch: after lifting
out.focusLostAfterRelease = p.shown();
p.advance(1000); p.focusOut(null);   // lifted but no click came (dragged away): no longer a press after a second
out.focusLostAfterReleaseGrace = p.shown();
// while the panel is open its toasts sit above it: using a toast is not leaving the quick-nav
// (closing would drop the toast back down from under the pointer, so its × could not be clicked)
p = page();
p.clickToggle(); p.press(p.toastClose);
out.pressOnToast = p.shown();
p.focusOut(p.toastClose);
out.focusToToast = p.shown();
p = page();
p.enter(); p.leaveTo(p.toastClose); p.advance(1000);
out.hoverOntoToast = p.shown();
p.toastLeaveTo(p.outside); p.advance(299);
out.hoverOffToastAlmost = p.shown();
p.advance(1);
out.hoverOffToast = p.shown();
p = page();
p.enter(); p.leaveTo(p.outside); p.advance(100); p.toastEnter(); p.advance(1000);
out.hoverAcrossToToast = p.shown();
p = page();
p.enter(); p.leaveTo(p.toastClose); p.toastLeaveTo(p.root); p.advance(1000);
out.hoverToastBackToPanel = p.shown();
// Tab out right after clicking 到顶 in the panel (the case a real browser showed): closes
p = page({ scrollHeight: 4000, innerHeight: 800, scrollY: 2000 });
p.clickToggle();
p.press(p.buttons.top); p.press(p.buttons.top, 'pointerup'); p.click(p.buttons.top); p.advance(1);
p.focusOut(null);
out.tabOutAfterPanelClick = p.shown();

// every opening publishes the panel's current height (toasts move above it)
p = page({ panelHeight: 236 });
p.clickToggle();
const firstOpen = p.panelVar();
p.clickToggle();
p.panel.offsetHeight = 180;  // a shorter window caps the panel
p.enter();
out.panelHeight = [firstOpen, p.panelVar()];

// 到顶 / 到底 jump straight there and update what is available
p = page({ scrollHeight: 5000, innerHeight: 800, scrollY: 1200 });
p.clickToggle();
out.midPage = p.disabled().slice(1);
p.click(p.bottomIcon);  // the click lands on the icon inside the button
out.toBottom = [p.calls.slice(), p.win.scrollY, p.disabled().slice(1)];
p.click(p.buttons.top);
out.toTop = [p.calls.slice(1), p.win.scrollY, p.disabled().slice(1)];

// unavailable buttons do nothing
p = page({ scrollHeight: 2000, innerHeight: 800, scrollY: 0 });
p.clickToggle();
p.click(p.buttons.top);                          // already at the top
p.scroll(1200);                                  // now at the bottom
p.click(p.buttons.bottom);
p.click(p.buttons.back);                         // nothing to go back to
out.disabledNoop = [p.disabled(), p.calls, p.stack()];

// the page grew while open (results appended, no scroll / resize yet): 到底 still goes, to the new bottom
p = page({ scrollHeight: 2000, innerHeight: 800, scrollY: 1200 });
p.clickToggle();
const staleBottom = p.disabled()[2];
p.doc.documentElement.scrollHeight = 5000;
p.click(p.buttons.bottom);
out.grewThenBottom = [staleBottom, p.calls.slice(), p.disabled()[2]];
// already moved to the top before the scroll frame: 到顶 is re-checked and does nothing
p = page({ scrollHeight: 5000, innerHeight: 800, scrollY: 600 });
p.clickToggle();
const staleTop = p.disabled()[1];
p.win.scrollY = 0;
p.click(p.buttons.top);
out.movedThenTop = [staleTop, p.calls.slice(), p.disabled()[1]];

// while open, every scroll and resize re-checks 到顶 / 到底, not only the first one
p = page({ scrollHeight: 5000, innerHeight: 800, scrollY: 0 });
p.clickToggle();
out.viewport = [p.disabled().slice(1)];
p.scroll(1200); out.viewport.push(p.disabled().slice(1));
p.scroll(4200); out.viewport.push(p.disabled().slice(1));
p.win.innerHeight = 600; p.resize(); out.viewport.push(p.disabled().slice(1));  // shorter window: more below
p.scroll(0); out.viewport.push(p.disabled().slice(1));

// browser zoom can leave the position a fraction of a pixel short of the very top / bottom
p = page({ scrollHeight: 5000, innerHeight: 800, scrollY: 4199.5 });
p.clickToggle();
const nearBottom = p.disabled().slice(1);
p.scroll(0.5);
out.subPixel = [nearBottom, p.disabled().slice(1)];

// the body's size is watched, but only an open panel is updated
p = page({ scrollHeight: 2000, innerHeight: 800, scrollY: 1200, resizeObserver: true });
const observed = p.observed();
p.doc.documentElement.scrollHeight = 5000; p.bodyResized();
const whileClosed = p.disabled()[2];
p.clickToggle();
const onOpen = p.disabled()[2];
p.doc.documentElement.scrollHeight = 2000; p.bodyResized();
out.resizeObserver = [observed, whileClosed, onOpen, p.disabled()[2]];

// destination links: only a plain click that leaves this page records a step
p = page({ path: '/downloads', title: '下载管理 - JMComic 下载控制台', scrollY: 640, scrollHeight: 3000 });
p.clickToggle();
[{ ctrlKey: true }, { shiftKey: true }, { metaKey: true }, { altKey: true }, { button: 1 }]
  .forEach(mods => p.click(p.links['/library'], mods));
out.modifiedClicks = p.stack();
p.click(p.links['/downloads']);
out.currentDest = p.stack();
p.click(p.linkLabels['/library']);  // the click lands on the label inside the link
out.plainClick = p.stack().map(s => [s.url, s.y, s.title]);
// on a search page the 搜索 link (address swapped by nav-memory.js) is still the current page
p = page({ path: '/search', search: '?keyword=a&page=3', title: '搜索 - JMComic 下载控制台' });
p.links['/search'].attrs.href = '/search?keyword=b';
p.click(p.links['/search']);
out.currentSearch = p.stack();
p.click(p.links['/']);  // Enter on a focused link: the link itself is the target
out.searchToHome = p.stack().map(s => [s.url, s.title]);

// 返回: one step per click, never adds one, a double click pops once; off while leaving
p = page({ path: '/downloads', scrollHeight: 3000, stack: [step('/library?page=2', '资源库', 300), step('/settings', '设置', 50)] });
out.backReady = [p.backLabel(), p.disabled()[0]];
p.clickToggle();
p.click(p.buttons.back);
p.click(p.buttons.back);
out.backOnce = [p.calls.slice(), p.stack().map(s => s.url), p.disabled()[0]];
p.scroll(500); p.resize();
out.backWhileLeaving = [p.disabled(), p.shown()];
p.advance(3999);
out.backBeforeReset = p.disabled()[0];
p.advance(1);
out.backAfterReset = [p.disabled()[0], p.backLabel()];
p.click(p.buttons.back);
out.backLast = [p.calls.slice(2), p.stack()];
p.pagehide();
out.pagehideCloses = p.shown();
p.advance(4000);
out.backEmpty = [p.disabled()[0], p.backLabel()];

// coming back to the page (pageshow) makes 返回 usable again right away
p = page({ path: '/downloads', stack: [step('/library', '资源库'), step('/settings', '设置')] });
p.clickToggle(); p.click(p.buttons.back);
const leavingState = p.disabled()[0];
p.pagehide(); p.pageshow();
out.pageshow = [leavingState, p.shown(), p.disabled()[0], p.backLabel()[1]];
p.clickToggle(); p.click(p.buttons.back);
out.pageshowThenBack = p.calls.map(c => c.slice(0, 2));

// has-back and the label follow the stack: a step for this very page or an expired one does not count
p = page({ path: '/downloads', stack: [step('/downloads', '下载管理')] });
out.selfOnly = p.backLabel();
p = page({ path: '/downloads', stack: [step('/library', '资源库')] });
const fresh = p.backLabel();
p.advance(12 * HOUR);
p.clickToggle();
out.expired = [fresh, p.backLabel(), p.disabled()[0]];

console.log(JSON.stringify(out));
"""


def run_harness(js_path):
    """Run the harness against one copy of quick-nav.js and return what it observed."""
    result = subprocess.run([NODE, "-e", HARNESS, str(js_path)],
                            capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def harness():
    if NODE is None:
        pytest.skip("needs Node.js (CI installs it)")
    return run_harness(STATIC_JS / "quick-nav.js")


def test_a_fresh_page_starts_closed_with_nothing_available(harness):
    shown, disabled, back = harness["initial"]
    assert shown == CLOSED
    assert disabled == ["true", "true", "true"]  # 返回 / 到顶 / 到底 on a page that cannot scroll
    assert back == [False, NO_BACK_LABEL, NO_BACK_LABEL]


def test_a_mouse_hover_opens_and_closes_after_a_short_delay(harness):
    assert harness["hoverOpen"] == OPEN
    assert harness["hoverJustLeft"] == OPEN and harness["hoverAlmost"] == OPEN  # no flicker on a diagonal move
    assert harness["hoverClosed"] == CLOSED
    assert harness["hoverReentered"] == OPEN  # coming back in time cancels the close


def test_keyboard_focus_inside_keeps_a_hovered_panel_open(harness):
    assert harness["keyboardKeepsOpen"] == OPEN
    assert harness["keyboardGone"] == CLOSED


def test_touch_and_pen_do_not_open_on_hover(harness):
    assert harness["touchEnter"] == CLOSED
    assert harness["penEnter"] == CLOSED


def test_the_toggle_opens_and_closes_on_click(harness):
    assert harness["clickOpen"] == OPEN
    assert harness["clickSurvivesLeave"] == OPEN  # opened by click: the mouse leaving does not close it
    assert harness["clickClosed"] == CLOSED


def test_clicking_a_hovered_panel_pins_it_open(harness):
    assert harness["pinned"] == OPEN
    assert harness["pinnedAfterLeave"] == OPEN


def test_escape_closes_and_returns_focus_only_from_inside(harness):
    assert harness["escapeOutside"] == [CLOSED, 0]
    assert harness["otherKey"] == OPEN
    assert harness["escapeInside"] == [CLOSED, ["toggle"]]
    assert harness["escapeWhenClosed"] == 1  # nothing open: Escape leaves focus alone


def test_pressing_outside_closes_but_pressing_inside_does_not(harness):
    assert harness["pressInside"] == OPEN
    assert harness["pressOutside"] == CLOSED


def test_focus_moving_away_closes_but_moving_within_does_not(harness):
    assert harness["focusToInside"] == OPEN
    assert harness["focusToOutside"] == CLOSED
    assert harness["focusLostNoPress"] == CLOSED


def test_focus_lost_right_after_a_press_inside_is_forgiven_for_one_second(harness):
    # pressing the panel's blank area moves focus to <body>: not "leaving"
    assert harness["focusLostAfterPress"] == OPEN    # however long the press lasts
    assert harness["focusLostAfterGrace"] == CLOSED  # the press ended with its click
    assert harness["focusLostAfterRelease"] == OPEN  # touch: focus moves after the finger lifts
    assert harness["focusLostAfterReleaseGrace"] == CLOSED
    assert harness["tabOutAfterPanelClick"] == CLOSED


def test_using_a_toast_above_the_open_panel_does_not_close_it(harness):
    assert harness["pressOnToast"] == OPEN
    assert harness["focusToToast"] == OPEN
    assert harness["hoverOntoToast"] == OPEN
    assert harness["hoverOffToastAlmost"] == OPEN
    assert harness["hoverOffToast"] == CLOSED       # leaving the toast for the page closes it like leaving the panel
    assert harness["hoverAcrossToToast"] == OPEN
    assert harness["hoverToastBackToPanel"] == OPEN


def test_opening_publishes_the_panel_height_for_toasts(harness):
    assert harness["panelHeight"] == ["236px", "180px"]  # re-measured on every opening


def test_top_and_bottom_jump_instantly_and_update_availability(harness):
    assert harness["midPage"] == ["false", "false"]
    # instant: Bootstrap's smooth scroll-behavior would otherwise glide there
    assert harness["toBottom"] == [[["scrollTo", 4200, "instant"]], 4200, ["false", "true"]]
    assert harness["toTop"] == [[["scrollTo", 0, "instant"]], 0, ["true", "false"]]


def test_unavailable_buttons_do_nothing(harness):
    disabled, calls, stack = harness["disabledNoop"]
    assert disabled == ["true", "false", "true"]
    assert calls == [] and stack == []


def test_top_and_bottom_recheck_the_page_before_deciding(harness):
    # 到底 was marked unavailable, then the page grew: the click still goes to the new bottom
    assert harness["grewThenBottom"] == ["true", [["scrollTo", 4200, "instant"]], "true"]
    # 到顶 was marked available, but the page is already at the top: nothing happens
    assert harness["movedThenTop"] == ["false", [], "true"]


def test_scrolling_and_resizing_keep_top_and_bottom_up_to_date(harness):
    assert harness["viewport"] == [
        ["true", "false"],   # opened at the top
        ["false", "false"],  # scrolled to the middle
        ["false", "true"],   # scrolled again, to the bottom
        ["false", "false"],  # the window got shorter: there is more below now
        ["true", "false"],   # back at the top
    ]


def test_a_fraction_of_a_pixel_from_the_edge_counts_as_there(harness):
    near_bottom, near_top = harness["subPixel"]
    assert near_bottom == ["false", "true"]
    assert near_top == ["true", "false"]


def test_a_resized_body_updates_an_open_panel_only(harness):
    assert harness["resizeObserver"] == [True, "true", "false", "true"]


def test_a_plain_click_on_a_destination_records_one_step(harness):
    assert harness["plainClick"] == [["/downloads", 640, "下载管理"]]
    assert harness["searchToHome"] == [["/search?keyword=a&page=3", "搜索“a”"]]


def test_modified_clicks_and_the_current_page_record_nothing(harness):
    assert harness["modifiedClicks"] == []  # Ctrl / Shift / Meta / Alt / middle button open elsewhere
    assert harness["currentDest"] == []
    assert harness["currentSearch"] == []


def test_return_never_grows_the_stack_and_a_double_click_pops_once(harness):
    calls, stack, back = harness["backOnce"]
    assert calls == [["expect", "/settings", 50, None], ["assign", "/settings"]]
    assert stack == ["/library?page=2"]
    assert back == "true"


def test_return_stays_off_while_leaving_even_on_scroll_or_resize(harness):
    disabled, shown = harness["backWhileLeaving"]
    assert disabled == ["true", "false", "false"]  # 到顶 / 到底 were refreshed, 返回 was not re-enabled
    assert shown == OPEN
    assert harness["backBeforeReset"] == "true"


def test_return_recovers_after_the_reset_delay(harness):
    assert harness["backAfterReset"] == ["false", [True, "返回：资源库", "返回：资源库"]]
    calls, stack = harness["backLast"]
    assert calls == [["expect", "/library?page=2", 300, None], ["assign", "/library?page=2"]]
    assert stack == []


def test_return_recovers_on_pageshow(harness):
    assert harness["pageshow"] == ["true", CLOSED, "false", "返回：资源库"]
    assert harness["pageshowThenBack"] == [["expect", "/settings"], ["assign", "/settings"],
                                           ["expect", "/library"], ["assign", "/library"]]


def test_pagehide_closes_the_panel(harness):
    assert harness["pagehideCloses"] == CLOSED


def test_has_back_and_the_return_label_follow_the_stack(harness):
    assert harness["backReady"] == [[True, "返回：设置", "返回：设置"], "false"]  # names the latest step
    assert harness["backEmpty"] == ["true", [False, NO_BACK_LABEL, NO_BACK_LABEL]]
    assert harness["selfOnly"] == [False, NO_BACK_LABEL, NO_BACK_LABEL]
    fresh, expired, back = harness["expired"]
    assert fresh == [True, "返回：资源库", "返回：资源库"]
    assert expired == [False, NO_BACK_LABEL, NO_BACK_LABEL] and back == "true"
