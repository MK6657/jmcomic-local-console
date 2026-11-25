/**
 * 首页功能 — SSE 实时进度 + 按钮事件
 *
 * 首页只需要 SSE 连接功能，不需要轮询/渲染。
 * 按钮不用内联 onclick：job_id 只放在 data-job-id（模板自动转义），由一个委托监听器读取。
 * 列表页只初始化展示方式和书封回退；首页依赖 sse-client.js、utils.js。
 */
(function () {
  'use strict';

  var switcher = document.getElementById('shelf-view');
  if (switcher) {
    var collection = switcher.closest('.atelier-collection');
    var defaultView = collection.dataset.layout;
    var requested = new URLSearchParams(window.location.search).get('view');
    var modes = {shelf: '书架', catalog: '名录'};
    collection.dataset.layout = Object.hasOwn(modes, requested) ? requested : defaultView;
    Object.keys(modes).forEach(function (mode) {
      var button = document.createElement('button');
      button.type = 'button';
      button.textContent = modes[mode];
      button.dataset.mode = mode;
      button.setAttribute('aria-controls', switcher.getAttribute('aria-controls'));
      button.setAttribute('aria-pressed', String(mode === collection.dataset.layout));
      button.addEventListener('click', function () {
        collection.dataset.layout = mode;
        switcher.querySelectorAll('button').forEach(function (other) {
          other.setAttribute('aria-pressed', String(other === button));
        });
        var url = new URL(window.location.href);
        if (mode === defaultView) url.searchParams.delete('view');
        else url.searchParams.set('view', mode);
        window.history.replaceState(window.history.state, '', url);
      });
      switcher.appendChild(button);
    });
    collection.addEventListener('error', function (event) {
      var image = event.target;
      if (image.tagName === 'IMG' && image.closest('.shelf-cover')) image.hidden = true;
    }, true);
    return;
  }

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

    var heroForm = document.getElementById('hero-search-form');
    var heroDownload = document.getElementById('hero-download-btn');
    var heroKeyword = document.getElementById('hero-keyword');
    if (heroForm && heroKeyword) {
      heroForm.addEventListener('submit', function () {
        var raw = heroKeyword.value.trim();
        if (window.extractAlbumId && (/^https?:\/\//i.test(raw) || /18comic|jmcomic/i.test(raw))) {
          var parsed = window.extractAlbumId(raw);
          if (parsed) heroKeyword.value = parsed;
        }
      });
    }
    if (heroDownload && heroKeyword) {
      heroDownload.addEventListener('click', function () {
        var raw = heroKeyword.value.trim();
        var id = window.extractAlbumId ? window.extractAlbumId(raw) : raw.replace(/^JM\s*/i, '');
        if (/^\d{1,20}$/.test(id)) window.location.href = '/album/' + id;
        else {
          heroKeyword.focus();
          showToast('请输入数字车号或漫画链接，例如 JM123456', 'warning');
        }
      });
    }

    // 最近下载 / 进行中任务缩略图加载失败时隐藏 img，回退到序号底纹
    document.addEventListener('error', function (event) {
      var image = event.target;
      if (image.tagName === 'IMG' && image.closest('.desk-thumb')) image.hidden = true;
    }, true);

    // 轻量异步填充首页书阁概览数字（无额外后端接口，不阻塞首屏渲染）
    var statReadable = document.getElementById('home-stat-readable');
    if (statReadable && window.apiFetch) {
      window.apiFetch('/api/library/stats', { timeoutMs: 8000, abortKey: 'home-stats' })
        .then(function (data) {
          if (!data || data.status !== 'ok') return;
          var setText = function (id, val) {
            var node = document.getElementById(id);
            if (node && val !== undefined && val !== null) node.textContent = String(val);
          };
          setText('home-stat-readable', data.readable_count || 0);
          setText('home-stat-wishlist', data.wishlist_count || 0);
          setText('home-stat-downloaded', data.downloaded_count || 0);
          setText('home-stat-tags', data.tag_count || 0);
        })
        .catch(function () { /* 静默忽略，保持占位符 */ });
    }

    // 最近下载：打开文件夹
    document.addEventListener('click', function (event) {
      var btn = event.target.closest('button[data-action="open-folder"]');
      if (!btn || !btn.dataset.jobId) return;
      window.openFolder(btn.dataset.jobId);
    });
  });
})();
