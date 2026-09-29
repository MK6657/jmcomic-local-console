"""批量下载（PR-B）的界面：确认窗口（static/js/batch-download.js）、收藏 / 资源库的接线、详情页章节表刷新、样式与模板。

The page code runs in Node with the stand-in DOM of tests/test_update_checks_ui.py (FAKE_DOM, loaded with importlib;
skipped when Node is missing, CI installs it): the real utils.js and batch-download.js with a Bootstrap Modal stub and
a scripted apiFetch, the wishlist.js / library.js functions that open the dialog, and the detail.js 章节更新 block.
Schedule-window answers come from the real core.scheduler.window_state, and a check against the real preview route
keeps the hand-built answers in the backend's shape. Nothing here goes online or starts a download, and the network is
blocked (conftest). tests/conftest.py points the app root at a temporary folder before any test module is imported, so
the session app's create_app() (db.init_db()) and every module-level core path land there, never in the real runtime/
or downloads/ — also when this file runs alone; each test then gets pytest's tmp database and a tmp downloads folder
(asserted by _isolated)."""
import importlib.util
import re
import threading
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC_JS = ROOT / "static" / "js"
TEMPLATES = ROOT / "templates"
PREVIEW = "/api/batch-downloads/preview"
CONFIRM = "/api/batch-downloads/confirm"


def _load_update_checks_ui():
    spec = importlib.util.spec_from_file_location("update_checks_ui_for_batch", ROOT / "tests" / "test_update_checks_ui.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_UI = _load_update_checks_ui()
FAKE_DOM = _UI.FAKE_DOM
NOW = _UI.NOW                      # 2026-07-15T15:30:00, the harness clock
YESTERDAY_2015 = _UI.YESTERDAY_2015
TODAY_1030 = _UI.TODAY_1030
_node = _UI._node
_js_block = _UI._js_block


def _read(name):
    return (STATIC_JS / name).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _isolated(client, tmp_path, monkeypatch):
    """Every test: pytest's tmp database and a tmp downloads folder; no download thread is left behind."""
    from core import database as db, jm_service, path_guard
    from routes import api_export, api_preview
    root = tmp_path / "downloads"
    root.mkdir()
    for module in (path_guard, api_preview, api_export, jm_service):
        monkeypatch.setattr(module, "DOWNLOAD_ROOT", root)
    assert Path(db.DB_PATH).resolve().is_relative_to(tmp_path.resolve())
    assert Path(path_guard.DOWNLOAD_ROOT).resolve().is_relative_to(tmp_path.resolve())
    yield root
    assert not [t.name for t in threading.enumerate() if t.name.startswith("dl-") and t.is_alive()]


# ─── answers in the routes' shape ─────────────────────────────────────────────


@pytest.fixture
def windows():
    """window_state() for each case, from the real scheduler (the dialog copy must follow the backend's fields)."""
    from core import database as db, scheduler

    def state(enabled, start, end, hour):
        db.set_setting("schedule_enabled", "true" if enabled else "false")
        db.set_setting("schedule_start", str(start))
        db.set_setting("schedule_end", str(end))
        return scheduler.window_state(datetime(2026, 7, 15, hour, 0, 0))

    out = {"off": state(False, 23, 7, 12), "open": state(True, 23, 7, 1), "today": state(True, 23, 7, 12),
           "tomorrow": state(True, 8, 22, 23), "never": state(True, 5, 5, 12), "invalid": state(True, "x", 7, 12)}
    assert out["today"]["opens_tomorrow"] is False and out["tomorrow"]["opens_tomorrow"] is True
    assert out["never"]["never"] is True and out["invalid"]["invalid"] is True and out["invalid"]["start"] is None
    return out


OFF = {"enabled": False, "open": True, "start": 23, "end": 7, "never": False, "invalid": False, "opens_tomorrow": False}


def _chapter(photo_id, index, title=None, confirmed=YESTERDAY_2015):
    return _UI._chapter(photo_id, index, title, confirmed)


def _a1_item(album_id, title, chapters, confirmed=YESTERDAY_2015):
    return {"album_id": album_id, "title": title, "scope": "chapters", "photo_ids": [c["photo_id"] for c in chapters],
            "chapters": chapters, "confirmed_at": confirmed, "checked_at": confirmed}


def _fav_item(album_id, title, state="never"):
    return {"album_id": album_id, "title": title, "scope": "all", "photo_ids": [], "state": state}


def _skip(album_id, title, reason):
    return {"album_id": album_id, "title": title, "reason": reason}


def _preview(kind, items=(), skipped=(), review=(), out=None, more=0, window=None, ahead=0, max_running=1,
             skip_existing=True, token="a" * 32):
    items = list(items)
    if out is None:
        out = ({"changed": len(review)} if kind == "new_chapters" else {} if kind == "selected_favourites"
               else {"readable": 0, "active": 0, "failed": 0, "missing": 0})
    return {"status": "ok", "kind": kind, "token": token, "limit": 50, "items": items,
            "counts": {"albums": len(items),
                       "chapters": sum(len(i["photo_ids"]) for i in items) if kind == "new_chapters" else None},
            "skipped": list(skipped), "more": more, "review": list(review), "out_of_scope": out,
            "window": window or OFF, "queue": {"ahead": ahead, "max_running": max_running},
            "settings": {"skip_existing": skip_existing}}


def _created(kind, items, window=None):
    return {"status": "ok", "kind": kind,
            "created": [{"album_id": i["album_id"], "job_id": "job_" + i["album_id"], "title": i["title"],
                         "photo_ids": i["photo_ids"]} for i in items],
            "counts": {"albums": len(items),
                       "chapters": sum(len(i["photo_ids"]) for i in items) if kind == "new_chapters" else None},
            "window": window or OFF, "queue": {"ahead": len(items), "max_running": 1}}


def _stale(preview):
    message = "清单有变化，已按最新情况重新列出，请再看一下后确认"
    return {"error": {"status": 409, "message": message,
                      "data": {"status": "error", "reason": "stale", "message": message, "preview": preview}}}


A1_ITEMS = [
    _a1_item("3001", "Alpha", [_chapter("74", 4), _chapter("75", 5)]),
    # no title (the server sends the id), 7 chapters without names
    _a1_item("3002", "3002", [_chapter(str(80 + n), n, "", TODAY_1030) for n in range(1, 8)], TODAY_1030),
]
A1_SKIPPED = [_skip("3003", "Gamma", "active"), _skip("3004", "3004", "active"), _skip("3005", "Eps", "too_many")]
A1_REVIEW = [{"album_id": "3009", "title": "Changed", "new_count": 1, "removed_count": 2}]
A2_ITEMS = [_fav_item("4001", "Never"), _fav_item("4002", "Cancel", "canceled")]
SELECTED_IDS = ["4001", "4010", "4011", "4012", "4013", "4014", "4015", "4016"]
SELECTED_SKIPPED = [_skip("4010", "Read", "readable"), _skip("4011", "Fail", "failed"),
                    _skip("4012", "Gone", "missing"), _skip("4013", "Busy", "active"),
                    _skip("4014", "Cleared", "records_cleared"), _skip("4016", "4016", "not_favourite")]

REASONS = {
    "active": "已在下载队列中（排队中 / 下载中 / 已暂停），不重复创建任务",
    "too_many": "新章节超过 1000 话，请到详情页选择要下载的章节",
    "records_cleared": "下载记录被清理过（例如在「下载管理」里清空了已结束的任务），下载目录里可能还有这部漫画的文件，"
                       "这次不整部下载；确定要整部下载请点收藏里这一行的「下载」，或到详情页选择章节下载",
    "readable": "已有已下载内容（可离线阅读），不整部重新下载；新章节请用资源库的「下载新章节」",
    "failed": "上次下载失败，请在「下载管理」的「失败」里重试",
    "missing": "下载过，本地文件不可用，请在「下载管理」里重新下载",
    "not_favourite": "已不在收藏里",
}
SCOPES = {
    "new_chapters": "只包含本地有已下载内容、检查确认过有新章节的漫画（收藏和没收藏的都算）；每部只下载列出的新章节，"
                    "已下载的章节和当初没选的章节都不会下载。",
    "undownloaded_favourites": "只包含收藏页“未下载”里的：从未下载过的，和下载被取消的；每部下载整部漫画（开始下载时上游的全部章节）。"
                               "下载失败的请在「下载管理」的「失败」里重试；已有已下载内容的（包括只下载了部分章节的）不会包含。",
    "selected_favourites": "只下载选中的收藏里“未下载”的（从未下载过的、下载被取消的），每部下载整部漫画；其余的不会下载，原因列在下面。",
}
EMPTY = {
    "new_chapters": "现在没有要下载的新章节。只有检查确认过的新章节会出现在这里；可以在漫画详情页点「立即检查」。"
                    "检查只核对章节列表，从不自动下载。",
    "undownloaded_favourites": "“未下载”里没有要下载的收藏。",
    "selected_favourites": "选中的收藏里没有这次可以整部下载的“未下载”收藏（原因见下）。",
}
STALE_NOTICE = "清单刚刚有变化（有任务开始或结束，或刚确认了新章节），已按最新情况重新列出，请再看一下后确认。"
STALE_EMPTY = "清单刚刚有变化：现在没有要下载的了，原因列在下面。"
STALE_EMPTY_BARE = "清单刚刚有变化：现在没有要下载的了。"
STALE_CLOSED = "清单有变化，这次没有加入下载队列；请重新打开清单再确认"
LOADING = "正在列出要下载的内容…（只读本地记录，不联网）"


def test_hand_built_answers_match_the_real_routes(client):
    """The answers fed to the dialog below have exactly the keys the real preview route sends."""
    from core import database as db
    db.add_wishlist("4001", "Never")
    for kind, extra in (("new_chapters", {}), ("undownloaded_favourites", {}),
                        ("selected_favourites", {"album_ids": ["4001", "4999"]})):
        data = client.post(PREVIEW, json={"kind": kind, **extra}).get_json()
        mine = _preview(kind)
        assert set(data) == set(mine), kind
        for key in ("window", "queue", "settings", "counts"):
            assert set(data[key]) == set(mine[key]), (kind, key)
        assert set(data["out_of_scope"]) == set(mine["out_of_scope"]), kind
        if kind != "new_chapters":
            assert [set(i) for i in data["items"]] == [set(_fav_item("1", "t"))]
            assert data["items"][0]["state"] == "never"
    selected = client.post(PREVIEW, json={"kind": "selected_favourites", "album_ids": ["4999"]}).get_json()
    assert [set(s) for s in selected["skipped"]] == [set(_skip("1", "t", "x"))]
    assert selected["skipped"][0]["reason"] == "not_favourite"
    assert db.get_all_jobs() == []                              # previews never create anything


# ─── the shared dialog in Node ────────────────────────────────────────────────

BATCH_HARNESS = FAKE_DOM + r"""
const [utilsPath, batchPath] = process.argv.slice(1);
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
fixClock(input.now);
(0, eval)(fs.readFileSync(utilsPath, 'utf8'));
const batchSrc = fs.readFileSync(batchPath, 'utf8');
document.activeElement = null;
// as in a browser: focus() on a disabled control does nothing, and disabling the focused control drops focus to <body>
document.createElement = tag => {
  const e = el(tag);
  let disabled = false;
  Object.defineProperty(e, 'disabled', { configurable: true, enumerable: true, get: () => disabled,
    set: v => { disabled = !!v; if (disabled && document.activeElement === e) document.activeElement = body; } });
  e.focus = () => { if (!disabled) document.activeElement = e; };
  return e;
};
let modals = [], calls = [], toasts = [], pending = [], done = 0;
global.showToast = (m, t) => toasts.push([m, t]);
global.bootstrap = { Modal: { getOrCreateInstance(node) {
  let m = modals.find(x => x.el === node);
  if (!m) {
    m = { el: node, shown: false, shows: 0, hides: 0,
      show() { if (this.shown) return; this.shown = true; this.shows++; (node.handlers['shown.bs.modal'] || []).forEach(fn => fn({})); },
      hide() { if (!this.shown) return; this.shown = false; this.hides++; (node.handlers['hidden.bs.modal'] || []).forEach(fn => fn({})); } };
    modals.push(m);
  }
  return m;
} } };

function answer(spec) {
  const p = pending.shift();
  if (!p) throw new Error('no request to answer');
  if (spec.error) { const e = makeError(spec.error); if (spec.error.data) e.data = spec.error.data; p.reject(e); }
  else p.resolve(spec.ok);
}

function snap() {
  const g = id => document.getElementById(id);
  const root = g('batch-dialog');
  if (!root) return { built: false, focus: document.activeElement ? document.activeElement.id : null };
  const vis = n => !!n && !n.hidden;
  const icon = n => { const i = n.querySelector('i'); return i ? i.className : null; };
  const texts = n => n.children.filter(c => c.nodeType === 3).map(c => c.data).join('');
  const status = g('batch-dialog-status'), extra = g('batch-dialog-extra'), win = g('batch-dialog-window');
  const list = g('batch-dialog-list'), sk = g('batch-dialog-skipped'), rv = g('batch-dialog-review');
  const cancel = g('batch-dialog-cancel'), confirm = g('batch-dialog-confirm'), notice = g('batch-dialog-notice');
  const parent = root.parentNode, next = parent.children[parent.children.indexOf(root) + 1];
  return {
    built: true,
    placement: { parent: parent === body ? 'BODY' : (parent.id || parent.className),
                 next: next ? (next.id || next.className) : null },
    dialogs: body.querySelectorAll('#batch-dialog').length,
    open: window.batchDownloadDialog.isOpen(),
    shown: modals.length ? modals[0].shown : false,
    hides: modals.length ? modals[0].hides : 0,
    root: { cls: root.className, attrs: Object.assign({}, root.attrs), dialog: root.children[0].className },
    title: g('batch-dialog-title').textContent,
    status: texts(status).trim(), statusIcon: icon(status),
    statusAttrs: { role: status.getAttribute('role'), live: status.getAttribute('aria-live') },
    retry: status.querySelectorAll('button').map(b => ({ text: b.textContent.trim(), cls: b.className, icon: icon(b) })),
    notice: vis(notice) ? { text: notice.textContent, role: notice.getAttribute('role'), cls: notice.className } : null,
    summary: vis(g('batch-dialog-summary')) ? g('batch-dialog-summary').textContent : null,
    scope: vis(g('batch-dialog-scope')) ? g('batch-dialog-scope').textContent : null,
    extra: vis(extra) ? extra.children.filter(c => c.nodeType === 3).map(c => c.data) : null,
    window: vis(win) ? { text: win.textContent, cls: win.className, icon: icon(win) } : null,
    listLabel: list.getAttribute('aria-label'),
    items: vis(list) ? list.children.map(li => {
      const cb = li.querySelector('input'), label = li.querySelector('label');
      return { cls: li.className, id: cb.id, type: cb.type, checked: !!cb.checked, disabled: !!cb.disabled,
               aria: cb.getAttribute('aria-label'), for: label.getAttribute('for'),
               title: li.querySelector('.batch-dialog-title').textContent,
               idText: li.querySelector('.batch-dialog-id').textContent,
               meta: li.querySelector('.batch-dialog-meta').textContent };
    }) : null,
    skippedTitle: vis(g('batch-dialog-skipped-title')) ? g('batch-dialog-skipped-title').textContent : null,
    skippedLabel: sk.getAttribute('aria-labelledby'),
    skipped: vis(sk) ? sk.children.map(li => li.textContent) : null,
    reviewTitle: vis(g('batch-dialog-review-title')) ? g('batch-dialog-review-title').textContent : null,
    review: vis(rv) ? rv.children.map(li => {
      const a = li.querySelector('a');
      return { text: li.textContent, link: a && { text: a.textContent, href: a.getAttribute('href'),
               target: a.getAttribute('target'), rel: a.getAttribute('rel') } };
    }) : null,
    outside: vis(g('batch-dialog-outside')) ? g('batch-dialog-outside').textContent : null,
    cancel: { text: cancel.textContent, dismiss: cancel.getAttribute('data-bs-dismiss'), cls: cancel.className },
    confirm: { text: confirm.textContent.trim(), hidden: !!confirm.hidden, disabled: !!confirm.disabled,
               busy: confirm.getAttribute('aria-busy'), icon: icon(confirm), cls: confirm.className },
    focus: document.activeElement ? (document.activeElement.id || document.activeElement.tagName) : null,
    images: body.querySelectorAll('img').length,
    done,
  };
}

async function runCase(c) {
  body.children = []; modals = []; calls = []; toasts = []; pending = []; done = 0; document.activeElement = null;
  window.apiFetch = (url, opts) => {
    opts = opts || {};
    calls.push({ url, method: opts.method || 'GET', body: opts.body ? JSON.parse(opts.body) : null,
                 abortKey: opts.abortKey || null, timeoutMs: opts.timeoutMs === undefined ? null : opts.timeoutMs,
                 json: !!(opts.headers && opts.headers['Content-Type'] === 'application/json') });
    return new Promise((resolve, reject) => pending.push({ resolve, reject }));
  };
  // the page around the dialog: base.html's .main-container, then #quick-nav and #toast-container (page: 'base';
  // 'nested': #quick-nav inside a wrapper); 'main': only .main-container; by default a bare <body>
  let main = body;
  if (c.page) {
    main = document.createElement('div'); main.className = 'main-container container'; body.appendChild(main);
    if (c.page === 'base' || c.page === 'nested') {
      const holder = c.page === 'nested' ? body.appendChild(document.createElement('div')) : body;
      const nav = document.createElement('div'); nav.id = 'quick-nav'; nav.className = 'quick-nav';
      holder.appendChild(nav);
      const toastBox = document.createElement('div'); toastBox.id = 'toast-container'; body.appendChild(toastBox);
    }
  }
  (0, eval)(batchSrc);
  const opener = document.createElement('button'); opener.id = 'opener'; main.appendChild(opener);
  const other = document.createElement('button'); other.id = 'other'; main.appendChild(other);
  const steps = [];
  let seenCalls = 0, seenToasts = 0;
  const record = extra => {
    steps.push(Object.assign(snap(), extra || {}, { calls: calls.slice(seenCalls), toasts: toasts.slice(seenToasts) }));
    seenCalls = calls.length; seenToasts = toasts.length;
  };
  for (const step of c.steps) {
    let returned;
    if (step.open) {
      other.focus();
      returned = window.batchDownloadDialog.open(step.open, { albumIds: step.albumIds,
        opener: step.noOpener ? undefined : opener, onDone: () => { done++; } });
    } else if (step.answer) {
      answer(step.answer);
    } else if (step.untick !== undefined) {
      const cb = document.getElementById('batch-item-' + step.untick);
      cb.checked = !!step.checked;
      (cb.handlers.change || []).forEach(fn => fn({ type: 'change', target: cb }));
    } else if (step.confirm) {
      const b = document.getElementById('batch-dialog-confirm');
      for (let i = 0; i < step.confirm; i++) { b.focus(); b.click(); }   // a click (or Enter) focuses the button first
    } else if (step.focus) {
      document.getElementById(step.focus).focus();
    } else if (step.forceConfirm) {
      const b = document.getElementById('batch-dialog-confirm');
      (b.handlers.click || []).forEach(fn => fn({ type: 'click', target: b }));
    } else if (step.retry) {
      document.getElementById('batch-dialog-status').querySelector('button').click();
    } else if (step.hide) {
      modals[0].hide();
    }
    await flush();
    record(returned === undefined ? {} : { returned });
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


def _dialog(cases):
    return _node(BATCH_HARNESS, STATIC_JS / "utils.js", STATIC_JS / "batch-download.js", data={"now": NOW, "cases": cases})


def _opened(kind, preview, albumIds=None):
    """Open + answer the preview: the two steps every scenario starts with."""
    step = {"open": kind}
    if albumIds is not None:
        step["albumIds"] = albumIds
    return [step, {"answer": {"ok": preview}}]


def _post(url, body, **extra):
    call = {"url": url, "method": "POST", "body": body, "abortKey": None, "timeoutMs": 60000, "json": True}
    call.update(extra)
    return call


# 46
def test_one_preview_post_per_open():
    out = _dialog([
        {"name": "a1", "steps": [{"open": "new_chapters"}, {"open": "new_chapters"}]},
        {"name": "a2", "steps": [{"open": "undownloaded_favourites"}]},
        {"name": "selected", "steps": [{"open": "selected_favourites", "albumIds": ["5", 6, "5"]}]},
        {"name": "none_selected", "steps": [{"open": "selected_favourites", "albumIds": []},
                                            {"open": "bogus"}]},
        {"name": "reopen", "steps": _opened("new_chapters", _preview("new_chapters", A1_ITEMS))
         + [{"hide": True}, {"open": "new_chapters"}]},
    ])
    first, again = out["a1"]
    assert first["returned"] is True and again["returned"] is False     # already open: nothing new
    assert first["calls"] == [_post(PREVIEW, {"kind": "new_chapters"}, abortKey="batch-dialog-preview")]
    assert again["calls"] == []
    assert out["a2"][0]["calls"] == [_post(PREVIEW, {"kind": "undownloaded_favourites"}, abortKey="batch-dialog-preview")]
    assert out["selected"][0]["calls"] == [_post(PREVIEW, {"kind": "selected_favourites", "album_ids": ["5", "6"]},
                                                 abortKey="batch-dialog-preview")]
    assert [s["returned"] for s in out["none_selected"]] == [False, False]
    assert all(s["calls"] == [] for s in out["none_selected"]) and out["none_selected"][0]["built"] is False
    *_, hidden, reopened = out["reopen"]
    assert hidden["open"] is False and reopened["open"] is True and reopened["dialogs"] == 1   # built once, reused
    assert [c["url"] for c in reopened["calls"]] == [PREVIEW]
    # the loading state again, not the old list
    assert reopened["status"] == LOADING and reopened["items"] is None and reopened["confirm"]["disabled"] is True
    for name, steps in out.items():
        if name == "htmlParsed":
            continue
        assert all(c["url"] == PREVIEW for s in steps for c in s["calls"]), name


# 47
def test_copy_per_kind():
    a1 = _preview("new_chapters", A1_ITEMS, A1_SKIPPED, A1_REVIEW)
    a2 = _preview("undownloaded_favourites", A2_ITEMS, [_skip("4003", "Cleared", "records_cleared")],
                  out={"readable": 3, "active": 0, "failed": 2, "missing": 1})
    selected = _preview("selected_favourites", [_fav_item("4001", "Never")], SELECTED_SKIPPED)
    extra = _preview("undownloaded_favourites", A2_ITEMS, skip_existing=False, more=5,
                     out={"readable": 0, "active": 0, "failed": 0, "missing": 0})
    out = _dialog([
        {"name": "a1", "steps": _opened("new_chapters", a1)},
        {"name": "a2", "steps": _opened("undownloaded_favourites", a2)},
        {"name": "selected", "steps": _opened("selected_favourites", selected, SELECTED_IDS)},
        {"name": "extra", "steps": _opened("undownloaded_favourites", extra)},
    ])
    view = out["a1"][1]
    assert view["title"] == "下载新章节" and view["status"] == "" and view["notice"] is None
    assert view["summary"] == "将下载 2 部漫画的 9 话新章节"
    assert view["scope"] == SCOPES["new_chapters"] and view["extra"] is None
    assert view["items"] == [
        {"cls": "batch-dialog-item", "id": "batch-item-3001", "type": "checkbox", "checked": True, "disabled": False,
         "aria": "下载 Alpha", "for": "batch-item-3001", "title": "Alpha", "idText": "#3001",
         "meta": "新章节 2 话：第4话 · 第5话 · 昨天 20:15 检查确认"},
        {"cls": "batch-dialog-item", "id": "batch-item-3002", "type": "checkbox", "checked": True, "disabled": False,
         "aria": "下载 车号 3002", "for": "batch-item-3002", "title": "车号 3002", "idText": "#3002",
         "meta": "新章节 7 话：第1话 · 第2话 · 第3话 · 第4话 · 第5话 … 等 7 话 · 今天 10:30 检查确认"},
    ]
    assert view["listLabel"] == "这次会下载的漫画"
    assert view["skippedTitle"] == "这些不会下载（3 部）" and view["skippedLabel"] == "batch-dialog-skipped-title"
    assert view["skipped"] == ["Gamma（3003）：" + REASONS["active"], "车号 3004：" + REASONS["active"],
                               "Eps（3005）：" + REASONS["too_many"]]
    assert view["reviewTitle"] == "章节列表有变动的 1 部不在这里下载，请先到详情页核对："
    assert view["review"] == [{"text": "Changed：新出现 1 话，另有 2 话已不在上游 · 去核对",
                               "link": {"text": "去核对", "href": "/album/3009", "target": "_blank", "rel": "noopener"}}]
    assert view["outside"] is None
    assert view["cancel"] == {"text": "取消", "dismiss": "modal", "cls": "btn btn-outline-secondary"}
    assert view["confirm"] == {"text": "加入下载队列（2 部）", "hidden": False, "disabled": False, "busy": None,
                               "icon": None, "cls": "btn btn-primary"}

    view = out["a2"][1]
    assert view["title"] == "下载未下载的收藏" and view["summary"] == "将下载 2 部收藏，每部下载整部漫画"
    assert view["scope"] == SCOPES["undownloaded_favourites"]
    assert [(i["title"], i["meta"]) for i in view["items"]] == [("Never", "整部漫画 · 从未下载"),
                                                              ("Cancel", "整部漫画 · 上次下载已取消")]
    assert view["skipped"] == ["Cleared（4003）：" + REASONS["records_cleared"]]
    assert view["review"] is None and view["reviewTitle"] is None
    # only the parts > 0, in the 收藏 order
    assert view["outside"] == ("不包含：失败 2 部（请在「下载管理」的「失败」里重试） · 已有已下载内容 3 部 · "
                               "下载过 · 本地文件不可用 1 部")

    view = out["selected"][1]
    assert view["title"] == "下载选中的收藏"
    assert view["summary"] == "选中的 8 部里，将下载 1 部“未下载”的收藏，每部下载整部漫画"
    assert view["scope"] == SCOPES["selected_favourites"] and view["outside"] is None
    assert view["skippedTitle"] == "这些不会下载（6 部）"
    assert view["skipped"] == ["Read（4010）：" + REASONS["readable"], "Fail（4011）：" + REASONS["failed"],
                               "Gone（4012）：" + REASONS["missing"], "Busy（4013）：" + REASONS["active"],
                               "Cleared（4014）：" + REASONS["records_cleared"], "车号 4016：" + REASONS["not_favourite"]]

    view = out["extra"][1]
    assert view["extra"] == ["设置里关闭了「跳过已存在的文件」：本地已有的页也会重新下载。",
                             "另有 5 部这次不下载（一次最多 50 部）；加入队列后可以再点一次。"]
    assert view["outside"] is None                          # nothing outside: no line at all


WINDOW_COPY = {
    ("off", 0, 1): ("确认后加入下载队列，按顺序开始（同时最多 1 个任务）。", False, "bi bi-info-circle"),
    ("off", 3, 2): ("确认后加入下载队列，按顺序开始（同时最多 2 个任务；前面还有 3 个任务）。", False, "bi bi-info-circle"),
    ("open", 1, 1): ("现在在定时下载时间段内（23:00–07:00）：确认后按顺序开始（同时最多 1 个任务；前面还有 1 个任务）；"
                     "到 07:00 还没开始的任务会等下一次时间段。", False, "bi bi-info-circle"),
    ("today", 0, 1): ("已开启定时下载（23:00–07:00），现在不在时间段内：这些任务先排队，今天 23:00 起才开始下载（到时程序需要开着）。",
                      True, "bi bi-hourglass-split"),
    ("tomorrow", 0, 1): ("已开启定时下载（08:00–22:00），现在不在时间段内：这些任务先排队，明天 08:00 起才开始下载（到时程序需要开着）。",
                         True, "bi bi-hourglass-split"),
    ("never", 0, 1): ("已开启定时下载，但开始和结束时间都是 05:00：排队的任务不会自动开始。可以到「设置 → 定时下载」修改时间段。",
                      True, "bi bi-exclamation-triangle"),
    ("invalid", 0, 1): ("已开启定时下载，但时间段设置无效：排队的任务不会自动开始。可以到「设置 → 定时下载」修改。",
                        True, "bi bi-exclamation-triangle"),
}


# 47 (the window line, from the real window_state)
def test_window_line_says_truthfully_when_jobs_start(windows):
    cases = [{"name": "|".join(map(str, key)), "steps": _opened(
        "undownloaded_favourites", _preview("undownloaded_favourites", A2_ITEMS, window=windows[key[0]],
                                            ahead=key[1], max_running=key[2]))} for key in WINDOW_COPY]
    out = _dialog(cases)
    for key, (text, wait, icon) in WINDOW_COPY.items():
        line = out["|".join(map(str, key))][1]["window"]
        assert line == {"text": text, "cls": "batch-dialog-alert" + (" batch-dialog-alert--wait" if wait else ""),
                        "icon": icon}, key


# 48
def test_empty_states_hide_confirm_and_say_close():
    out = _dialog([
        {"name": "a1", "steps": _opened("new_chapters", _preview("new_chapters", [], A1_SKIPPED, A1_REVIEW))},
        {"name": "a2", "steps": _opened("undownloaded_favourites", _preview(
            "undownloaded_favourites", out={"readable": 0, "active": 4, "failed": 0, "missing": 0}))},
        {"name": "selected", "steps": _opened("selected_favourites", _preview(
            "selected_favourites", [], SELECTED_SKIPPED[:1]), ["4010"])},
    ])
    for kind, name in (("new_chapters", "a1"), ("undownloaded_favourites", "a2"), ("selected_favourites", "selected")):
        view = out[name][1]
        assert view["summary"] == EMPTY[kind], kind
        assert view["confirm"]["hidden"] is True and view["cancel"]["text"] == "关闭", kind
        assert view["items"] is None and view["scope"] is None and view["window"] is None, kind
    # skipped and review are still listed
    assert len(out["a1"][1]["skipped"]) == 3 and out["a1"][1]["review"][0]["link"]["href"] == "/album/3009"
    assert out["a2"][1]["outside"] == "不包含：排队中 / 下载中 4 部"
    assert out["selected"][1]["skipped"] == ["Read（4010）：" + REASONS["readable"]]


# 49
def test_titles_are_text_never_markup():
    evil = '<img src=x onerror=alert(1)>'
    preview = _preview("new_chapters", [_a1_item("3001", evil, [_chapter("74", 4, evil)])],
                       [_skip("3003", evil, "active")], [{"album_id": "3009", "title": evil, "new_count": 1,
                                                         "removed_count": 1}])
    out = _dialog([{"name": "xss", "steps": _opened("new_chapters", preview)}])
    view = out["xss"][1]
    assert view["items"][0]["title"] == evil and view["items"][0]["aria"] == "下载 " + evil
    assert view["items"][0]["meta"].startswith("新章节 1 话：" + evil)
    assert view["skipped"][0].startswith(evil + "（3003）") and view["review"][0]["text"].startswith(evil + "：")
    assert view["images"] == 0 and out["htmlParsed"] == []
    assert "innerHTML" not in _read("batch-download.js")


# 50 (+ 60: the modal the harness built)
def test_focus_starts_on_cancel_and_returns_to_the_opener():
    preview = _preview("new_chapters", A1_ITEMS)
    out = _dialog([
        {"name": "focus", "steps": _opened("new_chapters", preview) + [{"hide": True}]},
        {"name": "no_opener", "steps": [{"open": "new_chapters", "noOpener": True}, {"hide": True}]},
    ])
    loading, loaded, hidden = out["focus"]
    assert loading["focus"] == "batch-dialog-cancel"          # Enter never confirms by accident
    assert loading["shown"] is True and loading["open"] is True
    assert loading["status"] == LOADING and loading["statusIcon"] == "bi bi-hourglass-split"
    assert loading["statusAttrs"] == {"role": "status", "live": "polite"}
    assert loading["confirm"] == {"text": "加入下载队列", "hidden": False, "disabled": True, "busy": None, "icon": None,
                                  "cls": "btn btn-primary"}
    assert loading["items"] is None and loading["summary"] is None
    assert loaded["confirm"]["disabled"] is False
    assert hidden["open"] is False and hidden["focus"] == "opener"
    # no opener given: focus goes back to whatever had it when the dialog opened
    assert out["no_opener"][1]["focus"] == "other"
    # the modal: no fade (no animation at all), a labelled and described dialog, a big scrollable body
    root = loading["root"]
    assert root["cls"] == "modal"
    assert root["attrs"] == {"id": "batch-dialog", "tabindex": "-1", "role": "dialog", "aria-modal": "true",
                             "aria-labelledby": "batch-dialog-title", "aria-describedby": "batch-dialog-summary"}
    assert root["dialog"] == "modal-dialog modal-lg modal-dialog-scrollable"


def test_preview_failure_offers_retry():
    retry = {"text": "重试", "cls": "btn btn-sm btn-outline-primary", "icon": "bi bi-arrow-clockwise"}
    out = _dialog([
        {"name": "server", "steps": [{"open": "new_chapters"},
                                     {"answer": {"error": {"status": 500, "message": "没能列出要下载的内容"}}},
                                     {"retry": True}, {"answer": {"ok": _preview("new_chapters", A1_ITEMS)}}]},
        {"name": "bad", "steps": [{"open": "selected_favourites", "albumIds": ["1"]},
                                  {"answer": {"error": {"status": 400, "message": "请选择 1-50 个收藏"}}}]},
        {"name": "network", "steps": [{"open": "new_chapters"},
                                      {"answer": {"error": {"name": "TypeError", "message": "Failed to fetch"}}}]},
        {"name": "not_ok", "steps": [{"open": "new_chapters"},
                                     {"answer": {"ok": {"status": "error", "message": "x"}}}]},
        {"name": "abort", "steps": [{"open": "new_chapters"}, {"answer": {"error": {"name": "AbortError"}}}]},
    ])
    _, failed, again, loaded = out["server"]
    assert failed["status"] == "没能列出要下载的内容" and failed["retry"] == [retry]
    assert failed["confirm"]["hidden"] is True and failed["cancel"]["text"] == "关闭" and failed["toasts"] == []
    assert again["calls"] == [_post(PREVIEW, {"kind": "new_chapters"}, abortKey="batch-dialog-preview")]
    assert again["status"] == LOADING and again["focus"] == "batch-dialog-cancel"
    assert loaded["summary"] == "将下载 2 部漫画的 9 话新章节" and loaded["confirm"]["hidden"] is False
    assert loaded["cancel"]["text"] == "取消"
    assert out["bad"][1]["status"] == "没能列出要下载的内容：请选择 1-50 个收藏"
    assert out["network"][1]["status"] == "没能列出要下载的内容：网络错误"
    assert out["not_ok"][1]["status"] == "没能列出要下载的内容：x"
    assert out["abort"][1]["status"] == LOADING and out["abort"][1]["toasts"] == []    # leaving the page: silent


# 51
def test_unticking_updates_the_summary_and_the_exclude_list():
    preview = _preview("new_chapters", A1_ITEMS)
    out = _dialog([
        {"name": "one", "steps": _opened("new_chapters", preview) + [{"untick": "3002"}, {"confirm": 1}]},
        {"name": "all", "steps": _opened("new_chapters", preview)
         + [{"untick": "3001"}, {"untick": "3002"}, {"confirm": 1}, {"forceConfirm": True},
            {"untick": "3002", "checked": True}]},
    ])
    _, _, unticked, confirmed = out["one"]
    assert unticked["summary"] == "将下载 1 部漫画的 2 话新章节（已取消勾选 1 部）"
    assert unticked["confirm"]["text"] == "加入下载队列（1 部）" and unticked["confirm"]["disabled"] is False
    assert [i["checked"] for i in unticked["items"]] == [True, False] and unticked["calls"] == []
    assert confirmed["calls"] == [_post(CONFIRM, {"kind": "new_chapters", "token": "a" * 32, "exclude": ["3002"]})]
    _, _, _, none, clicked, forced, back = out["all"]
    assert none["summary"] == "将下载 0 部漫画的 0 话新章节（已取消勾选 2 部）"
    assert none["confirm"]["text"] == "加入下载队列（0 部）" and none["confirm"]["disabled"] is True
    assert clicked["calls"] == [] and forced["calls"] == []    # nothing ticked: nothing is sent
    assert back["summary"] == "将下载 1 部漫画的 7 话新章节（已取消勾选 1 部）" and back["confirm"]["disabled"] is False


# 52
def test_double_click_sends_one_confirm():
    out = _dialog([{"name": "twice", "steps": _opened("new_chapters", _preview("new_chapters", A1_ITEMS))
                    + [{"confirm": 2}, {"forceConfirm": True}]},
                   {"name": "selected", "steps": _opened("selected_favourites", _preview(
                       "selected_favourites", [_fav_item("4001", "Never")], SELECTED_SKIPPED), SELECTED_IDS)
                    + [{"confirm": 1}]}])
    _, _, busy, forced = out["twice"]
    assert busy["calls"] == [_post(CONFIRM, {"kind": "new_chapters", "token": "a" * 32, "exclude": []})]
    assert forced["calls"] == []                            # the in-flight guard, even past the disabled button
    assert busy["confirm"] == {"text": "正在加入下载队列…", "hidden": False, "disabled": True, "busy": "true",
                               "icon": "bi bi-hourglass-split", "cls": "btn btn-primary"}
    assert all(i["disabled"] for i in busy["items"]) and busy["open"] is True
    assert busy["cancel"]["text"] == "关闭"                     # the request is out: closing cannot take it back
    assert out["selected"][2]["calls"] == [_post(CONFIRM, {"kind": "selected_favourites", "token": "a" * 32,
                                                           "exclude": [], "album_ids": SELECTED_IDS})]


# 53
def test_stale_answer_relists_and_never_confirms_by_itself():
    fresh = _preview("new_chapters", A1_ITEMS + [_a1_item("3006", "New", [_chapter("91", 1)])], token="b" * 32)
    gone = _preview("new_chapters", A1_ITEMS[:1], token="c" * 32)
    busy = {"error": {"status": 409, "message": "上一次确认还在处理，请稍后再试",
                      "data": {"status": "error", "reason": "busy", "message": "上一次确认还在处理，请稍后再试"}}}
    out = _dialog([
        {"name": "stale", "steps": _opened("new_chapters", _preview("new_chapters", A1_ITEMS))
         + [{"untick": "3002"}, {"confirm": 1}, {"answer": _stale(fresh)}, {"confirm": 1}]},
        {"name": "gone", "steps": _opened("new_chapters", _preview("new_chapters", A1_ITEMS))
         + [{"untick": "3002"}, {"confirm": 1}, {"answer": _stale(gone)}, {"confirm": 1}, {"answer": _stale(fresh)}]},
        {"name": "busy", "steps": _opened("new_chapters", _preview("new_chapters", A1_ITEMS))
         + [{"confirm": 1}, {"answer": busy}]},
        {"name": "closed", "steps": _opened("new_chapters", _preview("new_chapters", A1_ITEMS))
         + [{"confirm": 1}, {"hide": True}, {"answer": _stale(fresh)}]},
    ])
    *_, relisted, again = out["stale"]
    assert relisted["calls"] == [] and relisted["toasts"] == []       # no automatic second confirm
    assert relisted["open"] is True and relisted["done"] == 0
    assert relisted["notice"] == {"text": STALE_NOTICE, "role": "alert",
                                  "cls": "batch-dialog-alert batch-dialog-alert--wait"}
    assert relisted["focus"] == "batch-dialog-summary"
    assert [(i["id"], i["checked"], i["disabled"]) for i in relisted["items"]] == [
        ("batch-item-3001", True, False), ("batch-item-3002", False, False), ("batch-item-3006", True, False)]
    assert relisted["summary"] == "将下载 2 部漫画的 3 话新章节（已取消勾选 1 部）"
    assert relisted["confirm"] == {"text": "加入下载队列（2 部）", "hidden": False, "disabled": False, "busy": None,
                                   "icon": None, "cls": "btn btn-primary"}
    assert again["calls"] == [_post(CONFIRM, {"kind": "new_chapters", "token": "b" * 32, "exclude": ["3002"]})]
    # an unticked album that is no longer listed is forgotten: when it comes back it is ticked like any other
    *_, dropped, back = out["gone"]
    assert dropped["calls"] == [_post(CONFIRM, {"kind": "new_chapters", "token": "c" * 32, "exclude": []})]
    assert [(i["id"], i["checked"]) for i in back["items"]] == [
        ("batch-item-3001", True), ("batch-item-3002", True), ("batch-item-3006", True)]
    assert back["summary"] == "将下载 3 部漫画的 10 话新章节"
    *_, answered = out["busy"]
    assert answered["toasts"] == [["上一次确认还在处理，请稍后再试", "warning"]]
    assert answered["open"] is True and answered["confirm"]["disabled"] is False and answered["confirm"]["busy"] is None
    assert answered["notice"] is None and answered["done"] == 0
    assert answered["cancel"]["text"] == "取消"
    # closed while the confirm was in flight: nothing was queued and no list is on screen to look at again
    assert out["closed"][-1]["toasts"] == [[STALE_CLOSED, "warning"]]
    assert out["closed"][-1]["open"] is False and out["closed"][-1]["notice"] is None


# 53 (a stale answer with nothing left: the notice does not ask for a confirm the dialog no longer offers)
def test_stale_answer_with_an_empty_list():
    empty = _preview("new_chapters", [], [_skip("3001", "Alpha", "active")], token="d" * 32)
    fresh = _preview("new_chapters", A1_ITEMS[:1], token="e" * 32)
    out = _dialog([{"name": "empty", "steps": _opened("new_chapters", _preview("new_chapters", A1_ITEMS))
                    + [{"confirm": 1}, {"answer": _stale(empty)}, {"hide": True}]
                    + _opened("new_chapters", _preview("new_chapters", A1_ITEMS))
                    + [{"confirm": 1}, {"answer": _stale(fresh)}]}])
    _, _, _, emptied, hidden, reopened, _, _, relisted = out["empty"]
    assert emptied["notice"] == {"text": STALE_EMPTY, "role": "alert",
                                 "cls": "batch-dialog-alert batch-dialog-alert--wait"}
    assert emptied["items"] is None and emptied["summary"] == EMPTY["new_chapters"]
    assert emptied["confirm"]["hidden"] is True and emptied["cancel"]["text"] == "关闭"
    assert emptied["skipped"] == ["Alpha（3001）：" + REASONS["active"]]
    assert emptied["calls"] == [] and emptied["toasts"] == [] and emptied["focus"] == "batch-dialog-summary"
    assert hidden["notice"] is None and reopened["notice"] is None
    assert relisted["notice"]["text"] == STALE_NOTICE and relisted["confirm"]["hidden"] is False
    # nothing left and no reason on screen (e.g. the favourite was removed): the notice points at nothing below
    bare = _preview("new_chapters", [], token="f" * 32)
    out = _dialog([{"name": "bare", "steps": _opened("new_chapters", _preview("new_chapters", A1_ITEMS))
                    + [{"confirm": 1}, {"answer": _stale(bare)}]}])
    gone = out["bare"][-1]
    assert gone["notice"]["text"] == STALE_EMPTY_BARE and "原因" not in gone["notice"]["text"]
    assert gone["skipped"] in (None, []) and gone["confirm"]["hidden"] is True


# 53 (the dialog sits where Tab cannot leave it: before base.html's #quick-nav, like the template modals)
def test_dialog_is_placed_so_tab_stays_inside():
    out = _dialog([{"name": page or "bare", "page": page, "steps": [{"open": "new_chapters"}, {"hide": True},
                                                                    {"open": "new_chapters"}]}
                   for page in ("base", "nested", "main", None)])
    for name in ("base", "nested", "main", "bare"):
        assert all(step["dialogs"] == 1 for step in out[name]), name        # built once, never moved or copied
    assert out["base"][0]["placement"] == {"parent": "BODY", "next": "quick-nav"}
    assert out["nested"][0]["placement"] == {"parent": "main-container container", "next": None}
    assert out["main"][0]["placement"] == {"parent": "main-container container", "next": None}
    assert out["bare"][0]["placement"]["parent"] == "BODY"
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    # base.html: #quick-nav is a direct child of <body>, after .main-container (where the template modals live)
    assert re.search(r'^<div class="quick-nav" id="quick-nav">$', base, flags=re.M)
    assert base.index('<div class="main-container container">') < base.index('id="quick-nav"') \
        < base.index('id="toast-container"')
    for page in ("library.html", "wishlist.html"):
        assert "block quick_nav" not in (TEMPLATES / page).read_text(encoding="utf-8"), page


# 53 (keyboard focus while 加入下载队列 is busy: never dropped on the page, back on the button after an error)
def test_keyboard_focus_stays_in_the_dialog_while_confirming():
    busy = {"error": {"status": 409, "message": "上一次确认还在处理，请稍后再试",
                      "data": {"status": "error", "reason": "busy", "message": "上一次确认还在处理，请稍后再试"}}}
    errors = {"busy": busy, "server": {"error": {"status": 500, "message": "没能加入下载队列，没有创建任何任务，请重新打开清单"}},
              "network": {"error": {"name": "TypeError", "message": "Failed to fetch"}},
              "not_ok": {"ok": {"status": "error", "message": "x"}}}
    preview = _preview("new_chapters", A1_ITEMS)
    cases = [{"name": name, "steps": _opened("new_chapters", preview) + [{"confirm": 1}, {"answer": answer}]}
             for name, answer in errors.items()]
    # the focus on a checkbox (Safari does not focus a clicked button): it is disabled too
    cases.append({"name": "box", "steps": _opened("new_chapters", preview)
                  + [{"focus": "batch-item-3001"}, {"forceConfirm": True}, {"answer": busy}]})
    cases.append({"name": "closed", "steps": _opened("new_chapters", preview)
                  + [{"confirm": 1}, {"hide": True}, {"answer": busy}]})
    out = _dialog(cases)
    for name in errors:
        *_, waiting, answered = out[name]
        assert waiting["confirm"]["disabled"] is True and waiting["focus"] == "batch-dialog", name   # Esc still works
        assert answered["confirm"]["disabled"] is False and answered["focus"] == "batch-dialog-confirm", name
        assert answered["open"] is True and len(answered["toasts"]) == 1, name
    *_, focused, waiting, answered = out["box"]
    assert focused["focus"] == "batch-item-3001"
    assert waiting["items"][0]["disabled"] is True and waiting["focus"] == "batch-dialog"
    assert answered["focus"] == "batch-dialog-confirm"
    *_, waiting, hidden, answered = out["closed"]
    assert waiting["focus"] == "batch-dialog" and hidden["focus"] == "opener"
    assert answered["focus"] == "opener" and answered["open"] is False       # never pulled back into a closed dialog


UTILS_ERROR_HARNESS = FAKE_DOM + r"""
const [utilsPath] = process.argv.slice(1);
(0, eval)(fs.readFileSync(utilsPath, 'utf8'));
const answers = [
  { ok: false, status: 409, json: () => Promise.resolve({ status: 'error', reason: 'stale', message: 'm', preview: { token: 't' } }) },
  { ok: false, status: 502, json: () => Promise.reject(new Error('not json')) },
  { ok: true, status: 201, json: () => Promise.resolve({ status: 'ok' }) },
];
global.fetch = () => Promise.resolve(answers.shift());
(async () => {
  const out = [];
  for (let i = 0; i < 3; i++) {
    try { out.push({ ok: await window.apiFetch('/x', { method: 'POST' }) }); }
    catch (e) { out.push({ status: e.status, message: e.message, data: e.data === undefined ? 'none' : e.data }); }
  }
  process.stdout.write(JSON.stringify(out));
})().catch(err => { console.error(err && err.stack || err); process.exit(1); });
"""


# 53 (utils.js carries the server's answer on HTTP errors)
def test_api_fetch_errors_carry_the_server_answer():
    stale, not_json, ok = _node(UTILS_ERROR_HARNESS, STATIC_JS / "utils.js")
    assert stale == {"status": 409, "message": "m",
                     "data": {"status": "error", "reason": "stale", "message": "m", "preview": {"token": "t"}}}
    assert not_json == {"status": 502, "message": "请求失败 (HTTP 502)", "data": "none"}
    assert ok == {"ok": {"status": "ok"}}


# 54
def test_confirm_toasts_close_and_call_on_done_once(windows):
    a2 = _preview("undownloaded_favourites", A2_ITEMS)
    selected = _preview("selected_favourites", [_fav_item("4001", "Never")], SELECTED_SKIPPED)
    cases = []
    for name in ("off", "open", "today", "tomorrow", "never", "invalid"):
        cases.append({"name": "a1_" + name, "steps": _opened("new_chapters", _preview("new_chapters", A1_ITEMS))
                      + [{"confirm": 1}, {"answer": {"ok": _created("new_chapters", A1_ITEMS, windows[name])}}]})
    cases.append({"name": "a2", "steps": _opened("undownloaded_favourites", a2)
                  + [{"confirm": 1}, {"answer": {"ok": _created("undownloaded_favourites", A2_ITEMS)}}]})
    cases.append({"name": "a2_today", "steps": _opened("undownloaded_favourites", a2)
                  + [{"confirm": 1}, {"answer": {"ok": _created("undownloaded_favourites", A2_ITEMS, windows["today"])}}]})
    cases.append({"name": "selected", "steps": _opened("selected_favourites", selected, SELECTED_IDS)
                  + [{"confirm": 1}, {"answer": {"ok": _created("selected_favourites", [_fav_item("4001", "Never")])}}]})
    errors = {
        "nothing": ({"status": 400, "message": "没有勾选要下载的漫画",
                     "data": {"status": "error", "reason": "nothing_selected", "message": "没有勾选要下载的漫画"}},
                    [["没有勾选要下载的漫画", "warning"]]),
        "server": ({"status": 500, "message": "没能加入下载队列，没有创建任何任务，请重新打开清单"},
                   [["没能加入下载队列，没有创建任何任务，请重新打开清单", "danger"]]),
        "bad_request": ({"status": 400, "message": "缺少确认信息，请重新打开清单"},
                        [["缺少确认信息，请重新打开清单", "danger"]]),
        "network": ({"name": "TypeError", "message": "Failed to fetch"}, [["网络错误", "danger"]]),
        "timeout": ({"isTimeout": True, "message": "请求超时，请稍后重试"}, [["请求超时，请稍后重试", "danger"]]),
        "abort": ({"name": "AbortError"}, []),
    }
    # after a successful confirm the same dialog never sends again (even if the handler ran once more)
    cases.append({"name": "again", "steps": _opened("new_chapters", _preview("new_chapters", A1_ITEMS))
                  + [{"confirm": 1}, {"answer": {"ok": _created("new_chapters", A1_ITEMS)}}, {"forceConfirm": True}]})
    for name, (error, _) in errors.items():
        cases.append({"name": name, "steps": _opened("new_chapters", _preview("new_chapters", A1_ITEMS))
                      + [{"confirm": 1}, {"answer": {"error": error}}]})
    out = _dialog(cases)
    queued = "已加入下载队列：2 部漫画的 9 话新章节"
    expected = {
        "a1_off": [queued, "success"], "a1_open": [queued, "success"],
        "a1_today": [queued + "，今天 23:00 起开始下载（定时下载）", "info"],
        "a1_tomorrow": [queued + "，明天 08:00 起开始下载（定时下载）", "info"],
        "a1_never": [queued + "；定时下载的时间段设置不对，任务不会自动开始", "warning"],
        "a1_invalid": [queued + "；定时下载的时间段设置不对，任务不会自动开始", "warning"],
        "a2": ["已加入下载队列：2 部收藏（整部）", "success"],
        "a2_today": ["已加入下载队列：2 部收藏（整部），今天 23:00 起开始下载（定时下载）", "info"],
        "selected": ["已加入下载队列：1 部收藏（整部）", "success"],
    }
    for name, toast in expected.items():
        after = out[name][-1]
        assert after["toasts"] == [toast], name
        assert after["open"] is False and after["hides"] == 1 and after["done"] == 1, name
        assert after["focus"] == "opener" and after["calls"] == [], name
    again = out["again"][-1]
    assert again["calls"] == [] and again["toasts"] == [] and again["done"] == 1
    for name, (_, toasts) in errors.items():
        after = out[name][-1]
        assert after["toasts"] == toasts, name
        assert after["open"] is True and after["done"] == 0, name              # the dialog stays; try again
        assert after["confirm"]["disabled"] is False and after["confirm"]["busy"] is None, name
        assert after["confirm"]["text"] == "加入下载队列（2 部）", name
        assert all(not i["disabled"] for i in after["items"]), name


# 55
def test_batch_script_never_downloads_or_claims_completeness():
    source = _read("batch-download.js")
    for forbidden in ("/api/jobs", "/api/wishlist/download", ".click(", "innerHTML", "insertAdjacentHTML",
                      "setInterval", "setTimeout", "本地文件完整", "已下载 · 可离线阅读", "有更新", "modal fade"):
        assert forbidden not in source, forbidden
    assert "root = node('div', 'modal');" in source          # no fade class: no animation (checked live in 50)
    # the only endpoints it knows are the two batch routes
    assert set(re.findall(r"'(/api/[^']*)'", source)) == {PREVIEW, CONFIRM}


# ─── 收藏 (wishlist.js): the header button, 下载选中的收藏 and the single-row 下载 ──────────

WISHLIST_HARNESS = FAKE_DOM + r"""
const [utilsPath] = process.argv.slice(1);
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
fixClock(input.now);
(0, eval)(fs.readFileSync(utilsPath, 'utf8'));
const fakeEl = el;   // wishlist.js has its own el(): keep the stand-in's
document.createElement = tag => { const e = fakeEl(tag); e.dataset = {}; return e; };
window.readLink = { create: () => null, stateFor: () => 'unknown' };
global.tbody = el('tbody'); body.appendChild(tbody);
for (const id of ['wishlist-download-undownloaded-btn', 'batch-download-btn']) { const b = el('button'); b.id = id; body.appendChild(b); }
global.currentPage = 3;
global.allItems = input.items;
let selected = [];
global.getSelectedIds = () => selected.slice();
const loads = []; global.loadWishlist = page => { loads.push(page); };
const toasts = []; global.showToast = (m, t) => toasts.push([m, t]);
const confirms = []; let confirmAnswer = true;
window.confirm = m => { confirms.push(m); return confirmAnswer; };
const opens = []; let dialogOpen = false, lastDone = null;
window.batchDownloadDialog = {
  open: (kind, o) => { opens.push({ kind, albumIds: o.albumIds == null ? null : o.albumIds, opener: o.opener ? o.opener.id : null });
                       lastDone = o.onDone; return true; },
  isOpen: () => dialogOpen };
const calls = [], answers = [];
window.apiFetch = (url, opts) => {
  opts = opts || {};
  calls.push({ url, method: opts.method || 'GET', body: opts.body ? JSON.parse(opts.body) : null, timeoutMs: opts.timeoutMs });
  const a = answers.shift();
  if (!a) return new Promise(() => {});
  return a.error ? Promise.reject(makeError(a.error)) : Promise.resolve(a.ok);
};
(0, eval)(input.code);
(async () => {
  const out = { steps: [] };
  let seen = { calls: 0, toasts: 0, loads: 0, confirms: 0, opens: 0 };
  for (const step of input.steps) {
    if (step.selected) selected = step.selected;
    if ('confirm' in step) confirmAnswer = step.confirm;
    if ('dialogOpen' in step) dialogOpen = step.dialogOpen;
    if (step.answer) answers.push(step.answer);
    let result = null;
    if (step.run === 'onDone') lastDone();
    else {
      result = window[step.run].apply(null, step.args || []);
      if (result && typeof result.then === 'function') result = await result;
    }
    await flush();
    out.steps.push({ result: result === undefined ? null : result, calls: calls.slice(seen.calls),
      toasts: toasts.slice(seen.toasts), loads: loads.slice(seen.loads), confirms: confirms.slice(seen.confirms),
      opens: opens.slice(seen.opens) });
    seen = { calls: calls.length, toasts: toasts.length, loads: loads.length, confirms: confirms.length, opens: opens.length };
  }
  out.rows = input.items.map(item => {
    const b = buildRow(item, false).querySelectorAll('button')[0];
    return { action: b.dataset.action, title: b.title, aria: b.getAttribute('aria-label') };
  });
  out.htmlParsed = htmlParsed;
  process.stdout.write(JSON.stringify(out));
})().catch(err => { console.error(err && err.stack || err); process.exit(1); });
"""

WISHLIST_BLOCKS = ["function toast(", "function el(", "function icon(", "function badge(", "function cell(",
                   "function buildRow(", "function actionButton(", "function statusBadges(", "function activityBadge(",
                   "function findItem(", "function scheduledStart(", "async function downloadSingle(",
                   "function openBatchDialog(", "function batchDownload(", "function downloadUndownloaded(",
                   "function canRefreshNow("]
READABLE_CONFIRM = ("「Readable」已有已下载的内容（可能只是部分章节）。\n"
                    "整部下载会按上游现在的全部章节下载：开启了「跳过已存在的文件」、漫画还在原来的下载文件夹里时，已下载的页通常会跳过；"
                    "关闭了这个设置、按作者整理过、或只剩压缩包的漫画，可能会重新下载全部图片。\n"
                    "只要新章节请用资源库的「下载新章节」。确定整部下载吗？")


def _single(job, window, skipped=()):
    return {"ok": {"status": "ok", "job_ids": [{"album_id": "21", "job_id": "job_1"}] if job else [],
                   "skipped": list(skipped), "window": window}}


# 56
def test_wishlist_opens_the_dialog_and_confirms_readable_single_rows(windows):
    source = _read("wishlist.js")
    items = [{"album_id": "21", "title": "Readable", "readable": True, "status_group": "readable"},
             {"album_id": "22", "title": "Plain", "readable": False, "status_group": "none"}]
    active = [{"album_id": "22", "reason": "active"}]
    steps = [
        {"run": "batchDownload", "selected": ["11", "12"]},
        {"run": "onDone"},
        {"run": "batchDownload", "selected": []},
        {"run": "downloadUndownloaded"},
        {"run": "onDone"},
        {"run": "downloadSingle", "args": ["21"], "confirm": False},
        {"run": "downloadSingle", "args": ["21"], "confirm": True, "answer": _single(True, windows["off"])},
        {"run": "downloadSingle", "args": ["22"], "answer": _single(False, windows["off"], active)},
        {"run": "downloadSingle", "args": ["22"], "answer": _single(True, windows["today"])},
        {"run": "downloadSingle", "args": ["22"], "answer": _single(True, windows["tomorrow"])},
        {"run": "downloadSingle", "args": ["22"], "answer": _single(True, windows["never"])},
        {"run": "downloadSingle", "args": ["22"], "answer": _single(True, windows["open"])},
        {"run": "downloadSingle", "args": ["22"], "answer": {"error": {"status": 500, "message": "没能加入下载队列，没有创建任何任务"}}},
        {"run": "canRefreshNow", "dialogOpen": False},
        {"run": "canRefreshNow", "dialogOpen": True},
    ]
    code = "\n".join(_js_block(source, marker) for marker in WISHLIST_BLOCKS)
    out = _node(WISHLIST_HARNESS, STATIC_JS / "utils.js", data={"now": NOW, "items": items, "steps": steps, "code": code})
    s = out["steps"]
    # 下载选中的收藏: the dialog with the ticked ids; nothing is posted from the page itself
    assert s[0]["opens"] == [{"kind": "selected_favourites", "albumIds": ["11", "12"], "opener": "batch-download-btn"}]
    assert s[0]["calls"] == [] and s[1]["loads"] == [3]                 # onDone: the same page (filters in state)
    assert s[2]["opens"] == [] and s[2]["calls"] == []
    assert s[3]["opens"] == [{"kind": "undownloaded_favourites", "albumIds": None,
                              "opener": "wishlist-download-undownloaded-btn"}]
    assert s[4]["loads"] == [3]
    # a readable row asks first; cancel sends nothing
    assert s[5]["confirms"] == [READABLE_CONFIRM] and s[5]["calls"] == [] and s[5]["toasts"] == []
    assert s[6]["confirms"] == [READABLE_CONFIRM]
    assert s[6]["calls"] == [{"url": "/api/wishlist/download", "method": "POST", "body": {"ids": ["21"]}, "timeoutMs": 60000}]
    assert s[6]["toasts"] == [["已加入下载队列", "success"]] and s[6]["loads"] == [3]
    # a non-readable row stays one click
    assert s[7]["confirms"] == [] and s[7]["toasts"] == [["这部漫画已在下载队列中，没有重复创建任务", "info"]]
    assert s[8]["toasts"] == [["已加入下载队列，今天 23:00 起开始（定时下载）", "info"]]
    assert s[9]["toasts"] == [["已加入下载队列，明天 08:00 起开始（定时下载）", "info"]]
    assert s[10]["toasts"] == [["已加入下载队列；定时下载的时间段设置不对，任务不会自动开始", "warning"]]
    assert s[11]["toasts"] == [["已加入下载队列", "success"]]
    assert s[12]["toasts"] == [["没能加入下载队列，没有创建任何任务", "danger"]]
    assert s[13]["result"] is True and s[14]["result"] is False       # no list rebuild under the open dialog
    # the readable row's 下载 says it will ask first
    assert out["rows"] == [{"action": "download", "title": "下载（已有已下载内容：整部下载前会先确认）",
                            "aria": "下载（已有已下载内容：整部下载前会先确认）"},
                           {"action": "download", "title": "下载", "aria": "下载"}]
    assert out["htmlParsed"] == []
    # wiring: the buttons call these functions; /api/wishlist/download is only the single row's
    flat = source.replace("\r\n", "\n")
    assert "document.getElementById('wishlist-download-undownloaded-btn').addEventListener('click', downloadUndownloaded);" in flat
    assert "document.getElementById('batch-download-btn').addEventListener('click', batchDownload);" in flat
    assert "window.batchDownload = batchDownload;" in flat
    assert flat.count("/api/wishlist/download") == 1
    assert "/api/wishlist/download" in _js_block(source, "async function downloadSingle(")


# ─── 资源库 (library.js): 下载新章节 ──────────────────────────────────────────────

LIBRARY_HARNESS = FAKE_DOM + r"""
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
global.grid = el('div'); global.activeFilters = el('div'); global.pagination = el('div');
const btn = el('button'); btn.id = 'library-download-new-btn'; body.appendChild(btn);
global.currentPage = 4;
const loads = [], stats = [];
global.loadLibrary = (page, opts) => loads.push([page, opts === undefined ? null : opts]);
global.loadStats = () => stats.push(1);
const opens = []; let dialogOpen = false, lastDone = null;
(0, eval)(input.code);
downloadNewChapters();                 // no dialog script on the page: nothing happens
const without = opens.length;
window.batchDownloadDialog = {
  open: (kind, o) => { opens.push({ kind, albumIds: o.albumIds === undefined ? 'unset' : o.albumIds, opener: o.opener ? o.opener.id : null });
                       lastDone = o.onDone; return true; },
  isOpen: () => dialogOpen };
downloadNewChapters();
const afterOpen = { opens: opens.slice(), loads: loads.slice(), stats: stats.length };
lastDone();
const afterDone = { loads: loads.slice(), stats: stats.length };
const refresh = [canRefreshNow()];
dialogOpen = true;
refresh.push(canRefreshNow());
process.stdout.write(JSON.stringify({ without, afterOpen, afterDone, refresh }));
"""


# 57
def test_library_download_new_chapters_button():
    source = _read("library.js")
    code = "\n".join(_js_block(source, m) for m in ("function downloadNewChapters(", "function canRefreshNow("))
    out = _node(LIBRARY_HARNESS, data={"code": code})
    assert out["without"] == 0
    assert out["afterOpen"] == {"opens": [{"kind": "new_chapters", "albumIds": "unset", "opener": "library-download-new-btn"}],
                                "loads": [], "stats": 0}
    # onDone: the same page again without the spinner (place and filters kept), and the stats
    assert out["afterDone"] == {"loads": [[4, {"showSpinner": False}]], "stats": 1}
    assert out["refresh"] == [True, False]
    flat = source.replace("\r\n", "\n")
    assert "document.getElementById('library-download-new-btn').addEventListener('click', downloadNewChapters);" in flat
    assert "/api/batch-downloads" not in source and "/api/jobs'" not in _js_block(source, "function downloadNewChapters(")


# ─── 详情页: the chapter table refresh after 立即检查 / 选中这些章节 ─────────────────

DETAIL_START = "// ── 章节更新 ──"
DETAIL_END = "// ── 章节更新结束 ──"

DETAIL_REFRESH_HARNESS = FAKE_DOM + r"""
const [utilsPath, detailPath] = process.argv.slice(1);
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
fixClock(input.now);
(0, eval)(fs.readFileSync(utilsPath, 'utf8'));
const src = fs.readFileSync(detailPath, 'utf8');
const START = '// ── 章节更新 ──', END = '// ── 章节更新结束 ──';
const code = src.slice(src.indexOf('function toastErr'), src.indexOf('function loadAlbum'))
  + src.slice(src.indexOf(START), src.indexOf(END));
global.albumId = '3001';
document.activeElement = null;
document.createElement = tag => { const e = el(tag); e.focus = () => { document.activeElement = e; }; return e; };
let calls = [], toasts = [], scrolls = [], claims = 0;
global.showToast = (m, t) => toasts.push([m, t]);
global.setTimeout = () => 0;
global.clearTimeout = () => {};
window.scrollY = 480;
window.scrollTo = o => scrolls.push(o);
global.refreshChapterClaim = () => { claims++; };

function page() {
  body.children = [];
  const box = el('div'); box.id = 'album-update-status'; box.hidden = true; body.appendChild(box);
  const head = el('span'); body.appendChild(head);
  const count = el('span'); count.id = 'chapter-count'; head.appendChild(count);
  const status = el('span'); status.id = 'chapter-refresh-status'; head.appendChild(status);
  const selectMain = el('input'); selectMain.id = 'select-all-chapters'; body.appendChild(selectMain);
  const table = el('table'); table.className = 'chapter-table'; body.appendChild(table);
  const selectInline = el('input'); selectInline.id = 'select-all-inline'; table.appendChild(selectInline);
  const rows = el('tbody'); rows.id = 'chapter-rows'; table.appendChild(rows);
  return { box, count, status, rows, selectMain, selectInline };
}

function snapshot(p) {
  const trs = p.rows.children;
  const cb = tr => tr.querySelector('.chapter-checkbox-item');
  return {
    rows: trs.map(tr => cb(tr).value),
    checked: trs.filter(tr => cb(tr).checked).map(tr => cb(tr).value),
    marks: Object.fromEntries(trs.map(tr => [cb(tr).value, tr.children[2].querySelectorAll('.badge').map(m => m.textContent)])
      .filter(([, m]) => m.length)),
    titles: trs.map(tr => tr.children[2].children.filter(n => n.nodeType === 3).map(n => n.data).join('')),
    indexes: trs.map(tr => tr.children[1].textContent),
    count: p.count.textContent,
    status: p.status.textContent,
    selectAll: [!!p.selectMain.checked, !!p.selectInline.checked],
    rowScrolls: trs.filter(tr => tr.scrolls.length).map(tr => cb(tr).value),
    focus: document.activeElement ? document.activeElement.value : null,
    focusInTable: !!document.activeElement && p.rows.contains(document.activeElement),
    lastRowHtml: trs.length ? trs[trs.length - 1].outerHTML : null,
  };
}

async function runCase(c) {
  const p = page();
  calls = []; toasts = []; scrolls = []; claims = 0; document.activeElement = null;
  (0, eval)(code);   // fresh module state for every case
  const keep = {};
  (c.checked || []).forEach(id => { keep[id] = true; });
  fillChapterRows(c.photos, keep);
  p.count.textContent = String(c.photos.length);
  const gets = [], held = [], albums = [], heldAlbums = [];
  window.apiFetch = (url, opts) => {
    opts = opts || {};
    calls.push({ url, method: opts.method || 'GET', body: opts.body === undefined ? null : opts.body,
                 abortKey: opts.abortKey || null, timeoutMs: opts.timeoutMs === undefined ? null : opts.timeoutMs });
    if (url === '/api/updates/3001' && (opts.method || 'GET') === 'GET') return gets.length ? Promise.resolve(gets.shift()) : new Promise(() => {});
    if (url === '/api/updates/3001/check') return new Promise((resolve, reject) => held.push({ resolve, reject }));
    if (url === '/api/album/3001') {
      const a = albums.shift();
      if (!a || a.hold) return new Promise((resolve, reject) => heldAlbums.push({ resolve, reject }));
      return a.error ? Promise.reject(makeError(a.error)) : Promise.resolve(a.ok);
    }
    return new Promise(() => {});
  };
  const steps = [];
  let seenCalls = 0, seenToasts = 0, seenScrolls = 0;
  for (const step of c.steps) {
    if (step.albums) albums.push(...step.albums);
    if (step.focus) document.activeElement = p.rows.children.map(tr => tr.querySelector('.chapter-checkbox-item')).find(x => x.value === step.focus);
    if (step.load !== undefined) {
      gets.push(step.load);
      refreshUpdateStatus();
    } else if (step.check) {
      p.box.querySelector('[data-action="check-updates"]').click();
      await flush();
      held.shift().resolve(step.check);
    } else if (step.select) {
      p.box.querySelector('[data-action="select-new-chapters"]').click();
    } else if (step.answerAlbum) {
      const h = heldAlbums.shift();
      if (step.answerAlbum.error) h.reject(makeError(step.answerAlbum.error)); else h.resolve(step.answerAlbum.ok);
    }
    await flush();
    steps.push(Object.assign(snapshot(p), { calls: calls.slice(seenCalls), toasts: toasts.slice(seenToasts),
                                            scrollTo: scrolls.slice(seenScrolls), claims }));
    seenCalls = calls.length; seenToasts = toasts.length; seenScrolls = scrolls.length;
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


def _photos(*ids):
    return [{"photo_id": str(i), "title": f"第{i - 70}话", "page_count": 20} for i in ids]


def _album(*ids):
    return {"ok": {"status": "ok", "data": {"album_id": "3001", "title": "T", "photos": _photos(*ids)}}}


def _with_new(*new_ids, outcome=None):
    row = _UI._row(baseline_ids=[str(i) for i in range(71, 76)], baseline_source="download", upstream_count=5 + len(new_ids),
                   result="new", new_chapters=[_chapter(str(i), i - 70) for i in new_ids],
                   last_success_at=_UI.TODAY_1330, next_check_at=_UI.TOMORROW_1030)
    return _UI._payload(row, outcome=outcome)


ALBUM_GET = {"url": "/api/album/3001", "method": "GET", "body": None, "abortKey": "detail-chapter-refresh",
             "timeoutMs": 30000}
CHECK_POST = {"url": "/api/updates/3001/check", "method": "POST", "body": None, "abortKey": "detail-update-check",
              "timeoutMs": 100000}
FIVE = list(range(71, 76))
SIX = list(range(71, 77))


def _detail_refresh(cases):
    return _node(DETAIL_REFRESH_HARNESS, STATIC_JS / "utils.js", STATIC_JS / "detail.js",
                 data={"now": NOW, "cases": cases})


# 58
def test_detail_refreshes_the_chapter_table_after_a_check():
    before = _UI._payload(_UI.ROWS["no_update"])
    out = _detail_refresh([
        {"name": "new", "photos": _photos(*FIVE), "checked": ["72"],
         "steps": [{"load": before}, {"focus": "73", "check": _with_new(76, outcome="new"), "albums": [_album(*SIX)]},
                   {"select": True}]},
        {"name": "present", "photos": _photos(*SIX), "steps": [{"load": before}, {"check": _with_new(76, outcome="new")}]},
        {"name": "fails", "photos": _photos(*FIVE), "checked": ["72"],
         "steps": [{"load": before}, {"check": _with_new(76, outcome="new"),
                                      "albums": [{"error": {"status": 500, "message": "x"}}]}]},
        {"name": "aborted", "photos": _photos(*FIVE),
         "steps": [{"load": before}, {"check": _with_new(76, outcome="new"), "albums": [{"error": {"name": "AbortError"}}]}]},
        {"name": "twice", "photos": _photos(*FIVE),
         "steps": [{"load": before}, {"check": _with_new(76, outcome="new"), "albums": [_album(*FIVE)]},
                   {"check": _with_new(76, outcome="coalesced")}, {"select": True}]},
        {"name": "not_eligible", "photos": _photos(*FIVE),
         "steps": [{"load": before}, {"check": dict(_UI.NOT_ELIGIBLE, outcome="failed")}]},
    ])
    loaded, checked, selected = out["new"]
    assert loaded["rows"] == ["71", "72", "73", "74", "75"] and loaded["count"] == "5"
    # exactly one read-only GET of the album, after the check
    assert checked["calls"] == [CHECK_POST, ALBUM_GET]
    assert checked["rows"] == ["71", "72", "73", "74", "75", "76"] and checked["count"] == "6"
    assert checked["indexes"] == ["1", "2", "3", "4", "5", "6"]
    assert checked["checked"] == ["72"]                         # the earlier tick is kept, the new row is not ticked
    assert checked["marks"] == {"76": ["新"]} and checked["titles"][-1] == "第6话"
    assert checked["selectAll"] == [False, False]
    assert checked["scrollTo"] == [{"top": 480, "behavior": "instant"}]
    assert checked["focus"] == "73" and checked["focusInTable"] is True     # keyboard focus on the same chapter
    assert checked["claims"] == 1                               # “部分章节已下载 · M/N 话” is checked again
    assert checked["status"] == "章节列表已更新，新增 1 话"
    assert checked["toasts"] == [["发现 1 话新章节（只提示，不会自动下载）", "success"]]
    # the rebuilt row has the same markup as before (checkbox, index, title, pages, 在线观看本章)
    html = checked["lastRowHtml"]
    assert html.startswith('<tr><td class="chapter-checkbox"><input class="form-check-input chapter-checkbox-item" '
                           'type="checkbox" name="chapters"></input></td><td>6</td><td>第6话')
    assert ('<td>20</td><td class="chapter-online"><a class="btn btn-sm btn-outline-primary" '
            'href="/online/3001?chapter=76" title="在线观看本章" aria-label="在线观看第 6 章">'
            '<i class="bi bi-globe2" aria-hidden="true"></i></a></td></tr>') in html
    # then 选中这些章节 ticks exactly the new rows, with no request
    assert selected["checked"] == ["76"] and selected["calls"] == [] and selected["rowScrolls"] == ["76"]
    assert selected["toasts"] == [["已选中 1 话新章节，点「下载选中章节」开始下载", "info"]]
    # nothing missing → no album GET
    assert out["present"][1]["calls"] == [CHECK_POST] and out["present"][1]["status"] == ""
    # the GET fails → a warning, the table is untouched
    failed = out["fails"][1]
    assert failed["toasts"] == [["发现 1 话新章节（只提示，不会自动下载）", "success"],
                                ["章节列表没能刷新，请稍后刷新页面再选", "warning"]]
    assert failed["rows"] == ["71", "72", "73", "74", "75"] and failed["checked"] == ["72"] and failed["count"] == "5"
    assert failed["status"] == "" and failed["claims"] == 0 and failed["scrollTo"] == []
    aborted = out["aborted"][1]
    assert aborted["toasts"] == [["发现 1 话新章节（只提示，不会自动下载）", "success"]] and aborted["status"] == ""
    # the same missing set twice → one GET; selecting then says to reload the page later
    _, first, second, select = out["twice"]
    assert first["calls"] == [CHECK_POST, ALBUM_GET] and first["status"] == "章节列表已更新，新增 0 话"
    assert second["calls"] == [CHECK_POST]
    assert select["calls"] == [] and select["toasts"] == [["章节列表里还没有这些新章节，请稍后刷新页面再选", "warning"]]
    assert out["not_eligible"][1]["calls"] == [CHECK_POST]
    assert out["htmlParsed"] == []


def test_select_refreshes_then_selects():
    new76 = _with_new(76)
    out = _detail_refresh([
        {"name": "select", "photos": _photos(*FIVE), "checked": ["71"],
         "steps": [{"load": new76}, {"select": True, "albums": [{"hold": True}]}, {"select": True},
                   {"answerAlbum": _album(*SIX)}]},
        {"name": "still_missing", "photos": _photos(*FIVE),
         "steps": [{"load": new76}, {"select": True, "albums": [_album(*FIVE)]}, {"select": True}]},
        # the refresh brings 76 but not 77: one refresh only, then ask for a page reload later
        {"name": "partly", "photos": _photos(*FIVE), "checked": ["72"],
         "steps": [{"load": _with_new(76, 77)}, {"select": True, "albums": [_album(*SIX)]}]},
    ])
    _, asked, waiting, answered = out["select"]
    assert asked["toasts"] == [["章节列表里还没有这些新章节，正在刷新章节列表…", "info"]]
    assert asked["calls"] == [ALBUM_GET] and asked["status"] == "正在刷新章节列表…" and asked["checked"] == ["71"]
    assert waiting["toasts"] == [["正在刷新章节列表，请稍候", "info"]] and waiting["calls"] == []
    assert answered["rows"] == [str(i) for i in SIX] and answered["checked"] == ["76"]
    assert answered["toasts"] == [["已选中 1 话新章节，点「下载选中章节」开始下载", "info"]]
    assert answered["rowScrolls"] == ["76"] and answered["marks"] == {"76": ["新"]}
    assert answered["status"] == "章节列表已更新，新增 1 话" and answered["calls"] == []
    _, refreshed, again = out["still_missing"]
    assert refreshed["calls"] == [ALBUM_GET]
    assert refreshed["toasts"] == [["章节列表里还没有这些新章节，正在刷新章节列表…", "info"],
                                   ["章节列表里还没有这些新章节，请稍后刷新页面再选", "warning"]]
    assert again["calls"] == [] and again["toasts"] == [["章节列表里还没有这些新章节，请稍后刷新页面再选", "warning"]]
    _, partly = out["partly"]
    assert partly["calls"] == [ALBUM_GET] and partly["rows"] == [str(i) for i in SIX]
    assert partly["toasts"] == [["章节列表里还没有这些新章节，正在刷新章节列表…", "info"],
                                ["章节列表里还没有这些新章节，请稍后刷新页面再选", "warning"]]
    assert partly["checked"] == ["72"] and partly["status"] == "章节列表已更新，新增 1 话"


def test_detail_table_is_built_from_dom_and_wired_by_delegation():
    source = _read("detail.js")
    flat = source.replace("\r\n", "\n")
    block = flat[flat.index(DETAIL_START):flat.index(DETAIL_END)]
    for forbidden in ("/api/jobs", "createDownloadJob", "download-selected-btn", "download-all-btn", ".click(",
                      "innerHTML", "insertAdjacentHTML"):
        assert forbidden not in block, forbidden
    for name in ("function chapterRow(", "function fillChapterRows(", "function syncSelectAll(",
                 "function missingNewChapterIds(", "function refreshChapterTable("):
        assert name in block, name
    render = flat[flat.index("function renderAlbum"):flat.index("function bindBackLink")]
    assert '<span class="badge bg-secondary" id="chapter-count">' in render
    assert '<span id="chapter-refresh-status" class="chapter-refresh-status" role="status" aria-live="polite"></span>' in render
    assert "<tbody id=\"chapter-rows\"></tbody></table>" in render and "chapter-checkbox-item" not in render
    assert render.index("container.innerHTML = html;") < render.index("fillChapterRows(album.photos || [], {});")
    assert "暂无章节信息" in render
    events = flat[flat.index("function bindEvents"):flat.index("function createDownloadJob")]
    assert "chapterRows.addEventListener('change', syncSelectAll);" in events
    assert "document.querySelectorAll('.chapter-checkbox-item').forEach" in events   # re-queried on every toggle
    assert ":checked" not in events.split("// 下载选中章节")[0]


# 59
def test_batch_css_tokens_only_no_animation():
    css = (ROOT / "static" / "css" / "style.css").read_text(encoding="utf-8")
    block = css[css.index("批量下载确认 · 章节表刷新（2026-09-28 新增"):]
    block = block[block.index("*/") + 2:]
    if "/* ════" in block:
        block = block[:block.index("/* ════")]
    rules = re.sub(r"/\*.*?\*/", "", block, flags=re.S)
    for selector in (".batch-dialog-summary {", ".batch-dialog-alert {", ".batch-dialog-alert--wait {",
                     ".batch-dialog-list {", ".batch-dialog-item {", ".batch-dialog-sub {", ".chapter-refresh-status {"):
        assert selector in rules, selector
    for prop, value in re.findall(r"([\w-]+)\s*:\s*([^;{}]+);", rules):
        if "color" in prop or "background" in prop or prop.startswith("border"):
            colours = re.sub(r"\b1px solid\b", "", value).strip()
            assert re.fullmatch(r"var\(--[\w-]+\)", colours), (prop, value)
        assert "#" not in value and "rgb" not in value, (prop, value)
        if prop == "font-size":
            assert value.strip() in ("12px", "14px"), value
        if prop == "font-weight":
            assert value.strip() in ("400", "500"), value
        if prop == "border-radius":
            assert value.strip() == "var(--radius-lg)", value
    assert not re.search(r"transition|animation|@keyframes", rules)
    assert set(re.findall(r"(\d+)px", rules)) <= {"1", "4", "8", "12", "14", "16"}
    assert "--text-tertiary" not in rules                     # 12px ids use --text-secondary (AA contrast)


# 60
def test_templates_buttons_scripts_and_settings_copy(client):
    library = client.get("/library").get_data(as_text=True)
    group = library[library.index('<div class="d-flex gap-2 flex-wrap">'):]
    group = group[:group.index("</div>")]
    first = re.search(r"<button([^>]*)>(.*?)</button>", group, re.S)
    assert 'id="library-download-new-btn"' in first.group(1)
    for attr in ('type="button"', 'class="btn btn-outline-primary btn-sm"', 'aria-haspopup="dialog"',
                 'title="只下载检查确认过的新章节：先列出要下载的漫画和章节，确认后才加入下载队列"'):
        assert attr in first.group(1), attr
    assert first.group(2).split() == ['<i', 'class="bi', 'bi-bell"', 'aria-hidden="true"></i>', '下载新章节']

    wishlist = client.get("/wishlist").get_data(as_text=True)
    header = wishlist[wishlist.index('<h4 class="text-nowrap">'):wishlist.index("<!-- 搜索 / 下载状态筛选")]
    first = re.search(r"<button([^>]*)>(.*?)</button>", header, re.S)
    assert 'id="wishlist-download-undownloaded-btn"' in first.group(1)
    for attr in ('type="button"', 'class="btn btn-outline-primary btn-sm"', 'aria-haspopup="dialog"',
                 'title="只下载“未下载”里的收藏（从未下载过的、下载被取消的），每部下载整部：先列出清单，确认后才加入下载队列"'):
        assert attr in first.group(1), attr
    assert first.group(2).split() == ['<i', 'class="bi', 'bi-cloud-arrow-down"', 'aria-hidden="true"></i>',
                                      '下载未下载的收藏']
    selected = re.search(r'<button([^>]*id="batch-download-btn"[^>]*)>(.*?)</button>', wishlist, re.S)
    assert 'title="只下载选中的收藏里“未下载”的（整部）：先列出清单，确认后才加入下载队列"' in selected.group(1)
    assert 'aria-haspopup="dialog"' in selected.group(1)
    assert selected.group(2).split()[-1] == "下载选中的收藏" and "批量下载" not in selected.group(2)

    for html, page in ((library, "library.js"), (wishlist, "wishlist.js")):
        tag = '<script src="/static/js/%s?v='
        assert html.index(tag % "utils.js") < html.index(tag % "batch-download.js") < html.index(tag % page), page
    detail = client.get("/album/123").get_data(as_text=True)
    assert "batch-download.js" not in detail

    settings = client.get("/settings").get_data(as_text=True)
    assert "不受时间段限制" not in settings and "您仍可随时手动启动下载" not in settings
    assert ("启用定时下载后，所有排队的任务（包括刚加入的、批量加入的）都只在这个时间段内开始；"
            "时间段外加入的任务会先排队，到时间段开始时程序需要开着。") in settings
