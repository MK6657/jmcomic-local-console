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

    // 为页面上所有 running 任务连接 SSE（服务器端渲染已展示初始数据）
    window.connectAllSSE();

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
