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
    var railStart = 0;
    var cells = new Map();
    var activeThumb = null;
    var spinnerTimer = 0;
    var manifest, cancelImage, imageScope, active;
    var previousRevoke = null;
    var imageInterrupted = false;
    var preloaded = null; // { src, blob, controller }
    var prefixes = ['/api/preview-img/', '/api/preview-archive/'];

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
        if (!active || manifest) return;
        var request = manifest = nav.manifest('/api/preview/' + encodeURIComponent(albumId), 30000);
        request.result
            .then(function (data) {
                if (request !== manifest) return;
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

                $('preview-range').max = pages.length;
                if (pages.length) goPage(nav.initialPage(albumId), true);
                else showError('本地没有找到图片', false);
            })
            .catch(function (err) {
                if (request !== manifest || (err && err.name === 'AbortError')) return;
                if (err && err.status) {
                    // HTTP 错误：保持原有「服务端 message」提示；503（压缩包暂时打不开）另给“重试”
                    showError(err.message || '加载失败', err.status === 503);
                } else {
                    showError('网络错误: ' + (err.message || '未知错误'));
                }
            }).finally(function () { if (request === manifest) manifest = null; });
    }

    // ── 缩略图（分批渲染，点击由容器统一委托） ──
    function renderThumbnails(start) {
        railStart = Math.max(1, Math.min(start, pages.length - 8));
        var last = Math.min(railStart + 8, pages.length);
        cells.forEach(function (cell, number) {
            if (number < railStart || number > last) { cell.cancel(); cell.button.remove(); cells.delete(number); }
        });
        activeThumb = null;
        for (var number = railStart; number <= last; number++) {
            var cell = cells.get(number);
            if (!cell) {
                var button = document.createElement('button');
                button.type = 'button'; button.className = 'thumb-item'; button.dataset.page = number;
                button.setAttribute('aria-label', '第 ' + number + ' 页');
                var image = document.createElement('img'); image.alt = ''; image.decoding = 'async';
                var label = document.createElement('span'); label.textContent = number; label.className = 'thumb-number';
                button.append(image, label);
                var later = Array.from(cells.keys()).filter(function (key) { return key > number; });
                if (later.length) thumbContainer.insertBefore(button, cells.get(Math.min.apply(null, later)).button);
                else thumbContainer.appendChild(button);
                var thumbUrl = nav.imageUrl(pages[number - 1].url, prefixes);
                var thumbCancel = nav.loadImage(image, thumbUrl, function () {});
                cell = {
                    button: button,
                    interrupted: false,
                    pausePending: function () {
                        if (thumbCancel && thumbCancel.abortPending && thumbCancel.abortPending()) cell.interrupted = true;
                    },
                    resumePending: function () {
                        if (cell.interrupted) {
                            cell.interrupted = false;
                            thumbCancel = nav.loadImage(image, thumbUrl, function () {});
                        }
                    },
                    cancel: function () { if (thumbCancel) thumbCancel(); }
                };
                cells.set(number, cell);
            }
            cell.button.classList.toggle('active', number === currentPage);
            if (number === currentPage) { activeThumb = cell.button; activeThumb.setAttribute('aria-current', 'page'); }
            else cell.button.removeAttribute('aria-current');
        }
        $('thumb-prev').disabled = railStart === 1;
        $('thumb-next').disabled = last === pages.length;
        if (activeThumb) centerActiveThumb(); else thumbContainer.scrollTo({ left: 0, behavior: 'instant' });
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
        $('preview-range').value = page;
        jumpInput.value = page;
        $('btn-first').disabled = $('btn-prev').disabled = page <= 1;
        $('btn-next').disabled = $('btn-last').disabled = page >= pages.length;

        showImage(false);
        renderThumbnails(page - 4);
    }

    function clearPreload() {
        if (!preloaded) return;
        if (preloaded.controller) preloaded.controller.abort();
        preloaded = null;
    }

    function preloadNextPage() {
        if (!active || document.hidden || currentPage < 1 || currentPage >= pages.length) return;
        var nextData = pages[currentPage];
        var nextSrc = nextData ? nav.imageUrl(nextData.url, prefixes) : null;
        if (!nextSrc || (preloaded && preloaded.src === nextSrc)) return;
        clearPreload();
        var controller = new AbortController();
        var entry = { src: nextSrc, blob: null, controller: controller };
        preloaded = entry;
        fetch(nextSrc, { signal: controller.signal }).then(function (r) {
            if (controller.signal.aborted || !r.ok) return;
            return r.blob();
        }).then(function (blob) {
            if (!blob || controller.signal.aborted || preloaded !== entry) return;
            entry.blob = blob;
            entry.controller = null;
        }).catch(function () {});
    }

    // 旧图保留到新图加载完成（浏览器会继续显示当前图片），超过 200ms 才显示加载指示，避免翻页闪烁
    function showImage(retry) {
        var pageData = pages[currentPage - 1];
        if (imageScope) imageScope.abort();
        if (cancelImage) {
            if (cancelImage.isLoaded && cancelImage.isLoaded()) {
                if (previousRevoke) previousRevoke();
                previousRevoke = cancelImage.revoke;
            } else {
                cancelImage(true);
            }
            cancelImage = null;
        }
        imageInterrupted = false;
        imageScope = new AbortController();
        var src = nav.imageUrl(pageData.url, prefixes);
        if (retry && src) {
            var url = new URL(src, location.origin);
            url.searchParams.set('retry', Date.now());
            src = url.pathname + url.search;
        }
        imageHint.classList.add('d-none');
        if (!previewImage.getAttribute('src')) previewImage.classList.add('d-none');
        clearTimeout(spinnerTimer);
        spinnerTimer = setTimeout(function () { imgSpinner.classList.remove('d-none'); }, 200);
        previewImage.alt = '第 ' + currentPage + ' 页';

        if (!retry && preloaded && preloaded.src === src && preloaded.blob) {
            var readyBlob = preloaded.blob;
            preloaded = null;
            var objUrl = URL.createObjectURL(readyBlob);
            cancelImage = function (preserveDisplayed) {
                if (!preserveDisplayed) previewImage.removeAttribute('src');
                if (objUrl && !preserveDisplayed) {
                    URL.revokeObjectURL(objUrl);
                    objUrl = null;
                }
            };
            cancelImage.abortPending = function () { return false; };
            cancelImage.revoke = function () { if (objUrl) { URL.revokeObjectURL(objUrl); objUrl = null; } };
            cancelImage.isLoaded = function () { return true; };
            previewImage.src = objUrl;
            return;
        }
        cancelImage = nav.loadImage(previewImage, src, imageFailed, { keepPrevious: true });
    }

    previewImage.addEventListener('load', function () {
        if (previousRevoke) {
            previousRevoke();
            previousRevoke = null;
        }
        clearTimeout(spinnerTimer);
        imgSpinner.classList.add('d-none');
        imageHint.classList.add('d-none');
        previewImage.classList.remove('d-none');
        preloadNextPage();
    });
    previewImage.addEventListener('error', function () { if (previewImage.getAttribute('src')) imageFailed(); });
    function imageFailed(response) {
        if (!active) return;
        var src = nav.imageUrl(pages[currentPage - 1].url, prefixes);
        var scope = imageScope;
        var page = currentPage;
        showImageHint('图片加载失败', true);
        // 压缩包里这一页坏了：重试没有用，说明原因并给在线阅读（同一章的同一页）；
        // 压缩包已重新打包、这一页不在了：刷新页面拿新的页列表
        if (!src || response === null) return;
        var explanation = response ? nav.archiveResponse(src.includes('/api/preview-archive/'), response) :
            nav.archivePageProblem(new Request(src, { signal: scope.signal }));
        explanation.then(function (problem) {
            if (!problem || scope.signal.aborted || page !== currentPage) return;
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
    }

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
    });
    $('thumb-prev').addEventListener('click', function () { renderThumbnails(railStart - 9); });
    $('thumb-next').addEventListener('click', function () { renderThumbnails(railStart + 9); });
    $('preview-range').addEventListener('input', function (event) { $('page-current').textContent = event.target.value; });
    $('preview-range').addEventListener('change', function (event) { goPage(event.target.value); });
    var cycleWidth = nav.bindWidthToggle
        ? nav.bindWidthToggle(container, $('preview-width-btn'), function () { centerActiveThumb(); })
        : null;
    var tools = $('reader-tools');
    function showTools(open) {
        tools.hidden = !open;
        $('preview-tools-toggle').setAttribute('aria-expanded', String(open));
        if (!open && tools.contains(document.activeElement)) container.focus({ preventScroll: true });
    }
    $('preview-tools-toggle').addEventListener('click', function () { showTools(tools.hidden); });
    $('preview-tools-close').addEventListener('click', function () { showTools(false); });
    $('image-wrapper').addEventListener('click', function (event) {
        if (event.target.closest('a,button') || window.getSelection().toString()) return;
        var rect = event.currentTarget.getBoundingClientRect();
        var fraction = (event.clientX - rect.left) / rect.width;
        if (fraction < 1 / 3) goPage(currentPage - 1);
        else if (fraction > 2 / 3) goPage(currentPage + 1);
        else showTools(tools.hidden);
    });

    // 方向键翻页；带修饰键时交给浏览器（Alt+← 是后退），输入框内不拦截
    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') { showTools(false); return; }
        if (!nav.plainKey(e) || !pages.length) return;
        if (e.key.toLowerCase() === 'w' && !e.repeat && cycleWidth) { e.preventDefault(); cycleWidth(); return; }
        if (e.key.toLowerCase() === 'm' && !e.repeat) { e.preventDefault(); showTools(tools.hidden); return; }
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

    nav.bindModeSwitch(continuousLink);
    nav.bindBack($('preview-back'));

    // ── 启动 ──
    if (albumId) nav.bindLifecycle(function (unloading) {
        active = false;
        clearTimeout(spinnerTimer);
        imgSpinner.classList.add('d-none');
        clearPreload();
        if (manifest) { manifest.cancel(); manifest = null; }
        if (imageScope) imageScope.abort();
        if (unloading) {
            imageInterrupted = false;
            if (previousRevoke) { previousRevoke(); previousRevoke = null; }
            if (cancelImage) { cancelImage(); cancelImage = null; }
            cells.forEach(function (cell) { cell.cancel(); cell.button.remove(); });
            cells.clear();
            activeThumb = null;
        } else {
            if (cancelImage && cancelImage.abortPending && cancelImage.abortPending()) {
                imageInterrupted = true;
                cancelImage = null;
            }
            cells.forEach(function (cell) { cell.pausePending(); });
        }
    }, function () {
        active = true;
        if (currentPage) {
            cells.forEach(function (cell) { cell.resumePending(); });
            if (imageInterrupted || (!cancelImage && !previewImage.getAttribute('src'))) {
                showImage(false);
            } else {
                preloadNextPage();
            }
            if (!cells.size) renderThumbnails(currentPage - 4);
        } else {
            loadPreview();
        }
    });
    else showError('缺少 album_id 参数');

})();
