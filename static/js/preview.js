/**
 * 图片预览页（单页翻页）JavaScript
 * 依赖: utils.js (window.apiFetch, window.escapeHtmlAttr)、reading-nav.js (window.readingNav)
 * album_id 取自 .preview-container[data-album-id]
 */
(function () {
    'use strict';

    var container = document.querySelector('.preview-container');
    if (!container) return;
    var albumId = container.getAttribute('data-album-id');
    var nav = window.readingNav;

    // ── 状态 ──
    var pages = [];          // {page, url, chapter}，page 与下标一一对应（page = 下标 + 1）
    var currentPage = 0;
    var THUMBNAILS_PER_PAGE = 100;
    var thumbnailsLoaded = 0;
    var activeThumb = null;
    var spinnerTimer = 0;
    var preloader = new Image();   // 预读下一页，翻页时直接命中缓存

    // ── DOM 引用 ──
    var $ = function (id) { return document.getElementById(id); };
    var previewImage = $('preview-image');
    var imgSpinner = $('img-spinner');
    var imageHint = $('no-image-hint');
    var thumbContainer = $('thumb-container');
    var jumpInput = $('page-jump-input');
    var continuousLink = $('preview-continuous');

    // ── 加载数据 ──
    function loadPreview() {
        // 统一走 apiFetch（自带 AbortController/超时/pagehide 清理）
        window.apiFetch('/api/preview/' + encodeURIComponent(albumId))
            .then(function (data) {
                if (data.status !== 'ok') {
                    showError(data.message || '加载失败');
                    return;
                }
                pages = data.pages || [];
                var albumTitle = data.title || '';

                // 显示阅读器
                $('preview-loading').classList.add('d-none');
                $('reader-content').classList.remove('d-none');
                $('album-title').textContent = albumTitle;
                $('page-total').textContent = pages.length;
                jumpInput.max = pages.length;
                document.title = albumTitle + ' - JMComic 图片预览';

                renderThumbnails();
                if (pages.length) goPage(nav.initialPage(albumId), true);
                else showImageHint('本地没有找到图片', false);
            })
            .catch(function (err) {
                if (err && err.name === 'AbortError') return; // pagehide 中止，静默
                if (err && err.status) {
                    // HTTP 错误：保持原有「服务端 message」提示；503（压缩包暂时打不开）另给“重试”
                    showError(err.message || '加载失败', err.status === 503);
                } else {
                    showError('网络错误: ' + (err.message || '未知错误'));
                }
            });
    }

    // ── 缩略图（分批渲染，点击由容器统一委托） ──
    function renderThumbnails() {
        thumbContainer.innerHTML = '';
        thumbnailsLoaded = 0;
        activeThumb = null;
        loadMoreThumbnails();
    }

    function loadMoreThumbnails() {
        var oldBtn = thumbContainer.querySelector('.load-more-item');
        var hadFocus = oldBtn && oldBtn === document.activeElement;
        if (oldBtn) oldBtn.remove();

        var start = thumbnailsLoaded;
        var end = Math.min(start + THUMBNAILS_PER_PAGE, pages.length);
        var html = '';
        for (var i = start; i < end; i++) {
            html += '<button type="button" class="thumb-item" data-page="' + (i + 1)
                  + '" aria-label="第 ' + (i + 1) + ' 页" title="第 ' + (i + 1) + ' 页">'
                  + '<img src="' + window.escapeHtmlAttr(pages[i].url) + '" alt="" loading="lazy" decoding="async">'
                  + '<span class="thumb-number">' + (i + 1) + '</span></button>';
        }
        thumbContainer.insertAdjacentHTML('beforeend', html);
        thumbnailsLoaded = end;

        if (thumbnailsLoaded < pages.length) {
            var loadBtn = document.createElement('button');
            loadBtn.type = 'button';
            loadBtn.className = 'load-more-item';
            loadBtn.textContent = '加载更多';
            loadBtn.title = '剩余 ' + (pages.length - thumbnailsLoaded) + ' 页缩略图';
            thumbContainer.appendChild(loadBtn);
        }
        // 键盘用户点了「加载更多」后，焦点落在新一批的第一张上，而不是丢回页面顶部
        if (hadFocus) {
            var firstNew = thumbContainer.querySelector('.thumb-item[data-page="' + (start + 1) + '"]');
            if (firstNew) firstNew.focus({ preventScroll: true });
        }
    }

    function highlightThumb(page) {
        while (thumbnailsLoaded < page) loadMoreThumbnails();
        if (activeThumb) {
            activeThumb.classList.remove('active');
            activeThumb.removeAttribute('aria-current');
        }
        activeThumb = thumbContainer.querySelector('.thumb-item[data-page="' + page + '"]');
        if (!activeThumb) return;
        activeThumb.classList.add('active');
        activeThumb.setAttribute('aria-current', 'page');
        centerActiveThumb();
    }

    function centerActiveThumb() {
        if (!activeThumb) return;
        var bounds = activeThumb.getBoundingClientRect();
        var rail = thumbContainer.getBoundingClientRect();
        thumbContainer.scrollTo({ left: thumbContainer.scrollLeft + bounds.left - rail.left - (rail.width - bounds.width) / 2, behavior: 'instant' });
    }

    // ── 翻页 ──
    function goPage(page, force) {
        if (!pages.length) return;
        page = Math.min(pages.length, Math.max(1, Math.floor(Number(page)) || 1));
        if (page === currentPage && !force) return;
        currentPage = page;
        nav.remember(albumId, page, true);
        continuousLink.href = '/read/' + encodeURIComponent(albumId) + '?page=' + page;

        $('page-current').textContent = page;
        jumpInput.value = page;
        $('btn-first').disabled = $('btn-prev').disabled = page <= 1;
        $('btn-next').disabled = $('btn-last').disabled = page >= pages.length;

        showImage(false);
        highlightThumb(page);
    }

    // 旧图保留到新图加载完成（浏览器会继续显示当前图片），超过 200ms 才显示加载指示，避免翻页闪烁
    function showImage(retry) {
        var pageData = pages[currentPage - 1];
        var src = pageData.url;
        if (retry) {
            var url = new URL(src, location.origin);
            url.searchParams.set('retry', Date.now());
            src = url.pathname + url.search;
        }
        imageHint.classList.add('d-none');
        clearTimeout(spinnerTimer);
        spinnerTimer = setTimeout(function () { imgSpinner.classList.remove('d-none'); }, 200);
        previewImage.alt = '第 ' + currentPage + ' 页';
        previewImage.src = src;
    }

    previewImage.addEventListener('load', function () {
        clearTimeout(spinnerTimer);
        imgSpinner.classList.add('d-none');
        imageHint.classList.add('d-none');
        previewImage.classList.remove('d-none');
        var next = pages[currentPage];
        if (next) preloader.src = next.url;
    });
    previewImage.addEventListener('error', function () {
        var src = previewImage.getAttribute('src');
        if (!src) return;
        var page = currentPage;
        showImageHint('图片加载失败', true);
        // 压缩包里这一页坏了：重试没有用，说明原因并给在线阅读（同一章的同一页）；
        // 压缩包已重新打包、这一页不在了：刷新页面拿新的页列表
        nav.archivePageProblem(src).then(function (problem) {
            if (!problem || page !== currentPage) return;
            if (problem.reason === 'archive_busy') {
                showImageHint(problem.message, true);  // 暂时的：保留“重试”
                return;
            }
            if (problem.reason === 'archive_changed') {
                showImageHint(problem.message, false, null, true);
                return;
            }
            showImageHint(problem.message, false, nav.onlinePageUrl(albumId, pages[page - 1]));
        });
    });

    // online：{url, exact}（nav.onlinePageUrl）时加“在线阅读（这一页）”；reload 为 true 时加“刷新页面”
    function showImageHint(message, canRetry, online, reload) {
        clearTimeout(spinnerTimer);
        imgSpinner.classList.add('d-none');
        previewImage.classList.add('d-none');
        var icon = document.createElement('i');
        icon.className = 'bi bi-exclamation-circle';
        icon.setAttribute('aria-hidden', 'true');
        var text = document.createElement('p');
        text.className = 'mt-2 mb-0';
        text.textContent = message;
        imageHint.replaceChildren(icon, text);
        if (canRetry) {
            var retry = document.createElement('button');
            retry.type = 'button';
            retry.className = 'btn btn-sm btn-outline-primary mt-2';
            retry.innerHTML = '<i class="bi bi-arrow-clockwise" aria-hidden="true"></i> 重试';
            retry.addEventListener('click', function () { showImage(true); });
            imageHint.appendChild(retry);
        }
        if (online) {
            var link = document.createElement('a');
            link.className = 'btn btn-sm btn-outline-primary mt-2';
            link.href = online.url;
            var globe = document.createElement('i');
            globe.className = 'bi bi-globe2';
            globe.setAttribute('aria-hidden', 'true');
            link.append(globe, online.exact ? ' 在线阅读这一页' : ' 在线阅读');
            imageHint.appendChild(link);
        }
        if (reload) {
            var again = document.createElement('button');
            again.type = 'button';
            again.className = 'btn btn-sm btn-outline-primary mt-2';
            again.textContent = '刷新页面';
            again.addEventListener('click', function () { location.reload(); });
            imageHint.appendChild(again);
        }
        imageHint.classList.remove('d-none');
    }

    // ── 错误 ──
    function showError(msg, canRetry) {
        // 标题不再停在“加载中...”；具体原因在下面的错误提示里
        $('album-title').textContent = '无法预览本地文件';
        document.title = '无法预览本地文件 - JMComic 图片预览';
        $('preview-loading').classList.add('d-none');
        $('reader-content').classList.add('d-none');
        $('preview-error').classList.remove('d-none');
        $('error-message').textContent = msg;
        $('preview-retry').classList.toggle('d-none', !canRetry);
    }
    $('preview-retry').addEventListener('click', function () {
        $('preview-error').classList.add('d-none');
        $('preview-loading').classList.remove('d-none');
        loadPreview();
    });

    // ── 事件绑定 ──
    $('btn-first').addEventListener('click', function () { goPage(1); });
    $('btn-prev').addEventListener('click', function () { goPage(currentPage - 1); });
    $('btn-next').addEventListener('click', function () { goPage(currentPage + 1); });
    $('btn-last').addEventListener('click', function () { goPage(pages.length); });
    $('page-jump').addEventListener('submit', function (e) {
        e.preventDefault();
        goPage(jumpInput.value);
    });
    thumbContainer.addEventListener('click', function (e) {
        var thumb = e.target.closest('.thumb-item');
        if (thumb) goPage(Number(thumb.getAttribute('data-page')));
        else if (e.target.closest('.load-more-item')) loadMoreThumbnails();
    });
    $('thumb-prev').addEventListener('click', function () { thumbContainer.scrollBy({ left: -thumbContainer.clientWidth * 0.8, behavior: 'instant' }); });
    $('thumb-next').addEventListener('click', function () { thumbContainer.scrollBy({ left: thumbContainer.clientWidth * 0.8, behavior: 'instant' }); });

    // 方向键翻页；带修饰键时交给浏览器（Alt+← 是后退），输入框内不拦截
    document.addEventListener('keydown', function (e) {
        if (e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey || e.shiftKey || !pages.length) return;
        if (e.target.closest('input, textarea, select, [contenteditable="true"]')) return;
        var target = { ArrowLeft: currentPage - 1, ArrowRight: currentPage + 1, Home: 1, End: pages.length }[e.key];
        if (target === undefined) return;
        e.preventDefault();
        goPage(target);
    });

    var resizeFrame = 0;
    window.addEventListener('resize', function () {
        if (resizeFrame) return;
        resizeFrame = requestAnimationFrame(function () { resizeFrame = 0; centerActiveThumb(); });
    });
    window.addEventListener('pageshow', function (event) { if (event.persisted && !pages.length) loadPreview(); });

    nav.bindModeSwitch(continuousLink);
    nav.bindBack($('preview-back'));

    // ── 启动 ──
    if (albumId) loadPreview();
    else showError('缺少 album_id 参数');

})();
