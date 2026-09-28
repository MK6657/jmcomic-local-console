/**
 * 下载管理页 — 全量渲染 + 操作按钮 + 轮询
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
 */
(function () {
  'use strict';

  // 仅在下载管理页执行
  if (!document.getElementById('downloadTabs')) return;

  /** @type {number|null} */
  var pollTimer = null;

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
        showToast('已创建新任务 (新ID: ' + data.job_id + ')', 'success');
        refreshJobs();
      })
      .catch(function (err) {
        if (err && err.name === 'AbortError') return; // pagehide 中止，静默
        showToast('重试失败: ' + err.message, 'danger');
      });
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
    downloadExport('/api/export/' + encodeJobId(jobId) + '/zip', 'ZIP');
  }

  function exportPdf(jobId) {
    downloadExport('/api/export/' + encodeJobId(jobId) + '/pdf', 'PDF');
  }

  /**
   * 导出通用处理：调用 API 获取 blob 并下载
   * @param {string} url
   * @param {string} label 'ZIP' | 'PDF'
   */
  function downloadExport(url, label) {
    showToast('⏳ 正在生成 ' + label + ' 文件...', 'info');
    // 需要原始 Response（blob），不走 apiFetch；手动注册 AbortController，
    // pagehide 时由 utils.js 统一 abort，避免大文件导出请求残留占用连接池
    var controller = new AbortController();
    window.registerAbortable(controller);
    fetch(url, { method: 'POST', signal: controller.signal })
      .then(function (r) {
        if (!r.ok) {
          return r.json().then(function (data) {
            throw new Error(data.message || '导出失败');
          });
        }
        // 本程序不会回 204：这是下载工具（如 IDM）接管了带附件的响应，自己保存文件，只给页面一个空响应。
        // 不再另存一个空文件，也不说“导出完成”
        if (r.status === 204) return null;
        return r.blob();
      })
      .then(function (blob) {
        if (blob === null) {
          showToast('ℹ️ ' + label + ' 已交给下载工具保存，请在下载工具里查看', 'info');
          return;
        }
        if (!blob.size) throw new Error('导出的文件是空的');
        var filename = label === 'ZIP' ? 'download.zip' : 'download.pdf';
        var link = document.createElement('a');
        link.href = URL.createObjectURL(blob);
        link.download = filename;
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        URL.revokeObjectURL(link.href);
        showToast('✅ ' + label + ' 导出完成', 'success');
      })
      .catch(function (err) {
        if (err && err.name === 'AbortError') return; // pagehide 中止，静默
        showToast('❌ ' + label + ' 导出失败: ' + (err.message || '网络错误'), 'danger');
      })
      .finally(function () {
        window.unregisterAbortable(controller);
      });
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
   * 批量重试所有失败任务（串行，避免触发风控）
   */
  function batchRetryFailed() {
    var msg = '确定批量重试所有「失败」任务？\n\n将逐个创建新任务并开始下载。\n注意：原有失败记录会被保留。';
    if (!confirmAction(msg)) return;

    var container = document.getElementById('failed-section');
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

    var total = jobIds.length;
    var done = 0;

    function retryNext(index) {
      if (index >= jobIds.length) {
        showToast('已重试 ' + total + ' 个失败任务（成功 ' + done + ' 个）', 'success');
        refreshJobs();
        return;
      }

      apiFetch('/api/jobs/' + encodeJobId(jobIds[index]) + '/retry', { method: 'POST' })
        .then(function () {
          done++;
          retryNext(index + 1);
        })
        .catch(function (e) {
          // pagehide 中止时终止整条重试链并静默（后续请求也必然被中止），
          // 避免 bfcache 恢复后弹出"成功 0 个"的误导性汇总
          if (e && e.name === 'AbortError') return;
          retryNext(index + 1);
        });
    }

    retryNext(0);
  }

  // ── 按钮事件：一个委托监听器（静态工具栏 + 动态任务卡片） ──

  var ACTIONS = {
    pause: function (el) { pauseJob(el.getAttribute('data-job-id')); },
    resume: function (el) { resumeJob(el.getAttribute('data-job-id')); },
    cancel: function (el) { cancelJob(el.getAttribute('data-job-id')); },
    retry: function (el) { retryJob(el.getAttribute('data-job-id')); },
    'delete': function (el) { deleteJob(el.getAttribute('data-job-id')); },
    'open-folder': function (el) { window.openFolder(el.getAttribute('data-job-id')); },
    'export-zip': function (el) { exportZip(el.getAttribute('data-job-id')); },
    'export-pdf': function (el) { exportPdf(el.getAttribute('data-job-id')); },
    'batch-retry': function () { batchRetryFailed(); },
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
  var READABLE_RECHECK_MS = 60000; // 文件可能在页面打开期间被移走：至少每分钟复查一次

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
    if (ids.length === 0) { readableAlbums = {}; localInfo = {}; return; }

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
        renderSection('completed', lastCompleted, renderCompletedCard);
        renderSection('failed', lastFailed, renderFailedCard);
      })
      .catch(function () {
        // 失败时保留上次结果，下次轮询重试（中止/超时/网络错误都静默，不打扰用户）
        if (serial === readableSerial) readableKey = null;
      });
  }

  // ── 刷新任务列表 ──

  function refreshJobs() {
    // abortKey：轮询/操作触发的连续刷新自动中止上一次未完成的请求，避免堆积
    apiFetch('/api/jobs', { abortKey: 'downloads-refresh' })
      .then(function (data) {
        var jobs = data.jobs || [];
        renderJobs(jobs);
        reconnectSSE(jobs);
      })
      .catch(function (err) {
        if (err && err.name === 'AbortError') return; // pagehide/新刷新中止，静默
        showToast('加载任务列表失败: ' + err.message, 'danger');
      });
  }

  // ── SSE 重连（下载管理页专用） ──

  function reconnectSSE(jobs) {
    window.disconnectAllSSE();
    jobs.forEach(function (job) {
      if (job.status === 'running' || job.status === 'paused') {
        window.connectSSE(job.job_id);
      }
    });
  }

  // ── 渲染函数 ──

  function renderJobs(jobs) {
    // 按状态分组，同时缓存 job_id → title 映射供 SSE toast 使用
    var groups = { running: [], paused: [], queued: [], completed: [], failed: [] };
    jobs.forEach(function (job) {
      window._jobTitleMap[job.job_id] = job.title || job.album_id || job.job_id;
      var total = job.total_pages || 1;
      var done = job.done_pages || 0;
      job._pct = Math.round(done / total * 100);
      job._path = job.output_path || '';
      job._error = job.error_message || '';
      if (groups[job.status]) groups[job.status].push(job);
    });

    // paused 任务也算在"进行中"区域一并显示
    var runningItems = groups.running.concat(groups.paused);

    // 渲染每个区域（已完成/失败区先用缓存的可读结果渲染，“阅读”按钮不会随轮询闪烁）
    lastCompleted = groups.completed;
    lastFailed = groups.failed;
    renderSection('running', runningItems, renderRunningCard);
    renderSection('queued', groups.queued, renderQueuedCard);
    renderSection('completed', groups.completed, renderCompletedCard);
    renderSection('failed', groups.failed, renderFailedCard);
    refreshReadable(groups.completed.concat(groups.failed));

    // 更新计数徽章 — running 计数包含 paused
    var runningCount = groups.running.length + groups.paused.length;
    var runningEl = document.getElementById('running-count');
    if (runningEl) runningEl.textContent = runningCount;
    ['queued', 'completed', 'failed'].forEach(function (s) {
      var el = document.getElementById(s + '-count');
      if (el) el.textContent = groups[s].length;
    });
  }

  /** readableAlbums 里的判断结果：true / false；本地只剩打不开的压缩包 → 'archive_problem'（“阅读”点开说明原因）；
   *  还没判断 → undefined */
  function readableState(albumId) {
    var key = String(albumId);
    if (!Object.prototype.hasOwnProperty.call(readableAlbums, key)) return undefined;
    return window.readLink.stateFor(readableAlbums[key], (localInfo[key] || {}).state);
  }

  function renderSection(status, items, renderFn) {
    var container = document.getElementById(status + '-section');
    if (!container) return;
    if (items.length === 0) {
      container.innerHTML = '<div class="text-center text-muted py-4"><i class="bi bi-emoji-neutral" style="font-size:2rem;"></i><p class="mt-2 mb-0">没有' + statusLabel(status) + '</p></div>';
      return;
    }
    container.innerHTML = items.map(function (job) { return renderFn(job); }).join('');
  }

  function statusLabel(s) {
    return { running: '进行中的任务', paused: '暂停中的任务', queued: '排队中的任务', completed: '已完成的任务', failed: '失败的任务' }[s] || s;
  }

  function renderRunningCard(job) {
    var isPaused = job.status === 'paused';
    var badgeClass = isPaused ? 'bg-warning text-dark' : 'bg-primary';
    var badgeIcon = isPaused ? 'bi-pause-circle' : 'bi-arrow-repeat';
    var badgeText = isPaused ? '已暂停' : job._pct + '%';
    var jobIdAttr = escapeHtmlAttr(job.job_id);

    var actionButtons = '';
    if (isPaused) {
      actionButtons = ''
        + jobButton('resume', job, 'btn-outline-success me-1', '恢复下载', '<i class="bi bi-play-fill"></i> 恢复')
        + jobButton('cancel', job, 'btn-outline-danger', '取消', '<i class="bi bi-x-circle"></i> 取消');
    } else {
      actionButtons = ''
        + jobButton('pause', job, 'btn-outline-warning me-1', '暂停下载', '<i class="bi bi-pause-fill"></i> 暂停')
        + jobButton('cancel', job, 'btn-outline-danger', '取消', '<i class="bi bi-x-circle"></i> 取消');
    }

    // paused 状态时进度条停止动画
    var progressClass = isPaused ? 'progress-bar' : 'progress-bar progress-bar-striped progress-bar-animated';

    return '<div class="card job-card shadow-sm mb-3" data-job-id="' + jobIdAttr + '" data-status="' + escapeHtmlAttr(job.status) + '">'
      + '<div class="card-body">'
      + '<div class="d-flex justify-content-between align-items-start mb-2">'
      + '<div><h6 class="mb-1">' + escapeHtml(job.title) + '</h6>'
      + '<small class="text-muted" id="info-' + jobIdAttr + '">' + (job.done_pages || 0) + ' / ' + (job.total_pages || 0) + ' 页</small></div>'
      + '<span class="badge ' + badgeClass + '" id="pct-' + jobIdAttr + '"><i class="' + badgeIcon + '"></i> ' + badgeText + '</span>'
      + '</div>'
      + '<div class="progress mb-2">'
      + '<div id="progress-' + jobIdAttr + '" class="' + progressClass + '" role="progressbar" style="width:' + job._pct + '%"></div>'
      + '</div>'
      + '<div class="d-flex justify-content-end">'
      + actionButtons
      + '</div></div></div>';
  }

  function renderQueuedCard(job) {
    return '<div class="card job-card shadow-sm mb-3" data-job-id="' + escapeHtmlAttr(job.job_id) + '" data-status="queued">'
      + '<div class="card-body d-flex justify-content-between align-items-center">'
      + '<div><h6 class="mb-1">' + escapeHtml(job.title) + '</h6><small class="text-muted">等待中...</small></div>'
      + jobButton('cancel', job, 'btn-outline-danger', '取消', '<i class="bi bi-x-circle"></i> 取消')
      + '</div></div>';
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
    // 阅读（连续滚动）每张卡片都有：已下载打开本地文件，否则在线阅读；
    // 预览（/preview，单页翻页）只读本地文件，只在本地可读时出现
    var readBtn = window.readLink.html(job.album_id, readableState(job.album_id), 'btn-sm');
    // 标题/状态/路径在左，按钮组在右；状态徽章跟着标题，不随按钮多少左右移动。
    // 放不下时按钮组整体换行（长路径不再把按钮文字挤成竖排）
    return '<div class="card job-card shadow-sm mb-3" data-job-id="' + escapeHtmlAttr(job.job_id) + '" data-status="completed">'
      + '<div class="card-body job-card-row">'
      + '<div class="job-card-info"><h6 class="mb-1">' + escapeHtml(job.title) + '</h6>'
      + '<div class="job-card-status">' + cbzBadge + '<span class="badge bg-success">已完成</span>' + marker + '</div>'
      + '<small class="text-muted"><i class="bi bi-folder"></i> ' + escapeHtml(job._path) + '</small></div>'
      + '<div class="job-card-actions">'
      + readBtn
      + (readable && job._path ? '<a class="btn btn-sm btn-outline-info" href="/preview/' + albumPath + '" title="单页翻页预览本地文件"><i class="bi bi-images" aria-hidden="true"></i> 预览</a>' : '')
      + (job._path ? jobButton('open-folder', job, 'btn-outline-secondary', '打开文件夹', '<i class="bi bi-folder2-open"></i>', true) : '')
      + (job._path ? jobButton('export-zip', job, 'btn-outline-success', '导出 ZIP', '<i class="bi bi-file-zip"></i> 📦', true) : '')
      + (job._path ? jobButton('export-pdf', job, 'btn-outline-danger', '导出 PDF', '<i class="bi bi-file-pdf"></i> 📄', true) : '')
      + jobButton('retry', job, 'btn-outline-primary', '重新下载', '<i class="bi bi-arrow-counterclockwise"></i>', true)
      + jobButton('delete', job, 'btn-outline-danger', '删除记录', '<i class="bi bi-trash"></i>', true)
      + '</div></div></div>';
  }

  function renderFailedCard(job) {
    var errorHtml = '';
    if (job._error) {
      errorHtml = '<div class="alert alert-danger py-2 px-3 mt-2 mb-0" role="alert" style="font-size:0.85rem;border-left:4px solid var(--error);">'
        + '<i class="bi bi-exclamation-circle me-1"></i>'
        + '<strong>错误信息：</strong> ' + escapeHtml(job._error)
        + '</div>';
    }

    return '<div class="card job-card shadow-sm mb-3 border-danger" data-job-id="' + escapeHtmlAttr(job.job_id) + '" data-status="failed">'
      + '<div class="card-body">'
      + '<div class="d-flex justify-content-between align-items-start">'
      + '<div><h6 class="mb-1">' + escapeHtml(job.title) + '</h6></div>'
      + '<span class="badge bg-danger">失败</span>'
      + '</div>'
      + errorHtml
      + '<div class="d-flex justify-content-end gap-2 mt-2">'
      + window.readLink.html(job.album_id, readableState(job.album_id), 'btn-sm')
      + jobButton('retry', job, 'btn-outline-warning', '重试', '<i class="bi bi-arrow-counterclockwise"></i> 重试')
      + jobButton('delete', job, 'btn-outline-danger', '删除记录', '<i class="bi bi-trash"></i>', true)
      + '</div></div></div>';
  }

  // ── SSE 事件回调（连接建立后刷新列表） ──
  window.setSSECallbacks({
    onCompleted: function () { refreshJobs(); },
    onFailed: function () { refreshJobs(); },
  });

  // ── 轮询：离开页面（含进入 bfcache）时停止，返回时立即刷新并恢复 ──
  function startPolling() {
    if (!pollTimer) pollTimer = setInterval(refreshJobs, 5000);
  }
  function stopPolling() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = null;
  }

  // ── 初始化 ──
  document.addEventListener('DOMContentLoaded', function () {
    refreshJobs();
    startPolling();
  });

  window.addEventListener('pagehide', stopPolling);
  window.addEventListener('pageshow', function (event) {
    if (!event.persisted) return;
    readableKey = null; // 回到本页：重新判断哪些可以离线阅读
    refreshJobs();
    startPolling();
  });
  // 切回本标签页时重新判断（文件可能在别处被移动/删除）
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState !== 'visible' || !pollTimer) return;
    readableKey = null;
    refreshJobs();
  });
})();
