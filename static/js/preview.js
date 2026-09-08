/**
 * 图片预览页 JavaScript
 * 依赖: utils.js (window.escapeHtml, window.escapeHtmlAttr)
 * 外部变量: albumId (由模板内联 <script> 注入)
 */
(function () {
    'use strict';

    // ── 状态 ──
    var pages = [];          // {page, url, chapter}
    var totalPages = 0;
    window.currentPage = 0;
    var albumTitle = '';
    var THUMBNAILS_PER_PAGE = 100;
    var thumbnailsLoaded = 0;

    // ── DOM 引用 ──
    var $ = function (id) { return document.getElementById(id); };
    var previewImage = $('preview-image');
    var imgSpinner = $('img-spinner');
    var noImageHint = $('no-image-hint');
    var imgWrapper = $('image-wrapper');
    var albumTitleEl = $('album-title');
    var pageCurrent = $('page-current');
    var pageTotal = $('page-total');
    var thumbContainer = $('thumb-container');
    var loadingEl = $('preview-loading');
    var readerContent = $('reader-content');
    var errorEl = $('preview-error');
    var errorMsg = $('error-message');
    var jumpInput = $('page-jump-input');

    // 暴露给 HTML onclick
    window.goPage = goPage;
    window.jumpToPage = jumpToPage;
    window.onImageError = onImageError;

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
                totalPages = data.total_pages || 0;
                albumTitle = data.title || '';

                // 显示阅读器
                loadingEl.classList.add('d-none');
                readerContent.classList.remove('d-none');
                albumTitleEl.textContent = albumTitle;

                // 更新页面信息
                pageTotal.textContent = totalPages;
                document.title = albumTitle + ' - JMComic 图片预览';

                // 生成缩略图
                renderThumbnails();

                // 跳转到第一页
                if (totalPages > 0) {
                    goPage(1);
                } else {
                    noImageHint.style.display = 'block';
                }
            })
            .catch(function (err) {
                if (err && err.name === 'AbortError') return; // pagehide 中止，静默
                if (err && err.status) {
                    // HTTP 错误：保持原有「服务端 message」提示
                    showError(err.message || '加载失败');
                } else {
                    showError('网络错误: ' + (err.message || '未知错误'));
                }
            });
    }

    // ── 渲染缩略图（分页加载） ──
    function renderThumbnails() {
        thumbContainer.innerHTML = '';
        thumbnailsLoaded = 0;
        loadMoreThumbnails();
    }

    function loadMoreThumbnails() {
        // 移除旧的"加载更多"按钮（如果存在）
        var oldBtn = thumbContainer.querySelector('.load-more-item');
        if (oldBtn) oldBtn.remove();

        var start = thumbnailsLoaded;
        var end = Math.min(start + THUMBNAILS_PER_PAGE, pages.length);
        var html = '';
        for (var i = start; i < end; i++) {
            var p = pages[i];
            html += '<div class="thumb-item" data-page="' + window.escapeHtmlAttr(String(p.page))
                  + '" onclick="goPage(' + window.escapeHtmlAttr(String(p.page)) + ')" title="第 ' + window.escapeHtmlAttr(String(p.page)) + ' 页">'
                  + '<img src="' + window.escapeHtmlAttr(p.url) + '" alt="第' + p.page + '页" loading="lazy" />'
                  + '</div>';
        }
        // 用 insertAdjacentHTML 追加而非覆盖
        thumbContainer.insertAdjacentHTML('beforeend', html);
        thumbnailsLoaded = end;

        // 如果还有更多，追加"加载更多"按钮
        if (thumbnailsLoaded < pages.length) {
            var remaining = pages.length - thumbnailsLoaded;
            var loadBtn = document.createElement('div');
            loadBtn.className = 'load-more-item';
            loadBtn.innerHTML = '<i class="bi bi-plus-circle" title="点击加载更多（剩余 ' + remaining + ' 页）"></i>';
            loadBtn.addEventListener('click', loadMoreThumbnails);
            thumbContainer.appendChild(loadBtn);
        }
    }

    // ── 翻页 ──
    function goPage(page) {
        // 边界检查
        if (totalPages === 0) return;
        if (page < 1) page = 1;
        if (page > totalPages) page = totalPages;

        if (page === currentPage) return;
        currentPage = page;

        // 更新页码显示
        pageCurrent.textContent = currentPage;
        jumpInput.value = currentPage;

        // 查找对应页
        var pageData = null;
        for (var i = 0; i < pages.length; i++) {
            if (pages[i].page === currentPage) {
                pageData = pages[i];
                break;
            }
        }

        if (!pageData) {
            showImageError('页面数据丢失');
            return;
        }

        // ── 清除旧图片，释放内存 ──
        previewImage.onload = null;
        previewImage.onerror = null;
        previewImage.removeAttribute('src');
        // 清空 src 后强制重置，避免浏览器缓存旧图片数据
        previewImage.src = '';

        // 显示加载中
        imgSpinner.classList.remove('d-none');
        previewImage.style.display = 'none';
        noImageHint.style.display = 'none';

        // 加载图片
        previewImage.src = pageData.url;
        previewImage.onload = function () {
            imgSpinner.classList.add('d-none');
            previewImage.style.display = 'inline';
        };
        previewImage.onerror = function () {
            imgSpinner.classList.add('d-none');
            previewImage.style.display = 'none';
            noImageHint.innerHTML = '<i class="bi bi-exclamation-circle" style="font-size:3rem;"></i>'
                                  + '<p class="mt-2">图片加载失败</p>'
                                  + '<button class="btn btn-sm btn-outline-light mt-2" onclick="goPage(' + currentPage + ')">'
                                  + '<i class="bi bi-arrow-clockwise"></i> 重试</button>';
            noImageHint.style.display = 'block';
        };

        // 更新缩略图高亮
        var thumbs = thumbContainer.querySelectorAll('.thumb-item');
        for (var j = 0; j < thumbs.length; j++) {
            var t = thumbs[j];
            if (parseInt(t.getAttribute('data-page')) === currentPage) {
                t.classList.add('active');
                // 滚动到可视区
                t.scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'center' });
            } else {
                t.classList.remove('active');
            }
        }

        // 更新按钮状态
        updateNavButtons();
    }

    function updateNavButtons() {
        var btnFirst = $('btn-first');
        var btnPrev = $('btn-prev');
        var btnNext = $('btn-next');
        var btnLast = $('btn-last');

        btnFirst.disabled = (currentPage <= 1);
        btnPrev.disabled = (currentPage <= 1);
        btnNext.disabled = (currentPage >= totalPages);
        btnLast.disabled = (currentPage >= totalPages);
    }

    // ── 跳转页 ──
    function jumpToPage() {
        var val = parseInt(jumpInput.value, 10);
        if (isNaN(val) || val < 1) val = 1;
        if (val > totalPages) val = totalPages;
        goPage(val);
    }

    // ── 图片错误 ──
    function onImageError() {
        imgSpinner.classList.add('d-none');
        previewImage.style.display = 'none';
        noImageHint.innerHTML = '<i class="bi bi-exclamation-circle" style="font-size:3rem;"></i>'
                              + '<p class="mt-2">图片加载失败</p>'
                              + '<button class="btn btn-sm btn-outline-light mt-2" onclick="goPage(' + currentPage + ')">'
                              + '<i class="bi bi-arrow-clockwise"></i> 重试</button>';
        noImageHint.style.display = 'block';
    }

    function showImageError(msg) {
        imgSpinner.classList.add('d-none');
        previewImage.style.display = 'none';
        noImageHint.innerHTML = '<i class="bi bi-exclamation-circle" style="font-size:3rem;"></i>'
                              + '<p class="mt-2">' + window.escapeHtml(msg) + '</p>';
        noImageHint.style.display = 'block';
    }

    // ── 错误 ──
    function showError(msg) {
        loadingEl.classList.add('d-none');
        readerContent.classList.add('d-none');
        errorEl.classList.remove('d-none');
        errorMsg.textContent = msg;
    }

    // ── 键盘事件 ──
    document.addEventListener('keydown', function (e) {
        // 不在输入框中触发
        if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;

        if (e.key === 'ArrowLeft') {
            e.preventDefault();
            goPage(currentPage - 1);
        } else if (e.key === 'ArrowRight') {
            e.preventDefault();
            goPage(currentPage + 1);
        } else if (e.key === 'Home') {
            e.preventDefault();
            goPage(1);
        } else if (e.key === 'End') {
            e.preventDefault();
            goPage(totalPages);
        }
    });

    // ── 启动 ──
    document.addEventListener('DOMContentLoaded', function () {
        if (typeof albumId !== 'undefined' && albumId) {
            loadPreview();
        } else {
            showError('缺少 album_id 参数');
        }
    });

})();
