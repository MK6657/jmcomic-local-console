/**
 * 首页功能 — SSE 实时进度
 *
 * 首页只需要 SSE 连接功能，不需要轮询/渲染。
 * 依赖: connectSSE / connectAllSSE (sse-client.js)
 */
(function () {
  'use strict';

  document.addEventListener('DOMContentLoaded', function () {
    // 如果页面上有 #downloadTabs，说明是下载管理页，不做处理
    if (document.getElementById('downloadTabs')) return;

    // 为页面上所有 running 任务连接 SSE（服务器端渲染已展示初始数据）
    window.connectAllSSE();
  });
})();
