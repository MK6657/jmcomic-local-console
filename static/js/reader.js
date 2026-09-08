/* Local-only, continuous reader. No remote image URLs or automatic downloads. */
(function () {
  'use strict';
  var root = document.querySelector('.continuous-reader');
  if (!root) return;
  var albumId = root.getAttribute('data-album-id');
  var pages = [];
  var rendered = 0;
  var current = 1;
  var loading = false;
  var loadSerial = 0;
  var pageObserver;
  var moreObserver;
  var visiblePages = new Set();
  var storageKey = 'jm-reader-page:' + albumId;
  var byId = function (id) { return document.getElementById(id); };
  var tools = byId('reader-tools');
  var jumpAnchor = null;
  var jumpAlignment = 'start';
  var alignFrame = 0;
  function positionJump() {
    if (jumpAlignment === 'top') window.scrollTo({ top: 0, behavior: 'instant' });
    else byId('reader-page-' + jumpAnchor).scrollIntoView({ block: jumpAlignment, behavior: 'instant' });
  }
  function alignJump() {
    if (jumpAnchor === null || alignFrame) return;
    alignFrame = requestAnimationFrame(function () {
      alignFrame = 0;
      if (jumpAnchor !== null) {
        positionJump();
      }
    });
  }
  ['wheel', 'touchstart', 'pointerdown'].forEach(function (name) {
    window.addEventListener(name, function () { jumpAnchor = null; }, { passive: true });
  });

  function showTools(visible, focus) {
    tools.hidden = !visible;
    byId('reader-tools-toggle').setAttribute('aria-expanded', String(visible));
    if (visible && focus) byId('reader-back').focus({ preventScroll: true });
    if (!visible && tools.contains(document.activeElement)) root.focus({ preventScroll: true });
  }
  byId('reader-tools-toggle').addEventListener('click', function () { showTools(tools.hidden, true); });
  byId('reader-tools-close').addEventListener('click', function () { showTools(false); });
  document.addEventListener('click', function (event) {
    if (event.target.closest('a, button, input, select, textarea, nav, footer, .reader-toolbar, .reader-tools')) return;
    if (window.getSelection().toString()) return;
    showTools(tools.hidden);
  });
  document.addEventListener('keydown', function (event) {
    if (['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' '].indexOf(event.key) >= 0) jumpAnchor = null;
    if (event.key === 'Escape') { showTools(false); return; }
    if (event.target.closest('input, textarea, select, [contenteditable="true"]')) return;
    if (event.key.toLowerCase() === 'm' && !event.ctrlKey && !event.altKey && !event.metaKey && !event.repeat) {
      event.preventDefault(); showTools(tools.hidden, true);
    }
  });
  ['wheel', 'touchmove'].forEach(function (name) {
    window.addEventListener(name, function () {
      var editing = tools.contains(document.activeElement) && document.activeElement.matches('input, select, textarea');
      if (!tools.hidden && !editing) showTools(false);
    }, { passive: true });
  });

  function remember() {
    try { sessionStorage.setItem(storageKey, String(current)); } catch (_) {}
  }
  function setCurrent(number) {
    current = number;
    byId('reader-paged').href = '/preview/' + encodeURIComponent(albumId) + '?page=' + current;
    if (new URLSearchParams(location.search).has('page')) {
      try {
        var locationUrl = new URL(location.href);
        locationUrl.searchParams.set('page', current);
        history.replaceState(history.state, '', locationUrl.pathname + locationUrl.search);
      } catch (_) {}
    }
    byId('reader-progress').textContent = '第 ' + current + ' / ' + pages.length + ' 页 · 向下滚动阅读';
    if (document.activeElement !== byId('reader-page-input')) byId('reader-page-input').value = current;
    remember();
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
  function localImageUrl(value) {
    try {
      var url = new URL(value, location.origin);
      return url.origin === location.origin && url.pathname.indexOf('/api/preview-img/') === 0 ? url.href : null;
    } catch (_) { return null; }
  }
  function appendPages() {
    var end = Math.min(rendered + 20, pages.length);
    var fragment = document.createDocumentFragment();
    for (; rendered < end; rendered++) {
      (function (page, index) {
        var figure = document.createElement('figure');
        figure.className = 'reader-page';
        figure.id = 'reader-page-' + (index + 1);
        figure.dataset.page = index + 1;
        figure.setAttribute('aria-label', '第 ' + (index + 1) + ' 页');
        var caption = document.createElement('figcaption');
        caption.textContent = '第 ' + (index + 1) + ' 页' + (page.chapter ? ' · ' + page.chapter : '');
        var img = document.createElement('img');
        img.alt = '第 ' + (index + 1) + ' 页';
        img.loading = index < 2 ? 'eager' : 'lazy';
        img.decoding = 'async';
        var error = document.createElement('div');
        error.className = 'reader-page-error d-none';
        var label = document.createElement('p');
        label.textContent = '这张图片加载失败，可以重试。';
        var retry = document.createElement('button');
        retry.type = 'button';
        retry.className = 'btn btn-outline-primary btn-sm';
        retry.textContent = '重新加载图片';
        error.append(label, retry);
        var src = localImageUrl(page.url);
        img.onload = function () { error.classList.add('d-none'); img.classList.remove('d-none'); alignJump(); };
        img.onerror = function () { img.classList.add('d-none'); error.classList.remove('d-none'); alignJump(); };
        retry.addEventListener('click', function () {
          if (!src) return;
          error.classList.add('d-none');
          img.classList.remove('d-none');
          var url = new URL(src);
          url.searchParams.set('retry', Date.now());
          img.src = url.href;
        });
        if (index === 0) figure.appendChild(caption);
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
    window.apiFetch('/api/preview/' + encodeURIComponent(albumId), { abortKey: 'continuous-reader' })
      .then(function (data) {
        if (serial !== loadSerial) return;
        if (!Array.isArray(data.pages) || !data.pages.length) throw new Error('本地没有可阅读的图片。');
        pages = data.pages;
        rendered = 0;
        disconnect();
        byId('reader-pages').replaceChildren();
        byId('reader-title').textContent = data.title || '连续阅读';
        document.title = (data.title || '连续阅读') + ' - JMComic';
        byId('reader-jump').classList.remove('d-none');
        byId('reader-page-input').max = pages.length;
        byId('reader-top').disabled = false;
        byId('reader-bottom').disabled = false;
        appendPages();
        var saved = 1;
        try { saved = Number(sessionStorage.getItem(storageKey)) || 1; } catch (_) {}
        var requested = Number(new URLSearchParams(location.search).get('page'));
        if (requested > 0 && Number.isFinite(requested)) saved = requested;
        setCurrent(1);
        observe();
        if (saved > 1) requestAnimationFrame(function () { jump(saved); });
      })
      .catch(function (err) {
        if (serial !== loadSerial) return;
        if (err.name === 'AbortError') return;
        byId('reader-empty').classList.remove('d-none');
        byId('reader-message').textContent = err.status === 404
          ? '未找到可阅读的本地图片。请先下载漫画；如果原图已删除，需要重新下载。此按钮不会自动下载。'
          : err.message || '读取失败，请稍后重试。';
        byId('reader-progress').textContent = '本地阅读';
      })
      .finally(function () {
        if (serial !== loadSerial) return;
        loading = false;
        byId('reader-loading').classList.add('d-none');
      });
  }
  byId('reader-jump').addEventListener('submit', function (event) {
    event.preventDefault();
    var number = byId('reader-page-input').value;
    showTools(false);
    jump(number);
  });
  byId('reader-top').addEventListener('click', function () { showTools(false); jump(1, 'top'); });
  byId('reader-bottom').addEventListener('click', function () { showTools(false); jump(pages.length, 'end'); });
  byId('reader-load-more').addEventListener('click', appendPages);
  byId('reader-retry').addEventListener('click', load);
  byId('reader-paged').addEventListener('click', function (event) {
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button !== 0) return;
    event.preventDefault(); remember(); location.replace(this.href);
  });
  byId('reader-back').addEventListener('click', function (event) {
    try {
      if (new URL(document.referrer).origin === location.origin && history.length > 1) {
        event.preventDefault(); history.back();
      }
    } catch (_) {}
  });
  window.addEventListener('pagehide', function () {
    remember(); disconnect(); showTools(false); jumpAnchor = null; loading = false; loadSerial += 1;
  });
  window.addEventListener('pageshow', function (event) {
    if (event.persisted) { if (pages.length) observe(); else load(); }
  });
  load();
})();
