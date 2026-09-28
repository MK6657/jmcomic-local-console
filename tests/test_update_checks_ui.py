"""检查新章节（PR-A）的界面：详情页状态行、列表标记、下载管理、设置页。

The page code runs in Node with a small stand-in DOM (skipped when Node is missing; CI installs it): the real
static/js/utils.js, the detail.js block between the 章节更新 markers, the list functions of library.js / wishlist.js,
the whole downloads.js, and the settings.js block between the 检查新章节状态 markers. API answers are built with the
real core.update_store.describe, so the copy is checked against the backend's own shapes. Nothing here goes online
or starts a download; the fixture test runs in pytest's tmp folders with the network blocked (conftest)."""
import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC_JS = ROOT / "static" / "js"
NODE = shutil.which("node")

NOW = "2026-07-15T15:30:00"          # the harness clock (local time, far from any DST change)
TODAY_1030 = "2026-07-15T10:30:00"
TODAY_1330 = "2026-07-15T13:30:00"
YESTERDAY_2015 = "2026-07-14T20:15:00"
IN_30_MIN = "2026-07-15T16:00:00"
IN_2_HOURS = "2026-07-15T17:30:00"
TOMORROW_1030 = "2026-07-16T10:30:00"

DETAIL_START = "// ── 章节更新 ──"
DETAIL_END = "// ── 章节更新结束 ──"
SETTINGS_START = "// ── 检查新章节状态 ──"
SETTINGS_END = "// ── 检查新章节状态结束 ──"


def _read(name):
    return (STATIC_JS / name).read_text(encoding="utf-8")


def _node(script, *args, data=None):
    """Run a harness: file paths as arguments, the scenario as JSON on stdin (Windows limits the command line)."""
    if NODE is None:
        pytest.skip("needs Node.js (CI installs it)")
    result = subprocess.run([NODE, "-e", script, *[str(a) for a in args]], input=json.dumps(data, ensure_ascii=False),
                            capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _js_block(source, marker):
    """The source from `marker` (e.g. 'function el(') to its matching closing brace (plus a trailing ';')."""
    start = source.index(marker)
    i = source.index("{", start)
    depth, quote = 0, None
    while True:
        ch = source[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif source.startswith("//", i):
            i = source.index("\n", i)
            continue
        elif source.startswith("/*", i):
            i = source.index("*/", i) + 2
            continue
        elif ch in "'\"`":
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    end = i + 1
    if source[end:end + 1] == ";":
        end += 1
    return source[start:end]


# ─── API answers built with the backend's own describe() ──────────────────────


def _row(**fields):
    row = {"baseline_ids": None, "baseline_source": None, "baseline_at": None, "gone_ids": [], "removed_ids": [],
           "upstream_count": None, "new_chapters": [], "result": None, "last_success_at": None,
           "last_attempt_at": None, "last_trigger": None, "error_kind": None, "error_detail": None,
           "fail_count": 0, "next_check_at": None}
    row.update(fields)
    row["new_count"] = len(row["new_chapters"])
    return row


def _payload(row=None, auto=True, checking=False, paused=None, outcome=None, album_id="3001"):
    """What GET /api/updates/<id> (and, with outcome, POST …/check) answers for a readable album."""
    from core import update_store
    nxt = (row or {}).get("next_check_at")
    effective = max([v for v in (nxt, paused) if v], default=None) if auto else None
    data = {"status": "ok", "album_id": album_id, "eligible": True, "auto_enabled": auto, "checking": checking,
            "update": update_store.describe(row, auto, checking, effective, paused)}
    if outcome:
        data["outcome"] = outcome
    return data


NOT_ELIGIBLE = {"status": "ok", "album_id": "3001", "eligible": False}


def _chapter(photo_id, index, title=None, confirmed=YESTERDAY_2015):
    return {"photo_id": photo_id, "index": index, "title": f"第{index}话" if title is None else title,
            "confirmed_at": confirmed}


BASE = ["71", "72", "73"]
NEW_2 = [_chapter("74", 4), _chapter("75", 5)]

ROWS = {
    "never_download": _row(baseline_ids=BASE, baseline_source="download", baseline_at=TODAY_1030,
                           next_check_at=TOMORROW_1030),
    "never_legacy": None,
    "baseline": _row(baseline_ids=[str(n) for n in range(1, 13)], baseline_source="first_check",
                     baseline_at=TODAY_1030, upstream_count=12, result="baseline", last_success_at=TODAY_1030,
                     last_attempt_at=TODAY_1030, next_check_at=TOMORROW_1030),
    "no_update": _row(baseline_ids=BASE, baseline_source="download", upstream_count=3, result="no_update",
                      last_success_at=TODAY_1330, last_attempt_at=TODAY_1330, next_check_at=IN_2_HOURS),
    "new": _row(baseline_ids=BASE, baseline_source="download", upstream_count=5, result="new",
                new_chapters=NEW_2, last_success_at=TODAY_1330, next_check_at=TOMORROW_1030),
    "changed": _row(baseline_ids=BASE, baseline_source="download", upstream_count=3, result="changed",
                    removed_ids=["73"], new_chapters=[_chapter("74", 3, "第3话（重新上传）")],
                    last_success_at=TODAY_1330, next_check_at=TOMORROW_1030),
    "failed": _row(baseline_ids=BASE, baseline_source="download", error_kind="network", fail_count=1,
                   last_attempt_at=TODAY_1330, next_check_at=IN_30_MIN),
    "failed_paused": _row(baseline_ids=BASE, baseline_source="download", error_kind="network", fail_count=3,
                          last_attempt_at=TODAY_1330, next_check_at="2026-07-15T15:40:00"),
    "failed_after_no_update": _row(baseline_ids=BASE, baseline_source="download", upstream_count=3,
                                   result="no_update", last_success_at="2026-07-14T09:05:00",
                                   error_kind="timeout", fail_count=2, next_check_at="2026-07-15T16:30:00"),
    "failed_after_baseline": _row(baseline_ids=[str(n) for n in range(1, 13)], baseline_source="first_check",
                                  upstream_count=12, result="baseline", last_success_at="2026-07-14T09:05:00",
                                  error_kind="upstream_error", fail_count=1, next_check_at=IN_30_MIN),
    "failed_with_new": _row(baseline_ids=BASE, baseline_source="download", upstream_count=4, result="new",
                            new_chapters=[_chapter("74", 4)], last_success_at=TODAY_1030,
                            error_kind="not_found", fail_count=1, next_check_at=IN_30_MIN),
    "failed_with_changed": _row(baseline_ids=BASE, baseline_source="download", upstream_count=3,
                                result="changed", removed_ids=["73"], new_chapters=[_chapter("74", 3)],
                                last_success_at=TODAY_1030, error_kind="upstream_error", fail_count=1,
                                next_check_at=IN_30_MIN),
}


# ─── the stand-in DOM shared by every harness ─────────────────────────────────

FAKE_DOM = r"""
const fs = require('fs');
const RealDate = Date;
function fixClock(iso) {
  const fixed = new RealDate(iso).getTime();
  class FakeDate extends RealDate {
    constructor(...a) { if (a.length === 0) super(fixed); else super(...a); }
    static now() { return fixed; }
  }
  global.Date = FakeDate;
}
const esc = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const escAttr = s => esc(s).replace(/"/g, '&quot;');
const htmlParsed = [];   // every innerHTML write by the code under test (text must never be parsed as HTML)
function textNode(v) { return { nodeType: 3, data: String(v), parentNode: null, get textContent() { return this.data; } }; }
function serialize(n) {
  if (n.nodeType === 3) return esc(n.data);
  if (n.nodeType === 11) return n.children.map(serialize).join('');
  const tag = n.tagName.toLowerCase();
  const attrs = (n.className ? ' class="' + escAttr(n.className) + '"' : '')
    + Object.entries(n.attrs).map(([k, v]) => ' ' + k + '="' + escAttr(v) + '"').join('');
  return '<' + tag + attrs + '>' + (n.rawHtml !== null ? n.rawHtml : n.children.map(serialize).join('')) + '</' + tag + '>';
}
function matches(n, sel) {
  if (n.nodeType !== 1) return false;
  return sel.split(',').some(one => {
    const m = one.trim().match(/^([a-zA-Z]*)((?:[.#][\w-]+)*)((?:\[[^\]]+\])*)$/);
    if (!m) throw new Error('selector not modelled by the harness: ' + one);
    if (m[1] && n.tagName !== m[1].toUpperCase()) return false;
    for (const part of m[2].match(/[.#][\w-]+/g) || []) {
      if (part[0] === '.' && !n.classList.contains(part.slice(1))) return false;
      if (part[0] === '#' && n.id !== part.slice(1)) return false;
    }
    for (const a of m[3].match(/\[[^\]]+\]/g) || []) {
      const mm = a.slice(1, -1).match(/^([\w-]+)(?:="?([^"]*)"?)?$/);
      const v = n.getAttribute(mm[1]);
      if (v === null || (mm[2] !== undefined && v !== mm[2])) return false;
    }
    return true;
  });
}
function el(tag) {
  const cls = new Set(), attrs = {}, handlers = {};
  const e = {
    nodeType: 1, tagName: String(tag).toUpperCase(), children: [], parentNode: null, attrs, handlers, rawHtml: null,
    hidden: false, checked: false, disabled: false, value: '', scrolls: [],
    get className() { return Array.from(cls).join(' '); },
    set className(v) { cls.clear(); String(v).split(/\s+/).forEach(c => c && cls.add(c)); },
    classList: {
      add: (...n) => n.forEach(c => cls.add(c)), remove: (...n) => n.forEach(c => cls.delete(c)),
      contains: c => cls.has(c),
      toggle: (c, f) => { const on = f === undefined ? !cls.has(c) : !!f; if (on) cls.add(c); else cls.delete(c); return on; },
    },
    get id() { return attrs.id || ''; }, set id(v) { attrs.id = String(v); },
    get title() { return attrs.title || ''; }, set title(v) { attrs.title = String(v); },
    get href() { return attrs.href || ''; }, set href(v) { attrs.href = String(v); },
    get type() { return attrs.type || ''; }, set type(v) { attrs.type = String(v); },
    setAttribute(k, v) { attrs[k] = String(v); }, getAttribute: k => (k in attrs ? attrs[k] : null),
    hasAttribute: k => k in attrs, removeAttribute(k) { delete attrs[k]; },
    appendChild(c) {
      if (c.nodeType === 11) { c.children.splice(0).forEach(k => e.appendChild(k)); return c; }
      if (c.parentNode) c.parentNode.children.splice(c.parentNode.children.indexOf(c), 1);
      c.parentNode = e; e.children.push(c); e.rawHtml = null; return c;
    },
    insertBefore(c, ref) {
      if (!ref) return e.appendChild(c);
      if (c.parentNode) c.parentNode.children.splice(c.parentNode.children.indexOf(c), 1);
      c.parentNode = e; e.children.splice(e.children.indexOf(ref), 0, c); e.rawHtml = null; return c;
    },
    remove() { if (e.parentNode) { e.parentNode.children.splice(e.parentNode.children.indexOf(e), 1); e.parentNode = null; } },
    get textContent() { return e.rawHtml !== null ? e.rawHtml.replace(/<[^>]*>/g, '') : e.children.map(c => c.textContent).join(''); },
    set textContent(v) { e.children.forEach(c => { c.parentNode = null; }); e.children = []; e.rawHtml = null; if (v !== '' && v != null) e.appendChild(textNode(v)); },
    get innerHTML() { return e.rawHtml !== null ? e.rawHtml : e.children.map(serialize).join(''); },
    set innerHTML(v) { htmlParsed.push(String(v)); e.children = []; e.rawHtml = String(v); },
    get outerHTML() { return serialize(e); },
    addEventListener(type, fn) { (handlers[type] = handlers[type] || []).push(fn); },
    click() { if (e.disabled) return; (handlers.click || []).forEach(fn => fn({ type: 'click', target: e, preventDefault() {} })); },
    all() { return e.children.filter(c => c.nodeType === 1).flatMap(c => [c, ...c.all()]); },
    matches: sel => matches(e, sel),
    querySelectorAll(sel) { return e.all().filter(n => matches(n, sel)); },
    querySelector(sel) { return e.querySelectorAll(sel)[0] || null; },
    closest(sel) { for (let x = e; x && x.nodeType === 1; x = x.parentNode) if (matches(x, sel)) return x; return null; },
    contains(n) { for (let x = n; x; x = x.parentNode) if (x === e) return true; return false; },
    scrollIntoView(opts) { e.scrolls.push(opts === undefined ? null : opts); },
  };
  return e;
}
const body = el('body');
const docHandlers = {}, winHandlers = {};
global.window = global;
window.addEventListener = (t, fn) => { (winHandlers[t] = winHandlers[t] || []).push(fn); };
global.document = {
  body, readyState: 'complete', visibilityState: 'visible', hidden: false,
  createElement: el, createTextNode: textNode,
  createDocumentFragment: () => { const f = el('#fragment'); f.nodeType = 11; return f; },
  getElementById: id => body.all().find(n => n.id === id) || null,
  querySelectorAll: sel => body.querySelectorAll(sel),
  querySelector: sel => body.querySelector(sel),
  addEventListener: (t, fn) => { (docHandlers[t] = docHandlers[t] || []).push(fn); },
};
const flush = async () => { for (let i = 0; i < 10; i++) await new Promise(r => setImmediate(r)); };
function makeError(spec) {
  const e = new Error(spec.message || 'failed');
  if (spec.name) e.name = spec.name;
  if (spec.status) e.status = spec.status;
  if (spec.isTimeout) e.isTimeout = true;
  return e;
}
"""


# ─── the detail page: status row, 立即检查, 选中这些章节, 新 marks ─────────────────

DETAIL_HARNESS = FAKE_DOM + r"""
const [utilsPath, detailPath] = process.argv.slice(1);
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
fixClock(input.now);
(0, eval)(fs.readFileSync(utilsPath, 'utf8'));
const src = fs.readFileSync(detailPath, 'utf8');
const START = '// ── 章节更新 ──', END = '// ── 章节更新结束 ──';
if (src.indexOf(START) < 0 || src.indexOf(END) < src.indexOf(START)) throw new Error('章节更新 markers missing');
const code = src.slice(src.indexOf('function toastErr'), src.indexOf('function loadAlbum'))
  + src.slice(src.indexOf(START), src.indexOf(END));
global.albumId = '3001';
let calls = [], toasts = [], timers = [];
global.showToast = (m, t) => toasts.push([m, t]);
global.setTimeout = (fn, ms) => { timers.push({ fn, ms }); return timers.length; };
global.clearTimeout = id => { if (timers[id - 1]) timers[id - 1].fn = null; };

function page(chapters, checked) {
  body.children = [];
  const box = el('div'); box.id = 'album-update-status'; box.className = 'update-status'; box.hidden = true;
  body.appendChild(box);
  const selectMain = el('input'); selectMain.id = 'select-all-chapters'; body.appendChild(selectMain);
  const table = el('table'); table.className = 'chapter-table'; body.appendChild(table);
  const selectInline = el('input'); selectInline.id = 'select-all-inline'; table.appendChild(selectInline);
  const rows = chapters.map((c, n) => {
    const tr = el('tr'); table.appendChild(tr);
    const box1 = el('td'); tr.appendChild(box1);
    const cb = el('input'); cb.className = 'form-check-input chapter-checkbox-item'; cb.value = c.id;
    cb.checked = checked.indexOf(c.id) >= 0; box1.appendChild(cb);
    const index = el('td'); index.textContent = String(n + 1); tr.appendChild(index);
    const title = el('td'); title.textContent = c.title; tr.appendChild(title);
    return { id: c.id, tr, cb, titleCell: title };
  });
  return { box, rows, selectMain, selectInline };
}

function snapshot(p) {
  const box = p.box;
  const kids = box.children.filter(c => c.nodeType === 1);
  const badge = kids.find(c => c.classList.contains('badge'));
  const lead = kids.find(c => c.classList.contains('update-status-text') || c.classList.contains('update-status-warn'));
  const one = cls => { const n = box.querySelector('.' + cls); return n ? n.textContent : null; };
  const icon = n => { const i = n && n.querySelector('i'); return i ? i.className : null; };
  return {
    hidden: box.hidden,
    order: kids.map(c => c.getAttribute('data-action') || c.className),
    badge: badge ? { cls: badge.className, text: badge.textContent.trim(), title: badge.title, icon: icon(badge) } : null,
    lead: lead ? { cls: lead.className, text: lead.textContent.trim(), icon: icon(lead) } : null,
    buttons: box.querySelectorAll('button').map(b => ({
      action: b.getAttribute('data-action'), text: b.textContent.trim(), icon: icon(b), cls: b.className,
      type: b.type, disabled: !!b.disabled, busy: b.getAttribute('aria-busy'), title: b.title })),
    names: one('update-new-list'),
    meta: one('update-status-meta'),
    marks: Object.fromEntries(p.rows.map(r => [r.id, r.titleCell.querySelectorAll('.badge').map(m => (
      { text: m.textContent, title: m.title, cls: m.className }))]).filter(([, m]) => m.length)),
    rowTitles: p.rows.map(r => r.titleCell.textContent),
    checked: p.rows.filter(r => r.cb.checked).map(r => r.id),
    selectAll: [!!p.selectMain.checked, !!p.selectInline.checked],
    scrolls: p.rows.filter(r => r.tr.scrolls.length).map(r => [r.id, r.tr.scrolls]),
    images: body.querySelectorAll('img').length,
  };
}

async function runCase(c) {
  const p = page(c.chapters || [71, 72, 73, 74, 75].map(n => ({ id: String(n), title: '第' + (n - 70) + '话' })), c.checked || []);
  calls = []; toasts = []; timers = [];
  (0, eval)(code);   // fresh module state for every case
  const gets = [];
  let held = [];
  window.apiFetch = (url, opts) => {
    opts = opts || {};
    calls.push({ url, method: opts.method || 'GET', body: opts.body === undefined ? null : opts.body,
                 abortKey: opts.abortKey || null, timeoutMs: opts.timeoutMs === undefined ? null : opts.timeoutMs });
    if (url === '/api/updates/3001' && (opts.method || 'GET') === 'GET') {
      return gets.length ? Promise.resolve(gets.shift()) : new Promise(() => {});
    }
    if (url === '/api/updates/3001/check') {
      return new Promise((resolve, reject) => held.push({ resolve, reject }));
    }
    return new Promise(() => {});
  };
  const steps = [];
  let seenCalls = 0, seenToasts = 0;
  const record = (extra) => {
    const s = Object.assign(snapshot(p), extra || {}, { calls: calls.slice(seenCalls), toasts: toasts.slice(seenToasts),
      timers: timers.filter(t => t.fn).map(t => t.ms) });
    seenCalls = calls.length; seenToasts = toasts.length;
    steps.push(s);
  };
  const answer = async (spec) => {
    const h = held.shift();
    if (!h) throw new Error('no check request to answer');
    if (spec.error) h.reject(makeError(spec.error)); else h.resolve(spec.ok);
    await flush();
  };
  for (const step of c.steps) {
    if (step.load !== undefined) {
      gets.push(step.load);
      refreshUpdateStatus();
      await flush();
      record();
    } else if (step.click) {
      if (step.refresh) gets.push(step.refresh);
      const b = p.box.querySelector('[data-action="' + step.click + '"]');
      if (!b) throw new Error('no button ' + step.click);
      b.click();
      await flush();
      if (step.click === 'check-updates') {
        if (step.again) { checkUpdatesNow(); b.click(); await flush(); }
        record({ during: true });
        await answer(step.answer);
      }
      record();
    } else if (step.runTimers) {
      if (step.refresh) gets.push(step.refresh);
      timers.forEach(t => { if (t.fn) { const fn = t.fn; t.fn = null; fn(); } });
      await flush();
      record();
    }
  }
  return steps;
}

(async () => {
  const out = {};
  for (const c of input.cases) out[c.name] = await runCase(c);
  out.htmlParsed = htmlParsed;
  process.stdout.write(JSON.stringify(out));
})().catch(err => { console.error(err && err.stack || err); process.exit(1); });
"""


def _detail(cases):
    return _node(DETAIL_HARNESS, STATIC_JS / "utils.js", STATIC_JS / "detail.js", data={"now": NOW, "cases": cases})


def _view(name, **kwargs):
    """The detail row right after GET answers with ROWS[name]."""
    out = _detail([{"name": "x", "steps": [{"load": _payload(ROWS[name], **kwargs)}]}])
    return out["x"][0]


CHECK = {"action": "check-updates", "text": "立即检查", "icon": "bi bi-arrow-repeat", "cls": "btn btn-sm btn-outline-primary",
         "type": "button", "disabled": False, "busy": None, "title": "只取一次章节列表核对，不会下载任何内容"}
BUSY = dict(CHECK, text="检查中…", icon="bi bi-hourglass-split", disabled=True, busy="true")


def _select(text):
    return {"action": "select-new-chapters", "text": text, "icon": "bi bi-check2-square",
            "cls": "btn btn-sm btn-outline-primary", "type": "button", "disabled": False, "busy": None, "title": ""}


# (row, auto, extra) → (lead text, meta); the badge states are checked separately below
COPY = {
    "never_download_auto": ("never_download", True, {}, "还没检查过新章节",
                            "下载时记下了上游的 3 话；后台会自动检查，每部漫画大约一天一次"),
    "never_download_off": ("never_download", False, {}, "还没检查过新章节",
                           "下载时记下了上游的 3 话 · 自动检查已关闭，可以点「立即检查」"),
    "never_legacy_auto": ("never_legacy", True, {}, "还没检查过新章节", "后台会在一天内自动检查，之后每部漫画大约一天一次"),
    "never_legacy_off": ("never_legacy", False, {}, "还没检查过新章节", "自动检查已关闭，可以点「立即检查」"),
    "checking": ("no_update", True, {"checking": True}, "正在检查新章节…", "只取一次章节列表核对，不会下载任何内容"),
    "baseline_auto": ("baseline", True, {}, "已记下上游现有的 12 话",
                      "今天 10:30 检查 · 以后新出的章节会在这里提示 · 下次约 明天 10:30"),
    "baseline_off": ("baseline", False, {}, "已记下上游现有的 12 话",
                     "今天 10:30 检查 · 以后新出的章节会在这里提示 · 自动检查已关闭"),
    "no_update_auto": ("no_update", True, {}, "没有新章节", "上次检查：今天 13:30 · 上游共 3 话 · 下次约 2 小时后"),
    "no_update_off": ("no_update", False, {}, "没有新章节", "上次检查：今天 13:30 · 上游共 3 话 · 自动检查已关闭"),
    "failed_auto": ("failed", True, {}, "这次没检查成功：连不上服务器", "约 30 分钟后 自动重试"),
    "failed_paused": ("failed_paused", True, {"paused": "2026-07-15T15:45:00"}, "这次没检查成功：连不上服务器",
                      "连续几次连不上服务器，自动检查暂停，约 15 分钟后 再试"),
    "failed_off": ("failed", False, {}, "这次没检查成功：连不上服务器", "自动检查已关闭，可以稍后再点「立即检查」"),
    "failed_after_no_update": ("failed_after_no_update", True, {}, "这次没检查成功：服务器响应太慢",
                               "约 1 小时后 自动重试 · 上次成功检查：昨天 09:05（没有新章节）"),
    "failed_after_baseline_off": ("failed_after_baseline", False, {}, "这次没检查成功：服务器返回的章节列表无法识别",
                                  "自动检查已关闭，可以稍后再点「立即检查」 · 上次成功检查：昨天 09:05（已记下 12 话）"),
}
ICONS = {"never": "bi bi-clock-history", "checking": "bi bi-hourglass-split", "baseline": "bi bi-bookmark-check",
         "no_update": "bi bi-check2-circle", "failed": "bi bi-exclamation-triangle"}


@pytest.fixture(scope="module")
def copy_views():
    cases = [{"name": name, "steps": [{"load": _payload(ROWS[row], auto=auto, **extra)}]}
             for name, (row, auto, extra, _, _) in COPY.items()]
    out = _detail(cases)
    return {name: out[name][0] for name in COPY}


@pytest.mark.parametrize("name", list(COPY))
def test_detail_update_row_copy_per_state(copy_views, name):
    row, auto, extra, text, meta = COPY[name]
    view = copy_views[name]
    assert view["hidden"] is False and view["badge"] is None
    state = next(s for s in ("never", "checking", "baseline", "no_update", "failed") if name.startswith(s))
    warn = state == "failed"
    assert view["lead"] == {"cls": "update-status-warn" if warn else "update-status-text", "text": text,
                            "icon": ICONS[state]}
    assert view["meta"] == meta and view["names"] is None
    assert view["buttons"] == [BUSY if state == "checking" else CHECK]
    assert view["order"] == [view["lead"]["cls"], "check-updates", "update-status-meta"]
    assert view["marks"] == {} and view["checked"] == []


def test_detail_new_state_shows_badge_names_and_select():
    view = _view("new")
    assert view["badge"] == {"cls": "badge status-badge-update", "text": "有新章节 · 2 话", "icon": "bi bi-bell",
                             "title": "上游在你下载之后新出的章节；下载时没选的章节不算"}
    assert view["lead"] is None
    assert view["names"] == "第4话 · 第5话"
    assert view["meta"] == "昨天 20:15 检查确认 · 检查不会自动下载 · 下次约 明天 10:30"
    assert view["buttons"] == [_select("选中这些章节"), CHECK]
    assert view["order"] == ["badge status-badge-update", "select-new-chapters", "check-updates",
                             "update-new-list", "update-status-meta"]
    off = _view("new", auto=False)
    assert off["meta"] == "昨天 20:15 检查确认 · 检查不会自动下载 · 自动检查已关闭"
    # more than 5: the first five names, then how many in total
    many = _row(baseline_ids=BASE, baseline_source="download", result="new", last_success_at=TODAY_1330,
                new_chapters=[_chapter(str(80 + n), n) for n in range(1, 8)], next_check_at=TOMORROW_1030)
    out = _detail([{"name": "m", "steps": [{"load": _payload(many)}]}])["m"][0]
    assert out["names"] == "第1话 · 第2话 · 第3话 · 第4话 · 第5话 … 等 7 话"
    assert out["badge"]["text"] == "有新章节 · 7 话"


def test_detail_changed_state():
    view = _view("changed")
    assert view["badge"] == {"cls": "badge status-badge-warning", "text": "章节列表有变动", "icon": "bi bi-exclamation-triangle",
                             "title": "上游新出现了章节，同时有以前的章节不见了（可能删除或重新上传）"}
    assert view["lead"] == {"cls": "update-status-text", "text": "新出现 1 话，另有 1 话已不在上游", "icon": None}
    assert view["names"] == "第3话（重新上传）"
    assert view["meta"] == "今天 13:30 检查 · 请先核对下面的章节列表再决定是否下载"
    assert view["buttons"] == [_select("选中新出现的章节"), CHECK]


@pytest.mark.parametrize("name, auto, badge, meta", [
    ("failed_with_new", True, "有新章节 · 1 话",
     "最近一次检查没成功（上游暂时找不到这部漫画（可能已下架）），约 30 分钟后 自动重试；上面的新章节是 昨天 20:15 确认的"),
    ("failed_with_changed", False, "章节列表有变动",
     "最近一次检查没成功（服务器返回的章节列表无法识别），可以稍后再点「立即检查」；上面的新章节是 昨天 20:15 确认的"),
])
def test_detail_failure_keeps_confirmed_new_chapters_on_screen(name, auto, badge, meta):
    view = _view(name, auto=auto)
    assert view["badge"]["text"] == badge and view["meta"] == meta
    assert view["buttons"][0]["action"] == "select-new-chapters" and view["marks"] == {"74": [
        {"text": "新", "title": "昨天 20:15 检查确认的新章节", "cls": "badge status-badge-update ms-1 update-new-mark"}]}


def test_detail_hidden_when_not_eligible():
    out = _detail([
        {"name": "never", "steps": [{"load": NOT_ELIGIBLE}]},
        # a stale row after the local files were deleted: the row and the 新 marks disappear
        {"name": "gone", "steps": [{"load": _payload(ROWS["new"])}, {"load": NOT_ELIGIBLE}]},
        # a failed GET keeps whatever is shown (no request of its own for 立即检查)
        {"name": "error", "steps": [{"load": {"status": "error", "message": "x"}}]},
    ])
    never = out["never"][0]
    assert never["hidden"] is True and never["order"] == [] and never["buttons"] == []
    assert never["calls"] == [{"url": "/api/updates/3001", "method": "GET", "body": None,
                               "abortKey": "detail-update-status", "timeoutMs": 15000}]
    shown, gone = out["gone"]
    assert shown["hidden"] is False and set(shown["marks"]) == {"74", "75"}
    assert gone["hidden"] is True and gone["order"] == [] and gone["marks"] == {}
    assert out["error"][0]["hidden"] is True and out["error"][0]["calls"][0]["url"] == "/api/updates/3001"


# name → (row, outcome, toast). The answers are built when a test runs (_outcome_answer), never at collection:
# _payload imports core, which binds its module-level paths (database, logs) to whatever app root is set then
OUTCOMES = {
    "new": ("new", "new", ["发现 2 话新章节（只提示，不会自动下载）", "success"]),
    "changed": ("changed", "changed", ["章节列表有变动，请核对", "warning"]),
    "no_update": ("no_update", "no_update", ["没有新章节", "info"]),
    "baseline": ("baseline", "baseline", ["已记下上游现有的 12 话", "info"]),
    "failed": ("failed", "failed", ["这次没检查成功：连不上服务器", "warning"]),
    "throttled": ("no_update", "throttled", ["刚刚检查过，结果如上", "info"]),
    "coalesced_new": ("new", "coalesced", ["发现 2 话新章节（只提示，不会自动下载）", "success"]),
    "coalesced_no_update": ("no_update", "coalesced", ["没有新章节", "info"]),
}


def _outcome_answer(name):
    row, outcome, _ = OUTCOMES[name]
    return _payload(ROWS[row], outcome=outcome)


ERRORS = {
    "busy": ({"status": 409, "message": "正在检查别的漫画，请稍后再试"}, ["正在检查别的漫画，请稍后再试", "warning"]),
    "not_target": ({"status": 409, "message": "本地还没有已下载的内容，不检查新章节"},
                   ["本地还没有已下载的内容，不检查新章节", "warning"]),
    "server": ({"status": 500, "message": "检查新章节失败"}, ["检查新章节失败", "danger"]),
    "timeout": ({"isTimeout": True, "message": "请求超时，请稍后重试"}, ["请求超时，请稍后重试", "danger"]),
    "transport": ({"name": "TypeError", "message": "Failed to fetch"}, ["网络错误", "danger"]),
}


@pytest.fixture(scope="module")
def check_runs():
    before = _payload(ROWS["never_download"])
    cases = [{"name": name, "steps": [{"load": before},
                                      {"click": "check-updates", "answer": {"ok": _outcome_answer(name)}}]}
             for name in OUTCOMES]
    cases += [{"name": name, "steps": [{"load": before},
                                       {"click": "check-updates", "answer": {"error": error}, "refresh": before}]}
              for name, (error, _) in ERRORS.items()]
    cases.append({"name": "abort", "steps": [{"load": before}, {"click": "check-updates",
                                                                "answer": {"error": {"name": "AbortError"}}}]})
    cases.append({"name": "twice", "steps": [{"load": before}, {"click": "check-updates", "again": True,
                                                                "answer": {"ok": _outcome_answer("no_update")}}]})
    return _detail(cases)


@pytest.mark.parametrize("name", list(OUTCOMES))
def test_detail_toasts_per_outcome(check_runs, name):
    toast = OUTCOMES[name][2]
    loaded, during, after = check_runs[name]
    assert during["toasts"] == [] and after["toasts"] == [toast]
    # while the check runs: 正在检查新章节… and a disabled 检查中… button
    assert during["lead"]["text"] == "正在检查新章节…" and during["buttons"][-1] == BUSY
    # afterwards the row shows the answer (the same shape as GET)
    assert after["buttons"][-1] == CHECK and after["hidden"] is False
    expected = {"new": "有新章节 · 2 话", "coalesced_new": "有新章节 · 2 话", "changed": "章节列表有变动"}.get(name)
    if expected:
        assert after["badge"]["text"] == expected
    else:
        assert after["badge"] is None and after["lead"]["text"] != "正在检查新章节…"


@pytest.mark.parametrize("name", list(ERRORS))
def test_detail_check_errors_show_the_server_message_or_network_error(check_runs, name):
    _, toast = ERRORS[name]
    loaded, during, after = check_runs[name]
    assert after["toasts"] == [toast]
    # the row comes back (button usable again) and the state is read again, read-only
    assert after["buttons"][-1] == CHECK and after["lead"]["text"] == "还没检查过新章节"
    assert [c["url"] for c in after["calls"]] == ["/api/updates/3001"]
    assert after["calls"][0]["method"] == "GET"


def test_detail_check_aborted_by_leaving_the_page_is_silent(check_runs):
    loaded, during, after = check_runs["abort"]
    # no toast; the row leaves 检查中… and the state is read again (read-only), e.g. after a bfcache return
    assert after["toasts"] == [] and after["buttons"][-1] == CHECK
    assert [(c["url"], c["method"]) for c in after["calls"]] == [("/api/updates/3001", "GET")]


def test_detail_check_button_only_posts_update_endpoint(check_runs):
    for name, steps in check_runs.items():
        if name == "htmlParsed":
            continue
        urls = [c["url"] for step in steps for c in step["calls"]]
        assert not any("/api/jobs" in url for url in urls), name
        assert set(urls) <= {"/api/updates/3001", "/api/updates/3001/check"}, name
        posts = [c for step in steps for c in step["calls"] if c["url"] == "/api/updates/3001/check"]
        # exactly one POST per click: no body (the server ignores it anyway), 100 s for lock wait + check
        assert posts == [{"url": "/api/updates/3001/check", "method": "POST", "body": None,
                          "abortKey": "detail-update-check", "timeoutMs": 100000}], name
    # a second click (or call) while the first check runs sends nothing
    assert [c["url"] for c in check_runs["twice"][1]["calls"]] == ["/api/updates/3001/check"]
    block = _read("detail.js")
    block = block[block.index(DETAIL_START):block.index(DETAIL_END)]
    # no download path at all: not the jobs API, not the download buttons, no synthetic clicks
    for forbidden in ("/api/jobs", "createDownloadJob", "download-selected-btn", "download-all-btn", ".click("):
        assert forbidden not in block, forbidden


@pytest.fixture(scope="module")
def select_runs():
    chapters = [{"id": str(n), "title": f"第{n - 70}话"} for n in (71, 72, 73, 74, 75)]
    return _detail([
        {"name": "select", "checked": ["71"], "steps": [{"load": _payload(ROWS["new"])},
                                                        {"click": "select-new-chapters"}]},
        {"name": "all_new", "chapters": chapters[3:], "steps": [{"load": _payload(ROWS["new"])},
                                                                {"click": "select-new-chapters"}]},
        {"name": "missing", "chapters": chapters[:4], "checked": ["72"],
         "steps": [{"load": _payload(ROWS["new"])}, {"click": "select-new-chapters"}]},
        {"name": "changed", "steps": [{"load": _payload(ROWS["changed"])}, {"click": "select-new-chapters"}]},
    ])


def test_select_new_chapters_only_ticks_checkboxes(select_runs):
    loaded, clicked = select_runs["select"]
    assert loaded["checked"] == ["71"]                      # showing the row ticks nothing
    assert clicked["checked"] == ["74", "75"]               # exactly the new chapters
    assert clicked["selectAll"] == [False, False]
    assert clicked["scrolls"] == [["74", [{"block": "center", "behavior": "instant"}]]]
    assert clicked["toasts"] == [["已选中 2 话新章节，点「下载选中章节」开始下载", "info"]]
    assert clicked["calls"] == []                           # no request at all, least of all /api/jobs
    all_new = select_runs["all_new"][1]
    assert all_new["checked"] == ["74", "75"] and all_new["selectAll"] == [True, True]
    changed = select_runs["changed"][1]
    assert changed["checked"] == ["74"] and changed["calls"] == []


def test_select_refreshes_the_table_when_it_lacks_the_new_chapters(select_runs):
    # PR-B: instead of asking for a page reload, the chapter table is fetched again (read-only) and then selected;
    # tests/test_batch_downloads_ui.py follows the refresh through to the ticked rows
    loaded, clicked = select_runs["missing"]
    assert clicked["checked"] == ["72"] and clicked["scrolls"] == []   # nothing ticked before the table is refreshed
    assert clicked["toasts"] == [["章节列表里还没有这些新章节，正在刷新章节列表…", "info"]]
    assert clicked["calls"] == [{"url": "/api/album/3001", "method": "GET", "body": None,
                                 "abortKey": "detail-chapter-refresh", "timeoutMs": 30000}]


def test_detail_marks_only_confirmed_new_rows():
    out = _detail([
        {"name": "marks", "steps": [{"load": _payload(ROWS["new"])},
                                    # the new chapters were downloaded (absorbed): the marks go away
                                    {"load": _payload(ROWS["no_update"])}]},
        {"name": "never", "steps": [{"load": _payload(ROWS["never_download"])}]},
        {"name": "checking", "steps": [{"load": _payload(ROWS["new"], checking=True)}]},
    ])
    marked, cleared = out["marks"]
    mark = {"text": "新", "title": "昨天 20:15 检查确认的新章节", "cls": "badge status-badge-update ms-1 update-new-mark"}
    assert marked["marks"] == {"74": [mark], "75": [mark]}
    assert marked["rowTitles"] == ["第1话", "第2话", "第3话", "第4话新", "第5话新"]
    assert marked["checked"] == []                          # marks never tick anything
    assert cleared["marks"] == {} and cleared["rowTitles"] == ["第1话", "第2话", "第3话", "第4话", "第5话"]
    assert out["never"][0]["marks"] == {}
    # a background check of this album: the marks stay, the row says 正在检查新章节… and polls
    checking = out["checking"][0]
    assert set(checking["marks"]) == {"74", "75"} and checking["buttons"] == [BUSY]


def test_detail_polls_while_a_background_check_runs():
    running = _payload(ROWS["no_update"], checking=True)
    done = _payload(ROWS["new"])
    out = _detail([
        {"name": "done", "steps": [{"load": running}, {"runTimers": True, "refresh": done}, {"runTimers": True}]},
        {"name": "cap", "steps": [{"load": running}] + [{"runTimers": True, "refresh": running}] * 45},
    ])
    first, polled, idle = out["done"]
    assert first["timers"] == [3000] and first["lead"]["text"] == "正在检查新章节…"
    assert [c["url"] for c in polled["calls"]] == ["/api/updates/3001"] and polled["badge"]["text"] == "有新章节 · 2 话"
    assert polled["timers"] == [] and idle["calls"] == []
    gets = sum(len(step["calls"]) for step in out["cap"])
    assert gets == 41                                       # the first read + at most 40 polls


def test_remote_titles_rendered_as_text():
    evil = '<img src=x onerror=alert(1)>'
    row = _row(baseline_ids=BASE, baseline_source="download", result="new", last_success_at=TODAY_1330,
               new_chapters=[_chapter("74", 4, evil)], next_check_at=TOMORROW_1030)
    out = _detail([{"name": "xss", "steps": [{"load": _payload(row)}, {"click": "select-new-chapters"}]}])
    view = out["xss"][0]
    assert view["names"] == evil and view["images"] == 0
    assert out["htmlParsed"] == []                          # nothing in the update block is parsed as HTML
    block = _read("detail.js")
    block = block[block.index(DETAIL_START):block.index(DETAIL_END)]
    assert "innerHTML" not in block and "insertAdjacentHTML" not in block


def test_detail_block_is_wired_outside_the_offline_slice():
    source = _read("detail.js")
    # the existing offline harness evaluates setOfflineStatus … bindEvents: the update code stays out of it
    offline = source[source.index("function setOfflineStatus"):source.index("function bindEvents")]
    assert "update" not in offline.lower().replace("setofflinestatus", "")
    start, end = source.index(DETAIL_START), source.index(DETAIL_END)
    assert source.index("function createDownloadJob") < start < end < source.index("// ── 收藏功能")
    load = source[source.index("function loadAlbum"):source.index("// 无封面或封面加载失败")]
    assert load.index("refreshOfflineStatus();") < load.index("refreshUpdateStatus();")
    pageshow = source[source.index("window.addEventListener('pageshow'"):]
    assert "refreshOfflineStatus();\n            refreshUpdateStatus();" in pageshow.replace("\r\n", "\n")
    render = source[source.index("function renderAlbum"):source.index("function bindBackLink")]
    offline_div = render.index('id="album-offline-status"')
    update_div = render.index('<div id="album-update-status" class="update-status" role="status" aria-live="polite" hidden></div>')
    assert offline_div < update_div < render.index('<div class="row mt-3">')


# ─── utils.js window.updateBadges ─────────────────────────────────────────────

BADGES_HARNESS = FAKE_DOM + r"""
const [utilsPath] = process.argv.slice(1);
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
fixClock(input.now);
(0, eval)(fs.readFileSync(utilsPath, 'utf8'));
const B = window.updateBadges;
const view = n => n && { tag: n.tagName, cls: n.className, title: n.title, href: n.href || null,
                         text: n.textContent, html: n.outerHTML };
const out = { chips: {}, time: {}, after: {}, about: {}, reason: {} };
for (const [name, update, opts] of input.chips) out.chips[name] = view(B.chip(update, opts || {}));
out.html = input.chips.map(([, update, opts]) => B.chipHtml(update, opts || {}));
for (const iso of input.times) out.time[String(iso)] = B.formatTime(iso, input.now);
for (const iso of input.afters) out.after[String(iso)] = B.formatAfter(iso, input.now);
for (const iso of input.afters) out.about[String(iso)] = B.about(iso, input.now);
out.defaultNow = [B.formatTime('2026-07-15T15:00:00'), B.formatAfter('2026-07-15T16:00:00')];
for (const kind of ['network', 'timeout', 'not_found', 'upstream_error', 'other', null]) out.reason[String(kind)] = B.reasonText(kind);
process.stdout.write(JSON.stringify(out));
"""

NEW_UPDATE = {"state": "new", "new_count": 2, "removed_count": 0, "confirmed_at": "2026-07-15T15:00:00",
              "titles": ["第13话", "第14话"]}
CHANGED_UPDATE = {"state": "changed", "new_count": 1, "removed_count": 2, "confirmed_at": TODAY_1030, "titles": ["x"]}


@pytest.fixture(scope="module")
def badges():
    chips = [
        ["none", None, {}],
        ["zero", {"state": "new", "new_count": 0, "removed_count": 0, "confirmed_at": None, "titles": []}, {}],
        ["no_update", {"state": "no_update", "new_count": 0}, {}],
        ["new", NEW_UPDATE, {}],
        ["new_many", dict(NEW_UPDATE, new_count=7, titles=["a", "b", "c", "d", "e"]), {}],
        ["new_untitled", dict(NEW_UPDATE, titles=["", " "]), {}],
        ["compact", NEW_UPDATE, {"compact": True}],
        ["changed", CHANGED_UPDATE, {}],
        ["changed_compact", CHANGED_UPDATE, {"compact": True}],
        ["link", dict(NEW_UPDATE, new_count=1), {"link": True, "albumId": "900500"}],
        ["link_changed", CHANGED_UPDATE, {"link": True, "albumId": 900009}],
        ["link_bad_id", NEW_UPDATE, {"link": True, "albumId": "12a"}],
        ["xss", dict(NEW_UPDATE, titles=['<img src=x onerror=alert(1)>']), {}],
    ]
    times = ["2026-07-15T15:29:30", "2026-07-15T15:29:01", "2026-07-15T15:29:00", "2026-07-15T14:30:01",
             "2026-07-15T14:30:00", "2026-07-15T00:00:00", "2026-07-14T23:59:00", "2026-07-14T00:00:00",
             "2026-07-13T09:05:00", "2026-01-01T00:00:00", "2025-12-31T08:00:00", "2026-07-15T15:30:40",
             "2026-07-15T18:00:00", None, "", "not a date"]
    afters = ["2026-07-15T15:29:00", "2026-07-15T15:30:00", "2026-07-15T15:30:30", "2026-07-15T16:29:00",
              "2026-07-15T16:30:00", "2026-07-15T21:29:00", "2026-07-15T21:30:00", "2026-07-15T23:59:00",
              "2026-07-16T08:00:00", "2026-07-20T09:00:00", None]
    return _node(BADGES_HARNESS, STATIC_JS / "utils.js",
                 data={"now": NOW, "chips": chips, "times": times, "afters": afters})


def test_update_badges_chip_only_for_confirmed_facts(badges):
    chips = badges["chips"]
    assert chips["none"] is None and chips["zero"] is None and chips["no_update"] is None
    assert chips["new"]["cls"] == "badge status-badge-update" and chips["new"]["tag"] == "SPAN"
    assert chips["new"]["text"] == " 有新章节 · 2 话"
    assert chips["new"]["title"] == "上游在你下载之后新出了 2 话（30 分钟前 确认）：第13话、第14话。检查不会自动下载，打开详情选择下载"
    assert '<i class="bi bi-bell" aria-hidden="true"></i>' in chips["new"]["html"]
    assert chips["new_many"]["title"] == "上游在你下载之后新出了 7 话（30 分钟前 确认）：a、b、c、d、e…。检查不会自动下载，打开详情选择下载"
    assert chips["new_untitled"]["title"] == "上游在你下载之后新出了 2 话（30 分钟前 确认）。检查不会自动下载，打开详情选择下载"
    compact = chips["compact"]
    assert compact["text"] == " 有新章节 · 2 话" and compact["title"] == chips["new"]["title"]
    assert compact["html"] == ('<span class="badge status-badge-update" title="' + chips["new"]["title"] + '">'
                               '<i class="bi bi-bell" aria-hidden="true"></i> <span class="visually-hidden">有</span>'
                               '新章节 · 2<span class="visually-hidden"> 话</span></span>')
    for name in ("changed", "changed_compact"):
        assert chips[name]["cls"] == "badge status-badge-warning" and chips[name]["text"] == " 章节有变动"
        assert chips[name]["title"] == "上游新出现 1 话，另有 2 话已不在上游；打开详情核对"
        assert '<i class="bi bi-exclamation-triangle" aria-hidden="true"></i>' in chips[name]["html"]


def test_update_badges_link_variant_for_downloads(badges):
    chips = badges["chips"]
    assert chips["link"]["html"] == (
        '<a class="badge status-badge-update" href="/album/900500" title="上游在你下载之后新出了 1 话（30 分钟前 确认）；'
        '打开详情选择下载"><i class="bi bi-bell" aria-hidden="true"></i> 有新章节 · 1 话</a>')
    assert chips["link_changed"]["html"] == (
        '<a class="badge status-badge-warning" href="/album/900009" title="上游新出现 1 话，另有 2 话已不在上游；打开详情核对">'
        '<i class="bi bi-exclamation-triangle" aria-hidden="true"></i> 章节有变动</a>')
    assert chips["link_bad_id"]["tag"] == "SPAN" and chips["link_bad_id"]["href"] is None
    # chipHtml: '' when there is nothing to show
    assert badges["html"][:3] == ["", "", ""] and badges["html"][9] == chips["link"]["html"]


def test_update_badges_time_formats(badges):
    assert badges["time"] == {
        "2026-07-15T15:29:30": "刚刚", "2026-07-15T15:29:01": "刚刚", "2026-07-15T15:29:00": "1 分钟前",
        "2026-07-15T14:30:01": "59 分钟前", "2026-07-15T14:30:00": "今天 14:30", "2026-07-15T00:00:00": "今天 00:00",
        "2026-07-14T23:59:00": "昨天 23:59", "2026-07-14T00:00:00": "昨天 00:00", "2026-07-13T09:05:00": "7月13日 09:05",
        "2026-01-01T00:00:00": "1月1日 00:00", "2025-12-31T08:00:00": "2025年12月31日 08:00",
        "2026-07-15T15:30:40": "刚刚", "2026-07-15T18:00:00": "今天 18:00", "null": "", "": "", "not a date": "",
    }
    assert badges["after"] == {
        "2026-07-15T15:29:00": "稍后", "2026-07-15T15:30:00": "稍后", "2026-07-15T15:30:30": "约 1 分钟后",
        "2026-07-15T16:29:00": "约 59 分钟后", "2026-07-15T16:30:00": "约 1 小时后", "2026-07-15T21:29:00": "约 6 小时后",
        "2026-07-15T21:30:00": "今天 21:30", "2026-07-15T23:59:00": "今天 23:59", "2026-07-16T08:00:00": "明天 08:00",
        "2026-07-20T09:00:00": "7月20日 09:00", "null": "稍后",
    }
    # “约 …” for sentences: never “约 约”, and 稍后 stays 稍后
    assert badges["about"]["2026-07-15T16:29:00"] == "约 59 分钟后"
    assert badges["about"]["2026-07-16T08:00:00"] == "约 明天 08:00"
    assert badges["about"]["null"] == "稍后"
    assert badges["defaultNow"] == ["30 分钟前", "约 30 分钟后"]   # without `now` it uses the current time


def test_update_badges_reason_text(badges):
    assert badges["reason"] == {"network": "连不上服务器", "timeout": "服务器响应太慢",
                                "not_found": "上游暂时找不到这部漫画（可能已下架）",
                                "upstream_error": "服务器返回的章节列表无法识别", "other": "原因未知", "null": "原因未知"}


def test_update_badges_titles_are_text(badges):
    xss = badges["chips"]["xss"]
    assert "<img src=x onerror=alert(1)>" in xss["title"]            # the literal title attribute value
    assert "<img" not in xss["html"].replace("&lt;img", "")          # serialized escaped, never markup
    utils = _read("utils.js")
    assert "innerHTML" not in utils[utils.index("检查新章节的标记与文案"):]


# ─── 资源库 / 收藏: chips guarded by window.updateBadges ─────────────────────────

LIST_HARNESS = FAKE_DOM + r"""
const [utilsPath] = process.argv.slice(1);
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
fixClock(input.now);
(0, eval)(fs.readFileSync(utilsPath, 'utf8'));
(0, eval)(input.code);
const out = {};
for (const [withBadges, run] of [[true, 'with'], [false, 'without']]) {
  const saved = window.updateBadges;
  if (!withBadges) delete window.updateBadges;
  out[run] = input.items.map(item => {
    const box = el('div');
    box.appendChild(window[input.entry](item));
    return box.all().filter(n => n.classList.contains('badge') || n.classList.contains('offline-badge'))
      .map(n => ({ cls: n.className, text: n.textContent.trim(), title: n.title }));
  });
  window.updateBadges = saved;
}
process.stdout.write(JSON.stringify(out));
"""

LIST_ITEMS = [
    {"album_id": "1", "status_group": "readable", "readable": True, "update": NEW_UPDATE},
    {"album_id": "2", "status_group": "readable", "readable": True, "update": CHANGED_UPDATE, "archive": "cbz"},
    {"album_id": "3", "status_group": "readable", "readable": True, "update": None},
    {"album_id": "4", "status_group": "readable", "readable": True,
     "update": {"state": "new", "new_count": 0, "removed_count": 0, "confirmed_at": None, "titles": []}},
    # not readable (a favourite that was never downloaded / files deleted): nothing, even with a stale update
    {"album_id": "5", "status_group": "none", "readable": False, "update": NEW_UPDATE},
    {"album_id": "6", "status_group": "readable", "readable": True, "update": NEW_UPDATE, "activity": "queued"},
]


def _list(script, names, entry, extra=""):
    source = _read(script)
    code = extra + "\n".join(_js_block(source, marker) for marker in names) + f"\nwindow.{entry} = {entry};"
    return _node(LIST_HARNESS, STATIC_JS / "utils.js",
                 data={"now": NOW, "items": LIST_ITEMS, "entry": entry, "code": code})


def test_library_chip_after_the_offline_and_archive_badges():
    source = _read("library.js")
    out = _list("library.js", ["function el(", "function icon(", "function badge(", "var ACTIVITY_BADGES",
                               "function activityBadge(", "function offlineBadge(", "function getStatusBadge("],
                "getStatusBadge")
    new, changed, plain, zero, unread, active = out["with"]
    assert [b["text"] for b in new] == ["已下载内容 · 可离线阅读", "有新章节 · 2 话"]
    assert new[1]["cls"] == "badge status-badge-update"
    assert new[1]["title"] == "上游在你下载之后新出了 2 话（30 分钟前 确认）：第13话、第14话。检查不会自动下载，打开详情选择下载"
    assert [b["text"] for b in changed] == ["已下载内容 · 可离线阅读", "CBZ", "章节有变动"]
    assert changed[2]["title"] == "上游新出现 1 话，另有 2 话已不在上游；打开详情核对"
    assert [b["text"] for b in plain] == ["已下载内容 · 可离线阅读"] and len(zero) == 1
    assert [b["text"] for b in unread] == ["未下载"]
    assert [b["text"] for b in active] == ["已下载内容 · 可离线阅读", "有新章节 · 2 话", "排队中"]
    # without utils.js's helper (e.g. the test_library_card_click stub) the cards still render, just without chips
    assert [[b["text"] for b in badges] for badges in out["without"]] == [
        ["已下载内容 · 可离线阅读"], ["已下载内容 · 可离线阅读", "CBZ"], ["已下载内容 · 可离线阅读"],
        ["已下载内容 · 可离线阅读"], ["未下载"], ["已下载内容 · 可离线阅读", "排队中"]]
    status = _js_block(source, "function getStatusBadge(")
    assert "if (item.update && window.updateBadges) {" in status
    assert status.index("window.localBadges.archive(item.archive)") < status.index("window.updateBadges.chip(item.update)")


def test_wishlist_compact_chip_only_for_readable_rows():
    source = _read("wishlist.js")
    out = _list("wishlist.js", ["function el(", "function icon(", "function badge(", "function activityBadge(",
                                "function statusBadges("], "statusBadges")
    new, changed, plain, zero, unread, active = out["with"]
    assert [b["text"] for b in new] == ["已下载内容 · 可离线阅读", "有新章节 · 2 话"]   # visible: 新章节 · 2
    assert new[1]["title"].startswith("上游在你下载之后新出了 2 话") and new[1]["cls"] == "badge status-badge-update"
    assert [b["text"] for b in changed] == ["已下载内容 · 可离线阅读", "CBZ", "章节有变动"]
    assert len(plain) == 1 and len(zero) == 1 and [b["text"] for b in unread] == ["未下载"]
    assert [b["text"] for b in active] == ["已下载内容 · 可离线阅读", "有新章节 · 2 话", "排队中"]
    assert all("有新章节" not in b["text"] and "章节有变动" not in b["text"]
               for badges in out["without"] for b in badges)
    status = _js_block(source, "function statusBadges(")
    assert "if (item.update && window.updateBadges) {" in status
    assert "window.updateBadges.chip(item.update, { compact: true })" in status


def test_list_chips_guarded_by_update_badges():
    # test_library_card_click's stand-in window has no updateBadges: every use must be guarded
    for script in ("library.js", "wishlist.js", "downloads.js"):
        source = _read(script)
        uses = [m.start() for m in re.finditer(r"window\.updateBadges\.", source)]
        assert uses, script
        for at in uses:
            assert "window.updateBadges" in source[max(0, at - 200):at], (script, at)
    assert "item.update" not in _read("search.js") and "updateBadges" not in _read("home.js")


# ─── 下载管理: the chip on the newest completed card per album only ─────────────

DOWNLOADS_HARNESS = FAKE_DOM + r"""
const [utilsPath, downloadsPath] = process.argv.slice(1);
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
fixClock(input.now);
(0, eval)(fs.readFileSync(utilsPath, 'utf8'));
for (const id of ['downloadTabs', 'downloadTabsContent', 'running-section', 'queued-section', 'completed-section',
                  'failed-section', 'running-count', 'queued-count', 'completed-count', 'failed-count']) {
  const n = el('div'); n.id = id; body.appendChild(n);
}
const calls = [];
global.showToast = () => {};
global.setInterval = () => 1;
global.clearInterval = () => {};
window._jobTitleMap = {};
window.setSSECallbacks = () => {};
window.connectSSE = () => {};
window.disconnectAllSSE = () => {};
window.apiFetch = global.apiFetch = (url, opts) => {
  opts = opts || {};
  calls.push({ url, method: opts.method || 'GET', body: opts.body ? JSON.parse(opts.body) : null, abortKey: opts.abortKey || null });
  if (url === '/api/jobs') return Promise.resolve({ status: 'ok', jobs: input.jobs });
  if (url === '/api/preview/available') {
    const ids = JSON.parse(opts.body).album_ids;
    return Promise.resolve({ status: 'ok', readable: ids.filter(id => input.readable.includes(id)), archives: {},
      local: Object.fromEntries(ids.filter(id => input.readable.includes(id)).map(id => [id, { state: 'loose', archive: null }])) });
  }
  if (url === '/api/updates/states') {
    const ids = JSON.parse(opts.body).album_ids;
    return Promise.resolve({ status: 'ok', updates: Object.fromEntries(Object.entries(input.updates)) });
  }
  return new Promise(() => {});
};
(0, eval)(fs.readFileSync(downloadsPath, 'utf8'));
(async () => {
  (docHandlers.DOMContentLoaded || []).forEach(fn => fn());
  await flush();
  const html = document.getElementById('completed-section').innerHTML;
  const cards = {};
  html.split('<div class="card job-card').slice(1).forEach(part => {
    const id = part.match(/data-job-id="([^"]+)"/)[1];
    const status = part.match(/<div class="job-card-status">([\s\S]*?)<\/div>/)[1];
    cards[id] = status;
  });
  const failed = document.getElementById('failed-section').innerHTML;
  process.stdout.write(JSON.stringify({ calls, cards, failed }));
})().catch(err => { console.error(err && err.stack || err); process.exit(1); });
"""


def _job(job_id, album_id, status="completed"):
    return {"job_id": job_id, "album_id": album_id, "title": f"Title {job_id}", "status": status,
            "output_path": f"D:/x/{job_id}", "total_pages": 1, "done_pages": 1}


def _downloads(jobs, readable, updates):
    return _node(DOWNLOADS_HARNESS, STATIC_JS / "utils.js", STATIC_JS / "downloads.js",
                 data={"now": NOW, "jobs": jobs, "readable": readable, "updates": updates})


def test_downloads_chip_only_on_newest_completed_card():
    jobs = [_job("j_new", "900500"), _job("j_old", "900500"), _job("j_11", "900011"), _job("j_9", "900009"),
            _job("j_2", "900002"), _job("j_700", "900700"), _job("j_600", "900600", "failed")]
    updates = {"900500": dict(NEW_UPDATE, new_count=1), "900009": CHANGED_UPDATE,
               "900700": NEW_UPDATE}   # never sent for 900700 (not readable); even if it were, no chip
    out = _downloads(jobs, ["900500", "900011", "900009", "900002"], updates)
    states = [c for c in out["calls"] if c["url"] == "/api/updates/states"]
    # only the readable albums are asked about, read-only, sorted like readableKey
    assert states == [{"url": "/api/updates/states", "method": "POST", "abortKey": "downloads-updates-0",
                       "body": {"album_ids": ["900002", "900009", "900011", "900500"]}}]
    assert not any(c["url"].startswith("/api/jobs") and c["method"] != "GET" for c in out["calls"])
    cards = out["cards"]
    chip = ('<a class="badge status-badge-update" href="/album/900500" title="上游在你下载之后新出了 1 话（30 分钟前 确认）；'
            '打开详情选择下载"><i class="bi bi-bell" aria-hidden="true"></i> 有新章节 · 1 话</a>')
    assert cards["j_new"].endswith("可离线阅读</span>" + chip)          # right after the offline marker
    assert "status-badge-update" not in cards["j_old"] and "有新章节" not in cards["j_old"]
    assert cards["j_9"].endswith('<i class="bi bi-exclamation-triangle" aria-hidden="true"></i> 章节有变动</a>')
    assert 'href="/album/900009"' in cards["j_9"]
    for job_id in ("j_11", "j_2", "j_700"):
        assert "有新章节" not in cards[job_id] and "章节有变动" not in cards[job_id], job_id
    assert "有新章节" not in out["failed"]


def test_downloads_update_states_are_chunked_like_readable():
    jobs = [_job(f"j{n}", str(800000 + n)) for n in range(201)]
    readable = [str(800000 + n) for n in range(201)]
    out = _downloads(jobs, readable, {})
    states = [c for c in out["calls"] if c["url"] == "/api/updates/states"]
    assert [c["abortKey"] for c in states] == ["downloads-updates-0", "downloads-updates-1"]
    assert [len(c["body"]["album_ids"]) for c in states] == [200, 1]


# ─── 设置: the section and its status panel ─────────────────────────────────────


def test_settings_template_switch_and_copy(client):
    html = client.get("/settings").get_data(as_text=True)
    section = html[html.index('<div class="settings-section" id="update-check">'):]
    section = section[:section.index("<!-- ===== 界面操作 ===== -->")]
    assert html.index("定时下载") < html.index('id="update-check"') < html.index("界面操作")
    switch = re.search(r'<input type="checkbox" id="auto_update_check" name="auto_update_check"[^>]*>', section).group(0)
    assert 'role="switch"' in switch and 'value="true"' in switch and 'aria-describedby="auto_update_check_help"' in switch
    assert "checked" in switch                               # on by default
    assert '<label class="form-check-label" for="auto_update_check"><strong>自动检查已下载漫画的新章节</strong></label>' in section
    assert "<strong>从不下载任何内容</strong>" in section and "关闭后仍可在漫画详情页点「立即检查」" in section
    assert "下载时没选的章节不算" in section and "只收藏、没有下载的不查" in section
    assert '<div class="form-text" id="update-check-status" role="status" aria-live="polite">正在读取检查状态…</div>' in section
    assert "<h5><i class=\"bi bi-bell\"></i> 检查新章节</h5>" in section
    client.post("/api/settings", json={"auto_update_check": "false"})
    off = re.search(r'<input type="checkbox" id="auto_update_check"[^>]*>', client.get("/settings").get_data(as_text=True))
    assert "checked" not in off.group(0)


SETTINGS_HARNESS = FAKE_DOM + r"""
const [utilsPath, settingsPath] = process.argv.slice(1);
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
fixClock(input.now);
(0, eval)(fs.readFileSync(utilsPath, 'utf8'));
const src = fs.readFileSync(settingsPath, 'utf8');
const START = '// ── 检查新章节状态 ──', END = '// ── 检查新章节状态结束 ──';
(0, eval)(src.slice(src.indexOf(START), src.indexOf(END)));
const out = { lines: input.summaries.map(s => updateSummaryLines(s, input.now)) };
const box = el('div'); box.id = 'update-check-status'; box.textContent = '正在读取检查状态…'; body.appendChild(box);
const calls = [];
(async () => {
  for (const answer of [input.summaries[0], { error: { status: 500, message: 'x' } }, { error: { name: 'AbortError' } }]) {
    box.textContent = 'before';
    window.apiFetch = (url, opts) => { calls.push([url, opts.abortKey, opts.timeoutMs, opts.method || 'GET']);
      return answer.error ? Promise.reject(makeError(answer.error)) : Promise.resolve(answer); };
    loadUpdateSummary();
    await flush();
    out[answer.error ? (answer.error.name || 'error') : 'ok'] = box.children.map(c => c.nodeType === 1 ? [c.tagName, c.textContent] : ['#text', c.data]);
  }
  out.calls = calls;
  out.htmlParsed = htmlParsed;
  process.stdout.write(JSON.stringify(out));
})().catch(err => { console.error(err && err.stack || err); process.exit(1); });
"""


def _summary(phase, enabled=True, current=None, resume_at=None, counts=None, checks=0, last=None):
    base = {"targets": 12, "checked": 9, "with_updates": 0, "changed": 0, "failing": 0, "never": 3}
    base.update(counts or {})
    return {"status": "ok", "enabled": enabled,
            "runtime": {"armed": True, "running": True, "phase": phase, "resume_at": resume_at, "current": current},
            "counts": base, "checks_24h": checks, "last": last}


SUMMARIES = [
    (_summary("starting", resume_at="2026-07-15T15:36:00"), "程序刚启动，约 6 分钟后 开始检查"),
    (_summary("idle"), "后台检查中：一次只查一部，每部大约一天一次"),
    (_summary("checking", current={"album_id": "3001", "title": "<b>Some</b> title", "started_at": NOW}),
     "正在检查：<b>Some</b> title"),
    (_summary("checking", current={"album_id": "3001", "title": "", "started_at": NOW}), "正在检查：车号 3001"),
    (_summary("deferred"), "有下载任务在进行，检查等下载结束后继续"),
    (_summary("paused", resume_at="2026-07-15T15:45:00"), "连续几次连不上服务器，自动检查暂停，约 15 分钟后 再试"),
    (_summary("off", enabled=False), "自动检查已关闭。已有的检查结果仍会显示，可以在漫画详情页点「立即检查」。"),
    (_summary("not_running"), "自动检查已开启，但后台检查没有在运行（重启程序后生效）"),
]
LASTS = [
    (None, "还没有检查过"),
    ({"album_id": "3001", "title": "Name", "at": TODAY_1330, "outcome": "no_update", "new_count": 0,
      "upstream_count": 3, "error_kind": None}, "最近一次：今天 13:30 · 《Name》 · 没有新章节"),
    ({"album_id": "3001", "title": "Name", "at": TODAY_1330, "outcome": "new", "new_count": 2,
      "upstream_count": 5, "error_kind": None}, "最近一次：今天 13:30 · 《Name》 · 发现 2 话新章节"),
    ({"album_id": "3001", "title": "Name", "at": TODAY_1330, "outcome": "changed", "new_count": 1,
      "upstream_count": 3, "error_kind": None}, "最近一次：今天 13:30 · 《Name》 · 章节列表有变动"),
    ({"album_id": "3001", "title": "", "at": "2026-07-15T15:29:50", "outcome": "baseline", "new_count": 0,
      "upstream_count": 12, "error_kind": None}, "最近一次：刚刚 · 车号 3001 · 已记下 12 话"),
    ({"album_id": "3001", "title": "Name", "at": YESTERDAY_2015, "outcome": "failed", "new_count": 0,
      "upstream_count": None, "error_kind": "timeout"}, "最近一次：昨天 20:15 · 《Name》 · 没检查成功（服务器响应太慢）"),
]
COUNTS = [
    ({}, True, 0, "本地有内容的漫画 12 部 · 检查过 9 部 · 过去 24 小时检查 0 次"),
    ({"with_updates": 2, "changed": 1, "failing": 1}, True, 17,
     "本地有内容的漫画 12 部 · 检查过 9 部 · 2 部有新章节 · 1 部最近一次没检查成功（会自动重试） · 过去 24 小时检查 17 次"),
    ({"failing": 3}, False, 4,
     "本地有内容的漫画 12 部 · 检查过 9 部 · 3 部最近一次没检查成功（不会自动重试） · 过去 24 小时检查 4 次"),
]


@pytest.fixture(scope="module")
def settings_out():
    summaries = [s for s, _ in SUMMARIES]
    summaries += [_summary("idle", last=last) for last, _ in LASTS]
    summaries += [_summary("idle" if enabled else "off", enabled=enabled, counts=counts, checks=checks)
                  for counts, enabled, checks, _ in COUNTS]
    return _node(SETTINGS_HARNESS, STATIC_JS / "utils.js", STATIC_JS / "settings.js",
                 data={"now": NOW, "summaries": summaries})


def test_settings_summary_copy_per_phase(settings_out):
    lines = settings_out["lines"]
    for n, (_, phase) in enumerate(SUMMARIES):
        assert lines[n][0] == phase
    offset = len(SUMMARIES)
    for n, (_, text) in enumerate(LASTS):
        assert lines[offset + n][2] == text
    offset += len(LASTS)
    for n, (_, _, _, text) in enumerate(COUNTS):
        assert lines[offset + n][1] == text


def test_settings_summary_is_written_as_three_text_lines(settings_out):
    assert settings_out["ok"] == [["DIV", "程序刚启动，约 6 分钟后 开始检查"],
                                  ["DIV", "本地有内容的漫画 12 部 · 检查过 9 部 · 过去 24 小时检查 0 次"],
                                  ["DIV", "还没有检查过"]]
    assert settings_out["error"] == [["#text", "暂时读不到检查状态，请稍后刷新页面"]]
    assert settings_out["AbortError"] == [["#text", "before"]]          # leaving the page: nothing changes
    assert settings_out["calls"][0] == ["/api/updates/summary", "settings-update-summary", 15000, "GET"]
    assert settings_out["htmlParsed"] == []


def test_settings_summary_is_reloaded_with_the_settings():
    source = _read("settings.js").replace("\r\n", "\n")
    load = source[source.index("async function loadSettings"):source.index("// ── 事件绑定")]
    assert load.rstrip().endswith("loadUpdateSummary();\n  }")
    save = source[source.index("async function handleSave"):source.index("// ── 检查新章节状态 ──")]
    assert "showToast('设置已保存', 'success');\n      loadUpdateSummary();" in save
    block = source[source.index("// ── 检查新章节状态 ──"):source.index("// ── 检查新章节状态结束 ──")]
    assert "innerHTML" not in block and "textContent" in block


# ─── style.css ────────────────────────────────────────────────────────────────


def test_update_css_tokens_only_no_animation():
    css = (ROOT / "static" / "css" / "style.css").read_text(encoding="utf-8")
    block = css[css.index("检查新章节（2026-09-28 新增"):]
    block = block[block.index("*/") + 2:]
    if "/* ════" in block:                  # up to the next block (PR-B's batch dialog has its own test)
        block = block[:block.index("/* ════")]
    rules = re.sub(r"/\*.*?\*/", "", block, flags=re.S)
    assert ".badge.status-badge-update {" in rules and ".update-status {" in rules
    for prop, value in re.findall(r"([\w-]+)\s*:\s*([^;{}]+);", rules):
        if "color" in prop or "background" in prop or prop.startswith("border"):
            colours = re.sub(r"\b1px solid\b", "", value).strip()
            assert re.fullmatch(r"var\(--[\w-]+\)", colours), (prop, value)
        assert "#" not in value and "rgb" not in value, (prop, value)
    assert not re.search(r"transition|animation|@keyframes", rules)
    assert set(re.findall(r"(\d+)px", rules)) <= {"1", "4", "8", "12", "14"}
    badge = rules[rules.index(".badge.status-badge-update {"):]
    badge = badge[:badge.index("}")]
    assert "var(--primary-bg)" in badge and "var(--primary)" in badge and "var(--text-primary)" in badge


# ─── tests/ui_reader_fixture.py: the update samples, seeded offline ──────────────


def _fixture_module():
    spec = importlib.util.spec_from_file_location("ui_reader_fixture_for_tests", ROOT / "tests" / "ui_reader_fixture.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fixture_downloads(client, tmp_path, monkeypatch):
    from core import archive_pages, jm_service, local_availability, path_guard, update_checker
    from routes import api_export, api_preview
    root = tmp_path / "downloads"
    root.mkdir()
    for module in (path_guard, api_preview, api_export, jm_service):
        monkeypatch.setattr(module, "DOWNLOAD_ROOT", root)
    archive_pages.clear_cache()
    with local_availability._cache_lock:
        local_availability._cache.clear()
    update_checker._reset_for_tests()
    # install_update_stub assigns the module attribute directly: register it so teardown restores the real fetch
    monkeypatch.setattr(update_checker, "fetch_upstream_episodes", update_checker.fetch_upstream_episodes)
    yield root
    update_checker._reset_for_tests()
    archive_pages.clear_cache()
    with local_availability._cache_lock:
        local_availability._cache.clear()


def test_ui_fixture_update_samples_seed_the_planned_states(client, fixture_downloads, tmp_path, monkeypatch):
    from PIL import Image, ImageDraw
    from core import database as db, update_checker, update_store
    assert Path(db.DB_PATH).resolve().is_relative_to(tmp_path.resolve())
    fixture = _fixture_module()
    detail = fixture.add_update_samples(fixture_downloads, db, Image, ImageDraw)
    states = {album_id: update_store.state_of(update_store.get(album_id)) for album_id in
              ("900001", "900002", "900003", "900004", "900009", "900010", "900011", "900012", "900013",
               "900500", "900700")}
    assert states == {"900001": "never", "900002": "no_update", "900003": "failed", "900004": "baseline",
                      "900009": "changed", "900010": "never", "900011": "new", "900012": "never",
                      "900013": "never", "900500": "new", "900700": "new"}
    assert update_store.get("900001") is None and update_store.get("900012") is None
    failed = update_store.get("900003")
    assert failed["fail_count"] == 2 and failed["error_kind"] == "network" and failed["result"] == "no_update"
    assert update_store.get("900004")["upstream_count"] == 12
    assert update_store.get("900010")["baseline_ids"] == ["91001", "91002", "91003"]
    assert [c["photo_id"] for c in update_store.get("900011")["new_chapters"]] == ["91104"]
    # 900011 is readable and its detail proves 部分章节已下载 · 2/4 话
    db.set_cached_album_detail("900011", json.dumps(detail, ensure_ascii=False))
    assert client.get("/api/local-chapters/900011").get_json()["partial"] == {"downloaded": 2, "total": 4}
    data = client.get("/api/updates/900011").get_json()
    assert data["eligible"] is True and data["update"]["state"] == "new"
    assert client.get("/api/updates/900700").get_json() == {"status": "ok", "album_id": "900700", "eligible": False}
    # 立即检查 goes to the stub only; nothing creates a job
    monkeypatch.setattr(fixture, "SLOW_UPDATE", {})
    metrics = fixture.install_update_stub(update_checker)
    assert update_checker.fetch_upstream_episodes is metrics["fake"]
    from core import jm_service
    monkeypatch.setattr(jm_service, "new_check_client",
                        lambda *a, **k: pytest.fail("the fixture reached the real check client"))
    jobs = len(db.get_all_jobs())
    slow = client.post("/api/updates/900013/check").get_json()
    assert slow["outcome"] == "no_update" and slow["update"]["upstream_count"] == 2
    fail = client.post("/api/updates/900012/check").get_json()
    assert fail["outcome"] == "failed" and fail["update"]["error"]["kind"] == "network"
    conn = db.get_db()
    try:
        last = dict(conn.execute("SELECT requests, error_type FROM update_check_log WHERE album_id='900012' "
                                 "ORDER BY id DESC LIMIT 1").fetchone())
    finally:
        conn.close()
    assert last == {"requests": 1, "error_type": "fixture offline"}   # the stub answered, not a blocked real request
    assert client.post("/api/updates/900701/check").status_code == 409     # never downloaded
    assert metrics["calls"] == ["900013", "900012"] and metrics["max_in_flight"] == 1
    assert len(db.get_all_jobs()) == jobs


def test_ui_fixture_guards_the_stub_before_serving():
    source = (ROOT / "tests" / "ui_reader_fixture.py").read_text(encoding="utf-8")
    main = source[source.index("def main():"):]
    stub = main.index("install_update_stub(update_checker)")
    guard = main.index("assert update_checker.fetch_upstream_episodes is update_metrics[\"fake\"]")
    armed = main.index("assert not update_checker.runtime_status()[\"armed\"]")
    start = main.index("update_checker.start()")
    serve = main.index("serve(app")
    assert stub < guard < armed < start < serve
    assert main.count("update_checker.start()") == 1 and "if args.fast_updates:" in main[armed:start]
    assert '"/test/update-metrics"' in main and "SELECT COUNT(*) FROM jobs" in main


# ─── review round: a download that finishes must clear the 下载管理 chip at once ───

DOWNLOADS_PHASES_HARNESS = FAKE_DOM + r"""
const [utilsPath, downloadsPath] = process.argv.slice(1);
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
fixClock(input.now);
(0, eval)(fs.readFileSync(utilsPath, 'utf8'));
for (const id of ['downloadTabs', 'downloadTabsContent', 'running-section', 'queued-section', 'completed-section',
                  'failed-section', 'running-count', 'queued-count', 'completed-count', 'failed-count']) {
  const n = el('div'); n.id = id; body.appendChild(n);
}
let phase = 0;
let sse = null;
global.showToast = () => {};
global.setInterval = () => 1;
global.clearInterval = () => {};
window._jobTitleMap = {};
window.setSSECallbacks = (cb) => { sse = cb; };
window.connectSSE = () => {};
window.disconnectAllSSE = () => {};
window.apiFetch = global.apiFetch = (url, opts) => {
  opts = opts || {};
  const st = input.phases[phase];
  if (url === '/api/jobs') return Promise.resolve({ status: 'ok', jobs: st.jobs });
  if (url === '/api/preview/available') {
    const ids = JSON.parse(opts.body).album_ids;
    return Promise.resolve({ status: 'ok', readable: ids.filter(id => st.readable.includes(id)), archives: {},
      local: Object.fromEntries(ids.filter(id => st.readable.includes(id)).map(id => [id, { state: 'loose', archive: null }])) });
  }
  if (url === '/api/updates/states') return Promise.resolve({ status: 'ok', updates: st.updates });
  return new Promise(() => {});
};
(0, eval)(fs.readFileSync(downloadsPath, 'utf8'));
function chips() {
  const out = {};
  document.getElementById('completed-section').innerHTML.split('<div class="card job-card').slice(1).forEach(part => {
    const id = part.match(/data-job-id="([^"]+)"/)[1];
    out[id] = /有新章节/.test(part.match(/<div class="job-card-status">([\s\S]*?)<\/div>/)[1]);
  });
  return out;
}
(async () => {
  (docHandlers.DOMContentLoaded || []).forEach(fn => fn());
  await flush();
  const first = chips();
  phase = 1;
  sse.onCompleted();          // the new-chapter job just finished; the server already absorbed its chapters
  await flush();
  process.stdout.write(JSON.stringify({ first, second: chips() }));
})().catch(err => { console.error(err && err.stack || err); process.exit(1); });
"""


def test_downloads_chip_goes_away_when_the_new_chapters_are_downloaded():
    phases = [
        {"jobs": [_job("j_new", "900500", "running"), _job("j_old", "900500")], "readable": ["900500"],
         "updates": {"900500": dict(NEW_UPDATE, new_count=1)}},
        {"jobs": [_job("j_new", "900500"), _job("j_old", "900500")], "readable": ["900500"], "updates": {}},
    ]
    out = _node(DOWNLOADS_PHASES_HARNESS, STATIC_JS / "utils.js", STATIC_JS / "downloads.js",
                data={"now": NOW, "phases": phases})
    assert out["first"] == {"j_old": True}
    assert out["second"] == {"j_new": False, "j_old": False}     # never 有新章节 on the job that downloaded them
