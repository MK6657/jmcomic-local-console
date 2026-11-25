(function () {
  'use strict';
  var root = document.querySelector('.continuous-reader');
  if (!root) return;
  var byId = function (id) { return document.getElementById(id); };
  var albumId = root.getAttribute('data-album-id');
  var online = root.getAttribute('data-source') === 'online';
  var positionKey = online ? 'online:' + albumId : albumId;
  var prefixes = online ? ['/api/online-img/'] : ['/api/preview-img/', '/api/preview-archive/'];
  var AUTO_RETRY_MS = 3000;
  var nav = window.readingNav;
  var stage = byId('reader-pages');
  var tools = byId('reader-tools');
  var toggle = byId('reader-tools-toggle');
  var input = byId('reader-page-input');
  var range = byId('reader-range');
  var paged = byId('reader-paged');
  var pages = [], ratios = [], offsets = [], mounted = new Map();
  var current = 0, width = 0, frame = 0, dirty = false, active = false;
  var anchor = null, baseline = 0, manifest = null;
  var footerHeight = 32, gutter = 24;

  function showTools(open, focus) {
    tools.hidden = !open;
    toggle.setAttribute('aria-expanded', String(open));
    if (open && focus) byId('reader-back').focus({ preventScroll: true });
    if (!open && tools.contains(document.activeElement)) root.focus({ preventScroll: true });
  }
  function setCurrent(number, bookmark) {
    current = number;
    var page = pages[number - 1];
    if (paged) paged.href = '/preview/' + encodeURIComponent(albumId) + '?page=' + number;
    byId('reader-progress').textContent = '第 ' + number + ' / ' + pages.length + ' 页';
    if (document.activeElement !== input || bookmark) input.value = number;
    range.value = number;
    byId('reader-prev').disabled = number === 1;
    byId('reader-next').disabled = number === pages.length;
    stage.setAttribute('aria-label', (page.chapter || '漫画页面') + ' · 第 ' + number + ' 页');
    nav.remember(positionKey, number, bookmark);
  }
  function measure() {
    width = stage.clientWidth || 700;
    offsets = [0];
    for (var index = 0; index < pages.length; index++) {
      offsets.push(offsets[index] + Math.max(160, Math.round(width * (ratios[index] || 1.4))) + footerHeight + gutter);
    }
    stage.style.height = (offsets[pages.length] - gutter) + 'px';
    dirty = false;
  }
  function pageAt(pixel) {
    var low = 1, high = pages.length;
    while (low < high) {
      var mid = Math.floor((low + high) / 2);
      if (offsets[mid] <= pixel) low = mid + 1; else high = mid;
    }
    return low;
  }
  function mount(number) {
    var page = pages[number - 1];
    var figure = document.createElement('figure');
    figure.className = 'reader-page';
    figure.id = 'reader-page-' + number;
    figure.dataset.page = number;
    var img = document.createElement('img');
    img.alt = '第 ' + number + ' 页';
    img.decoding = 'async';
    var caption = document.createElement(number === 1 ? 'figcaption' : 'div');
    caption.className = 'folio-foot';
    var chapter = document.createElement('span');
    chapter.textContent = page.chapter || (online ? '在线展阅' : '本地典藏');
    var count = document.createElement('span');
    count.textContent = 'FOLIO ' + String(number).padStart(3, '0') + ' / ' + pages.length;
    caption.append(chapter, count);
    var error = document.createElement('div');
    error.className = 'reader-page-error d-none';
    var message = document.createElement('p');
    if (online) {
      var logs = document.createElement('a');
      logs.href = '/settings#logs';
      logs.className = 'ms-2 small';
      logs.textContent = '查看日志';
      message.append('在线获取这张图片失败，可以重试。', logs);
    } else message.textContent = '这张图片加载失败，可以重试。';
    var retry = document.createElement('button');
    retry.type = 'button';
    retry.className = 'btn btn-outline-primary btn-sm';
    retry.textContent = '重新加载图片';
    error.append(message, retry);
    figure.append(img, error, caption);
    var following = Array.from(mounted.keys()).filter(function (key) { return key > number; });
    if (following.length) stage.insertBefore(figure, mounted.get(Math.min.apply(null, following)).figure);
    else stage.appendChild(figure);
    var src = nav.imageUrl(page.url, prefixes);
    var cancel, timer, response, autoRetried = false, interrupted = false;
    var scope = new AbortController();
    function request(url) {
      if (cancel) cancel();
      interrupted = false;
      response = undefined;
      cancel = nav.loadImage(img, url, function (failure) { response = failure; img.onerror(); });
    }
    function reload() {
      if (!src || scope.signal.aborted) return;
      var url = new URL(src);
      url.searchParams.set('retry', Date.now());
      request(url.href);
    }
    img.onload = function () {
      if (!img.naturalWidth || scope.signal.aborted) return;
      var ratio = img.naturalHeight / img.naturalWidth;
      if (ratios[number - 1] !== ratio) { ratios[number - 1] = ratio; dirty = true; schedule(); }
    };
    img.onerror = function () {
      if (online && src && !autoRetried) {
        autoRetried = true;
        timer = setTimeout(reload, AUTO_RETRY_MS);
        return;
      }
      img.classList.add('d-none'); error.classList.remove('d-none');
      if (online || !src || response === null) return;
      var problem = response ? nav.archiveResponse(src.includes('/api/preview-archive/'), response) :
        nav.archivePageProblem(new Request(src, { signal: scope.signal }));
      problem.then(function (info) {
        if (!info || scope.signal.aborted || !retry.parentNode) return;
        message.textContent = info.message;
        if (info.reason === 'archive_busy') return;
        if (info.reason === 'archive_changed') {
          var again = document.createElement('button');
          again.type = 'button'; again.className = retry.className; again.textContent = '刷新页面';
          again.addEventListener('click', function () { location.reload(); });
          retry.replaceWith(again);
        } else {
          var target = nav.onlinePageUrl(albumId, page);
          var link = document.createElement('a');
          link.href = target.url; link.className = retry.className;
          link.textContent = target.exact ? '在线阅读这一页' : '在线阅读';
          retry.replaceWith(link);
        }
      });
    };
    retry.addEventListener('click', function () {
      error.classList.add('d-none'); img.classList.remove('d-none'); reload();
    });
    request(src);
    return {
      figure: figure,
      pausePending: function () {
        clearTimeout(timer);
        if (cancel && cancel.abortPending && cancel.abortPending()) interrupted = true;
      },
      resumePending: function () {
        if (interrupted && !scope.signal.aborted) {
          interrupted = false;
          request(src);
        }
      },
      remove: function () {
        clearTimeout(timer); scope.abort(); if (cancel) cancel(); img.onload = img.onerror = null; figure.remove();
      }
    };
  }
  function render(number, visibleEnd) {
    var first = Math.max(1, number - 5);
    var last = Math.min(pages.length, Math.max(first + 8, visibleEnd + 2, number + 3));
    mounted.forEach(function (slot, key) {
      if (key < first || key > last) { slot.remove(); mounted.delete(key); }
    });
    for (var key = first; key <= last; key++) {
      if (!mounted.has(key)) mounted.set(key, mount(key));
      var style = mounted.get(key).figure.style;
      style.top = offsets[key - 1] + 'px';
      style.height = (offsets[key] - offsets[key - 1] - gutter) + 'px';
    }
  }
  function align() {
    if (anchor === 'top') window.scrollTo({ top: 0, behavior: 'instant' });
    else mounted.get(current).figure.scrollIntoView({ block: anchor, behavior: 'instant' });
    baseline = window.scrollY;
  }
  function update() {
    frame = 0;
    if (!pages.length || !active) return;
    var pixel = 16 - stage.getBoundingClientRect().top;
    var was = pageAt(pixel);
    if (anchor && Math.abs(window.scrollY - baseline) > 2) anchor = null;
    var changed = dirty || width !== stage.clientWidth;
    if (changed) {
      var previousTop = offsets[was - 1];
      measure();
      if (!anchor && pixel > 0 && offsets[was - 1] !== previousTop) {
        window.scrollBy({ top: offsets[was - 1] - previousTop, behavior: 'instant' });
        pixel = 16 - stage.getBoundingClientRect().top;
      }
    }
    var number = anchor ? current : pageAt(pixel);
    render(number, pageAt((anchor ? offsets[current - 1] : pixel) + (window.innerHeight || 0)));
    if (anchor && changed) align();
    if (number !== current) setCurrent(number, false);
  }
  function schedule() { if (active && !frame) frame = requestAnimationFrame(update); }
  function jump(value, alignment, bookmark) {
    if (!active || !pages.length) return;
    var number = Math.max(1, Math.min(pages.length, Math.floor(Number(value)) || 1));
    if (dirty || !offsets.length || width !== stage.clientWidth) measure();
    anchor = alignment || 'start';
    render(number, pageAt(offsets[number - 1] + (window.innerHeight || 0)));
    setCurrent(number, bookmark);
    align();
  }
  function load() {
    if (!active || manifest) return;
    byId('reader-empty').classList.add('d-none');
    byId('reader-loading').classList.remove('d-none');
    var request = manifest = nav.manifest((online ? '/api/online/' : '/api/preview/') + encodeURIComponent(albumId), online ? 60000 : 30000);
    request.result.then(function (data) {
      if (request !== manifest) return;
      if (!Array.isArray(data.pages) || !data.pages.length) throw new Error(online ? '没有可在线阅读的页面。' : '本地没有可阅读的图片。');
      pages = data.pages;
      var start = nav.initialPage(positionKey);
      var params = new URLSearchParams(location.search), chapter = params.get('chapter');
      if (chapter) {
        var first = pages.findIndex(function (page) { return page.photo_id === chapter; });
        if (first >= 0) {
          var count = pages.filter(function (page) { return page.photo_id === chapter; }).length;
          start = first + 1 + Math.max(0, Math.min(count - 1, parseInt(params.get('offset'), 10) || 0));
        }
        var url = new URL(location.href);
        url.searchParams.delete('chapter'); url.searchParams.delete('offset'); url.searchParams.set('page', start);
        history.replaceState(history.state, '', url.pathname + url.search);
      }
      if (data.skipped_chapters && window.showToast) window.showToast('有 ' + data.skipped_chapters + ' 个章节暂时无法获取，已跳过', 'warning');
      var title = data.title || (online ? '在线阅读' : '连续阅读');
      byId('reader-title').textContent = title; document.title = title + ' - JMComic';
      input.max = range.max = pages.length; range.disabled = false;
      byId('reader-top').disabled = byId('reader-bottom').disabled = false;
      byId('reader-jump').classList.remove('d-none'); byId('reader-end').classList.remove('d-none');
      byId('reader-loading').classList.add('d-none');
      jump(start, start <= 1 ? 'top' : 'start', false);
    }).catch(function (err) {
      if (request !== manifest || err.name === 'AbortError') return;
      byId('reader-loading').classList.add('d-none'); byId('reader-empty').classList.remove('d-none');
      byId('reader-message').textContent = !online && err.status === 404 ? '未找到可阅读的本地图片。请先下载漫画；如果原图已删除，需要重新下载。此按钮不会自动下载。' : err.message || '读取失败，请稍后重试。';
      byId('reader-progress').textContent = online ? '在线阅读' : '本地阅读';
    }).finally(function () { if (request === manifest) manifest = null; });
  }
  toggle.addEventListener('click', function () { showTools(tools.hidden, true); });
  byId('reader-tools-close').addEventListener('click', function () { showTools(false); });
  stage.addEventListener('click', function (event) {
    if (!event.target.closest('a,button') && !window.getSelection().toString()) showTools(tools.hidden);
  });
  var cycleWidth = nav.bindWidthToggle
    ? nav.bindWidthToggle(root, byId('reader-width-btn'), function () { dirty = true; schedule(); })
    : null;
  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape') { showTools(false); return; }
    if (!nav.plainKey(event)) return;
    if (event.key.toLowerCase() === 'w' && !event.repeat && cycleWidth) { event.preventDefault(); cycleWidth(); return; }
    var dest = { ArrowLeft: current - 1, ArrowRight: current + 1, Home: 1, End: pages.length }[event.key];
    if (dest !== undefined && pages.length) {
      event.preventDefault(); jump(dest, event.key === 'End' ? 'end' : event.key === 'Home' ? 'top' : 'start', true);
    } else if (event.key.toLowerCase() === 'm' && !event.repeat) { event.preventDefault(); showTools(tools.hidden, true); }
  });
  ['wheel', 'touchmove'].forEach(function (name) {
    window.addEventListener(name, function (event) {
      if (!tools.contains(event.target) && !tools.hidden && document.activeElement !== input) showTools(false);
    }, { passive: true });
  });
  window.addEventListener('scroll', schedule, { passive: true }); window.addEventListener('resize', schedule);
  byId('reader-jump').addEventListener('submit', function (event) { event.preventDefault(); showTools(false); jump(input.value, 'start', true); });
  range.addEventListener('input', function () { byId('reader-progress').textContent = '第 ' + range.value + ' / ' + pages.length + ' 页'; });
  range.addEventListener('change', function () { jump(range.value, 'start', true); });
  byId('reader-top').addEventListener('click', function () { showTools(false); jump(1, 'top', true); });
  byId('reader-bottom').addEventListener('click', function () { showTools(false); jump(pages.length, 'end', true); });
  byId('reader-prev').addEventListener('click', function () { jump(current - 1, 'start', true); });
  byId('reader-next').addEventListener('click', function () { jump(current + 1, 'start', true); });
  byId('reader-retry').addEventListener('click', load);
  if (paged) nav.bindModeSwitch(paged);
  nav.bindBack(byId('reader-back')); nav.bindBack(byId('reader-origin'));
  nav.bindLifecycle(function (unloading) {
    active = false;
    if (frame) cancelAnimationFrame(frame); frame = 0;
    if (manifest) { manifest.cancel(); manifest = null; }
    if (unloading) {
      mounted.forEach(function (slot) { slot.remove(); });
      mounted.clear();
    } else {
      mounted.forEach(function (slot) { slot.pausePending(); });
    }
  }, function () {
    active = true;
    mounted.forEach(function (slot) { slot.resumePending(); });
    if (current) update(); else load();
  });
})();
