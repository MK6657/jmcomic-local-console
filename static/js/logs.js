/**
 * JMComic 下载控制台 — 设置页「日志与诊断」
 * 查看最近日志（最新在前）、按“只看问题 / 全部”切换、搜索、打开日志文件夹。
 * 依赖: utils.js (window.apiFetch)、base.html (window.showToast)
 *
 * 安全：日志里有远端漫画标题和用户输入，所有日志文字一律用 createElement + textContent 渲染，
 * 本文件从不把字符串当 HTML 解析（tests/test_log_viewer.py 有静态检查）。
 */

(function () {
  'use strict';

  var ENDPOINT = '/api/system/logs';
  var OPEN_FOLDER_ENDPOINT = '/api/system/logs/open-folder';
  var DEFAULT_LIMIT = 200;
  var MAX_LIMIT = 500;
  var SEARCH_DELAY_MS = 300;
  var SUMMARY_PREFIX = '↑ 同类消息';

  var LEVEL_LABELS = {
    CRITICAL: '严重',
    ERROR: '错误',
    WARNING: '警告',
    INFO: '信息',
    DEBUG: '调试',
  };

  var state = {
    source: 'error',
    query: '',
    limit: DEFAULT_LIMIT,
    loaded: false,
    // pagehide（含进入 bfcache）中止了正在进行的读取；页面恢复时重新读取，不留下一直转圈的状态
    interrupted: false,
    seq: 0,
    // 通过 #logs 打开时，列表渲染后页面变长，浏览器的滚动锚定会把视口推向页脚；渲染后再定位一次
    scrollAfterRender: false,
  };

  var els = {};
  var searchTimer = null;
  // 输入法组字中（拼音等）：组字未完成前不搜索，否则每个拼音字母都会触发一次完整的日志扫描
  var composing = false;
  // 本页面实例的标识：和 state.seq 一起随每次读取发给服务器，服务器据此停止本页已被取代的旧读取
  // （浏览器中止 fetch 并不会让服务器停止扫描）
  var VIEWER_ID = (function () {
    var bytes = new Uint8Array(8);
    try {
      window.crypto.getRandomValues(bytes);
    } catch (e) {
      for (var i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256);
    }
    return Array.prototype.map.call(bytes, function (b) { return ('0' + b.toString(16)).slice(-2); }).join('');
  })();

  function cacheElements() {
    els.section = document.getElementById('logs');
    els.dir = document.getElementById('log-dir');
    els.totalSize = document.getElementById('log-total-size');
    els.retention = document.getElementById('log-retention');
    els.search = document.getElementById('log-search');
    els.refreshBtn = document.getElementById('log-refresh-btn');
    els.openFolderBtn = document.getElementById('log-open-folder-btn');
    els.status = document.getElementById('log-status');
    els.list = document.getElementById('log-list');
    els.more = document.getElementById('log-more');
    els.moreBtn = document.getElementById('log-more-btn');
    els.sourceBtns = els.section ? els.section.querySelectorAll('[data-log-source]') : [];
  }

  // ── DOM 小工具（只用 textContent） ──

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function levelKind(level) {
    switch (String(level || '').toUpperCase()) {
      case 'CRITICAL':
      case 'ERROR':
        return 'error';
      case 'WARNING':
        return 'warning';
      case 'INFO':
        return 'info';
      default:
        return 'muted';
    }
  }

  function formatExtra(extra) {
    if (!extra || typeof extra !== 'object') return '';
    return Object.keys(extra).map(function (key) {
      var value = extra[key];
      if (value !== null && typeof value === 'object') {
        try { value = JSON.stringify(value); } catch (e) { value = String(value); }
      }
      return key + '=' + value;
    }).join('  ');
  }

  function formatSize(mb) {
    var n = Number(mb);
    if (!isFinite(n) || n <= 0) return '0 MB';
    if (n < 0.01) return '< 0.01 MB';
    return n.toFixed(2) + ' MB';
  }

  // ── 状态行：加载 / 空 / 错误 / 说明 ──

  // partial：没读完全部日志（超出读取上限），空结果不能显示成“确认没有”的对勾
  function setStatus(kind, text, withRetry, partial) {
    var box = els.status;
    clear(box);
    box.className = 'log-viewer-status' + (kind ? ' log-viewer-status--' + kind : '');
    if (kind === 'loading') {
      var spinner = el('span', 'spinner-border spinner-border-sm');
      spinner.setAttribute('aria-hidden', 'true');
      box.appendChild(spinner);
    } else if (kind === 'error') {
      var icon = el('i', 'bi bi-exclamation-octagon');
      icon.setAttribute('aria-hidden', 'true');
      box.appendChild(icon);
    } else if (kind === 'empty') {
      var ok = el('i', partial ? 'bi bi-info-circle' : 'bi bi-check2-circle');
      ok.setAttribute('aria-hidden', 'true');
      box.appendChild(ok);
    }
    box.appendChild(el('span', null, text));
    if (withRetry) {
      var retry = el('button', 'btn btn-outline-secondary btn-sm', '重试');
      retry.type = 'button';
      retry.addEventListener('click', load);
      box.appendChild(retry);
    }
  }

  function setBusy(busy) {
    if (els.list) els.list.setAttribute('aria-busy', busy ? 'true' : 'false');
    if (els.refreshBtn) els.refreshBtn.disabled = !!busy;
  }

  // ── 渲染 ──

  function renderSummary(data) {
    if (els.dir) els.dir.textContent = data.log_dir || '—';
    if (els.totalSize) {
      var files = Array.isArray(data.files) ? data.files.length : 0;
      els.totalSize.textContent = formatSize(data.total_size_mb) + (files ? '（' + files + ' 个文件）' : '');
    }
    if (els.retention) {
      els.retention.textContent = data.retention_days != null ? String(data.retention_days) : '—';
    }
  }

  function renderEntry(entry) {
    var level = String(entry.level || '').toUpperCase();
    var kind = levelKind(level);
    var item = el('li', 'log-entry log-entry--' + kind);

    var meta = el('div', 'log-entry-meta');
    var time = el('time', 'log-entry-time', entry.time || '时间未知');
    if (entry.time) time.setAttribute('datetime', String(entry.time).replace(' ', 'T'));
    meta.appendChild(time);

    var badge = el('span', 'log-level log-level--' + kind, LEVEL_LABELS[level] || level || '未知');
    if (level) badge.title = level;
    meta.appendChild(badge);

    if (entry.logger) meta.appendChild(el('span', 'log-entry-logger', entry.logger));

    if (entry.request_id) {
      var rid = el('span', 'log-entry-rid', entry.request_id);
      rid.title = '请求 ID（点一下即可选中复制）';
      meta.appendChild(rid);
      var trace = el('button', 'log-entry-trace', '只看此请求');
      trace.type = 'button';
      trace.dataset.requestId = String(entry.request_id);
      trace.title = '在全部日志中筛选同一请求（或同一下载任务）的记录';
      meta.appendChild(trace);
    }
    item.appendChild(meta);

    var text = String(entry.message || '');
    var nl = text.indexOf('\n');
    var firstLine = nl === -1 ? text : text.slice(0, nl);
    var rest = nl === -1 ? '' : text.slice(nl + 1);
    if (firstLine.indexOf(SUMMARY_PREFIX) === 0) item.classList.add('log-entry--summary');
    item.appendChild(el('div', 'log-entry-message', firstLine || '（空消息）'));

    if (rest) {
      var details = el('details', 'log-entry-detail');
      details.appendChild(el('summary', null, '详细信息（' + rest.split('\n').length + ' 行）'));
      details.appendChild(el('pre', 'log-entry-pre', rest));
      item.appendChild(details);
    }

    var extraText = formatExtra(entry.extra);
    if (extraText) item.appendChild(el('div', 'log-entry-extra', extraText));

    return item;
  }

  // 服务端本次最多读取的日志量（响应里的 read_limit_mb；带搜索词时更大）
  function readLimitText(data) {
    var mb = Number(data && data.read_limit_mb);
    return (isFinite(mb) && mb > 0 ? String(mb) : '4') + ' MB';
  }

  function emptyText(data) {
    if (data && data.truncated) {
      // 读取上限内没有匹配，但更早的日志根本没读：不能说“没有”
      var scope = '最近 ' + readLimitText(data) + ' 的日志里';
      var what = state.query ? '没有找到包含“' + state.query + '”的记录' : '没有可显示的记录';
      return scope + what + '；更早的日志没有读取，可以打开日志文件夹查看。';
    }
    if (state.query) return '没有找到包含“' + state.query + '”的日志。';
    if (state.source === 'error') return '最近没有警告或错误。想看运行记录可以切换到“全部”。';
    return '暂无日志记录。';
  }

  function renderEntries(data) {
    var entries = Array.isArray(data.entries) ? data.entries : [];
    clear(els.list);
    if (!entries.length) {
      els.list.hidden = true;
      els.more.hidden = true;
      setStatus('empty', emptyText(data), false, !!data.truncated);
      return;
    }
    var frag = document.createDocumentFragment();
    entries.forEach(function (entry) {
      if (entry && typeof entry === 'object') frag.appendChild(renderEntry(entry));
    });
    els.list.appendChild(frag);
    els.list.hidden = false;
    els.list.scrollTop = 0;

    var note = '显示最新 ' + entries.length + ' 条' + (state.source === 'error' ? '警告和错误' : '日志');
    if (state.query) note += '（筛选：' + state.query + '）';
    if (data.truncated) note += '。为保证速度只读取了最近 ' + readLimitText(data) + '，更早的记录请打开日志文件夹查看';
    setStatus('', note);
    els.more.hidden = !(entries.length >= state.limit && state.limit < MAX_LIMIT);
  }

  // Bootstrap 的 :root { scroll-behavior: smooth } 会让定位变成滚动动画；这里一律瞬时定位
  function scrollToSection() {
    els.section.scrollIntoView({ block: 'start', behavior: 'instant' });
  }

  // ── 请求 ──

  function load() {
    if (!els.section) return;
    var seq = ++state.seq;
    state.loaded = true;
    state.interrupted = false;
    setBusy(true);
    setStatus('loading', '正在读取日志…');

    var params = new URLSearchParams();
    params.set('source', state.source);
    params.set('limit', String(state.limit));
    if (state.query) params.set('q', state.query);
    params.set('viewer', VIEWER_ID);
    params.set('seq', String(seq));

    window.apiFetch(ENDPOINT + '?' + params.toString(), { timeoutMs: 20000, abortKey: 'log-viewer' })
      .then(function (data) {
        if (seq !== state.seq) return;
        renderSummary(data || {});
        renderEntries(data || {});
      })
      .catch(function (err) {
        if (err && err.name === 'AbortError') {
          // 被新的请求取代时 seq 已变，静默即可；仍是当前请求说明是 pagehide 中止的，页面恢复后重读
          if (seq === state.seq) state.interrupted = true;
          return;
        }
        if (seq !== state.seq) return;
        setStatus('error', '读取日志失败：' + (err && err.message ? err.message : '未知错误'), true);
      })
      .then(function () {
        if (seq !== state.seq) return;
        setBusy(false);
        if (state.scrollAfterRender) {
          state.scrollAfterRender = false;
          scrollToSection();
        }
      });
  }

  function ensureLoaded() {
    if (!state.loaded) load();
  }

  function openFolder() {
    var btn = els.openFolderBtn;
    if (btn) btn.disabled = true;
    window.apiFetch(OPEN_FOLDER_ENDPOINT, { method: 'POST', timeoutMs: 15000 })
      .then(function (data) {
        showToast((data && data.message) || '已打开日志文件夹', 'success');
      })
      .catch(function (err) {
        if (err && err.name === 'AbortError') return; // pagehide 中止，静默
        showToast(err.message || '打开日志文件夹失败', err.status === 501 ? 'warning' : 'danger');
      })
      .then(function () {
        if (btn) btn.disabled = false;
      });
  }

  // ── 交互 ──

  function setSource(source) {
    if (source !== 'error' && source !== 'app') return;
    state.source = source;
    Array.prototype.forEach.call(els.sourceBtns, function (btn) {
      var active = btn.getAttribute('data-log-source') === source;
      btn.classList.toggle('btn-primary', active);
      btn.classList.toggle('btn-outline-primary', !active);
      btn.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
  }

  function applySearch(value) {
    clearTimeout(searchTimer);
    searchTimer = null;
    var query = String(value || '').trim();
    if (query === state.query && state.loaded) return;
    state.query = query;
    state.limit = DEFAULT_LIMIT;
    load();
  }

  function bindEvents() {
    Array.prototype.forEach.call(els.sourceBtns, function (btn) {
      btn.addEventListener('click', function () {
        var source = btn.getAttribute('data-log-source');
        if (source === state.source && state.loaded) return;
        setSource(source);
        state.limit = DEFAULT_LIMIT;
        load();
      });
    });

    if (els.search) {
      var scheduleSearch = function () {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(function () { applySearch(els.search.value); }, SEARCH_DELAY_MS);
      };
      els.search.addEventListener('compositionstart', function () {
        composing = true;
        clearTimeout(searchTimer);
        searchTimer = null;
      });
      // 选定汉字后才搜索；有的浏览器在 compositionend 之后不再补发 input，所以这里也要安排一次
      els.search.addEventListener('compositionend', function () {
        composing = false;
        scheduleSearch();
      });
      els.search.addEventListener('input', function (e) {
        if (composing || e.isComposing) return;
        scheduleSearch();
      });
      els.search.addEventListener('keydown', function (e) {
        // 组字中的回车是输入法在确认候选词（keyCode 229），不是提交搜索
        if (e.key === 'Enter' && !e.isComposing && !composing && e.keyCode !== 229) {
          e.preventDefault();
          applySearch(els.search.value);
        }
      });
    }

    if (els.refreshBtn) els.refreshBtn.addEventListener('click', load);
    if (els.openFolderBtn) els.openFolderBtn.addEventListener('click', openFolder);
    if (els.moreBtn) {
      els.moreBtn.addEventListener('click', function () {
        state.limit = MAX_LIMIT;
        load();
      });
    }

    // “只看此请求”：切到全部日志并按请求 ID 搜索，串起同一请求的完整经过
    if (els.list) {
      els.list.addEventListener('click', function (e) {
        var btn = e.target && e.target.closest ? e.target.closest('.log-entry-trace') : null;
        if (!btn) return;
        var rid = btn.dataset.requestId || '';
        if (!rid) return;
        if (els.search) els.search.value = rid;
        setSource('app');
        state.query = rid;
        state.limit = DEFAULT_LIMIT;
        state.scrollAfterRender = true;
        load();
        if (els.search) els.search.focus({ preventScroll: true });
        scrollToSection();
      });
    }
  }

  // 从 /settings#logs 进入（在线阅读等页面的“查看日志”链接）时直接定位并加载
  function openFromHash() {
    if (window.location.hash !== '#logs') return false;
    scrollToSection();
    if (!state.loaded) {
      state.scrollAfterRender = true;
      load();
    }
    return true;
  }

  function observeSection() {
    if (!window.IntersectionObserver) {
      ensureLoaded();
      return;
    }
    var observer = new IntersectionObserver(function (records) {
      for (var i = 0; i < records.length; i++) {
        if (records[i].isIntersecting) {
          observer.disconnect();
          ensureLoaded();
          return;
        }
      }
    }, { rootMargin: '0px 0px 200px 0px' });
    observer.observe(els.section);
  }

  function init() {
    cacheElements();
    if (!els.section || !els.list || !els.status) return;
    bindEvents();
    openFromHash();
    observeSection();
    window.addEventListener('hashchange', openFromHash);
    // 从 bfcache 恢复：离开页面时被 utils.js 中止的读取不会自己重试，这里重新读取
    window.addEventListener('pageshow', function (event) {
      if (event.persisted && state.interrupted) load();
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
