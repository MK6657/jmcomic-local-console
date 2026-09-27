/**
 * library.js — 资源库页面功能
 * 依赖: utils.js (apiFetch), base.html (showToast)
 * 加载顺序: utils.js → library.js
 *
 * 请求统一走 window.apiFetch（自带 AbortController/超时/pagehide 清理），
 * 列表请求用 abortKey 防竞态（新请求自动中止上一次未完成的请求）
 *
 * 安全：标题/作者/标签来自 18comic 或用户输入，卡片与标签云一律用 DOM API + textContent/dataset 构建；
 * 所有点击都委托到容器，通过 data-action / data-* 取参数，不拼接内联 onclick。
 * 筛选条件（搜索词/状态/排序/标签/作者/页码）同步到地址栏：刷新、从阅读页返回后保持不变。
 */
(function () {
    'use strict';

    var DEFAULT_SORT = 'updated_at';

    // ── 全局状态 ──
    var currentPage = 1;
    var pageSize = 50;
    var selectedTags = [];
    var currentQuery = '';
    var currentStatus = '';
    var currentSort = DEFAULT_SORT;
    var currentAuthor = '';
    var totalItems = 0;
    var allTags = [];
    var itemsById = {}; // 当前页的条目：收藏时取标题/作者/封面

    // ── 定时器 ──
    var _searchTimer = null;
    var _refreshTimer = null;

    // ── 页面元素 ──
    var grid = document.getElementById('library-grid');
    var pagination = document.getElementById('library-pagination');
    var tagCloud = document.getElementById('tag-cloud');
    var activeFilters = document.getElementById('library-active-filters');
    var searchInput = document.getElementById('library-search');
    var statusSelect = document.getElementById('library-status');
    var sortSelect = document.getElementById('library-sort');

    // ── 异常 → Toast 文案（服务端错误/超时显示具体消息，网络错误显示通用文案） ──
    function toastErr(err, fallback) {
        if (err && (err.status || err.isTimeout)) return err.message || fallback || '操作失败';
        return '网络错误';
    }

    function toast(message, type) {
        if (typeof showToast === 'function') showToast(message, type);
    }

    // ── DOM 小工具：文本一律走 textContent ──
    function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    function icon(name) {
        var i = el('i', 'bi ' + name);
        i.setAttribute('aria-hidden', 'true');
        return i;
    }

    function newTabLink(href, className) {
        var a = el('a', className);
        a.href = href;
        a.target = '_blank';
        a.rel = 'noopener'; // 保留同源 referrer：详情页“返回”据此回到这一页（带筛选条件）
        return a;
    }

    function badge(className, text, title) {
        var b = el('span', 'badge ' + className, text);
        if (title) b.title = title;
        return b;
    }

    // ══════════════════════════════════════════════════════════════
    //  0. 地址栏状态（刷新 / 返回后恢复筛选）
    // ══════════════════════════════════════════════════════════════

    function setSelect(select, value, fallback) {
        select.value = value;
        if (select.value !== value) select.value = fallback; // 地址栏里的值不在选项中 → 默认
    }

    // 旧的 / 收藏页的状态值作别名（与 routes/api_library._STATUS_ALIASES 相同）：
    // 旧链接、收藏页带过来的 ?status=none / ?status=queued 打开对应的一档，而不是落回“全部”
    var STATUS_ALIASES = { none: 'undownloaded', queued: 'active' };

    function statusFromUrl(value) {
        return Object.prototype.hasOwnProperty.call(STATUS_ALIASES, value) ? STATUS_ALIASES[value] : value;
    }

    function readUrlState() {
        var params;
        try { params = new URLSearchParams(window.location.search); } catch (e) { return; }
        var tags = (params.get('tag') || '').split(',');
        selectedTags = [];
        for (var i = 0; i < tags.length; i++) {
            var t = tags[i].trim().toLowerCase();
            if (t && selectedTags.indexOf(t) === -1) selectedTags.push(t);
        }
        currentAuthor = (params.get('author') || '').trim();
        var page = parseInt(params.get('page'), 10);
        currentPage = page > 0 ? page : 1;
        searchInput.value = (params.get('q') || '').trim();
        setSelect(statusSelect, statusFromUrl(params.get('status') || ''), '');
        setSelect(sortSelect, params.get('sort') || DEFAULT_SORT, DEFAULT_SORT);
    }

    function writeUrlState() {
        var params = new URLSearchParams();
        if (currentQuery) params.set('q', currentQuery);
        if (currentStatus) params.set('status', currentStatus);
        if (currentSort && currentSort !== DEFAULT_SORT) params.set('sort', currentSort);
        if (selectedTags.length > 0) params.set('tag', selectedTags.join(','));
        if (currentAuthor) params.set('author', currentAuthor);
        if (currentPage > 1) params.set('page', String(currentPage));
        var qs = params.toString();
        var url = window.location.pathname + (qs ? '?' + qs : '') + window.location.hash;
        if (url === window.location.pathname + window.location.search + window.location.hash) return;
        try { window.history.replaceState(window.history.state, '', url); } catch (e) { /* 忽略 */ }
    }

    // ══════════════════════════════════════════════════════════════
    //  1. 加载资源库列表
    // ══════════════════════════════════════════════════════════════

    function loadLibrary(page, options) {
        options = options || {};
        var showSpinner = options.showSpinner !== undefined ? options.showSpinner : true;

        page = page || currentPage;
        currentQuery = searchInput.value.trim();
        currentStatus = statusSelect.value;
        currentSort = sortSelect.value;
        currentPage = page;
        writeUrlState();
        updateClearFiltersBtn();
        renderActiveFilters();

        var url = '/api/library?page=' + page + '&page_size=' + pageSize;
        if (currentQuery) url += '&q=' + encodeURIComponent(currentQuery);
        if (currentStatus) url += '&status=' + encodeURIComponent(currentStatus);
        if (currentSort) url += '&sort=' + encodeURIComponent(currentSort);
        if (selectedTags.length > 0) url += '&tag=' + encodeURIComponent(selectedTags.join(','));
        if (currentAuthor) url += '&author=' + encodeURIComponent(currentAuthor);

        if (showSpinner) {
            grid.innerHTML = '<div class="spinner-overlay"><div class="spinner-border text-primary" role="status"><span class="visually-hidden">加载中...</span></div></div>';
        }

        // abortKey：取消上一次未完成的列表请求（防竞态/防堆积）
        window.apiFetch(url, { abortKey: 'library-list' })
            .then(function (data) {
                if (data.status !== 'ok') {
                    showGridMessage('bi-exclamation-triangle', '加载失败: ' + (data.message || '未知错误'));
                    return;
                }
                totalItems = data.total || 0;
                var items = data.items || [];
                if (items.length === 0 && totalItems > 0 && page > 1 && !options.clamped) {
                    // 删除/筛选后当前页超出末页（或地址栏里是旧页码）→ 回到最后一页
                    loadLibrary(Math.ceil(totalItems / pageSize), { showSpinner: false, clamped: true });
                    return;
                }
                if (items.length === 0) {
                    itemsById = {};
                    renderEmpty();
                    pagination.textContent = '';
                    return;
                }
                renderCards(items);
                renderPagination(totalItems, page);
                // 从导航回到资源库：地址与离开时相同就回到原来的滚动位置（只在第一次画出列表时）
                if (window.navMemory) window.navMemory.restoreScroll();
            })
            .catch(function (err) {
                if (err && err.name === 'AbortError') return;
                if (err && err.status) {
                    // HTTP 错误：保持原有「加载失败: 服务端消息」文案
                    showGridMessage('bi-exclamation-triangle', '加载失败: ' + (err.message || '未知错误'));
                } else {
                    showGridMessage('bi-cloud-slash', '网络错误: ' + ((err && err.message) || ''));
                }
            });
    }

    function hasFilters() {
        return !!(selectedTags.length > 0 || currentQuery || currentStatus || currentAuthor);
    }

    function showGridMessage(iconName, text) {
        var box = el('div', 'empty-library');
        box.appendChild(icon(iconName));
        box.appendChild(el('p', null, text));
        grid.textContent = '';
        grid.appendChild(box);
    }

    function renderEmpty() {
        if (hasFilters()) {
            var box = el('div', 'empty-library');
            box.appendChild(icon('bi-funnel'));
            box.appendChild(el('p', null, '没有符合当前筛选条件的漫画'));
            var clear = el('button', 'btn btn-outline-secondary btn-sm', '清除筛选');
            clear.type = 'button';
            clear.dataset.action = 'clear-filters';
            box.appendChild(clear);
            grid.textContent = '';
            grid.appendChild(box);
            return;
        }
        grid.innerHTML = '<div class="empty-library"><i class="bi bi-inbox"></i><p>资源库为空</p><p class="small">去 <a href="/search" data-nav-memory="/search">搜索页面</a> 发现并收藏漫画吧</p></div>';
    }

    function renderCards(items) {
        var focus = rememberKeyboardFocus();
        var frag = document.createDocumentFragment();
        itemsById = {};
        for (var i = 0; i < items.length; i++) {
            itemsById[String(items[i].album_id)] = items[i];
            frag.appendChild(buildCard(items[i]));
        }
        grid.textContent = '';
        grid.appendChild(frag);
        restoreKeyboardFocus(focus);
    }

    // 重建卡片前记住键盘焦点（哪张卡片的第几个控件），重建后还回去。
    // 例如用键盘点“阅读”、再返回时列表会立即重新加载，焦点不会被甩回页面开头；鼠标点出来的焦点不管
    var FOCUSABLE = 'a[href], button';

    function rememberKeyboardFocus() {
        var active = document.activeElement;
        if (!active || !grid.contains(active)) return null;
        try { if (!active.matches(':focus-visible')) return null; } catch (e) { return null; }
        var card = active.closest('.library-card');
        if (!card) return null;
        return { id: card.dataset.albumId, index: Array.prototype.indexOf.call(card.querySelectorAll(FOCUSABLE), active) };
    }

    function restoreKeyboardFocus(saved) {
        if (!saved || saved.index < 0) return;
        var cards = grid.querySelectorAll('.library-card');
        for (var i = 0; i < cards.length; i++) {
            if (cards[i].dataset.albumId !== saved.id) continue;
            var target = cards[i].querySelectorAll(FOCUSABLE)[saved.index];
            if (target) target.focus();
            return;
        }
    }

    function buildCard(item) {
        var albumId = String(item.album_id);
        var detailUrl = '/album/' + encodeURIComponent(albumId);
        var title = item.title || '未命名';
        var author = (item.author || '').trim();
        var tags = item.tags || [];

        var card = el('div', 'library-card');
        card.dataset.albumId = albumId;

        // 封面（加载失败换成缺省封面：见 grid 上捕获阶段的 error 委托）
        var coverLink = newTabLink(detailUrl, 'card-cover-link');
        if (item.cover_url) {
            coverLink.title = title;
            var img = el('img', 'card-cover thumb-img');
            img.loading = 'lazy';
            img.alt = title;
            img.src = item.cover_url;
            coverLink.appendChild(img);
        } else {
            var placeholder = el('div', 'placeholder-cover');
            var placeholderIcon = icon('bi-image');
            placeholderIcon.style.fontSize = '2.5rem';
            placeholder.appendChild(placeholderIcon);
            coverLink.appendChild(placeholder);
        }
        card.appendChild(coverLink);

        // 正文：点空白处在新标签页打开详情（委托处理，链接/按钮不会重复打开）
        var body = el('div', 'card-body');

        // 标题
        var titleBox = el('div', 'card-title');
        titleBox.title = title;
        var titleLink = newTabLink(detailUrl, 'text-decoration-none text-dark');
        titleLink.textContent = title;
        titleBox.appendChild(titleLink);
        body.appendChild(titleBox);

        // 作者：点名字 → 只看该作者的作品（图标与名字同一行起排，长名字在图标右侧换行）
        var authorBox = el('div', 'card-author');
        authorBox.appendChild(icon('bi-person'));
        if (author) {
            var authorBtn = el('button', 'library-author-link', author);
            authorBtn.type = 'button';
            authorBtn.dataset.action = 'filter-author';
            authorBtn.dataset.author = author;
            authorBtn.title = '只看「' + author + '」的作品';
            authorBox.appendChild(authorBtn);
        } else {
            authorBox.appendChild(el('span', null, '未知作者'));
        }
        body.appendChild(authorBox);

        // 标签（全部显示）；× 是真正的按钮
        var tagsBox = el('div', 'card-tags');
        for (var j = 0; j < tags.length; j++) {
            var tag = tags[j];
            var tagBadge = el('span', 'tag-badge ' + (tag.source === 'user' ? 'tag-source-user' : 'tag-source-auto'), tag.tag);
            var remove = el('button', 'tag-remove', '×');
            remove.type = 'button';
            remove.dataset.action = 'remove-tag';
            remove.dataset.tag = tag.tag;
            remove.title = '删除此标签';
            remove.setAttribute('aria-label', '删除标签 ' + tag.tag);
            tagBadge.appendChild(remove);
            tagsBox.appendChild(tagBadge);
        }
        body.appendChild(tagsBox);

        // 状态徽章 + 按钮组（放不下时按钮组整体换行）：详情 / 收藏 / 阅读
        var actions = el('div', 'card-actions');
        actions.appendChild(getStatusBadge(item));
        var buttons = el('div', 'card-action-buttons');
        var info = newTabLink(detailUrl, 'btn btn-outline-info btn-sm');
        info.title = '详情';
        info.setAttribute('aria-label', '详情');
        info.appendChild(icon('bi-info-circle'));
        buttons.appendChild(info);
        var star = el('button');
        star.type = 'button';
        star.dataset.action = 'toggle-wishlist';
        setStarState(star, !!item.is_wishlisted);
        buttons.appendChild(star);
        // “阅读”每张卡片都有：已下载打开本地文件，否则在线阅读（utils.js readLink）
        var read = window.readLink.create(albumId, item.readable === true, 'btn-sm');
        if (read) buttons.appendChild(read);
        actions.appendChild(buttons);
        body.appendChild(actions);

        card.appendChild(body);
        return card;
    }

    // 切换按钮：名称固定为“收藏”，是否已收藏只由 aria-pressed 表达（名称跟着变会读成“取消收藏，已按下”）；
    // 悬停提示 title 仍说明点击的效果
    function setStarState(btn, starred) {
        btn.className = 'btn btn-sm ' + (starred ? 'btn-warning' : 'btn-outline-warning');
        btn.title = starred ? '取消收藏' : '收藏';
        btn.setAttribute('aria-label', '收藏');
        btn.setAttribute('aria-pressed', starred ? 'true' : 'false');
        btn.textContent = '';
        btn.appendChild(icon(starred ? 'bi-star-fill' : 'bi-star'));
    }

    // 状态由服务端按与收藏相同的规则算好（core.database.download_state）：
    // status_group = readable / active / failed / none，activity = 进行中任务的状态，files_missing = 文件已删除；
    // 可读判定是共用规则（core.local_availability）：item.readable
    var ACTIVITY_BADGES = {
        'queued': ['bg-info text-dark', '排队中'],
        'downloading': ['bg-primary', '下载中'],
        'paused': ['bg-warning', '已暂停']
    };

    function activityBadge(activity) {
        var spec = ACTIVITY_BADGES[activity] || ACTIVITY_BADGES.queued;
        return badge(spec[0], spec[1]);
    }

    // 与详情页、搜索页同一个“已下载”标记（style.css .offline-badge）
    function offlineBadge() {
        var b = el('span', 'offline-badge');
        b.title = '本地文件完整，可以离线阅读';
        b.appendChild(icon('bi-check-circle-fill'));
        // 手机上卡片放不下一行时只在“·”后换行，不把“可离线阅读”拆开
        var text = el('span');
        text.appendChild(el('span', 'text-nowrap', '已下载 ·'));
        text.appendChild(document.createTextNode(' '));
        text.appendChild(el('span', 'text-nowrap', '可离线阅读'));
        b.appendChild(text);
        return b;
    }

    // 与收藏清单同样的徽章：可读（+ 正在更新时的任务状态）/ 排队中·下载中·已暂停 / 失败 / 文件已删除 / 未下载
    function getStatusBadge(item) {
        var frag = document.createDocumentFragment();
        var group = item.status_group;
        if (group === 'readable') {
            frag.appendChild(offlineBadge());
            if (item.activity) frag.appendChild(activityBadge(item.activity));
        } else if (group === 'active') {
            frag.appendChild(activityBadge(item.activity));
        } else if (group === 'failed') {
            frag.appendChild(badge('bg-danger', '失败', '最近一次下载失败，可以重新下载'));
        } else if (item.files_missing) {
            frag.appendChild(badge('status-badge-muted', '文件已删除', '下载过，但本地文件已不在，需要重新下载'));
        } else {
            frag.appendChild(badge('bg-secondary', '未下载'));
        }
        return frag;
    }

    function renderPagination(total, page) {
        var totalPages = Math.max(1, Math.ceil(total / pageSize));
        pagination.textContent = '';

        function pageButton(p, label, current) {
            var b = el('button', 'btn btn-sm ' + (current ? 'btn-primary' : 'btn-outline-secondary'), label);
            b.type = 'button';
            b.dataset.page = String(p);
            if (current) b.setAttribute('aria-current', 'page');
            pagination.appendChild(b);
        }

        if (page > 1) pageButton(page - 1, '◀ 上一页', false);
        var start = Math.max(1, page - 2);
        var end = Math.min(totalPages, start + 4);
        start = Math.max(1, end - 4);
        for (var p = start; p <= end; p++) {
            pageButton(p, String(p), p === page);
        }
        if (page < totalPages) pageButton(page + 1, '下一页 ▶', false);
        pagination.appendChild(el('span', 'page-info ms-2', '共 ' + total + ' 条，第 ' + page + '/' + totalPages + ' 页'));
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
        var focusedTag = null;
        var active = document.activeElement;
        if (active && tagCloud.contains(active) && active.dataset.tag !== undefined) focusedTag = active.dataset.tag;

        tagCloud.textContent = '';
        var allBtn = el('button', 'tag-cloud-item tag-all' + (selectedTags.length === 0 ? ' active' : ''), '全部标签 ✕');
        allBtn.type = 'button';
        allBtn.dataset.action = 'clear-filters';
        allBtn.title = '清除全部筛选';
        tagCloud.appendChild(allBtn);

        var maxCount = 1;
        for (var j = 0; j < allTags.length; j++) {
            if (allTags[j].count > maxCount) maxCount = allTags[j].count;
        }
        for (var i = 0; i < allTags.length; i++) {
            var t = allTags[i];
            var count = t.count || 1;
            var ratio = maxCount > 1 ? Math.log(count) / Math.log(maxCount) : 0.5;
            var isActive = selectedTags.indexOf(t.tag) !== -1;
            var item = el('button', 'tag-cloud-item' + (isActive ? ' active' : ''));
            item.type = 'button';
            item.style.fontSize = (0.8 + ratio * 0.5).toFixed(2) + 'rem';
            item.dataset.tag = t.tag;
            item.setAttribute('aria-pressed', isActive ? 'true' : 'false');
            item.appendChild(document.createTextNode(t.tag + ' '));
            item.appendChild(el('small', null, '(' + count + ')'));
            tagCloud.appendChild(item);
            // 重建后把键盘焦点还给刚才操作的标签
            if (focusedTag !== null && t.tag === focusedTag) item.dataset.refocus = '1';
        }
        var refocus = tagCloud.querySelector('[data-refocus]');
        if (refocus) {
            delete refocus.dataset.refocus;
            refocus.focus();
        }

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
        btn.style.display = hasFilters() ? 'inline-block' : 'none';
    }

    function clearFilters() {
        selectedTags = [];
        currentAuthor = '';
        searchInput.value = '';
        statusSelect.value = '';
        sortSelect.value = DEFAULT_SORT;
        currentQuery = '';
        currentStatus = '';
        currentSort = DEFAULT_SORT;
        renderTagCloud();
        loadLibrary(1);
        updateClearFiltersBtn();
    }

    // ══════════════════════════════════════════════════════════════
    //  2b. 作者筛选（点卡片上的作者名；与标签/搜索/状态可叠加）
    // ══════════════════════════════════════════════════════════════

    function setAuthorFilter(author) {
        author = (author || '').trim();
        if (author === currentAuthor) return;
        currentAuthor = author;
        loadLibrary(1);
        // 从页面下方点进来时，把筛选栏和作者筛选提示带回视野（直接跳转，不做平滑滚动动画）
        var filterBar = document.querySelector('.library-filters');
        if (author && filterBar && filterBar.getBoundingClientRect().top < 0) {
            try {
                filterBar.scrollIntoView({ block: 'start', behavior: 'instant' });
            } catch (e) {
                filterBar.scrollIntoView();
            }
        }
        // 点的作者按钮随卡片重建而消失：把焦点交给新出现的“作者：X ✕”，键盘/读屏用户不会被甩回页面开头
        var chip = activeFilters.querySelector('[data-action="clear-author"]');
        if (author && chip) chip.focus();
    }

    function renderActiveFilters() {
        var existing = activeFilters.querySelector('[data-action="clear-author"]');
        // 作者没变（翻页、自动刷新等）就不重建，停在上面的键盘焦点不会丢
        if (currentAuthor && existing && existing.dataset.author === currentAuthor) return;
        activeFilters.textContent = '';
        if (!currentAuthor) {
            activeFilters.hidden = true;
            return;
        }
        var chip = el('button', 'tag-cloud-item active');
        chip.type = 'button';
        chip.dataset.action = 'clear-author';
        chip.dataset.author = currentAuthor;
        chip.title = '清除作者筛选';
        chip.setAttribute('aria-label', '清除作者筛选：' + currentAuthor);
        chip.appendChild(icon('bi-person'));
        chip.appendChild(document.createTextNode(' 作者：' + currentAuthor + ' '));
        var x = el('span', null, '✕');
        x.setAttribute('aria-hidden', 'true');
        chip.appendChild(x);
        activeFilters.appendChild(chip);
        activeFilters.hidden = false;
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
                document.getElementById('stat-readable').textContent = data.readable_count || 0;
                document.getElementById('stat-tags').textContent = data.tag_count || 0;
            })
            .catch(function () { /* 静默 */ });
    }

    // ══════════════════════════════════════════════════════════════
    //  4. 标签管理
    // ══════════════════════════════════════════════════════════════

    function addTag(albumId, tag) {
        if (!tag || !tag.trim()) {
            toast('请输入标签名称', 'warning');
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
                toast('标签已添加', 'success');
                loadLibrary(currentPage, { showSpinner: false });
                loadTagCloud();
                loadStats();
            } else {
                toast(data.message || '添加标签失败', 'danger');
            }
        })
        .catch(function (err) {
            if (err && err.name === 'AbortError') return; // pagehide 中止，静默
            toast(toastErr(err, '添加标签失败'), 'danger');
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
                toast('标签已删除', 'success');
                loadLibrary(currentPage, { showSpinner: false });
                loadTagCloud();
                loadStats();
            } else {
                toast(data.message || '删除标签失败', 'danger');
            }
        })
        .catch(function (err) {
            if (err && err.name === 'AbortError') return; // pagehide 中止，静默
            toast(toastErr(err, '删除标签失败'), 'danger');
        });
    }

    // ── 卡片上的标签删除快捷操作 ──
    function removeCardTag(albumId, tag, event) {
        if (event) event.stopPropagation();
        if (!albumId || !tag) return;
        if (!confirm('确定要删除标签 "' + tag + '" 吗？')) return;
        removeTag(albumId, tag);
    }

    function syncTags(albumId) {
        window.apiFetch('/api/library/' + encodeURIComponent(albumId) + '/tags/sync', {
            method: 'POST',
            timeoutMs: 30000
        })
        .then(function (data) {
            if (data.status === 'ok') {
                toast('同步完成，同步了 ' + (data.synced || 0) + ' 个标签', 'success');
                loadLibrary(currentPage, { showSpinner: false });
                loadTagCloud();
                loadStats();
            } else {
                toast(data.message || '同步失败', 'danger');
            }
        })
        .catch(function (err) {
            if (err && err.name === 'AbortError') return; // pagehide 中止，静默
            toast(toastErr(err, '同步失败'), 'danger');
        });
    }

    function reSyncAll() {
        if (!confirm('确定要重新同步所有漫画的标签吗？此操作将从 18comic 获取最新标签，用户自定义标签不受影响。')) return;
        toast('开始批量同步标签...', 'info');
        window.apiFetch('/api/library/tags/sync-all', {
            method: 'POST',
            timeoutMs: 120000
        })
        .then(function (data) {
            if (data.status === 'ok' || data.status === 'accepted') {
                toast('批量同步已开始，后台执行中', 'info');
                // 10 秒后自动刷新
                setTimeout(function () {
                    loadTagCloud();
                    loadStats();
                    loadLibrary(currentPage, { showSpinner: false });
                }, 10000);
            } else {
                toast(data.message || '批量同步失败', 'danger');
            }
        })
        .catch(function (err) {
            if (err && err.name === 'AbortError') return; // pagehide 中止，静默
            toast(toastErr(err, '批量同步失败'), 'danger');
        });
    }

    // ══════════════════════════════════════════════════════════════
    //  5. 收藏切换
    // ══════════════════════════════════════════════════════════════

    function toggleLibraryWishlist(albumId, btn) {
        var isStarred = btn.getAttribute('aria-pressed') === 'true';

        if (isStarred) {
            window.apiFetch('/api/wishlist/' + encodeURIComponent(albumId), { method: 'DELETE' })
                .then(function (data) {
                    if (data.status === 'ok') {
                        setStarState(btn, false);
                        toast('已取消收藏', 'info');
                        loadStats();
                        loadLibrary(currentPage, { showSpinner: false });
                    } else {
                        toast(data.message || '取消收藏失败', 'danger');
                    }
                })
                .catch(function (err) {
                    if (err && err.name === 'AbortError') return; // pagehide 中止，静默
                    toast(toastErr(err, '取消收藏失败'), 'danger');
                });
        } else {
            var item = itemsById[albumId] || {};
            window.apiFetch('/api/wishlist', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    album_id: albumId,
                    title: item.title || albumId,
                    author: item.author || '',
                    cover_url: item.cover_url || ''
                })
            })
            .then(function (data) {
                if (data.status === 'ok') {
                    setStarState(btn, true);
                    toast('已添加收藏', 'success');
                    loadStats();
                    loadLibrary(currentPage, { showSpinner: false });
                } else {
                    toast(data.message || '收藏失败', 'danger');
                }
            })
            .catch(function (err) {
                if (err && err.name === 'AbortError') return; // pagehide 中止，静默
                toast(toastErr(err, '收藏失败'), 'danger');
            });
        }
    }

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
        toggle.setAttribute('aria-expanded', tagCloudExpanded ? 'true' : 'false');
    }

    // ══════════════════════════════════════════════════════════════
    //  8. 自动刷新
    // ══════════════════════════════════════════════════════════════

    // 页面隐藏，或正用键盘操作卡片 / 作者筛选 / 分页按钮时跳过这一轮，免得重建把焦点弄丢
    function canRefreshNow() {
        return !document.hidden
            && !grid.contains(document.activeElement)
            && !activeFilters.contains(document.activeElement)
            && !pagination.contains(document.activeElement);
    }

    function startAutoRefresh() {
        if (_refreshTimer) clearInterval(_refreshTimer);
        _refreshTimer = setInterval(function () {
            if (canRefreshNow()) loadLibrary(currentPage, { showSpinner: false });
        }, 15000);
    }

    function stopAutoRefresh() {
        if (_refreshTimer) clearInterval(_refreshTimer);
        _refreshTimer = null;
        if (_searchTimer) clearTimeout(_searchTimer);
        _searchTimer = null;
    }

    function refreshLibrary() {
        loadLibrary(currentPage, { showSpinner: true });
        loadStats();
    }

    // ══════════════════════════════════════════════════════════════
    //  9. 事件委托（取代内联 onclick；参数只从 data-* 读取）
    // ══════════════════════════════════════════════════════════════

    grid.addEventListener('click', function (e) {
        var target = e.target.closest('[data-action]');
        if (target && grid.contains(target)) {
            var card = target.closest('.library-card');
            var albumId = card ? card.dataset.albumId : '';
            var action = target.dataset.action;
            if (action === 'remove-tag') {
                removeCardTag(albumId, target.dataset.tag || '', e);
            } else if (action === 'toggle-wishlist') {
                if (albumId) toggleLibraryWishlist(albumId, target);
            } else if (action === 'filter-author') {
                setAuthorFilter(target.dataset.author || '');
            } else if (action === 'clear-filters') {
                clearFilters();
            }
            return;
        }
        // 卡片正文空白处 → 新标签页打开详情；链接和按钮各自处理，不重复打开
        if (e.target.closest('a, button, input, select, textarea')) return;
        var body = e.target.closest('.card-body');
        var cardEl = body ? body.closest('.library-card') : null;
        if (cardEl && grid.contains(cardEl) && cardEl.dataset.albumId) {
            window.open('/album/' + encodeURIComponent(cardEl.dataset.albumId), '_blank', 'noopener');
        }
    });

    // 封面加载失败 → 缺省封面（error 不冒泡，用捕获阶段）
    grid.addEventListener('error', function (e) {
        var img = e.target;
        if (!img || img.tagName !== 'IMG' || img.dataset.fallback) return;
        img.dataset.fallback = '1';
        img.src = '/static/images/no-cover.svg';
    }, true);

    tagCloud.addEventListener('click', function (e) {
        var btn = e.target.closest('button');
        if (!btn || !tagCloud.contains(btn)) return;
        if (btn.dataset.action === 'clear-filters') {
            clearFilters();
        } else if (btn.dataset.tag !== undefined) {
            toggleTag(btn.dataset.tag);
        }
    });

    activeFilters.addEventListener('click', function (e) {
        var btn = e.target.closest('[data-action="clear-author"]');
        if (!btn) return;
        currentAuthor = '';
        loadLibrary(1);
        // 用键盘清除时（click.detail 为 0）按钮随之消失，把焦点交给搜索框而不是丢到页面开头
        if (e.detail === 0) searchInput.focus();
    });

    pagination.addEventListener('click', function (e) {
        var btn = e.target.closest('button[data-page]');
        if (!btn) return;
        var page = parseInt(btn.dataset.page, 10);
        if (page > 0) loadLibrary(page);
    });

    var tagCloudToggle = document.getElementById('tag-cloud-toggle');
    tagCloudToggle.addEventListener('click', toggleTagCloud);
    tagCloudToggle.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            toggleTagCloud();
        }
    });

    searchInput.addEventListener('input', debouncedSearch);
    statusSelect.addEventListener('change', function () { loadLibrary(1); });
    sortSelect.addEventListener('change', function () { loadLibrary(1); });
    document.getElementById('clear-filters-btn').addEventListener('click', clearFilters);
    document.getElementById('library-resync-btn').addEventListener('click', reSyncAll);
    document.getElementById('library-refresh-btn').addEventListener('click', refreshLibrary);

    // ── 供控制台/其他脚本调用（页面本身不再依赖这些全局函数） ──
    window.refreshLibrary = refreshLibrary;
    window.loadLibrary = loadLibrary;
    window.loadTagCloud = loadTagCloud;
    window.loadStats = loadStats;
    window.toggleTag = toggleTag;
    window.clearFilters = clearFilters;
    window.addTag = addTag;
    window.removeTag = removeTag;
    window.removeCardTag = removeCardTag;
    window.syncTags = syncTags;
    window.reSyncAll = reSyncAll;
    window.toggleLibraryWishlist = toggleLibraryWishlist;
    window.debouncedSearch = debouncedSearch;
    window.toggleTagCloud = toggleTagCloud;

    // 离开页面（含进入 bfcache，例如点“阅读”后）停止定时器；
    // 从阅读页等处返回、页面从 bfcache 恢复时：立即重新加载（可离线阅读/下载状态可能已变），并恢复定时器
    window.addEventListener('pagehide', stopAutoRefresh);
    window.addEventListener('pageshow', function (event) {
        if (!event.persisted) return;
        loadLibrary(currentPage, { showSpinner: false });
        loadStats();
        startAutoRefresh();
    });
    // 切回本标签页时立即刷新（例如在别的标签页下载完成，或本地文件被移走）
    document.addEventListener('visibilitychange', function () {
        if (document.visibilityState === 'visible' && _refreshTimer && canRefreshNow()) {
            loadLibrary(currentPage, { showSpinner: false });
        }
    });

    // ── 初始化 ──
    readUrlState();
    loadStats();
    loadTagCloud();
    renderTagCloud(); // 先画出“全部标签”与地址栏带来的已选状态，标签数据到了再补全
    loadLibrary(currentPage);
    startAutoRefresh();

})();
