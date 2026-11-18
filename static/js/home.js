/**
 * 首页功能 — SSE 实时进度 + 按钮事件
 *
 * 首页只需要 SSE 连接功能，不需要轮询/渲染。
 * 按钮不用内联 onclick：job_id 只放在 data-job-id（模板自动转义），由一个委托监听器读取。
 * 依赖: connectSSE / connectAllSSE (sse-client.js), openFolder (utils.js)
 */
(function () {
  'use strict';

  document.addEventListener('DOMContentLoaded', function () {
    // 如果页面上有 #downloadTabs，说明是下载管理页，不做处理
    if (document.getElementById('downloadTabs')) return;

    function finish(jobId, status, label, color) {
      var badge = document.getElementById('pct-' + jobId);
      if (!badge) return;
      badge.closest('[data-job-id]').dataset.status = status;
      badge.textContent = label;
      badge.classList.replace('bg-primary', color);
      document.getElementById('progress-' + jobId).parentElement.hidden = true;
    }
    window.setSSECallbacks({
      onCanceled: function (event, jobId) { finish(jobId, 'canceled', '已取消', 'bg-secondary'); },
      onCompleted: function (event, jobId) { finish(jobId, 'completed', '已完成', 'bg-success'); },
      onFailed: function (event, jobId) { finish(jobId, 'failed', '失败', 'bg-danger'); },
    });
    document.querySelectorAll('[data-status="running"]').forEach(function (row) {
      window._jobTitleMap[row.getAttribute('data-job-id')] = row.querySelector('strong').textContent;
    });
    function syncVisible() {
      if (document.visibilityState === 'hidden') window.disconnectAllSSE();
      else window.connectAllSSE();
    }
    document.addEventListener('visibilitychange', syncVisible);
    window.addEventListener('pageshow', function (event) { if (event.persisted) syncVisible(); });
    syncVisible();

    // 车号下载：带着输入框里的车号打开搜索页
    var heroDownload = document.getElementById('hero-download-btn');
    var heroKeyword = document.getElementById('hero-keyword');
    if (heroDownload && heroKeyword) {
      heroDownload.addEventListener('click', function () {
        window.location.href = '/search?keyword=' + encodeURIComponent(heroKeyword.value.trim());
      });
    }

    // 最近下载：打开文件夹
    document.addEventListener('click', function (event) {
      var btn = event.target.closest('button[data-action="open-folder"]');
      if (!btn || !btn.dataset.jobId) return;
      window.openFolder(btn.dataset.jobId);
    });
  });
})();
