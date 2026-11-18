/**
 * 单页翻页（preview.js）与连续阅读（reader.js）共用的阅读导航：
 * 页码记忆、地址栏 ?page= 同步、模式切换、返回。
 * 两种模式共用同一个 sessionStorage 键，互切时保留页码。
 */
(function () {
  'use strict';

  function storageKey(albumId) { return 'jm-reader-page:' + albumId; }

  // 带修饰键或非左键的点击交给浏览器（新标签页打开等）
  function isPlainClick(event) {
    return event.button === 0 && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey;
  }

  window.readingNav = {
    /** 起始页：地址栏 ?page= 优先，其次本标签页记住的页码，默认 1（由调用方夹到有效范围） */
    initialPage: function (albumId) {
      var requested = Math.floor(Number(new URLSearchParams(location.search).get('page')));
      if (Number.isFinite(requested) && requested >= 1) return requested;
      try { return Math.floor(Number(sessionStorage.getItem(storageKey(albumId)))) || 1; } catch (_) { return 1; }
    },

    /**
     * 记住当前页，并用 replaceState 同步地址栏（不新增历史记录）。
     * addToUrl=false 时只更新已有的 ?page=：连续阅读随滚动频繁换页，不主动往地址里加参数。
     */
    remember: function (albumId, page, addToUrl) {
      try { sessionStorage.setItem(storageKey(albumId), String(page)); } catch (_) {}
      try {
        var url = new URL(location.href);
        if (!addToUrl && !url.searchParams.has('page')) return;
        url.searchParams.set('page', page);
        history.replaceState(history.state, '', url.pathname + url.search);
      } catch (_) {}
    },

    /** 模式切换替换当前历史条目，返回时直接回到进入阅读前的页面，而不是在两种模式间来回跳 */
    bindModeSwitch: function (link, beforeLeave) {
      link.addEventListener('click', function (event) {
        if (!isPlainClick(event)) return;
        event.preventDefault();
        if (beforeLeave) beforeLeave();
        location.replace(link.href);
      });
    },

    /**
     * 从站内页面进入时后退（保留搜索结果与滚动位置）；直接打开、或本标签页里前面没有页面时跟随链接自身的 href。
     * 新标签页打开详情、再进阅读页又退回来时 history.length 为 2，但当前已是第一条历史，后退没有反应：
     * 有 Navigation API 时以 navigation.canGoBack 为准。
     */
    bindBack: function (link) {
      link.addEventListener('click', function (event) {
        if (!isPlainClick(event)) return;
        try {
          var canGoBack = (window.navigation && typeof window.navigation.canGoBack === 'boolean')
            ? window.navigation.canGoBack
            : history.length > 1;
          if (canGoBack && new URL(document.referrer).origin === location.origin) {
            event.preventDefault();
            history.back();
          }
        } catch (_) { /* referrer 为空：跟随 href */ }
      });
    },

    /**
     * 本地压缩包里的一页读不出来时服务端回说明（<img> 拿不到）：再取一次这一页读出原因。返回 Promise：
     *   {reason: 'archive_corrupt', message}  这一页坏了（422）：重试没有用，给在线阅读
     *   {reason: 'archive_changed', message}  压缩包已重新打包、这一页不在了（409）：刷新页面拿新的页列表
     *   {reason: 'archive_busy', message}     压缩包暂时打不开（503）：说明原因，保留“重试”
     *   null  不是压缩包的页、网络问题等（照常给“重试”）
     */
    archivePageProblem: function (url) {
      var archive;
      try {
        var path = new URL(url, location.origin).pathname;
        archive = path.indexOf('/api/preview-archive/') === 0;
        // 散图：只看 409（阅读页打开后这一页被打包进了压缩包）；真的不在了（404）照常给“重试”
        if (!archive && path.indexOf('/api/preview-img/') !== 0) return Promise.resolve(null);
      } catch (_) { return Promise.resolve(null); }
      return fetch(url, { cache: 'no-store' }).then(function (response) {
        if (response.ok || !((archive && (response.status === 422 || response.status === 503)) || response.status === 409)) return null;
        return response.json().then(function (data) {
          if (!data || ['archive_corrupt', 'archive_changed', 'archive_busy'].indexOf(data.reason) < 0) return null;
          var message = String(data.message || '压缩包里这一页读不出来');
          return { reason: data.reason, message: /[。！？]$/.test(message) ? message : message + '。' };
        });
      }).catch(function () { return null; });
    },

    /**
     * 本地的一页在线读：同一章（photo_id）里的同一位置（offset）。本地可能只下载了部分章节，按本地页码去在线阅读
     * 会是别的章节；章节目录名里没有 photo_id 时只能打开在线阅读的开头（exact=false，按钮不说“这一页”）。
     */
    onlinePageUrl: function (albumId, page) {
      var base = '/online/' + encodeURIComponent(albumId);
      if (page && /^[0-9]{1,20}$/.test(String(page.photo_id || ''))) {
        return { url: base + '?chapter=' + page.photo_id + '&offset=' + (Number(page.offset) || 0), exact: true };
      }
      return { url: base, exact: false };
    }
  };
})();
