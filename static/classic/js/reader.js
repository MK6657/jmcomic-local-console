/* Continuous reader. Local mode (/read) shows downloaded files only; online mode (/online) shows pages
   this app fetches on demand. Neither starts a download. */
(function () {
  'use strict';
  var root = document.querySelector('.continuous-reader');
  if (!root) return;
  var albumId = root.getAttribute('data-album-id');
  var online = root.getAttribute('data-source') === 'online';
  var positionKey = online ? 'online:' + albumId : albumId;   // online page memory stays separate
  // pages only ever come from this app: online pages, local loose files, or pages read out of the local CBZ / ZIP
  var imagePrefixes = online ? ['/api/online-img/'] : ['/api/preview-img/', '/api/preview-archive/'];
  var AUTO_RETRY_MS = 3000;   // online mode: one automatic retry of a failed page before showing the error
  var nav = window.readingNav;
  var pages = [];
  var rendered = 0;
  var current = 0;
  var loading = false;
  var loadSerial = 0;
  var pageObserver;
  var moreObserver;
  var visiblePages = new Set();
  var byId = function (id) { return document.getElementById(id); };
  var tools = byId('reader-tools');
  var toolsToggle = byId('reader-tools-toggle');
  var pageInput = byId('reader-page-input');
  var pagedLink = byId('reader-paged');

  // After a jump, lazy images around the target keep loading and shift the layout.
  // The anchor re-aligns the target after every load until the reader scrolls on their own.
  var jumpAnchor = null;
  var jumpAlignment = 'start';
  var anchorOffset = 0;   // viewport position of the anchored edge at the last baseline
  var anchorScrollY = 0;  // scrollY at the last baseline
  var alignFrame = 0;
  function anchorPosition() {
    // The anchored edge relative to the viewport: the document top for 'top', else the page's top/bottom.
    if (jumpAlignment === 'top') return -window.scrollY;
    var bounds = byId('reader-page-' + jumpAnchor).getBoundingClientRect();
    return jumpAlignment === 'end' ? bounds.bottom : bounds.top;
  }
  function recordAnchor() {
    anchorOffset = anchorPosition();
    anchorScrollY = window.scrollY;
  }
  function positionJump() {
    if (jumpAlignment === 'top') window.scrollTo({ top: 0, behavior: 'instant' });
    else byId('reader-page-' + jumpAnchor).scrollIntoView({ block: jumpAlignment, behavior: 'instant' });
    recordAnchor();
  }
  function alignJump() {
    if (jumpAnchor === null || alignFrame) return;
    alignFrame = requestAnimationFrame(function () {
      alignFrame = 0;
      if (jumpAnchor !== null) positionJump();
    });
  }
  function releaseJump() {
    if (jumpAnchor === null) return;
    jumpAnchor = null;
    // Pages already in view produce no new intersection events, so sync the counter now.
    if (visiblePages.size) setCurrent(Math.min.apply(null, Array.from(visiblePages)));
  }
  ['wheel', 'touchstart', 'pointerdown'].forEach(function (name) {
    window.addEventListener(name, releaseJump, { passive: true });
  });
  // Scrollbar drags and arrow clicks fire no pointer events, so scrolling itself must release the anchor.
  // When the reader scrolls, the page moves exactly as far as the viewport did. Images loading above move
  // the page without scrolling, and browser scroll anchoring scrolls without moving it; those only reset
  // the baseline. Small scrolls accumulate, so slow drags release too.
  window.addEventListener('scroll', function () {
    if (jumpAnchor === null || alignFrame) return;
    var scrolled = window.scrollY - anchorScrollY;
    var moved = anchorPosition() - anchorOffset;
    if (Math.abs(moved + scrolled) >= 48) recordAnchor();
    else if (Math.abs(scrolled) > 48) releaseJump();
  }, { passive: true });

  function showTools(visible, focus) {
    tools.hidden = !visible;
    toolsToggle.setAttribute('aria-expanded', String(visible));
    if (visible && focus) byId('reader-back').focus({ preventScroll: true });
    if (!visible && tools.contains(document.activeElement)) root.focus({ preventScroll: true });
  }
  toolsToggle.addEventListener('click', function () { showTools(tools.hidden, true); });
  byId('reader-tools-close').addEventListener('click', function () { showTools(false); });
  document.addEventListener('click', function (event) {
    if (event.target.closest('a, button, input, select, textarea, nav, footer, .reader-toolbar, .reader-tools')) return;
    if (window.getSelection().toString()) return;
    showTools(tools.hidden);
  });
  document.addEventListener('keydown', function (event) {
    if (['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' '].indexOf(event.key) >= 0) releaseJump();
    if (event.key === 'Escape') { showTools(false); return; }
    if (event.target.closest('input, textarea, select, [contenteditable="true"]')) return;
    if ((event.key === 'm' || event.key === 'M') && !event.ctrlKey && !event.altKey && !event.metaKey && !event.repeat) {
      event.preventDefault(); showTools(tools.hidden, true);
    }
  });
  ['wheel', 'touchmove'].forEach(function (name) {
    window.addEventListener(name, function () {
      var editing = tools.contains(document.activeElement) && document.activeElement.matches('input, select, textarea');
      if (!tools.hidden && !editing) showTools(false);
    }, { passive: true });
  });

  function setCurrent(number) {
    if (number === current) return;
    current = number;
    if (pagedLink) pagedLink.href = '/preview/' + encodeURIComponent(albumId) + '?page=' + current;
    byId('reader-progress').textContent = '第 ' + current + ' / ' + pages.length + ' 页';
    if (document.activeElement !== pageInput) pageInput.value = current;
    nav.remember(positionKey, current, false);
  }
  function disconnect() {
    if (pageObserver) pageObserver.disconnect();
    if (moreObserver) moreObserver.disconnect();
    visiblePages.clear();
  }
  function observe() {
    disconnect();
    if (!window.IntersectionObserver) return;
    pageObserver = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        var number = Number(entry.target.dataset.page);
        if (entry.isIntersecting) visiblePages.add(number);
        else visiblePages.delete(number);
      });
      if (visiblePages.size && jumpAnchor === null) setCurrent(Math.min.apply(null, Array.from(visiblePages)));
    }, { rootMargin: '0px 0px -25% 0px', threshold: 0 });
    root.querySelectorAll('.reader-page').forEach(function (figure) { pageObserver.observe(figure); });
    moreObserver = new IntersectionObserver(function (entries) {
      if (entries.some(function (entry) { return entry.isIntersecting; })) appendPages();
    }, { rootMargin: '800px' });
    moreObserver.observe(byId('reader-more'));
  }
  function readerImageUrl(value) {
    try {
      var url = new URL(value, location.origin);
      var ours = imagePrefixes.some(function (prefix) { return url.pathname.indexOf(prefix) === 0; });
      return url.origin === location.origin && ours ? url.href : null;
    } catch (_) { return null; }
  }
  function appendPages() {
    var end = Math.min(rendered + 20, pages.length);
    var fragment = document.createDocumentFragment();
    for (; rendered < end; rendered++) {
      (function (page, index) {
        var label = '第 ' + (index + 1) + ' 页';
        var figure = document.createElement('figure');
        figure.className = 'reader-page';
        figure.id = 'reader-page-' + (index + 1);
        figure.dataset.page = index + 1;
        figure.setAttribute('aria-label', label);
        var img = document.createElement('img');
        img.alt = label;
        img.loading = index < 2 ? 'eager' : 'lazy';
        img.decoding = 'async';
        var error = document.createElement('div');
        error.className = 'reader-page-error d-none';
        var message = document.createElement('p');
        if (online) {
          // The server logs why the upstream fetch failed; point there instead of guessing.
          var logs = document.createElement('a');
          logs.href = '/settings#logs';
          logs.className = 'ms-2 small';
          logs.textContent = '查看日志';
          message.append('在线获取这张图片失败，可以重试。', logs);
        } else {
          message.textContent = '这张图片加载失败，可以重试。';
        }
        var retry = document.createElement('button');
        retry.type = 'button';
        retry.className = 'btn btn-outline-primary btn-sm';
        retry.textContent = '重新加载图片';
        error.append(message, retry);
        var src = readerImageUrl(page.url);
        var autoRetried = false;
        function reload() {
          var url = new URL(src);
          url.searchParams.set('retry', Date.now());
          img.src = url.href;
        }
        img.onload = function () { error.classList.add('d-none'); img.classList.remove('d-none'); alignJump(); };
        img.onerror = function () {
          // Online pages come from upstream on demand and a brief hiccup is common: try once more by itself
          // after a short pause before asking the reader to retry.
          if (online && src && !autoRetried) {
            autoRetried = true;
            setTimeout(reload, AUTO_RETRY_MS);
            return;
          }
          img.classList.add('d-none'); error.classList.remove('d-none'); alignJump();
          // 压缩包里这一页坏了：重试没有用，说明原因并给在线阅读（同一章的同一页）；
          // 压缩包已重新打包、这一页不在了：刷新页面拿新的页列表
          if (!online && src) {
            nav.archivePageProblem(src).then(function (problem) {
              if (!problem) return;
              message.textContent = problem.message;
              if (problem.reason === 'archive_busy') return;  // 暂时的：保留“重新加载图片”
              if (problem.reason === 'archive_changed') {
                var again = document.createElement('button');
                again.type = 'button';
                again.className = 'btn btn-outline-primary btn-sm';
                again.textContent = '刷新页面';
                again.addEventListener('click', function () { location.reload(); });
                retry.replaceWith(again);
                return;
              }
              var target = nav.onlinePageUrl(albumId, page);
              var link = document.createElement('a');
              link.className = 'btn btn-outline-primary btn-sm';
              link.href = target.url;
              var globe = document.createElement('i');
              globe.className = 'bi bi-globe2';
              globe.setAttribute('aria-hidden', 'true');
              link.append(globe, target.exact ? ' 在线阅读这一页' : ' 在线阅读');
              retry.replaceWith(link);
            });
          }
        };
        retry.addEventListener('click', function () {
          if (!src) return;
          error.classList.add('d-none');
          img.classList.remove('d-none');
          reload();
        });
        // Only the first page carries the page/chapter caption; later pages keep it in alt text.
        if (index === 0) {
          var caption = document.createElement('figcaption');
          caption.textContent = label + (page.chapter ? ' · ' + page.chapter : '');
          figure.appendChild(caption);
        }
        figure.append(img, error);
        fragment.appendChild(figure);
        if (src) img.src = src;
        else img.onerror();
        if (pageObserver) pageObserver.observe(figure);
      })(pages[rendered], rendered);
    }
    byId('reader-pages').appendChild(fragment);
    byId('reader-more').classList.toggle('d-none', rendered >= pages.length);
    byId('reader-end').classList.toggle('d-none', rendered < pages.length || pages.length === 0);
  }
  function jump(number, alignment) {
    if (!pages.length) return;
    number = Math.max(1, Math.min(pages.length, Math.floor(Number(number)) || 1));
    while (rendered < number) appendPages();
    jumpAnchor = number;
    jumpAlignment = alignment || 'start';
    byId('reader-page-' + number).querySelector('img').loading = 'eager';
    positionJump();
    setCurrent(number);
  }
  function load() {
    if (loading) return;
    loading = true;
    var serial = ++loadSerial;
    byId('reader-empty').classList.add('d-none');
    byId('reader-loading').classList.remove('d-none');
    // Online lists need the album plus every chapter's page list from upstream, so allow longer.
    window.apiFetch((online ? '/api/online/' : '/api/preview/') + encodeURIComponent(albumId),
      { abortKey: 'continuous-reader', timeoutMs: online ? 60000 : 30000 })
      .then(function (data) {
        if (serial !== loadSerial) return;
        if (!Array.isArray(data.pages) || !data.pages.length) throw new Error(online ? '没有可在线阅读的页面。' : '本地没有可阅读的图片。');
        // Read the start page before setCurrent(1) overwrites the remembered one.
        var start = nav.initialPage(positionKey);
        // ?chapter=<photo_id> (chapter links on the detail page) opens at that chapter's first page and
        // becomes ?page=, so later scrolling keeps the address current and a reload resumes in place.
        var params = new URLSearchParams(location.search);
        var chapter = params.get('chapter');
        if (chapter) {
          var first = data.pages.findIndex(function (page) { return page.photo_id === chapter; });
          if (first >= 0) {
            // &offset=n（本地阅读“在线阅读这一页”）：这一章里的第 n+1 页，超出这一章时停在本章最后一页
            var inChapter = data.pages.filter(function (page) { return page.photo_id === chapter; }).length;
            var offset = Math.max(0, Math.min(inChapter - 1, parseInt(params.get('offset'), 10) || 0));
            start = first + 1 + offset;
          }
          try {
            var url = new URL(location.href);
            url.searchParams.delete('chapter');
            url.searchParams.delete('offset');
            url.searchParams.set('page', start);
            history.replaceState(history.state, '', url.pathname + url.search);
          } catch (_) {}
        }
        if (data.skipped_chapters && typeof window.showToast === 'function') {
          window.showToast('有 ' + data.skipped_chapters + ' 个章节暂时无法获取，已跳过', 'warning');
        }
        var title = data.title || (online ? '在线阅读' : '连续阅读');
        pages = data.pages;
        rendered = 0;
        current = 0;
        disconnect();
        byId('reader-pages').replaceChildren();
        byId('reader-title').textContent = title;
        document.title = title + ' - JMComic';
        byId('reader-jump').classList.remove('d-none');
        pageInput.max = pages.length;
        byId('reader-top').disabled = false;
        byId('reader-bottom').disabled = false;
        appendPages();
        setCurrent(1);
        observe();
        // Next frame: the loading card is hidden by then, so the jump measures the final layout.
        if (start > 1) requestAnimationFrame(function () { jump(start); });
      })
      .catch(function (err) {
        if (serial !== loadSerial) return;
        if (err.name === 'AbortError') return;
        byId('reader-empty').classList.remove('d-none');
        if (online) byId('reader-message').textContent = err.message || '在线加载失败，请稍后重试。';
        else byId('reader-message').textContent = err.status === 404
          ? '未找到可阅读的本地图片。请先下载漫画；如果原图已删除，需要重新下载。此按钮不会自动下载。'
          : err.message || '读取失败，请稍后重试。';
        byId('reader-progress').textContent = online ? '在线阅读' : '本地阅读';
      })
      .finally(function () {
        if (serial !== loadSerial) return;
        loading = false;
        byId('reader-loading').classList.add('d-none');
      });
  }
  byId('reader-jump').addEventListener('submit', function (event) {
    event.preventDefault();
    var number = pageInput.value;
    showTools(false);
    jump(number);
  });
  byId('reader-top').addEventListener('click', function () { showTools(false); jump(1, 'top'); });
  byId('reader-bottom').addEventListener('click', function () { showTools(false); jump(pages.length, 'end'); });
  byId('reader-load-more').addEventListener('click', appendPages);
  byId('reader-retry').addEventListener('click', load);
  if (pagedLink) nav.bindModeSwitch(pagedLink, function () { if (current) nav.remember(positionKey, current, false); });
  nav.bindBack(byId('reader-back'));
  window.addEventListener('pagehide', function () {
    // current stays 0 until pages load, so a failed visit never overwrites the remembered page.
    if (current) nav.remember(positionKey, current, false);
    disconnect(); showTools(false); releaseJump(); loading = false; loadSerial += 1;
  });
  window.addEventListener('pageshow', function (event) {
    if (event.persisted) { if (pages.length) observe(); else load(); }
  });
  load();
})();
