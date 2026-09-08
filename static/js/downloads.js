/**
 * 下载管理页 — 全量渲染 + 操作按钮 + 轮询
 *
 * 依赖: apiFetch, escapeHtml, confirmAction, encodeJobId (utils.js)
 *        connectSSE, disconnectAllSSE, setSSECallbacks, _jobTitleMap (sse-client.js)
 *         showToast (base.html)
 */
(function () {
  'use strict';

  // 仅在下载管理页执行
  if (!document.getElementById('downloadTabs')) return;

  /** @type {number|null} */
  var pollTimer = null;

  // ── 操作函数 ──

  window.cancelJob = function (jobId) {
    if (!confirmAction('确定取消此任务？\n排队中任务立即取消，下载中任务会尽快停止。')) return;
    apiFetch('/api/jobs/' + encodeJobId(jobId) + '/cancel', { method: 'POST' })
      .then(function () { refreshJobs(); })
      .catch(function (err) {
        if (err && err.name === 'AbortError') return; // pagehide 中止，静默
        showToast('取消失败: ' + err.message, 'danger');
      });
  };

  window.pauseJob = function (jobId) {
    apiFetch('/api/jobs/' + encodeJobId(jobId) + '/pause', { method: 'POST' })
      .then(function () {
        showToast('⏸ 已暂停', 'info');
        refreshJobs();
      })
      .catch(function (err) {
        if (err && err.name === 'AbortError') return; // pagehide 中止，静默
        showToast('暂停失败: ' + err.message, 'danger');
      });
  };

  window.resumeJob = function (jobId) {
    apiFetch('/api/jobs/' + encodeJobId(jobId) + '/resume', { method: 'POST' })
      .then(function () {
        showToast('▶ 已恢复', 'success');
        refreshJobs();
      })
      .catch(function (err) {
        if (err && err.name === 'AbortError') return; // pagehide 中止，静默
        showToast('恢复失败: ' + err.message, 'danger');
      });
  };

  window.retryJob = function (jobId) {
    apiFetch('/api/jobs/' + encodeJobId(jobId) + '/retry', { method: 'POST' })
      .then(function (data) {
        showToast('已创建新任务 (新ID: ' + data.job_id + ')', 'success');
        refreshJobs();
      })
      .catch(function (err) {
        if (err && err.name === 'AbortError') return; // pagehide 中止，静默
        showToast('重试失败: ' + err.message, 'danger');
      });
  };

  window.deleteJob = function (jobId) {
    if (!confirmAction('确定删除此记录？不会删除已下载的文件。')) return;
    apiFetch('/api/jobs/' + encodeJobId(jobId), { method: 'DELETE' })
      .then(function () { refreshJobs(); })
      .catch(function (err) {
        if (err && err.name === 'AbortError') return; // pagehide 中止，静默
        showToast('删除失败: ' + err.message, 'danger');
      });
  };

  // openFolder 由 utils.js 提供（此前此处有一份完全相同的重复定义，已移除）

  // ── 导出 ZIP / PDF ──

  window.exportZip = function (jobId) {
    downloadExport('/api/export/' + encodeJobId(jobId) + '/zip', 'ZIP');
  };

  window.exportPdf = function (jobId) {
    downloadExport('/api/export/' + encodeJobId(jobId) + '/pdf', 'PDF');
  };

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
        return r.blob();
      })
      .then(function (blob) {
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

  window.clearJobs = function (status) {
    var labels = { completed: '已完成', failed: '失败', canceled: '已取消' };
    var label = labels[status] || status;
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
  };

  window.clearFinished = function () {
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
  };

  /**
   * 批量重试所有失败任务（串行，避免触发风控）
   */
  window.batchRetryFailed = function () {
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
  };

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

    // 渲染每个区域
    renderSection('running', runningItems, renderRunningCard);
    renderSection('queued', groups.queued, renderQueuedCard);
    renderSection('completed', groups.completed, renderCompletedCard);
    renderSection('failed', groups.failed, renderFailedCard);

    // 更新计数徽章 — running 计数包含 paused
    var runningCount = groups.running.length + groups.paused.length;
    var runningEl = document.getElementById('running-count');
    if (runningEl) runningEl.textContent = runningCount;
    ['queued', 'completed', 'failed'].forEach(function (s) {
      var el = document.getElementById(s + '-count');
      if (el) el.textContent = groups[s].length;
    });
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

    var actionButtons = '';
    if (isPaused) {
      actionButtons = ''
        + '<button class="btn btn-sm btn-outline-success me-1" onclick="resumeJob(\'' + encodeJobId(job.job_id) + '\')" title="恢复下载"><i class="bi bi-play-fill"></i> 恢复</button>'
        + '<button class="btn btn-sm btn-outline-danger" onclick="cancelJob(\'' + encodeJobId(job.job_id) + '\')" title="取消"><i class="bi bi-x-circle"></i> 取消</button>';
    } else {
      actionButtons = ''
        + '<button class="btn btn-sm btn-outline-warning me-1" onclick="pauseJob(\'' + encodeJobId(job.job_id) + '\')" title="暂停下载"><i class="bi bi-pause-fill"></i> 暂停</button>'
        + '<button class="btn btn-sm btn-outline-danger" onclick="cancelJob(\'' + encodeJobId(job.job_id) + '\')" title="取消"><i class="bi bi-x-circle"></i> 取消</button>';
    }

    // paused 状态时进度条停止动画
    var progressClass = isPaused ? 'progress-bar' : 'progress-bar progress-bar-striped progress-bar-animated';

    return '<div class="card job-card shadow-sm mb-3" data-job-id="' + job.job_id + '" data-status="' + job.status + '">'
      + '<div class="card-body">'
      + '<div class="d-flex justify-content-between align-items-start mb-2">'
      + '<div><h6 class="mb-1">' + escapeHtml(job.title) + '</h6>'
      + '<small class="text-muted" id="info-' + job.job_id + '">' + (job.done_pages || 0) + ' / ' + (job.total_pages || 0) + ' 页</small></div>'
      + '<span class="badge ' + badgeClass + '" id="pct-' + job.job_id + '"><i class="' + badgeIcon + '"></i> ' + badgeText + '</span>'
      + '</div>'
      + '<div class="progress mb-2">'
      + '<div id="progress-' + job.job_id + '" class="' + progressClass + '" role="progressbar" style="width:' + job._pct + '%"></div>'
      + '</div>'
      + '<div class="d-flex justify-content-end">'
      + actionButtons
      + '</div></div></div>';
  }

  function renderQueuedCard(job) {
    return '<div class="card job-card shadow-sm mb-3" data-job-id="' + job.job_id + '" data-status="queued">'
      + '<div class="card-body d-flex justify-content-between align-items-center">'
      + '<div><h6 class="mb-1">' + escapeHtml(job.title) + '</h6><small class="text-muted">等待中...</small></div>'
      + '<button class="btn btn-sm btn-outline-danger" onclick="cancelJob(\'' + encodeJobId(job.job_id) + '\')"><i class="bi bi-x-circle"></i> 取消</button>'
      + '</div></div>';
  }

  function renderCompletedCard(job) {
    var cbzBadge = job.has_cbz ? '<span class="badge bg-info me-1"><i class="bi bi-archive"></i> 📦 CBZ</span> ' : '';
    return '<div class="card job-card shadow-sm mb-3" data-job-id="' + job.job_id + '" data-status="completed">'
      + '<div class="card-body d-flex justify-content-between align-items-center">'
      + '<div><h6 class="mb-1">' + escapeHtml(job.title) + '</h6>'
      + '<small class="text-muted"><i class="bi bi-folder"></i> ' + escapeHtml(job._path) + '</small></div>'
      + '<div class="d-flex align-items-center gap-2">'
      + cbzBadge
      + '<span class="badge bg-success">已完成</span>'
      + (job._path ? '<a class="btn btn-sm btn-outline-info" href="/preview/' + encodeJobId(job.album_id) + '" title="本地预览"><i class="bi bi-book"></i> 预览</a>' : '')
      + (job._path ? '<button class="btn btn-sm btn-outline-secondary" onclick="openFolder(\'' + encodeJobId(job.job_id) + '\')" title="打开文件夹"><i class="bi bi-folder-open"></i></button>' : '')
      + (job._path ? '<button class="btn btn-sm btn-outline-success" onclick="exportZip(\'' + encodeJobId(job.job_id) + '\')" title="导出 ZIP"><i class="bi bi-file-zip"></i> 📦</button>' : '')
      + (job._path ? '<button class="btn btn-sm btn-outline-danger" onclick="exportPdf(\'' + encodeJobId(job.job_id) + '\')" title="导出 PDF"><i class="bi bi-file-pdf"></i> 📄</button>' : '')
      + '<button class="btn btn-sm btn-outline-primary" onclick="retryJob(\'' + encodeJobId(job.job_id) + '\')" title="重新下载"><i class="bi bi-arrow-counterclockwise"></i></button>'
      + '<button class="btn btn-sm btn-outline-danger" onclick="deleteJob(\'' + encodeJobId(job.job_id) + '\')" title="删除记录"><i class="bi bi-trash"></i></button>'
      + '</div></div></div>';
  }

  function renderFailedCard(job) {
    var errorHtml = '';
    if (job._error) {
      errorHtml = '<div class="alert alert-danger py-2 px-3 mt-2 mb-0" role="alert" style="font-size:0.85rem;border-left:4px solid #dc3545;">'
        + '<i class="bi bi-exclamation-circle me-1"></i>'
        + '<strong>错误信息：</strong> ' + escapeHtml(job._error)
        + '</div>';
    }

    return '<div class="card job-card shadow-sm mb-3 border-danger" data-job-id="' + job.job_id + '" data-status="failed">'
      + '<div class="card-body">'
      + '<div class="d-flex justify-content-between align-items-start">'
      + '<div><h6 class="mb-1">' + escapeHtml(job.title) + '</h6></div>'
      + '<span class="badge bg-danger">失败</span>'
      + '</div>'
      + errorHtml
      + '<div class="d-flex justify-content-end gap-2 mt-2">'
      + '<button class="btn btn-sm btn-outline-warning" onclick="retryJob(\'' + encodeJobId(job.job_id) + '\')" title="重试"><i class="bi bi-arrow-counterclockwise"></i> 重试</button>'
      + '<button class="btn btn-sm btn-outline-danger" onclick="deleteJob(\'' + encodeJobId(job.job_id) + '\')" title="删除记录"><i class="bi bi-trash"></i></button>'
      + '</div></div></div>';
  }

  // ── SSE 事件回调（连接建立后刷新列表） ──
  window.setSSECallbacks({
    onCompleted: function () { refreshJobs(); },
    onFailed: function () { refreshJobs(); },
  });

  // ── 初始化 ──
  document.addEventListener('DOMContentLoaded', function () {
    refreshJobs();
    pollTimer = setInterval(refreshJobs, 5000);
  });

  // ── 页面关闭时停止轮询 ──
  window.addEventListener('beforeunload', function () {
    if (pollTimer) clearInterval(pollTimer);
  });
})();
