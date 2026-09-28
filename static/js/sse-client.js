/**
 * 统一 SSE 管理
 * 合并原 jobs.js 中 _sseSources（非下载页）和 sseConnections（下载页）为一份连接池
 *
 * 依赖: showToast (base.html), escapeHtml / apiFetch / encodeJobId (utils.js)
 *
 * 通过 setSSECallbacks 解耦不同页面的事件行为：
 *   - 首页（home.js）不设回调 → 仅显示 Toast
 *   - 下载管理页（downloads.js）设回调 → 触发 refreshJobs
 */
(function () {
  'use strict';

  /** @type {Object<string, EventSource>} 统一连接池 */
  var connections = {};

  /** job_id → title 映射，供 Toast 使用 */
  window._jobTitleMap = {};

  /**
   * 可注册的事件回调（由消费方通过 setSSECallbacks 注入）
   */
  var _callbacks = {
    onProgress: null,   // function(data)
    onCompleted: null,  // function(data)
    onFailed: null,     // function(data)
    onArchiving: null,  // function(data)
  };

  // ── 内部进度更新（兼容两套 DOM 结构） ──

  /**
   * 更新页面上所有匹配的进度元素
   * 同时支持：
   *   - data-job-id 属性选择器（首页 / 通用渲染）
   *   - id 选择器（下载管理页 renderRunningCard 产生的元素）
   */
  function _updateProgressUI(data) {
    var jobId = data.job_id;
    var pct = data.progress || 0;
    var done = data.done_pages || 0;
    var total = data.total_pages || 0;

    // 通过 data-job-id 属性查找（首页、通用模板）
    var bars = document.querySelectorAll('[data-job-id="' + jobId + '"] .progress-bar');
    var pctLabels = document.querySelectorAll('[data-job-id="' + jobId + '"] [data-pct]');
    var infoLabels = document.querySelectorAll('[data-job-id="' + jobId + '"] [data-info]');
    bars.forEach(function (el) { el.style.width = pct + '%'; el.setAttribute('aria-valuenow', pct); });
    pctLabels.forEach(function (el) { el.textContent = pct + '%'; });
    infoLabels.forEach(function (el) { el.textContent = done + '/' + total + ' 页'; });

    // 通过 id 引用查找（下载管理页 renderRunningCard 产生）
    var pctEl = document.getElementById('pct-' + jobId);
    var barEl = document.getElementById('progress-' + jobId);
    var infoEl = document.getElementById('info-' + jobId);
    if (pctEl) pctEl.textContent = pct + '%';
    if (barEl) {
      barEl.style.width = Math.min(pct, 100) + '%';
      barEl.setAttribute('aria-valuenow', pct);
    }
    if (infoEl) {
      infoEl.textContent = done + ' / ' + total + ' 页';
    }
  }

  // ── 公开 API ──

  /**
   * 为单个 job 建立 SSE 连接
   * 如果已存在连接则跳过（防重复）
   * @param {string} jobId
   */
  window.connectSSE = function (jobId) {
    if (connections[jobId]) return;

    var source = new EventSource('/api/jobs/' + encodeURIComponent(jobId) + '/events');
    connections[jobId] = source;

    /** 重试计数键：仅在连接出错时递增，连接成功打开或正常结束时清除 */
    var retryKey = '_sseRetry_' + jobId;

    // ── progress ──
    source.addEventListener('progress', function (e) {
      try {
        var data = JSON.parse(e.data);
        _updateProgressUI(data);
        if (_callbacks.onProgress) _callbacks.onProgress(data);
      } catch (err) { /* 忽略解析错误 */ }
    });

    // ── completed ──
    source.addEventListener('completed', function (e) {
      source.close();
      delete connections[jobId];
      delete window[retryKey];
      try {
        var data = JSON.parse(e.data);
        var title = window._jobTitleMap[data.job_id] || data.job_id;
        // 任务完成不代表整部漫画都已下载（可能只选了部分章节）
        showToast('✅ ' + title + ' 下载任务完成', 'success');
      } catch (err) { /* 忽略 */ }
      if (_callbacks.onCompleted) _callbacks.onCompleted(e);
    });

    // ── failed ──
    source.addEventListener('failed', function (e) {
      source.close();
      delete connections[jobId];
      delete window[retryKey];
      try {
        var data = JSON.parse(e.data);
        var title = window._jobTitleMap[data.job_id] || data.job_id;
        var errMsg = data.error_message ? ': ' + data.error_message : '';
        showToast('❌ ' + title + ' 下载失败' + errMsg, 'danger');
      } catch (err) { /* 忽略 */ }
      if (_callbacks.onFailed) _callbacks.onFailed(e);
    });

    // ── archiving ──
    source.addEventListener('archiving', function (e) {
      try {
        var data = JSON.parse(e.data);
        var pctEl = document.getElementById('pct-' + data.job_id);
        if (pctEl) pctEl.textContent = '📦 打包中';
      } catch (err) { /* 忽略 */ }
      if (_callbacks.onArchiving) _callbacks.onArchiving(e);
    });

    // ── 自动重连（最多连续失败 5 次）──
    // 修复：原实现在每次建立连接时递增计数（下载管理页每次轮询重建连接都会 +1），
    // 长时间停留后一旦出错就不再重连；改为仅在出错时递增、连接成功后归零
    source.onopen = function () {
      delete window[retryKey];
    };
    source.onerror = function () {
      source.close();
      delete connections[jobId];
      var retryCount = (window[retryKey] || 0) + 1;
      window[retryKey] = retryCount;
      if (retryCount >= 5) {
        console.warn('SSE 重连已达上限 jobId=' + jobId);
        delete window[retryKey];
        return;
      }
      setTimeout(function () {
        if (connections[jobId]) return;
        window.connectSSE(jobId);
      }, Math.min(3000 * retryCount, 15000));
    };
  };

  /**
   * 为页面上所有 [data-status="running"] 的元素建立 SSE 连接
   */
  window.connectAllSSE = function () {
    document.querySelectorAll('[data-status="running"]').forEach(function (el) {
      var jobId = el.getAttribute('data-job-id');
      if (jobId) window.connectSSE(jobId);
    });
  };

  /**
   * 断开所有 SSE 连接
   */
  window.disconnectAllSSE = function () {
    Object.keys(connections).forEach(function (id) {
      try { connections[id].close(); } catch (e) { /* 忽略 */ }
      delete connections[id];
    });
  };

  /**
   * 注册 SSE 事件回调（解耦不同页面的行为）
   * @param {object} cbs
   * @param {function} [cbs.onProgress]
   * @param {function} [cbs.onCompleted]
   * @param {function} [cbs.onFailed]
   * @param {function} [cbs.onArchiving]
   */
  window.setSSECallbacks = function (cbs) {
    if (cbs.onProgress) _callbacks.onProgress = cbs.onProgress;
    if (cbs.onCompleted) _callbacks.onCompleted = cbs.onCompleted;
    if (cbs.onFailed) _callbacks.onFailed = cbs.onFailed;
    if (cbs.onArchiving) _callbacks.onArchiving = cbs.onArchiving;
  };

  // ── 页面关闭/隐藏时自动断开所有 SSE ──
  // beforeunload：保留旧行为（同时在部分浏览器阻止 bfcache，对 SSE 页面是期望行为）
  window.addEventListener('beforeunload', function () {
    window.disconnectAllSSE();
  });
  // pagehide：正常卸载与进入 bfcache 时都会触发，确保 EventSource 连接被释放，
  // 不占用浏览器连接池（EventSource 长连接会长期占住一个连接槽）
  window.addEventListener('pagehide', function () {
    window.disconnectAllSSE();
  });
  // pageshow：从 bfcache 恢复（event.persisted 为 true）时，为页面上仍处于
  // running 状态的任务重建连接；connectSSE 内部有防重复逻辑（已存在连接则跳过），
  // 普通首次加载（persisted 为 false）不在此处理，与各页面现有初始化逻辑保持一致
  window.addEventListener('pageshow', function (event) {
    if (event.persisted) {
      window.connectAllSSE();
    }
  });
})();
