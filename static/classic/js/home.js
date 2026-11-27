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
      var card = badge.closest('[data-job-id]');
      if (card) card.dataset.status = status;
      badge.textContent = label;
      badge.classList.replace('bg-primary', color);
      var bar = document.getElementById('progress-' + jobId);
      if (bar && bar.parentElement) bar.parentElement.hidden = true;
    }
    window.setSSECallbacks({
      onCanceled: function (event, jobId) { finish(jobId, 'canceled', '已取消', 'bg-secondary'); },
      onCompleted: function (event, jobId) { finish(jobId, 'completed', '已完成', 'bg-success'); },
      onFailed: function (event, jobId) { finish(jobId, 'failed', '失败', 'bg-danger'); },
    });
    document.querySelectorAll('[data-status="running"]').forEach(function (row) {
      var jid = row.getAttribute('data-job-id');
      var strong = row.querySelector('strong');
      if (jid && strong) window._jobTitleMap[jid] = strong.textContent;
    });
    function syncVisible() {
      if (document.visibilityState === 'hidden') window.disconnectAllSSE();
      else window.connectAllSSE();
    }
    document.addEventListener('visibilitychange', syncVisible);
    window.addEventListener('pageshow', function (event) { if (event.persisted) syncVisible(); });
    syncVisible();

    // 车号下载：输入车号或漫画链接时直达详情页，否则带着关键词打开搜索页
    var heroForm = document.getElementById('hero-search-form');
    var heroDownload = document.getElementById('hero-download-btn');
    var heroKeyword = document.getElementById('hero-keyword');
    if (heroForm && heroKeyword) {
      heroForm.addEventListener('submit', function () {
        var raw = heroKeyword.value.trim();
        if (window.extractAlbumId) {
          var parsed = window.extractAlbumId(raw);
          if (parsed) heroKeyword.value = parsed;
        }
      });
    }
    if (heroDownload && heroKeyword) {
      heroDownload.addEventListener('click', function () {
        var raw = heroKeyword.value.trim();
        var id = window.extractAlbumId ? window.extractAlbumId(raw) : raw.replace(/^JM\s*/i, '');
        if (/^\d{1,20}$/.test(id)) {
          window.location.href = '/album/' + id;
        } else if (raw) {
          window.location.href = '/search?keyword=' + encodeURIComponent(raw);
        } else {
          heroKeyword.focus();
          if (typeof showToast === 'function') showToast('请输入数字车号或关键词，例如 JM123456', 'warning');
        }
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
