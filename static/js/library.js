/**
 * library.js — 资源库页面功能
 * 依赖: utils.js (escapeHtml/escapeHtmlAttr/apiFetch), base.html (showToast)
 * 加载顺序: utils.js → library.js
 *
 * 请求统一走 window.apiFetch（自带 AbortController/超时/pagehide 清理），
 * 列表请求用 abortKey 防竞态（新请求自动中止上一次未完成的请求）
 */
(function () {
    'use strict';

    // ── 全局状态 ──
    var currentPage = 1;
    var pageSize = 50;
    var selectedTags = [];
    var currentQuery = '';
    var currentStatus = '';
    var currentSort = 'updated_at';
    var totalItems = 0;
    var allTags = [];

    // ── 定时器 ──
    var _searchTimer = null;
    var _refreshTimer = null;

    // ── 异常 → Toast 文案（服务端错误/超时显示具体消息，网络错误显示通用文案） ──
    function toastErr(err, fallback) {
        if (err && (err.status || err.isTimeout)) return err.message || fallback || '操作失败';
        return '网络错误';
    }

    // ── 初始化 ──
    initLibrary();

    function initLibrary() {
        loadStats();
        loadTagCloud();
        loadLibrary(1);
        startAutoRefresh();
    }

    // ══════════════════════════════════════════════════════════════
    //  1. 加载资源库列表
    // ══════════════════════════════════════════════════════════════

    function loadLibrary(page, options) {
        options = options || {};
        var showSpinner = options.showSpinner !== undefined ? options.showSpinner : true;

        page = page || currentPage;
        var q = document.getElementById('library-search').value.trim();
        var status = document.getElementById('library-status').value;
        var sort = document.getElementById('library-sort').value;

        currentQuery = q;
        currentStatus = status;
        currentSort = sort;
        currentPage = page;

        var url = '/api/library?page=' + page + '&page_size=' + pageSize;
        if (q) url += '&q=' + encodeURIComponent(q);
        if (status) url += '&status=' + encodeURIComponent(status);
        if (sort) url += '&sort=' + encodeURIComponent(sort);
        if (selectedTags.length > 0) url += '&tag=' + encodeURIComponent(selectedTags.join(','));

        var grid = document.getElementById('library-grid');
        if (showSpinner) {
            grid.innerHTML = '<div class="spinner-overlay"><div class="spinner-border text-primary" role="status"><span class="visually-hidden">加载中...</span></div></div>';
        }

        // abortKey：取消上一次未完成的列表请求（防竞态/防堆积）
        window.apiFetch(url, { abortKey: 'library-list' })
            .then(function (data) {
                if (data.status !== 'ok') {
                    grid.innerHTML = '<div class="empty-library"><i class="bi bi-exclamation-triangle"></i><p>加载失败: ' + window.escapeHtml(data.message || '未知错误') + '</p></div>';
                    return;
                }
                totalItems = data.total || 0;
                var items = data.items || [];
                if (items.length === 0) {
                    grid.innerHTML = '<div class="empty-library"><i class="bi bi-inbox"></i><p>资源库为空</p><p class="small">去 <a href="/search">搜索页面</a> 发现并收藏漫画吧</p></div>';
                    document.getElementById('library-pagination').innerHTML = '';
                    return;
                }
                renderCards(items);
                renderPagination(totalItems, page);
            })
            .catch(function (err) {
                if (err && err.name === 'AbortError') return;
                if (err && err.status) {
                    // HTTP 错误：保持原有「加载失败: 服务端消息」文案
                    grid.innerHTML = '<div class="empty-library"><i class="bi bi-exclamation-triangle"></i><p>加载失败: ' + window.escapeHtml(err.message || '未知错误') + '</p></div>';
                } else {
                    grid.innerHTML = '<div class="empty-library"><i class="bi bi-cloud-slash"></i><p>网络错误: ' + window.escapeHtml(err.message) + '</p></div>';
                }
            });
    }

    function renderCards(items) {
        var grid = document.getElementById('library-grid');
        var html = '';
        for (var i = 0; i < items.length; i++) {
            var item = items[i];
            var coverSrc = item.cover_url || '';
            var title = item.title || '未命名';
            var author = item.author || '未知作者';
            var tags = item.tags || [];
            var displayStatus = (item.file_exists === false && item.download_status === 'completed') ? 'file_deleted' : item.download_status;
            var statusBadge = getStatusBadge(displayStatus);
            var isWishlisted = item.is_wishlisted;

            html += '<div class="library-card" data-album-id="' + window.escapeHtml(item.album_id) + '">';

            // 封面
            if (coverSrc) {
                html += '<a href="/album/' + encodeURIComponent(item.album_id) + '" target="_blank" rel="noopener noreferrer" class="card-cover-link" title="' + window.escapeHtml(title) + '">';
                html += '<img class="card-cover thumb-img" loading="lazy" src="' + window.escapeHtmlAttr(coverSrc) + '" alt="' + window.escapeHtmlAttr(title) + '" onerror="this.src=\'/static/images/no-cover.svg\';this.onerror=null;">';
                html += '</a>';
            } else {
                html += '<a href="/album/' + encodeURIComponent(item.album_id) + '" target="_blank" rel="noopener noreferrer" class="card-cover-link">';
                html += '<div class="placeholder-cover"><i class="bi bi-image" style="font-size:2.5rem;"></i></div>';
                html += '</a>';
            }

            html += '<div class="card-body" onclick="window.open(\'/album/' + encodeURIComponent(item.album_id) + '\', \'_blank\')">';
            // 标题
            html += '<div class="card-title" title="' + window.escapeHtmlAttr(title) + '"><a href="/album/' + encodeURIComponent(item.album_id) + '" target="_blank" rel="noopener noreferrer" class="text-decoration-none text-dark">' + window.escapeHtml(title) + '</a></div>';
            // 作者
            html += '<div class="card-author"><i class="bi bi-person"></i> ' + window.escapeHtml(author) + '</div>';
            // 标签（全部显示）
            html += '<div class="card-tags">';
            for (var j = 0; j < tags.length; j++) {
                var tag = tags[j];
                var sourceClass = tag.source === 'user' ? 'tag-source-user' : 'tag-source-auto';
                html += '<span class="tag-badge ' + sourceClass + '">'
                    + window.escapeHtml(tag.tag)
                    + '<span class="tag-remove" onclick="event.stopPropagation();removeCardTag(\'' + window.escapeHtml(item.album_id) + '\',\'' + window.escapeHtmlAttr(tag.tag) + '\',event)" title="删除此标签">&times;</span>'
                    + '</span>';
            }
            html += '</div>'; // card-tags

            // 状态徽章 + 操作按钮
            html += '<div class="card-actions">';
            html += statusBadge;
            html += '<a href="/album/' + encodeURIComponent(item.album_id) + '" class="btn btn-outline-info btn-sm" target="_blank" rel="noopener noreferrer" title="详情"><i class="bi bi-info-circle"></i></a>';
            html += '<button class="btn btn-sm ' + (isWishlisted ? 'btn-warning' : 'btn-outline-warning') + '" onclick="event.stopPropagation();toggleLibraryWishlist(\'' + encodeURIComponent(item.album_id) + '\', this)" title="' + (isWishlisted ? '取消收藏' : '收藏') + '"><i class="bi ' + (isWishlisted ? 'bi-star-fill' : 'bi-star') + '"></i></button>';
            html += '</div>'; // card-actions

            html += '</div>'; // card-body
            html += '</div>'; // library-card
        }
        grid.innerHTML = html;
    }

    function getStatusBadge(status) {
        var map = {
            'none': '<span class="badge bg-secondary">未下载</span>',
            'queued': '<span class="badge bg-info text-dark">排队中</span>',
            'downloading': '<span class="badge bg-primary">下载中</span>',
            'completed': '<span class="badge bg-success">已完成</span>',
            'failed': '<span class="badge bg-danger">失败</span>',
            'file_deleted': '<span class="badge bg-secondary" style="opacity:0.7;">文件已删除</span>',
        };
        return map[status] || '<span class="badge bg-secondary">未知</span>';
    }

    function renderPagination(total, page) {
        var container = document.getElementById('library-pagination');
        var totalPages = Math.max(1, Math.ceil(total / pageSize));
        var html = '';

        if (page > 1) {
            html += '<button class="btn btn-outline-secondary btn-sm" onclick="loadLibrary(' + (page - 1) + ')">◀ 上一页</button>';
        }
        var start = Math.max(1, page - 2);
        var end = Math.min(totalPages, start + 4);
        start = Math.max(1, end - 4);
        for (var p = start; p <= end; p++) {
            html += '<button class="btn btn-sm ' + (p === page ? 'btn-primary' : 'btn-outline-secondary') + '" onclick="loadLibrary(' + p + ')">' + p + '</button>';
        }
        if (page < totalPages) {
            html += '<button class="btn btn-outline-secondary btn-sm" onclick="loadLibrary(' + (page + 1) + ')">下一页 ▶</button>';
        }
        html += '<span class="page-info ms-2">共 ' + total + ' 条，第 ' + page + '/' + totalPages + ' 页</span>';

        container.innerHTML = html;
    }

    // ══════════════════════════════════════════════════════════════
    //  2. 标签云
    // ══════════════════════════════════════════════════════════════

    function loadTagCloud() {
        window.apiFetch('/api/library/tags', { timeoutMs: 15000 })
            .then(function (data) {
                if (data.status !== 'ok') return;
                allTags = data.tags || [];
                renderTagCloud();
            })
            .catch(function () { /* 静默 */ });
    }

    function renderTagCloud() {
        var container = document.getElementById('tag-cloud');
        var html = '';

        var allActive = selectedTags.length === 0;
        html += '<span class="tag-cloud-item tag-all' + (allActive ? ' active' : '') + '" onclick="clearFilters()">全部标签 ✕</span>';

        for (var i = 0; i < allTags.length; i++) {
            var t = allTags[i];
            var count = t.count || 1;
            var maxCount = 1;
            for (var j = 0; j < allTags.length; j++) {
                if (allTags[j].count > maxCount) maxCount = allTags[j].count;
            }
            var ratio = maxCount > 1 ? Math.log(count) / Math.log(maxCount) : 0.5;
            var fontSize = 0.8 + ratio * 0.5;
            var isActive = selectedTags.indexOf(t.tag) !== -1;
            html += '<span class="tag-cloud-item' + (isActive ? ' active' : '') + '"'
                + ' style="font-size:' + fontSize.toFixed(2) + 'rem;"'
                + ' data-tag="' + window.escapeHtmlAttr(t.tag) + '"'
                + ' onclick="toggleTag(\'' + window.escapeHtmlAttr(t.tag) + '\')">'
                + window.escapeHtml(t.tag) + ' <small>(' + count + ')</small>'
                + '</span>';
        }

        container.innerHTML = html;
        updateTagFilterHint();
    }

    function toggleTag(tag) {
        var idx = selectedTags.indexOf(tag);
        if (idx === -1) {
            selectedTags.push(tag);
        } else {
            selectedTags.splice(idx, 1);
        }
        renderTagCloud();
        loadLibrary(1);
        updateClearFiltersBtn();
    }

    function updateTagFilterHint() {
        var hint = document.getElementById('tag-filter-hint');
        if (selectedTags.length > 0) {
            hint.textContent = '已选 ' + selectedTags.length + ' 个标签: ' + selectedTags.join(', ');
        } else {
            hint.textContent = '点击标签多选筛选';
        }
    }

    function updateClearFiltersBtn() {
        var btn = document.getElementById('clear-filters-btn');
        var hasFilters = selectedTags.length > 0 || currentQuery || currentStatus;
        btn.style.display = hasFilters ? 'inline-block' : 'none';
    }

    function clearFilters() {
        selectedTags = [];
        document.getElementById('library-search').value = '';
        document.getElementById('library-status').value = '';
        document.getElementById('library-sort').value = 'updated_at';
        currentQuery = '';
        currentStatus = '';
        currentSort = 'updated_at';
        renderTagCloud();
        loadLibrary(1);
        updateClearFiltersBtn();
    }

    // ══════════════════════════════════════════════════════════════
    //  3. 统计信息
    // ══════════════════════════════════════════════════════════════

    function loadStats() {
        window.apiFetch('/api/library/stats', { timeoutMs: 10000 })
            .then(function (data) {
                if (data.status !== 'ok') return;
                document.getElementById('stat-total').textContent = data.total || 0;
                document.getElementById('stat-wishlist').textContent = data.wishlist_count || 0;
                document.getElementById('stat-downloaded').textContent = data.downloaded_count || 0;
                document.getElementById('stat-tags').textContent = data.tag_count || 0;
            })
            .catch(function () { /* 静默 */ });
    }

    // ══════════════════════════════════════════════════════════════
    //  4. 标签管理
    // ══════════════════════════════════════════════════════════════

    function addTag(albumId, tag) {
        if (!tag || !tag.trim()) {
            if (typeof showToast === 'function') showToast('请输入标签名称', 'warning');
            return;
        }
        window.apiFetch('/api/library/' + encodeURIComponent(albumId) + '/tags', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ tags: [tag.trim()] }),
            timeoutMs: 15000
        })
        .then(function (data) {
            if (data.status === 'ok') {
                if (typeof showToast === 'function') showToast('标签已添加', 'success');
                loadLibrary(currentPage, { showSpinner: false });
                loadTagCloud();
                loadStats();
            } else {
                if (typeof showToast === 'function') showToast(data.message || '添加标签失败', 'danger');
            }
        })
        .catch(function (err) {
            if (err && err.name === 'AbortError') return; // pagehide 中止，静默
            if (typeof showToast === 'function') showToast(toastErr(err, '添加标签失败'), 'danger');
        });
    }

    function removeTag(albumId, tag) {
        if (!tag) return;
        window.apiFetch('/api/library/' + encodeURIComponent(albumId) + '/tags', {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ tags: [tag] }),
            timeoutMs: 15000
        })
        .then(function (data) {
            if (data.status === 'ok') {
                if (typeof showToast === 'function') showToast('标签已删除', 'success');
                loadLibrary(currentPage, { showSpinner: false });
                loadTagCloud();
                loadStats();
            } else {
                if (typeof showToast === 'function') showToast(data.message || '删除标签失败', 'danger');
            }
        })
        .catch(function (err) {
            if (err && err.name === 'AbortError') return; // pagehide 中止，静默
            if (typeof showToast === 'function') showToast(toastErr(err, '删除标签失败'), 'danger');
        });
    }

    // ── 卡片上的标签删除快捷操作 ──
    window.removeCardTag = function (albumId, tag, event) {
        if (event) event.stopPropagation();
        if (!confirm('确定要删除标签 "' + tag + '" 吗？')) return;
        removeTag(albumId, tag);
    };

    function syncTags(albumId) {
        window.apiFetch('/api/library/' + encodeURIComponent(albumId) + '/tags/sync', {
            method: 'POST',
            timeoutMs: 30000
        })
        .then(function (data) {
            if (data.status === 'ok') {
                if (typeof showToast === 'function') showToast('同步完成，同步了 ' + (data.synced || 0) + ' 个标签', 'success');
                loadLibrary(currentPage, { showSpinner: false });
                loadTagCloud();
                loadStats();
            } else {
                if (typeof showToast === 'function') showToast(data.message || '同步失败', 'danger');
            }
        })
        .catch(function (err) {
            if (err && err.name === 'AbortError') return; // pagehide 中止，静默
            if (typeof showToast === 'function') showToast(toastErr(err, '同步失败'), 'danger');
        });
    }

    function reSyncAll() {
        if (!confirm('确定要重新同步所有漫画的标签吗？此操作将从 18comic 获取最新标签，用户自定义标签不受影响。')) return;
        if (typeof showToast === 'function') showToast('开始批量同步标签...', 'info');
        window.apiFetch('/api/library/tags/sync-all', {
            method: 'POST',
            timeoutMs: 120000
        })
        .then(function (data) {
            if (data.status === 'ok' || data.status === 'accepted') {
                if (typeof showToast === 'function') showToast('批量同步已开始，后台执行中', 'info');
                // 10 秒后自动刷新
                setTimeout(function () {
                    loadTagCloud();
                    loadStats();
                    loadLibrary(currentPage, { showSpinner: false });
                }, 10000);
            } else {
                if (typeof showToast === 'function') showToast(data.message || '批量同步失败', 'danger');
            }
        })
        .catch(function (err) {
            if (err && err.name === 'AbortError') return; // pagehide 中止，静默
            if (typeof showToast === 'function') showToast(toastErr(err, '批量同步失败'), 'danger');
        });
    }

    // ══════════════════════════════════════════════════════════════
    //  5. 收藏切换
    // ══════════════════════════════════════════════════════════════

    window.toggleLibraryWishlist = function (albumId, btn) {
        var icon = btn.querySelector('i');
        var isStarred = icon.classList.contains('bi-star-fill');

        if (isStarred) {
            window.apiFetch('/api/wishlist/' + encodeURIComponent(albumId), { method: 'DELETE' })
                .then(function (data) {
                    if (data.status === 'ok') {
                        icon.className = 'bi bi-star';
                        btn.classList.remove('btn-warning');
                        btn.classList.add('btn-outline-warning');
                        btn.title = '收藏';
                        if (typeof showToast === 'function') showToast('已取消收藏', 'info');
                        loadStats();
                        loadLibrary(currentPage, { showSpinner: false });
                    } else {
                        if (typeof showToast === 'function') showToast(data.message || '取消收藏失败', 'danger');
                    }
                })
                .catch(function (err) {
                    if (err && err.name === 'AbortError') return; // pagehide 中止，静默
                    if (typeof showToast === 'function') showToast(toastErr(err, '取消收藏失败'), 'danger');
                });
        } else {
            var card = btn.closest('.library-card');
            var titleEl = card ? card.querySelector('.card-title') : null;
            var title = titleEl ? titleEl.textContent.trim() : albumId;
            var authorEl = card ? card.querySelector('.card-author') : null;
            var author = authorEl ? authorEl.textContent.replace(/.*\u2009/, '').trim() : '';
            var coverImg = card ? card.querySelector('.card-cover') : null;
            var coverUrl = coverImg ? coverImg.getAttribute('src') : '';

            window.apiFetch('/api/wishlist', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    album_id: albumId,
                    title: title,
                    author: author,
                    cover_url: coverUrl
                })
            })
            .then(function (data) {
                if (data.status === 'ok') {
                    icon.className = 'bi bi-star-fill';
                    btn.classList.remove('btn-outline-warning');
                    btn.classList.add('btn-warning');
                    btn.title = '取消收藏';
                    if (typeof showToast === 'function') showToast('已添加收藏', 'success');
                    loadStats();
                    loadLibrary(currentPage, { showSpinner: false });
                } else {
                    if (typeof showToast === 'function') showToast(data.message || '收藏失败', 'danger');
                }
            })
            .catch(function (err) {
                if (err && err.name === 'AbortError') return; // pagehide 中止，静默
                if (typeof showToast === 'function') showToast(toastErr(err, '收藏失败'), 'danger');
            });
        }
    };

    // ══════════════════════════════════════════════════════════════
    //  6. 搜索防抖
    // ══════════════════════════════════════════════════════════════

    function debouncedSearch() {
        if (_searchTimer) clearTimeout(_searchTimer);
        _searchTimer = setTimeout(function () {
            loadLibrary(1);
            updateClearFiltersBtn();
        }, 300);
    }

    window.debouncedSearch = debouncedSearch;

    // ══════════════════════════════════════════════════════════════
    //  7. 标签云折叠
    // ══════════════════════════════════════════════════════════════

    var tagCloudExpanded = true;

    function toggleTagCloud() {
        var container = document.getElementById('tag-cloud-container');
        var toggle = document.getElementById('tag-cloud-toggle');
        tagCloudExpanded = !tagCloudExpanded;
        container.style.display = tagCloudExpanded ? '' : 'none';
        toggle.classList.toggle('collapsed', !tagCloudExpanded);
    }

    window.toggleTagCloud = toggleTagCloud;

    // ══════════════════════════════════════════════════════════════
    //  8. 自动刷新
    // ══════════════════════════════════════════════════════════════

    function startAutoRefresh() {
        if (_refreshTimer) clearInterval(_refreshTimer);
        _refreshTimer = setInterval(function () {
            if (!document.hidden) {
                loadLibrary(currentPage, { showSpinner: false });
            }
        }, 15000);
    }

    // ══════════════════════════════════════════════════════════════
    //  9. 工具函数（escapeHtml/escapeHtmlAttr 由 utils.js 提供）
    // ══════════════════════════════════════════════════════════════

    function refreshLibrary() {
        loadLibrary(currentPage, { showSpinner: true });
        loadStats();
    }

    window.refreshLibrary = refreshLibrary;
    window.loadLibrary = loadLibrary;
    window.loadTagCloud = loadTagCloud;
    window.loadStats = loadStats;
    window.toggleTag = toggleTag;
    window.clearFilters = clearFilters;
    window.addTag = addTag;
    window.removeTag = removeTag;
    window.syncTags = syncTags;
    window.reSyncAll = reSyncAll;

    window.addEventListener('beforeunload', function () {
        if (_refreshTimer) clearInterval(_refreshTimer);
        if (_searchTimer) clearTimeout(_searchTimer);
    });

})();
