/**
 * 下载管理页 — 分状态分页 + 操作按钮 + 轮询
 *
 * 依赖: apiFetch, escapeHtml, escapeHtmlAttr, confirmAction, encodeJobId, openFolder (utils.js)
 *        connectSSE, disconnectAllSSE, setSSECallbacks, _jobTitleMap (sse-client.js)
 *         showToast (base.html)
 *
 * 不使用内联 onclick：按钮只带 data-action + data-job-id / data-status，
 * 由 #downloadTabsContent 上的一个委托监听器分派（拼进内联 JS 的数据会被 HTML 解码后执行）。
 * 已完成任务按与其他页面相同的规则标记：本地可读 →“已下载内容 · 可离线阅读”+“预览”（/preview，单页翻页；
 * 本地可读不代表整部漫画都已下载，所以不说“完整”），
 * 只剩压缩包也能读时再跟一个 CBZ / ZIP 标记；判断过但读不到 → 原因（文件已删除 / 压缩包损坏 /
 * 压缩包无可阅读图片，utils.js localBadges），不给“预览”（它只读本地文件）。
 * 已完成和失败的任务都有“阅读”（utils.js readLink）：已下载打开本地文件，否则在线阅读。
 * 检查新章节已确认有新章节的漫画（/api/updates/states），只在它最新的已完成任务卡片上放“有新章节 · N 话”
 * （或“章节有变动”）链接，点开详情选择下载；这里从不下载。
 */
(function () {
  'use strict';

  // 仅在下载管理页执行
  if (!document.getElementById('downloadTabs')) return;

  /** @type {number|null} */
  var pollTimer = null;
  var activeGroup = 'all';
  var groupPages = new Map();
  var labels = { all: '同册传输卷宗', running: '进行中', paused: '已暂停', queued: '排队', completed: '已完成', failed: '失败', canceled: '已取消' };
  var current = null;
  var refreshSerial = 0;
  var pageSize = 50;

  // ── 操作函数 ──

  function cancelJob(jobId) {
    if (!confirmAction('确定取消此任务？\n排队中任务立即取消，下载中任务会尽快停止。')) return;
    apiFetch('/api/jobs/' + encodeJobId(jobId) + '/cancel', { method: 'POST' })
      .then(function () { refreshJobs(); })
      .catch(function (err) {
        if (err && err.name === 'AbortError') return; // pagehide 中止，静默
        showToast('取消失败: ' + err.message, 'danger');
      });
  }

  function pauseJob(jobId) {
    apiFetch('/api/jobs/' + encodeJobId(jobId) + '/pause', { method: 'POST' })
      .then(function () {
        showToast('⏸ 已暂停', 'info');
        refreshJobs();
      })
      .catch(function (err) {
        if (err && err.name === 'AbortError') return; // pagehide 中止，静默
        showToast('暂停失败: ' + err.message, 'danger');
      });
  }

  function resumeJob(jobId) {
    apiFetch('/api/jobs/' + encodeJobId(jobId) + '/resume', { method: 'POST' })
      .then(function () {
        showToast('▶ 已恢复', 'success');
        refreshJobs();
      })
      .catch(function (err) {
        if (err && err.name === 'AbortError') return; // pagehide 中止，静默
        showToast('恢复失败: ' + err.message, 'danger');
      });
  }

  function retryJob(jobId) {
    apiFetch('/api/jobs/' + encodeJobId(jobId) + '/retry', { method: 'POST' })
      .then(function (data) {
        var result = window.jobCreationFeedback(data);
        showToast(result.message, result.type);
        refreshJobs();
      })
      .catch(function (err) {
        if (err && err.name === 'AbortError') return; // pagehide 中止，静默
        showToast('重试失败: ' + err.message, 'danger');
      });
  }

  function retryMaintenance(button) {
    button.disabled = true;
    apiFetch('/api/albums/' + encodeURIComponent(button.getAttribute('data-album-id')) + '/maintenance/retry', { method: 'POST' })
      .then(function (data) { showToast(data.message, 'info'); refreshJobs(); })
      .catch(function (err) {
        if (err && err.name === 'AbortError') return;
        showToast('重新整理失败: ' + err.message, 'danger');
        refreshJobs();
      })
      .finally(function () { button.disabled = false; });
  }

  function maintenanceButton(albumId) {
    return '<button type="button" class="btn btn-sm btn-outline-warning" data-action="retry-maintenance" data-album-id="'
      + escapeHtmlAttr(albumId) + '"><i class="bi bi-arrow-counterclockwise" aria-hidden="true"></i> 重新整理整册</button>';
  }

  function renderMaintenanceFailures(failures) {
    var box = document.getElementById('maintenance-failures');
    if (!box) return;
    box.hidden = !failures.length;
    if (failures.length) replaceMarkup(box, '<strong>整册整理失败</strong>' + failures.map(function (item) {
      return '<div class="d-flex flex-wrap align-items-center gap-2 mt-2"><span>车号 '
        + escapeHtml(item.album_id) + '：' + escapeHtml(item.error) + '</span>' + maintenanceButton(item.album_id) + '</div>';
    }).join(''));
  }

  function deleteJob(jobId) {
    if (!confirmAction('确定删除此记录？不会删除已下载的文件。')) return;
    apiFetch('/api/jobs/' + encodeJobId(jobId), { method: 'DELETE' })
      .then(function () { refreshJobs(); })
      .catch(function (err) {
        if (err && err.name === 'AbortError') return; // pagehide 中止，静默
        showToast('删除失败: ' + err.message, 'danger');
      });
  }

  // openFolder 由 utils.js 提供（window.openFolder，首页也在用）

  // ── 导出 ZIP / PDF ──

  function exportZip(jobId) {
    window.downloadExport('/api/export/' + encodeJobId(jobId) + '/zip', 'ZIP');
  }

  function exportPdf(jobId) {
    window.downloadExport('/api/export/' + encodeJobId(jobId) + '/pdf', 'PDF');
  }

  // ── 批量清理 ──

  function clearJobs(status) {
    var labels = { completed: '已完成', failed: '失败', canceled: '已取消' };
    if (!labels.hasOwnProperty(status)) return;
    var label = labels[status];
    var msg = '确定清空所有「' + label + '」任务记录？\n\n此操作只删除数据库记录，不会删除已下载的文件。';
    if (!confirmAction(msg)) return;

    apiFetch('/api/jobs/clear/' + encodeJobId(status), { method: 'POST' })
      .then(function (data) {
        showToast('已删除 ' + data.deleted + ' 条记录', 'success');
        refreshJobs();
      })
      .catch(function (err) {
        if (err && err.name === 'AbortError') return; // pagehide 中止，静默
        showToast('操作失败: ' + err.message, 'danger');
      });
  }

  function clearFinished() {
    var msg = '确定清空所有「已完成 / 失败 / 已取消」任务记录？\n\n此操作只删除数据库记录，不会删除已下载的文件。\n排队中和进行中的任务不受影响。';
    if (!confirmAction(msg)) return;

    apiFetch('/api/jobs/clear/finished', { method: 'POST' })
      .then(function (data) {
        showToast('已删除 ' + data.deleted + ' 条记录', 'success');
        refreshJobs();
      })
      .catch(function (err) {
        if (err && err.name === 'AbortError') return; // pagehide 中止，静默
        showToast('操作失败: ' + err.message, 'danger');
      });
  }

  /**
   * 批量重试当前失败页（串行，用户确认的范围不跨页扩展）
   */
  async function batchRetryFailed(button) {
    var msg = '确定重试本页显示的「失败」任务？\n\n其他页不会重试。已有相同任务时不会重复创建，原失败记录保留。';
    if (!confirmAction(msg)) return;

    var container = document.getElementById('folio-list');
    if (!container) return;

    var cards = container.querySelectorAll('.job-card[data-status="failed"]');
    var jobIds = [];
    cards.forEach(function (el) {
      var id = el.getAttribute('data-job-id');
      if (id) jobIds.push(id);
    });

    if (jobIds.length === 0) {
      showToast('没有需要重试的失败任务', 'warning');
      return;
    }

    var counts = { created: 0, reused: 0, failed: 0, unknown: 0 };
    button.disabled = true;
    try {
      for (var jobId of jobIds) {
        try {
          var data = await apiFetch('/api/jobs/' + encodeJobId(jobId) + '/retry', { method: 'POST' });
          counts[window.jobCreationFeedback(data).outcome]++;
        } catch (err) {
          if (err && err.name === 'AbortError') return;
          if (err && err.status) counts.failed++; else counts.unknown++;
        }
      }
      var message = '本页重试：新建 ' + counts.created + '，复用 ' + counts.reused + '，失败 ' + counts.failed;
      if (counts.unknown) message += '，结果未确认 ' + counts.unknown + '（请核对任务列表）';
      var type = counts.created ? 'success' : 'info';
      if (counts.failed || counts.unknown) type = counts.failed === jobIds.length ? 'danger' : 'warning';
      showToast(message, type);
      refreshJobs();
    } finally {
      button.disabled = false;
    }
  }

  // ── 按钮事件：一个委托监听器（静态工具栏 + 动态任务卡片） ──

  var ACTIONS = {
    pause: function (el) { pauseJob(el.getAttribute('data-job-id')); },
    resume: function (el) { resumeJob(el.getAttribute('data-job-id')); },
    cancel: function (el) { cancelJob(el.getAttribute('data-job-id')); },
    retry: function (el) { retryJob(el.getAttribute('data-job-id')); },
    'retry-maintenance': retryMaintenance,
    'delete': function (el) { deleteJob(el.getAttribute('data-job-id')); },
    'open-folder': function (el) { window.openFolder(el.getAttribute('data-job-id')); },
    'export-zip': function (el) { exportZip(el.getAttribute('data-job-id')); },
    'export-pdf': function (el) { exportPdf(el.getAttribute('data-job-id')); },
    'batch-retry': batchRetryFailed,
    'jobs-page': function (el) {
      var page = Number(el.getAttribute('data-page'));
      if (!Number.isSafeInteger(page) || page < 1) return;
      groupPages.set(activeGroup, page);
      refreshJobs();
    },
    'read-chapter': async function (button) {
      var album = button.getAttribute('data-album-id');
      var photo = button.getAttribute('data-photo-id');
      button.disabled = true;
      try {
        var index = await apiFetch('/api/preview/' + encodeURIComponent(album), { timeoutMs: 15000 });
        var first = (index.pages || []).find(function (page) { return String(page.photo_id) === photo; });
        if (first) window.location.assign('/read/' + encodeURIComponent(album) + '?page=' + Number(first.page));
        else if (confirmAction('本地卷目未标记这一话的位置。要打开本册阅读吗？')) window.location.assign('/read/' + encodeURIComponent(album));
      } catch (err) {
        if (err.name !== 'AbortError') showToast(err.message || '读取本地卷目失败', 'danger');
      } finally { button.disabled = false; }
    },
    clear: function (el) { clearJobs(el.getAttribute('data-status')); },
    'clear-finished': function () { clearFinished(); }
  };

  var tabsContent = document.getElementById('downloadTabsContent');
  if (tabsContent) {
    tabsContent.addEventListener('click', function (event) {
      var el = event.target.closest('button[data-action]');
      if (!el || !tabsContent.contains(el) || el.disabled) return;
      var action = el.getAttribute('data-action');
      if (Object.prototype.hasOwnProperty.call(ACTIONS, action)) ACTIONS[action](el);
    });
  }

  /** 任务操作按钮：job_id 只放进 data-* 属性（escapeHtmlAttr 对属性值安全）；title 为固定文案 */
  function jobButton(action, job, className, title, inner, iconOnly) {
    return '<button type="button" class="btn btn-sm ' + className + '" data-action="' + action + '"'
      + ' data-job-id="' + escapeHtmlAttr(job.job_id) + '" title="' + title + '"'
      + (iconOnly ? ' aria-label="' + title + '"' : '') + '>' + inner + '</button>';
  }

  // ── 本地可读（已下载）判断：与搜索/详情/收藏/资源库同一规则（/api/preview/available） ──

  var readableAlbums = {};   // album_id → true（可读）/ false（判断过，不可读）；没有键 = 还没判断
  var localInfo = {};        // album_id → {state, archive}（/api/preview/available 的 local）：压缩包标记与不可读的原因
  var readableKey = null;    // 上次判断时的已完成 + 失败 album_id 集合
  var readableCheckedAt = 0;
  var readableSerial = 0;
  var lastCompleted = [];
  var lastFailed = [];
  var newestCompleted = {};  // album_id → 这部漫画最新的已完成任务的 job_id（新章节标记只放在这张卡片上）
  var READABLE_RECHECK_MS = 60000; // 文件可能在页面打开期间被移走：至少每分钟复查一次

  // ── 检查新章节：可读的漫画里已确认有新章节的（/api/updates/states，只读数据库） ──
  var updateInfo = {};       // album_id → {state, new_count, removed_count, confirmed_at, titles}；没有键 = 没有已确认的新章节
  var updatesKey = null;     // 上次查询时的可读 album_id 集合
  var lastNewestKey = null;  // 上次渲染时各漫画最新的已完成任务：变了（有任务新完成）就立即重新判断
  var updatesCheckedAt = 0;
  var updatesSerial = 0;

  function refreshUpdates(readableIds) {
    var ids = readableIds.slice().sort();
    var key = ids.join(',');
    if (key === updatesKey && Date.now() - updatesCheckedAt < READABLE_RECHECK_MS) return;
    updatesKey = key;
    updatesCheckedAt = Date.now();
    var serial = ++updatesSerial;
    if (ids.length === 0) { updateInfo = {}; return; }

    var chunks = [];
    for (var i = 0; i < ids.length; i += 200) chunks.push(ids.slice(i, i + 200)); // 接口单次最多 200 个
    Promise.all(chunks.map(function (chunk, n) {
      return apiFetch('/api/updates/states', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ album_ids: chunk }),
        timeoutMs: 15000,
        abortKey: 'downloads-updates-' + n
      });
    }))
      .then(function (results) {
        if (serial !== updatesSerial) return;
        var next = {};
        results.forEach(function (data) {
          if (data.status !== 'ok') throw new Error(data.message || '读取失败');
          var updates = data.updates || {};
          Object.keys(updates).forEach(function (id) { next[String(id)] = updates[id]; });
        });
        updateInfo = next;
        renderFolios();
      })
      .catch(function () {
        // 失败时保留上次结果，下次轮询重试（静默）
        if (serial === updatesSerial) updatesKey = null;
      });
  }

  /** 每部漫画最新的已完成任务（/api/jobs 按创建时间倒序：第一次出现的就是最新的） */
  function newestCompletedJobs(jobs) {
    var newest = {};
    jobs.forEach(function (job) {
      var key = String(job.album_id);
      if (job.latest_completed_job_id && job.latest_completed_job_id !== job.job_id) return;
      if (!Object.prototype.hasOwnProperty.call(newest, key)) newest[key] = job.job_id;
    });
    return newest;
  }

  function refreshReadable(finishedJobs) {
    var ids = [];
    finishedJobs.forEach(function (job) {
      var id = String(job.album_id || '');
      if (/^[0-9]{1,20}$/.test(id) && ids.indexOf(id) < 0) ids.push(id);
    });
    var key = ids.slice().sort().join(',');
    if (key === readableKey && Date.now() - readableCheckedAt < READABLE_RECHECK_MS) return;
    readableKey = key;
    readableCheckedAt = Date.now();
    var serial = ++readableSerial;
    if (ids.length === 0) { readableAlbums = {}; localInfo = {}; refreshUpdates([]); return; }

    var chunks = [];
    for (var i = 0; i < ids.length; i += 200) chunks.push(ids.slice(i, i + 200)); // 接口单次最多 200 个
    Promise.all(chunks.map(function (chunk, n) {
      return apiFetch('/api/preview/available', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ album_ids: chunk }),
        timeoutMs: 15000,
        abortKey: 'downloads-readable-' + n
      });
    }))
      .then(function (results) {
        if (serial !== readableSerial) return;
        var next = {};
        var info = {};
        ids.forEach(function (id) { next[id] = false; });
        results.forEach(function (data) {
          if (data.status !== 'ok') throw new Error(data.message || '判断失败');
          (data.readable || []).forEach(function (id) { next[String(id)] = true; });
          var local = data.local || {};
          Object.keys(local).forEach(function (id) { info[String(id)] = local[id]; });
        });
        readableAlbums = next;
        localInfo = info;
        renderFolios();
        // 可读的漫画里有没有已确认的新章节（只读数据库，不联网）
        refreshUpdates(ids.filter(function (id) { return next[id] === true; }));
      })
      .catch(function () {
        // 失败时保留上次结果，下次轮询重试（中止/超时/网络错误都静默，不打扰用户）
        if (serial === readableSerial) readableKey = null;
      });
  }

  // ── 刷新任务列表 ──

  function refreshJobs() {
    var group = activeGroup;
    var serial = ++refreshSerial;
    var album = group.startsWith('album-') ? group.slice(6) : '';
    var query = new URLSearchParams({ page: String(groupPages.get(group) || 1), page_size: group === 'all' ? '8' : String(pageSize),
      status: album ? 'all' : group, view: album ? 'album' : group === 'all' ? 'folios' : 'jobs' });
    if (album) query.set('album_id', album);
    // abortKey：轮询/操作触发的连续刷新自动中止上一次未完成的请求，避免堆积
    apiFetch('/api/jobs?' + query.toString(), { abortKey: 'downloads-refresh' })
      .then(function (data) {
        if (serial !== refreshSerial || group !== activeGroup) return;
        if (data.status !== 'ok' || !Array.isArray(data.jobs)) throw new Error(data.message || '任务列表响应不完整');
        var jobs = data.jobs;
        current = data;
        document.dispatchEvent(new CustomEvent('atelier-jobs-summary', { detail: data }));
        document.getElementById('downloadTabs').querySelectorAll('[data-filter-count]').forEach(function (node) {
          var count = (data.counts || {})[node.getAttribute('data-filter-count')];
          node.textContent = Number.isSafeInteger(count) ? ' ' + count : '';
        });
        var shownAlbums = new Set(jobs.filter(function (job) { return job.maintenance && job.maintenance.state === 'failed'; }).map(function (job) { return job.album_id; }));
        renderMaintenanceFailures((data.maintenance_failures || []).filter(function (item) { return !shownAlbums.has(item.album_id); }));
        if (Number.isInteger(data.page)) groupPages.set(group, data.page);
        var errorBox = document.getElementById('transfer-error');
        if (errorBox) errorBox.hidden = true;
        renderJobs(jobs);
        if (Number.isInteger(data.pages)) renderJobPagination(group, data);
        reconnectSSE(jobs);
      })
      .catch(function (err) {
        if (serial !== refreshSerial || (err && err.name === 'AbortError')) return;
        var errorBox = document.getElementById('transfer-error');
        if (errorBox) { errorBox.hidden = false; errorBox.textContent = '加载传输记录失败：' + err.message + '。稍后将自动重试。'; }
      });
  }

  // ── SSE 重连（下载管理页专用） ──

  function reconnectSSE(jobs) {
    var ids = jobs.filter(function (job) { return job.status === 'running'; })
      .slice(0, 5).map(function (job) { return job.job_id; });
    if (window.syncSSE) window.syncSSE(ids);
    else {
      window.disconnectAllSSE();
      ids.forEach(function (id) { window.connectSSE(id); });
    }
  }

  // ── 渲染函数 ──

  function renderJobs(jobs) {
    window._jobTitleMap = {};
    // 按状态分组，同时缓存 job_id → title 映射供 SSE toast 使用
    jobs.forEach(function (job) {
      window._jobTitleMap[job.job_id] = job.title || job.album_id || job.job_id;
    });
    lastCompleted = jobs.filter(function (job) { return job.status === 'completed'; });
    lastFailed = jobs.filter(function (job) { return job.status === 'failed'; });
    newestCompleted = newestCompletedJobs(lastCompleted);
    // 有任务新完成：立即重新判断可读与新章节（刚下载的新章节已被服务端并入，不能再挂着“有新章节”）
    var newestKey = JSON.stringify(newestCompleted);
    if (newestKey !== lastNewestKey) {
      lastNewestKey = newestKey;
      readableKey = null;
      updatesKey = null;
    }
    renderFolios();
    refreshReadable(lastCompleted.concat(lastFailed));
  }

  function renderJobPagination(group, data) {
    var box = document.getElementById('folio-pagination');
    if (!box) return;
    var key = [group, data.page, data.pages, data.total].join('/');
    if (box.getAttribute('data-page-key') === key) return;
    box.setAttribute('data-page-key', key);
    box.textContent = '';
    function addPage(page, label, current) {
      var button = document.createElement('button');
      button.type = 'button';
      button.className = 'btn btn-sm ' + (current ? 'btn-primary' : 'btn-outline-secondary');
      button.textContent = label;
      button.setAttribute('data-action', 'jobs-page');
      button.setAttribute('data-page', String(page));
      if (current) button.setAttribute('aria-current', 'page');
      box.appendChild(button);
    }
    if (data.page > 1) addPage(data.page - 1, '◀ 上一页', false);
    var end = Math.min(data.pages, Math.max(1, data.page - 2) + 4);
    for (var p = Math.max(1, end - 4); p <= end; p++) addPage(p, String(p), p === data.page);
    if (data.page < data.pages) addPage(data.page + 1, '下一页 ▶', false);
    var info = document.createElement('span');
    info.className = 'page-info ms-2';
    info.textContent = '共 ' + data.total + (group === 'all' ? ' 册' : ' 条') + '，第 ' + data.page + '/' + data.pages + ' 页';
    box.appendChild(info);
  }

  /** readableAlbums 里的判断结果：true / false；本地只剩打不开的压缩包 → 'archive_problem'（“阅读”点开说明原因）；
   *  还没判断 → undefined */
  function readableState(albumId) {
    var key = String(albumId);
    if (!Object.prototype.hasOwnProperty.call(readableAlbums, key)) return undefined;
    return window.readLink.stateFor(readableAlbums[key], (localInfo[key] || {}).state);
  }

  function replaceMarkup(box, markup) {
    if (box._folioMarkup === markup) return;
    var focus = document.activeElement;
    var controls = 'a,button';
    var old = focus && box.contains(focus) && focus.closest('.job-card');
    var index = old ? Array.from(old.querySelectorAll(controls)).indexOf(focus) : -1;
    box.innerHTML = markup;
    box._folioMarkup = markup;
    if (index >= 0) {
      var row = Array.from(box.querySelectorAll('.job-card')).find(function (item) { return item.dataset.jobId === old.dataset.jobId; });
      var replacement = row && row.querySelectorAll(controls)[index];
      if (replacement && replacement.getAttribute('data-action') === focus.getAttribute('data-action')) replacement.focus({ preventScroll: true });
    }
  }

  function renderFolios() {
    var list = document.getElementById('folio-list');
    if (!current || !list) return;
    var groups = new Map();
    current.jobs.forEach(function (job) {
      if (!groups.has(job.album_id)) groups.set(job.album_id, []);
      groups.get(job.album_id).push(job);
    });
    var html = '';
    groups.forEach(function (items, id) {
      var book = (current.books || {})[id] || {};
      var title = book.title || (items.find(function (job) { return job.title && job.title !== id; }) || {}).title || '书目 ' + id;
      var path = encodeURIComponent(id);
      var total = (current.album_totals || {})[id];
      var maintenance = (items.find(function (job) { return job.maintenance; }) || {}).maintenance;
      var ribbon = maintenance && { pending: maintenance.error ? '等待重试整册整理' : '等待整册整理', running: '正在整理整册', failed: '整册整理失败', completed: '整册整理完成' }[maintenance.state];
      html += '<article class="folio-card" data-album-id="' + escapeHtmlAttr(id) + '"><header class="folio-book-head">'
        + '<a class="bound-book folio-cover" href="/album/' + path + '" aria-label="查看书目：' + escapeHtmlAttr(title) + '"><span aria-hidden="true">藏</span><img alt="" src="/api/cover/' + path + '/portrait" loading="lazy" decoding="async"></a>'
        + '<div class="folio-book-caption"><p class="folio-kicker">馆藏卷宗 <b>JM ' + escapeHtml(id) + '</b></p>'
        + '<h3><a href="/album/' + path + '">' + escapeHtml(title) + '</a></h3>'
        + (book.author ? '<p class="folio-author">' + escapeHtml(book.author) + '</p>' : '') + '</div>'
        + (activeGroup.startsWith('album-') ? '' : '<a class="folio-history" href="#album-' + path + '">本册记录' + (Number.isInteger(total) ? ' · ' + total : '') + ' →</a>')
        + '</header>';
      if (ribbon) html += '<div class="folio-ribbon" data-state="' + escapeHtmlAttr(maintenance.state) + '"><strong>合卷</strong> ' + ribbon
        + (maintenance.error ? '：' + escapeHtml(maintenance.error) : '') + (maintenance.state === 'failed' ? maintenanceButton(id) : '') + '</div>';
      html += items.map(function (job) {
        if (job.status === 'completed') return renderCompletedCard(job);
        if (job.status === 'failed' || job.status === 'canceled') return renderFailedCard(job);
        return renderRunningCard(job);
      }).join('') + '</article>';
    });
    replaceMarkup(list, html || '<div class="folio-empty"><span class="folio-kicker">传输卷宗</span><p>这里还没有' + (activeGroup === 'all' || activeGroup.startsWith('album-') ? '任务记录' : labels[activeGroup] + '的任务') + '。</p><a href="' + (activeGroup === 'all' ? '/search' : '#all') + '">' + (activeGroup === 'all' ? '去查找书目' : '返回同册全景') + '</a></div>');
  }

  function chapterTitle(job) {
    var ids = job.selected_photo_ids || [];
    var book = (current.books || {})[job.album_id] || {};
    var chapter = ids.length === 1 && (book.chapters || {})[ids[0]];
    var scope = ids.length === 1 ? '章节 ' + ids[0] : ids.length ? '选定 ' + ids.length + ' 话' : '整册任务';
    if (chapter) {
      var name = chapter.title && chapter.title !== '-' ? String(chapter.title) : '';
      scope = /^第\s*[\d一二三四五六七八九十百]+\s*[话話章回]/.test(name) ? name : '第 ' + chapter.index + ' 话' + (name ? ' · ' + name : '');
    }
    var created = activeGroup !== 'all' && job.created_at ? ' · ' + String(job.created_at).replace('T', ' ').slice(0, 16) : '';
    return '<h4 class="folio-chapter">' + escapeHtml(scope) + '</h4><small class="folio-job-id">' + escapeHtml(job.job_id + created) + '</small>';
  }

  function renderRunningCard(job) {
    var isPaused = job.status === 'paused';
    var isQueued = job.status === 'queued';
    var total = Number(job.total_pages) || 0;
    var done = Number(job.done_pages) || 0;
    var pct = total > 0 ? Math.min(100, Math.round(done / total * 100)) : null;
    var jobIdAttr = escapeHtmlAttr(job.job_id);

    var actionButtons = '';
    if (isPaused) {
      actionButtons = ''
        + jobButton('resume', job, 'btn-primary', '恢复下载', '<i class="bi bi-play-fill"></i> 继续传输')
        + jobButton('cancel', job, 'btn-outline-danger', '取消', '<i class="bi bi-x-circle"></i> 取消');
    } else if (isQueued) {
      actionButtons = jobButton('cancel', job, 'btn-outline-danger', '取消', '取消');
    } else {
      actionButtons = ''
        + jobButton('pause', job, 'btn-outline-warning me-1', '暂停下载', '<i class="bi bi-pause-fill"></i> 暂停')
        + jobButton('cancel', job, 'btn-outline-danger', '取消', '<i class="bi bi-x-circle"></i> 取消');
    }

    return '<div class="card job-card chapter-item" data-job-id="' + jobIdAttr + '" data-status="' + escapeHtmlAttr(job.status) + '"><div class="folio-task-info">'
      + chapterTitle(job) + '<div class="folio-state">' + labels[job.status] + ' · <span id="info-' + jobIdAttr + '">' + (total ? done + ' / ' + total + ' 页' : '总页数待确认') + '</span></div>'
      + (pct === null ? '' : '<div class="folio-meter"><div id="progress-' + jobIdAttr + '" class="progress-bar" role="progressbar" aria-label="任务进度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="' + pct + '" style="width:' + pct + '%"></div></div>')
      + '</div><div class="job-card-actions">' + actionButtons + '</div></div>';
  }

  function renderCompletedCard(job) {
    var albumPath = encodeURIComponent(job.album_id);
    // 本地可读：与其他页面同一规则、同一标记。判断结果回来之前（known=false）两种标记都不显示
    var albumKey = String(job.album_id);
    var known = Object.prototype.hasOwnProperty.call(readableAlbums, albumKey);
    var readable = known && readableAlbums[albumKey] === true;
    var local = localInfo[albumKey] || {};
    var fromArchive = readable && local.state === 'archive'; // 没有散图，直接从压缩包读
    // 压缩包标记：只剩压缩包时跟在“可离线阅读”后面；确认散图可读、又打包过时放在最前（“已打包”，任务的 archive_format）。
    // 压缩包损坏 / 没有图片 / 文件不在、或还没判断时不放，只由原因徽章说明
    var packFormat = job.archive_format || (job.has_cbz ? 'cbz' : '');
    var cbzBadge = readable && local.state === 'loose' && packFormat
      ? window.localBadges.archiveHtml(packFormat, '已打包为 ' + packFormat.toUpperCase() + '；本地还有散图，阅读时优先用散图')
      : '';
    var marker = readable
      ? '<span class="offline-badge" title="本地有已下载的内容，可以离线阅读；不一定是整部漫画"><i class="bi bi-check-circle-fill" aria-hidden="true"></i>已下载内容 · 可离线阅读</span>'
        + (fromArchive ? window.localBadges.archiveHtml(local.archive) : '')
      // 不可读的原因：文件已删除 / 压缩包损坏 / 压缩包无可阅读图片
      : (known ? window.localBadges.problemHtml({ archive_corrupt: 'archive_corrupt', archive_empty: 'archive_empty' }[local.state] || 'deleted') : '');
    // 检查新章节已确认的新章节 / 章节有变动：只放在这部漫画最新的已完成任务上（点开详情选择下载，这里不下载）
    var updateChip = readable && newestCompleted[albumKey] === job.job_id && window.updateBadges
      ? window.updateBadges.chipHtml(updateInfo[albumKey], { link: true, albumId: job.album_id })
      : '';
    // 阅读（连续滚动）每张卡片都有：已下载打开本地文件，否则在线阅读；
    // 预览（/preview，单页翻页）只读本地文件，只在本地可读时出现
    var readBtn = window.readLink.html(job.album_id, readableState(job.album_id), 'btn-sm');
    // 标题/状态/路径在左，按钮组在右；状态徽章跟着标题，不随按钮多少左右移动。
    // 放不下时按钮组整体换行（长路径不再把按钮文字挤成竖排）
    if (readable && job.selected_photo_ids && job.selected_photo_ids.length === 1) {
      readBtn = '<button type="button" class="btn btn-sm btn-primary" data-action="read-chapter" data-album-id="' + escapeHtmlAttr(job.album_id)
        + '" data-photo-id="' + escapeHtmlAttr(job.selected_photo_ids[0]) + '"><i class="bi bi-book"></i> 展阅本话</button>';
    }
    return '<div class="card job-card chapter-item" data-job-id="' + escapeHtmlAttr(job.job_id) + '" data-status="completed">'
      + '<div class="folio-task-info">' + chapterTitle(job)
      + '<div class="job-card-status">' + cbzBadge + '<span class="badge bg-success">已完成</span>' + marker + updateChip + '</div>'
      + (job.total_pages > 0 ? '<small class="folio-pages">任务页数 · ' + Number(job.total_pages) + ' P</small>' : '') + '</div>'
      + '<div class="job-card-actions">'
      + readBtn
      + (readable && job.output_path ? '<a class="btn btn-sm btn-outline-info" href="/preview/' + albumPath + '" title="单页翻页预览本地文件">预览</a>' : '')
      + (job.output_path ? jobButton('export-zip', job, 'btn-outline-secondary', '导出本任务 ZIP', '导出 ZIP') + jobButton('export-pdf', job, 'btn-outline-secondary', '导出本任务 PDF', 'PDF')
        + jobButton('open-folder', job, 'btn-outline-secondary', '打开文件夹', '<i class="bi bi-folder2-open"></i>', true) : '')
      + jobButton('retry', job, 'btn-outline-primary', '重新下载', '<i class="bi bi-arrow-counterclockwise"></i>', true)
      + jobButton('delete', job, 'btn-outline-danger', '删除记录', '<i class="bi bi-trash"></i>', true)
      + '</div></div>';
  }

  function renderFailedCard(job) {
    var canceled = job.status === 'canceled';
    return '<div class="card job-card chapter-item" data-job-id="' + escapeHtmlAttr(job.job_id) + '" data-status="' + (canceled ? 'canceled' : 'failed') + '">'
      + '<div class="folio-task-info">' + chapterTitle(job) + '<div class="folio-state">' + (canceled ? '已取消' : '失败')
      + (job.error_message ? ' · ' + escapeHtml(job.error_message) : '') + '</div></div>'
      + '<div class="job-card-actions">'
      + window.readLink.html(job.album_id, readableState(job.album_id), 'btn-sm')
      + jobButton('retry', job, 'btn-outline-warning', '重试', '<i class="bi bi-arrow-counterclockwise"></i> 重试')
      + jobButton('delete', job, 'btn-outline-danger', '删除记录', '<i class="bi bi-trash"></i>', true)
      + '</div></div>';
  }

  // ── SSE 事件回调（连接建立后刷新列表） ──
  window.setSSECallbacks({
    onCompleted: function () { refreshJobs(); },
    onFailed: function () { refreshJobs(); },
    onCanceled: function () { refreshJobs(); },
  });

  // ── 轮询：离开页面（含进入 bfcache）时停止，返回时立即刷新并恢复 ──
  function startPolling() {
    if (!pollTimer) pollTimer = setInterval(refreshJobs, 5000);
  }
  function stopPolling() {
    refreshSerial++;
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = null;
  }

  // ── 初始化 ──
  function selectView() {
    var group = (window.location && window.location.hash || '#all').slice(1);
    activeGroup = Object.hasOwn(labels, group) || /^album-[0-9]{1,20}$/.test(group) ? group : 'all';
    current = null;
    renderMaintenanceFailures([]);
    var list = document.getElementById('folio-list');
    if (list) replaceMarkup(list, '<p class="folio-empty" role="status">正在读取传输记录…</p>');
    var pager = document.getElementById('folio-pagination');
    if (pager) { pager.textContent = ''; pager.removeAttribute('data-page-key'); }
    document.getElementById('downloadTabs').querySelectorAll('[data-filter]').forEach(function (link) {
      if (link.getAttribute('data-filter') === activeGroup) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    });
    var caption = document.getElementById('folio-caption');
    if (caption) caption.textContent = labels[activeGroup] || '本册任务记录 · JM ' + activeGroup.slice(6);
    var back = document.getElementById('folio-back');
    if (back) back.hidden = !activeGroup.startsWith('album-');
    var batch = document.getElementById('batch-retry');
    if (batch) batch.hidden = activeGroup !== 'failed';
    window.disconnectAllSSE();
    refreshJobs();
  }
  document.addEventListener('DOMContentLoaded', function () {
    var list = document.getElementById('folio-list');
    if (list) list.addEventListener('error', function (event) { if (event.target.tagName === 'IMG') event.target.hidden = true; }, true);
    selectView();
    startPolling();
  });
  window.addEventListener('hashchange', selectView);

  window.addEventListener('pagehide', stopPolling);
  window.addEventListener('pageshow', function (event) {
    if (!event.persisted) return;
    readableKey = null; // 回到本页：重新判断哪些可以离线阅读、哪些有已确认的新章节
    updatesKey = null;
    refreshJobs();
    startPolling();
  });
  // 切回本标签页时重新判断（文件可能在别处被移动/删除）
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'hidden') {
      stopPolling();
      window.disconnectAllSSE();
      return;
    }
    if (document.visibilityState !== 'visible') return;
    readableKey = null;
    updatesKey = null;   // 可能刚在详情页点过“立即检查”
    refreshJobs();
    startPolling();
  });
})();
