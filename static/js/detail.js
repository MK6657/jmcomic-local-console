/**
 * 漫画详情页 JS
 * 依赖: utils.js (window.escapeHtml, window.escapeHtmlAttr, window.apiFetch)
 * 依赖: reading-nav.js (window.readingNav.bindBack：从站内页面进入时“返回”走浏览器后退)
 * 依赖: base.html 内联 showToast
 * 变量: albumId 在模板中通过 <script>var albumId = ...</script> 定义
 *
 * 请求统一走 window.apiFetch（自带 AbortController/超时/pagehide 清理），
 * 解决多次页面跳转后残留请求占满浏览器连接池的问题
 *
 * 不使用内联事件处理器：标签名来自 18comic / 用户输入，拼进内联 onclick 的 JS 字符串时，
 * escapeHtmlAttr 的转义会在处理器执行前被浏览器解码回原字符，形成脚本注入。
 * 交互元素只带 data-action / data-tag，由 #album-content 上的一个委托监听器处理；
 * 含远程数据的标签区用 textContent / dataset 构建。
 */
(function (albumId) {
    'use strict';

    var container = document.getElementById('album-content');
    var spinner = document.getElementById('loading-spinner');
    var errorDiv = document.getElementById('error-message');
    var rendered = false;

    // ── 异常 → Toast 文案（服务端错误/超时显示具体消息，网络错误显示通用文案） ──
    function toastErr(err, fallback) {
        if (err && (err.status || err.isTimeout)) return err.message || fallback || '操作失败';
        return '网络错误';
    }

    // 获取详情（30s 超时）
    function loadAlbum() {
        window.apiFetch('/api/album/' + albumId, { timeoutMs: 30000 })
            .then(function (data) {
                if (data.status !== 'ok') {
                    throw new Error(data.message || '获取失败');
                }
                renderAlbum(data.data);
                rendered = true;
                // 本地是否已下载、可离线阅读（与搜索/下载管理/收藏/资源库同一判定）
                refreshOfflineStatus();
                // 检查新章节的状态（只读数据库；本地没有已下载内容时不显示）
                refreshUpdateStatus();
                // 收藏状态检查 + 收藏按钮绑定（按钮由 renderAlbum 动态生成，必须在渲染后执行）
                initWishlist(data.data);
                // 加载本地标签管理
                loadAlbumTags(albumId);
                // 异步触发标签同步（不阻塞用户交互，15s 超时）
                window.apiFetch('/api/library/' + encodeURIComponent(albumId) + '/tags/sync', {
                    method: 'POST',
                    timeoutMs: 15000
                })
                .then(function (syncData) {
                    if (syncData.status === 'ok' && syncData.synced > 0) {
                        // 有新的标签同步成功，刷新显示
                        loadAlbumTags(albumId);
                    }
                })
                .catch(function () { /* 静默失败 */ });
            })
            .catch(function (err) {
                if (err && err.name === 'AbortError') return; // pagehide 中止，静默
                spinner.classList.add('d-none');
                errorDiv.classList.remove('d-none');
                if (err && err.isTimeout) {
                    errorDiv.textContent = '加载超时（30秒），18comic 服务器响应较慢，请稍后重试或检查网络连接';
                } else {
                    errorDiv.textContent = '加载失败: ' + (err.message || '未知错误');
                }
            });
    }

    // 无封面或封面加载失败（CDN 不可达）时显示的占位，尺寸与封面一致
    var COVER_PLACEHOLDER = '<div class="detail-cover detail-cover-placeholder" role="img" aria-label="暂无封面"><i class="bi bi-image" aria-hidden="true"></i></div>';

    function renderAlbum(album) {
        spinner.classList.add('d-none');
        container.classList.remove('d-none');

        var onlineUrl = '/online/' + encodeURIComponent(album.album_id);
        var localReadUrl = '/read/' + encodeURIComponent(album.album_id);
        var html = '';

        // 封面 + 信息：同一个 .row 内的两列，垂直居中对齐——封面比信息卡片矮时封面居中，高时信息卡片居中
        html += '<div class="row g-4 mb-4 align-items-center">';

        // 左侧封面
        html += '<div class="col-12 col-md-4 col-lg-3">';
        if (album.cover) {
            html += '<img src="' + window.escapeHtmlAttr(album.cover) + '" class="detail-cover" alt="' + window.escapeHtmlAttr(album.title) + '" decoding="async">';
        } else {
            html += COVER_PLACEHOLDER;
        }
        html += '</div>';

        // 右侧信息
        html += '<div class="col-12 col-md-8 col-lg-9">';
        html += '<div class="card"><div class="card-body">';
        html += '<h3 class="card-title">' + window.escapeHtml(album.title) + ' <button type="button" id="wishlist-toggle-btn" class="btn btn-sm wishlist-btn btn-outline-warning ms-2" title="收藏" aria-label="收藏" aria-pressed="false"><i class="bi bi-star" aria-hidden="true"></i></button></h3>';
        // 已下载标记（+ 压缩包标记）或本地文件不可用的原因：由 refreshOfflineStatus 显示（放在标题外，收藏时取的标题文字不受影响）
        html += '<div id="album-offline-status" class="local-status-row" hidden><span class="offline-badge" title="本地有已下载的内容，可以离线阅读；不一定是整部漫画" hidden><i class="bi bi-check-circle-fill" aria-hidden="true"></i> 已下载内容 · 可离线阅读</span></div>';
        // 检查新章节的状态行（“部分章节已下载 · M/N 话”正下方）：只在本地有已下载内容时由 refreshUpdateStatus 显示
        html += '<div id="album-update-status" class="update-status" role="status" aria-live="polite" hidden></div>';
        html += '<div class="row mt-3">';
        html += '<div class="col-sm-6 mb-2"><strong><i class="bi bi-person"></i> 作者：</strong> ' + window.escapeHtml(album.author || '-') + '</div>';
        html += '<div class="col-sm-6 mb-2"><strong><i class="bi bi-hash"></i> 车号：</strong> <code>' + window.escapeHtml(album.album_id) + '</code></div>';
        if (album.views !== undefined) {
            html += '<div class="col-sm-6 mb-2"><strong><i class="bi bi-eye"></i> 观看：</strong> ' + window.escapeHtml(String(album.views)) + '</div>';
        }
        if (album.likes !== undefined) {
            html += '<div class="col-sm-6 mb-2"><strong><i class="bi bi-heart"></i> 点赞：</strong> ' + window.escapeHtml(String(album.likes)) + '</div>';
        }
        html += '</div>';

        // 标签
        if (album.tags && album.tags.length > 0) {
            html += '<div class="mt-3"><strong><i class="bi bi-tags"></i> 标签：</strong> ';
            album.tags.forEach(function (tag) {
                html += '<a href="/search?keyword=' + encodeURIComponent(tag) + '" class="badge bg-secondary text-decoration-none me-1">' + window.escapeHtml(tag) + '</a> ';
            });
            html += '</div>';
        }

        // 标签管理区（由 JS 动态加载）
        html += '<div id="album-tags-manager" class="mt-2"></div>';

        // 角色
        if (album.actors && album.actors.length > 0) {
            html += '<div class="mt-2"><strong><i class="bi bi-people"></i> 角色：</strong> ';
            album.actors.forEach(function (actor) {
                html += '<span class="badge bg-info text-dark me-1">' + window.escapeHtml(actor) + '</span> ';
            });
            html += '</div>';
        }

        // 作品
        if (album.works && album.works.length > 0) {
            html += '<div class="mt-2"><strong><i class="bi bi-collection"></i> 作品：</strong> ';
            album.works.forEach(function (work) {
                html += '<span class="badge bg-light text-dark me-1">' + window.escapeHtml(work) + '</span> ';
            });
            html += '</div>';
        }

        html += '</div></div>'; // 信息卡片结束
        html += '</div>';       // 右侧列结束
        html += '</div>';       // 封面 + 信息行结束

        // 章节列表（整行）
        html += '<div class="card">';
        html += '<div class="card-header d-flex justify-content-between align-items-center">';
        html += '<span><i class="bi bi-list-ol"></i> 章节列表 <span class="badge bg-secondary">' + (album.photos ? album.photos.length : 0) + '</span></span>';
        html += '<div class="form-check"><input type="checkbox" id="select-all-chapters" class="form-check-input"><label class="form-check-label" for="select-all-chapters">全选</label></div>';
        html += '</div>';

        html += '<div class="card-body p-0">';
        if (album.photos && album.photos.length > 0) {
            html += '<table class="table table-hover chapter-table mb-0"><thead><tr><th class="chapter-checkbox"><input type="checkbox" id="select-all-inline" class="form-check-input" title="全选"></th><th style="width:60px">序号</th><th>章节名</th><th style="width:80px">页数</th><th class="chapter-online"><span class="visually-hidden">在线观看</span></th></tr></thead><tbody>';

            album.photos.forEach(function (photo, idx) {
                html += '<tr>';
                html += '<td class="chapter-checkbox"><input type="checkbox" name="chapters" value="' + window.escapeHtmlAttr(photo.photo_id) + '" class="form-check-input chapter-checkbox-item"></td>';
                html += '<td>' + (idx + 1) + '</td>';
                html += '<td>' + window.escapeHtml(photo.title || '-') + '</td>';
                html += '<td>' + window.escapeHtml(String(photo.page_count || '?')) + '</td>';
                // 从本章第一页开始在线阅读（同一阅读页，可继续往后读其他章节）
                html += '<td class="chapter-online"><a class="btn btn-sm btn-outline-primary" href="' + onlineUrl + '?chapter=' + encodeURIComponent(photo.photo_id) + '" title="在线观看本章" aria-label="在线观看第 ' + (idx + 1) + ' 章"><i class="bi bi-globe2" aria-hidden="true"></i></a></td>';
                html += '</tr>';
            });

            html += '</tbody></table>';
        } else {
            html += '<div class="text-center text-muted py-4"><i class="bi bi-journal-text" style="font-size:2rem;"></i><p class="mt-2 mb-0">暂无章节信息</p></div>';
        }
        html += '</div>';

        // 底部按钮（窄屏自动换行）；“阅读”只在本地可读时显示（refreshOfflineStatus）
        html += '<div class="card-footer"><div class="d-flex flex-wrap gap-2">';
        html += '<button type="button" id="download-selected-btn" class="btn btn-primary"><i class="bi bi-download"></i> 下载选中章节</button>';
        html += '<button type="button" id="download-all-btn" class="btn btn-success"><i class="bi bi-download"></i> 下载全部</button>';
        html += '<a href="' + localReadUrl + '" id="local-read-btn" class="btn btn-primary" title="已下载内容：打开本地文件连续阅读，无需联网" hidden><i class="bi bi-book" aria-hidden="true"></i> 阅读</a>';
        html += '<a href="' + onlineUrl + '" id="online-read-btn" class="btn btn-outline-primary" title="页面实时从网络加载，不下载、不保存"><i class="bi bi-globe2"></i> 在线观看</a>';
        // 从搜索/资源库/收藏等站内页面进入时后退（保留原页面的结果与滚动位置），否则跟随 href
        html += '<a href="/search" id="detail-back" class="btn btn-outline-secondary ms-auto"><i class="bi bi-arrow-left" aria-hidden="true"></i> 返回</a>';
        html += '</div></div>';

        html += '</div>'; // 章节卡片结束

        container.innerHTML = html;

        var cover = container.querySelector('img.detail-cover');
        if (cover) cover.addEventListener('error', function () { cover.outerHTML = COVER_PLACEHOLDER; }, { once: true });

        bindBackLink(document.getElementById('detail-back'));

        // 绑定事件
        bindEvents(album);
    }

    // ── 返回 ──
    // 同一标签页里从站内页面进入（后面还有历史）→ 浏览器后退，回到原来的搜索结果/资源库/收藏页；
    // 否则跟随 href：
    //   1. 从资源库/收藏（在新标签页打开详情）或某次搜索进来 → 回到那一页（地址栏里带着筛选/搜索条件）
    //   2. 本标签页最近一次搜索（与顶部导航“搜索”同一来源：nav-memory.js）
    //   3. /search
    function referrerListPage() {
        try {
            var ref = new URL(document.referrer);
            if (ref.origin !== location.origin) return '';
            if (ref.pathname === '/library' || ref.pathname === '/wishlist') return ref.pathname + ref.search;
            if (ref.pathname === '/search' && new URLSearchParams(ref.search).get('keyword')) return ref.pathname + ref.search;
        } catch (_) { /* 没有 referrer */ }
        return '';
    }

    function bindBackLink(link) {
        if (!link) return;
        var from = referrerListPage();
        if (from) {
            link.setAttribute('href', from);
        } else {
            var lastSearch = window.navMemory && window.navMemory.lastUrl('/search');
            if (lastSearch) link.setAttribute('href', lastSearch); // 没有记住的搜索：保持 /search
        }
        if (window.readingNav && typeof window.readingNav.bindBack === 'function') {
            window.readingNav.bindBack(link);
        }
    }

    // ── 本地可读（已下载）状态 ──
    // archive：只剩压缩包也能读时的格式（跟一个 CBZ / ZIP 标记）；
    // problem：“下载过 · 本地文件不可用”的原因（文件已删除 / 压缩包损坏 / 压缩包无可阅读图片）
    function setOfflineStatus(readable, archive, problem) {
        var marker = document.getElementById('album-offline-status');
        var readBtn = document.getElementById('local-read-btn');
        if (marker) {
            var offline = marker.querySelector('.offline-badge');
            if (offline) offline.hidden = !readable;
            Array.prototype.forEach.call(marker.querySelectorAll('.badge'), function (b) { b.remove(); });
            var extra = readable ? window.localBadges.archive(archive) : window.localBadges.problem(problem);
            if (extra) marker.appendChild(extra);
            marker.hidden = !(readable || extra);
        }
        if (readBtn) readBtn.hidden = !readable;
    }

    // “部分章节已下载 · M/N 话”：只在服务端能用本地文件和刚取到的章节列表证明时出现（/api/local-chapters，
    // core.chapter_inventory；不联网）；否则只有“已下载内容 · 可离线阅读”，不说完整、也不猜数量
    function setChapterClaim(partial) {
        var marker = document.getElementById('album-offline-status');
        if (!marker) return;
        var old = marker.querySelector('.status-badge-partial');
        if (old) old.remove();
        if (!partial || !(partial.downloaded > 0) || !(partial.total > partial.downloaded)) return;
        var b = document.createElement('span');
        b.className = 'badge status-badge-partial';
        b.title = '本地完整地有 ' + partial.downloaded + ' 话，这部漫画共 ' + partial.total + ' 话（按刚取到的章节列表核对）';
        var i = document.createElement('i');
        i.className = 'bi bi-layers';
        i.setAttribute('aria-hidden', 'true');
        b.appendChild(i);
        b.appendChild(document.createTextNode(' 部分章节已下载 · ' + partial.downloaded + '/' + partial.total + ' 话'));
        marker.appendChild(b);
    }

    function refreshChapterClaim() {
        window.apiFetch('/api/local-chapters/' + encodeURIComponent(String(albumId)),
            { timeoutMs: 15000, abortKey: 'detail-local-chapters' })
        .then(function (data) { if (data.status === 'ok') setChapterClaim(data.partial); })
        .catch(function () { /* 静默：不显示章节数，不影响详情页其他功能 */ });
    }

    function refreshOfflineStatus() {
        window.apiFetch('/api/preview/available', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ album_ids: [String(albumId)] }),
            timeoutMs: 15000,
            abortKey: 'detail-offline-status'
        })
        .then(function (data) {
            if (data.status !== 'ok') return;
            var id = String(albumId);
            var readable = (data.readable || []).map(String).indexOf(id) >= 0;
            setOfflineStatus(readable, (data.archives || {})[id], (data.unavailable || {})[id]);
            if (readable) refreshChapterClaim();
        })
        .catch(function () { /* 静默：保持当前显示，不影响详情页其他功能 */ });
    }

    function bindEvents(album) {
        var selectAllMain = document.getElementById('select-all-chapters');
        var selectAllInline = document.getElementById('select-all-inline');
        var chapterCheckboxes = document.querySelectorAll('.chapter-checkbox-item');

        function toggleAll(checked) {
            chapterCheckboxes.forEach(function (cb) { cb.checked = checked; });
            if (selectAllMain) selectAllMain.checked = checked;
            if (selectAllInline) selectAllInline.checked = checked;
        }

        if (selectAllMain) {
            selectAllMain.addEventListener('change', function () { toggleAll(this.checked); });
        }
        if (selectAllInline) {
            selectAllInline.addEventListener('change', function () { toggleAll(this.checked); });
        }
        chapterCheckboxes.forEach(function (cb) {
            cb.addEventListener('change', function () {
                var all = chapterCheckboxes.length;
                var checked = document.querySelectorAll('.chapter-checkbox-item:checked').length;
                var state = all > 0 && checked === all;
                if (selectAllMain) selectAllMain.checked = state;
                if (selectAllInline) selectAllInline.checked = state;
            });
        });

        // 下载选中章节
        document.getElementById('download-selected-btn').addEventListener('click', function () {
            var checked = document.querySelectorAll('.chapter-checkbox-item:checked');
            if (checked.length === 0) { if (typeof showToast === 'function') showToast('请至少选择一个章节', 'warning'); return; }
            var photoIds = Array.from(checked).map(function (cb) { return cb.value; });
            createDownloadJob(album.album_id, photoIds);
        });

        // 下载全部章节
        document.getElementById('download-all-btn').addEventListener('click', function () {
            var allIds = Array.from(document.querySelectorAll('.chapter-checkbox-item')).map(function (cb) { return cb.value; });
            createDownloadJob(album.album_id, allIds);
        });
    }

    function createDownloadJob(albumId, photoIds) {
        window.apiFetch('/api/jobs', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ album_id: albumId, photo_ids: photoIds }),
            timeoutMs: 15000
        })
        .then(function (data) {
            if (data.status === 'ok') {
                if (typeof showToast === 'function') showToast('已加入下载队列', 'success');
            } else {
                if (typeof showToast === 'function') showToast(data.message || '创建任务失败', 'danger');
            }
        })
        .catch(function (err) {
            if (err && err.name === 'AbortError') return; // pagehide 中止，静默
            if (typeof showToast === 'function') showToast(toastErr(err, '创建任务失败'), 'danger');
        });
    }

    // ── 章节更新 ──
    // 检查新章节（/api/updates，core/update_checker.py）：状态行紧跟在“已下载内容 · 可离线阅读”“部分章节已下载 · M/N 话”下面，
    // 只在本地有已下载内容时显示（eligible；只收藏、下载失败、文件删了的都不显示）。
    //   “立即检查”只 POST /api/updates/<id>/check：只取一次章节列表核对，从不下载；
    //   “选中这些章节”只勾选章节表里的复选框，下载仍要点“下载选中章节”；
    //   章节表里已确认的新章节行标“新”（渲染后按复选框的 photo_id 加上，不自动勾选）。
    // 上游的章节标题只经 textContent / title 进入 DOM。后台检查不弹 Toast。
    var updateData = null;        // 最近一次读到 / 检查后的回答
    var updateChecking = false;   // “立即检查”正在进行
    var updatePollTimer = null;
    var updatePolls = 0;
    var UPDATE_POLL_MS = 3000;    // 后台正在检查这一部时每 3 秒再读一次……
    var UPDATE_POLL_MAX = 40;     // ……最多 40 次

    function updateToast(message, type) {
        if (typeof showToast === 'function') showToast(message, type);
    }

    function updateNode(tag, className, text) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    function updateIcon(name) {
        var i = updateNode('i', 'bi ' + name);
        i.setAttribute('aria-hidden', 'true');
        return i;
    }

    // 章节名：上游标题，没有时“第 N 话”
    function chapterName(c) {
        var title = String((c && c.title) || '').trim();
        return title || ('第' + (c && c.index != null ? c.index : '?') + '话');
    }

    function newChapterNames(chapters, n) {
        var shown = chapters.slice(0, 5).map(chapterName).join(' · ');
        return n > 5 ? shown + ' … 等 ' + n + ' 话' : shown;
    }

    // 回答 → 要显示的内容：lead（徽章或图标 + 文字）、names（新章节名）、meta（时间与说明）、select（勾选按钮文字）
    function updateView(data, busy, now) {
        var fmt = window.updateBadges;
        var u = data.update || {};
        var auto = !!data.auto_enabled;
        var n = Number(u.new_count) || 0;
        var chapters = u.new_chapters || [];
        var upstream = u.upstream_count != null ? u.upstream_count : u.baseline_count;
        var when = function (iso) { return fmt.formatTime(iso, now); };
        var next = auto ? ' · 下次' + fmt.about(u.next_check_at, now) : ' · 自动检查已关闭';
        var reason = fmt.reasonText(u.error && u.error.kind);
        var view = { badge: null, icon: '', warn: false, text: '', names: '', meta: '', select: '' };
        var state = busy || data.checking ? 'checking' : u.state;
        // 失败了但还带着已确认的新章节：照旧显示新章节，只换说明
        var pending = n > 0 ? (Number(u.removed_count) > 0 ? 'changed' : 'new') : '';
        if (state === 'failed' && pending) state = pending;

        if (state === 'checking') {
            view.icon = 'bi-hourglass-split';
            view.text = '正在检查新章节…';
            view.meta = '只取一次章节列表核对，不会下载任何内容';
        } else if (state === 'new' || state === 'changed') {
            if (state === 'new') {
                view.badge = { cls: 'status-badge-update', icon: 'bi-bell', text: '有新章节 · ' + n + ' 话',
                    title: '上游在你下载之后新出的章节；下载时没选的章节不算' };
                view.meta = when(u.confirmed_at) + ' 检查确认 · 检查不会自动下载' + next;
                view.select = '选中这些章节';
            } else {
                view.badge = { cls: 'status-badge-warning', icon: 'bi-exclamation-triangle', text: '章节列表有变动',
                    title: '上游新出现了章节，同时有以前的章节不见了（可能删除或重新上传）' };
                view.text = '新出现 ' + n + ' 话，另有 ' + (Number(u.removed_count) || 0) + ' 话已不在上游';
                view.meta = when(u.last_success_at) + ' 检查 · 请先核对下面的章节列表再决定是否下载';
                view.select = '选中新出现的章节';
            }
            view.names = newChapterNames(chapters, n);
            if (u.state === 'failed') {
                view.meta = '最近一次检查没成功（' + reason + '），'
                    + (auto ? fmt.about(u.next_check_at, now) + ' 自动重试' : '可以稍后再点「立即检查」')
                    + '；上面的新章节是 ' + when(u.confirmed_at) + ' 确认的';
            }
        } else if (state === 'failed') {
            view.warn = true;
            view.icon = 'bi-exclamation-triangle';
            view.text = '这次没检查成功：' + reason;
            if (!auto) view.meta = '自动检查已关闭，可以稍后再点「立即检查」';
            else if (u.paused_until) view.meta = '连续几次连不上服务器，自动检查暂停，' + fmt.about(u.next_check_at, now) + ' 再试';
            else view.meta = fmt.about(u.next_check_at, now) + ' 自动重试';
            if (u.last_success_at) {
                view.meta += ' · 上次成功检查：' + when(u.last_success_at) + '（'
                    + (u.last_result === 'baseline' ? '已记下 ' + upstream + ' 话' : '没有新章节') + '）';
            }
        } else if (state === 'baseline') {
            view.icon = 'bi-bookmark-check';
            view.text = '已记下上游现有的 ' + upstream + ' 话';
            view.meta = when(u.last_success_at) + ' 检查 · 以后新出的章节会在这里提示' + next;
        } else if (state === 'no_update') {
            view.icon = 'bi-check2-circle';
            view.text = '没有新章节';
            view.meta = '上次检查：' + when(u.last_success_at) + ' · 上游共 ' + upstream + ' 话' + next;
        } else {
            // never：还没成功检查过（下载时记下了基线，或是更新前下载的漫画）
            view.icon = 'bi-clock-history';
            view.text = '还没检查过新章节';
            var fromDownload = u.baseline_source === 'download' && u.baseline_count != null;
            if (fromDownload) {
                view.meta = '下载时记下了上游的 ' + u.baseline_count + ' 话'
                    + (auto ? '；后台会自动检查，每部漫画大约一天一次' : ' · 自动检查已关闭，可以点「立即检查」');
            } else {
                view.meta = auto ? '后台会在一天内自动检查，之后每部漫画大约一天一次' : '自动检查已关闭，可以点「立即检查」';
            }
        }
        return view;
    }

    // 章节表：已确认的新章节行（复选框的值是其 photo_id）在章节名后标“新”；不勾选
    function markNewChapters(chapters) {
        Array.prototype.forEach.call(document.querySelectorAll('.update-new-mark'), function (m) { m.remove(); });
        var byId = {};
        (chapters || []).forEach(function (c) { if (c && c.photo_id != null) byId[String(c.photo_id)] = c; });
        Array.prototype.forEach.call(document.querySelectorAll('.chapter-checkbox-item'), function (cb) {
            var c = byId[String(cb.value)];
            var row = c ? cb.closest('tr') : null;
            var cell = row ? row.children[2] : null;
            if (!cell) return;
            var mark = updateNode('span', 'badge status-badge-update ms-1 update-new-mark', '新');
            var when = window.updateBadges.formatTime(c.confirmed_at);
            mark.title = (when ? when + ' ' : '') + '检查确认的新章节';
            cell.appendChild(mark);
        });
    }

    function renderUpdateStatus(data) {
        var box = document.getElementById('album-update-status');
        if (!box || !window.updateBadges) return;
        box.textContent = '';
        var eligible = !!(data && data.eligible && data.update);
        markNewChapters(eligible ? data.update.new_chapters : []);
        if (!eligible) {
            box.hidden = true;   // 本地没有已下载的内容：不检查，也不显示以前的检查结果
            return;
        }
        var busy = updateChecking || !!data.checking;
        var view = updateView(data, busy);
        if (view.badge) {
            var badge = updateNode('span', 'badge ' + view.badge.cls);
            badge.title = view.badge.title;
            badge.appendChild(updateIcon(view.badge.icon));
            badge.appendChild(document.createTextNode(' ' + view.badge.text));
            box.appendChild(badge);
        }
        if (view.text) {
            var lead = updateNode('span', view.warn ? 'update-status-warn' : 'update-status-text');
            if (view.icon) {
                lead.appendChild(updateIcon(view.icon));
                lead.appendChild(document.createTextNode(' '));
            }
            lead.appendChild(document.createTextNode(view.text));
            box.appendChild(lead);
        }
        if (view.select) {
            var select = updateNode('button', 'btn btn-sm btn-outline-primary');
            select.type = 'button';
            select.setAttribute('data-action', 'select-new-chapters');
            select.appendChild(updateIcon('bi-check2-square'));
            select.appendChild(document.createTextNode(' ' + view.select));
            select.addEventListener('click', selectNewChapters);
            box.appendChild(select);
        }
        var check = updateNode('button', 'btn btn-sm btn-outline-primary');
        check.type = 'button';
        check.setAttribute('data-action', 'check-updates');
        check.title = '只取一次章节列表核对，不会下载任何内容';
        if (busy) {
            check.disabled = true;
            check.setAttribute('aria-busy', 'true');
            check.appendChild(updateIcon('bi-hourglass-split'));
            check.appendChild(document.createTextNode(' 检查中…'));
        } else {
            check.appendChild(updateIcon('bi-arrow-repeat'));
            check.appendChild(document.createTextNode(' 立即检查'));
            check.addEventListener('click', checkUpdatesNow);
        }
        box.appendChild(check);
        if (view.names) box.appendChild(updateNode('div', 'update-new-list', view.names));
        if (view.meta) box.appendChild(updateNode('div', 'update-status-meta', view.meta));
        box.hidden = false;
    }

    function stopUpdatePoll() {
        if (updatePollTimer) clearTimeout(updatePollTimer);
        updatePollTimer = null;
    }

    // 读检查状态（只读数据库；15s 超时）。后台正在检查这一部时每 3 秒再读一次，最多 40 次
    function refreshUpdateStatus(fromPoll) {
        stopUpdatePoll();
        if (fromPoll !== true) updatePolls = 0;
        window.apiFetch('/api/updates/' + encodeURIComponent(String(albumId)),
            { timeoutMs: 15000, abortKey: 'detail-update-status' })
        .then(function (data) {
            if (data.status !== 'ok' || updateChecking) return;   // “立即检查”的回答会自己显示
            // 后台轮询读到的和上次一样：不重建这一行（读屏软件不会每 3 秒重复播报）
            if (fromPoll === true && updateData && JSON.stringify(data) === JSON.stringify(updateData)) {
                data = updateData;
            } else {
                updateData = data;
                renderUpdateStatus(data);
            }
            if (data.eligible && data.checking && updatePolls < UPDATE_POLL_MAX) {
                updatePolls++;
                updatePollTimer = setTimeout(function () {
                    updatePollTimer = null;
                    refreshUpdateStatus(true);
                }, UPDATE_POLL_MS);
            }
        })
        .catch(function () { /* 静默：保持当前显示，不影响详情页其他功能 */ });
    }

    // “立即检查”之后的 Toast：[文案, 类型]
    function checkOutcomeToast(data) {
        var u = data.update || {};
        var outcome = data.outcome;
        if (outcome === 'coalesced') {
            // 等锁的时候这一部刚被查过：按查完的状态提示
            outcome = { 'new': 'new', changed: 'changed', no_update: 'no_update', baseline: 'baseline', failed: 'failed' }[u.state]
                || 'throttled';
        }
        var upstream = u.upstream_count != null ? u.upstream_count : u.baseline_count;
        switch (outcome) {
            case 'new': return ['发现 ' + (Number(u.new_count) || 0) + ' 话新章节（只提示，不会自动下载）', 'success'];
            case 'changed': return ['章节列表有变动，请核对', 'warning'];
            case 'no_update': return ['没有新章节', 'info'];
            case 'baseline': return ['已记下上游现有的 ' + upstream + ' 话', 'info'];
            case 'failed': return ['这次没检查成功：' + window.updateBadges.reasonText(u.error && u.error.kind), 'warning'];
            case 'throttled': return ['刚刚检查过，结果如上', 'info'];
        }
        return null;
    }

    // 立即检查：只 POST /api/updates/<id>/check（请求体不需要），服务端只取一次章节列表，从不下载。
    // 最多等 20 秒拿锁再加一次检查自己的时限：前端给 100 秒
    // 重建这一行后把键盘焦点放回“立即检查”（点之前焦点在它上面时）
    function refocusCheckButton(refocus) {
        if (!refocus) return;
        var b = document.querySelector('#album-update-status [data-action="check-updates"]');
        if (b && !b.disabled) b.focus();
    }

    function checkUpdatesNow() {
        if (updateChecking) return;
        var active = document.activeElement;
        var refocus = !!(active && active.getAttribute && active.getAttribute('data-action') === 'check-updates');
        updateChecking = true;
        stopUpdatePoll();
        if (updateData) renderUpdateStatus(updateData);   // 显示“正在检查新章节…”与“检查中…”
        window.apiFetch('/api/updates/' + encodeURIComponent(String(albumId)) + '/check',
            { method: 'POST', timeoutMs: 100000, abortKey: 'detail-update-check' })
        .then(function (data) {
            updateChecking = false;
            if (data.status !== 'ok') {
                if (updateData) renderUpdateStatus(updateData);
                updateToast(data.message || '检查新章节失败', 'danger');
                return;
            }
            updateData = data;
            renderUpdateStatus(data);
            refocusCheckButton(refocus);
            var toast = checkOutcomeToast(data);
            if (toast) updateToast(toast[0], toast[1]);
        })
        .catch(function (err) {
            updateChecking = false;
            if (updateData) renderUpdateStatus(updateData);
            refocusCheckButton(refocus);
            if (err && err.name === 'AbortError') {
                // pagehide 中止（检查在服务端照样完成、结果照样保存）：不提示，悄悄重新读一次，
                // 从 bfcache 回来时不会停在“检查中…”
                refreshUpdateStatus();
                return;
            }
            if (err && err.status === 409) {
                // 本地还没有已下载的内容 / 正在检查别的漫画：服务端的说明
                updateToast(err.message || '现在不能检查新章节', 'warning');
            } else {
                updateToast(toastErr(err, '检查新章节失败'), 'danger');
            }
            refreshUpdateStatus();
        });
    }

    // 选中这些章节：只勾选章节表里这些新章节的复选框（其余取消勾选），不创建下载任务
    function selectNewChapters() {
        var u = updateData && updateData.eligible ? (updateData.update || {}) : {};
        var wanted = {};
        (u.new_chapters || []).forEach(function (c) { if (c && c.photo_id != null) wanted[String(c.photo_id)] = true; });
        var total = Object.keys(wanted).length;
        if (!total) return;
        var boxes = Array.prototype.slice.call(document.querySelectorAll('.chapter-checkbox-item'));
        var present = boxes.filter(function (cb) { return wanted[String(cb.value)]; });
        if (present.length < total) {
            // 章节表是检查之前取的：刷新页面会重新取章节列表（确认新章节时服务端已丢掉这部漫画的详情缓存）
            updateToast('章节列表里还没有这些新章节，请刷新页面后再选', 'warning');
            return;
        }
        boxes.forEach(function (cb) { cb.checked = !!wanted[String(cb.value)]; });
        var all = boxes.length > 0 && present.length === boxes.length;
        ['select-all-chapters', 'select-all-inline'].forEach(function (id) {
            var b = document.getElementById(id);
            if (b) b.checked = all;
        });
        // 瞬时定位到第一话新章节（Bootstrap 的 :root { scroll-behavior: smooth } 会让它慢慢滑过去）
        var row = present[0].closest('tr');
        if (row && typeof row.scrollIntoView === 'function') row.scrollIntoView({ block: 'center', behavior: 'instant' });
        updateToast('已选中 ' + present.length + ' 话新章节，点「下载选中章节」开始下载', 'info');
    }
    // ── 章节更新结束 ──

    // ── 收藏功能 ──
    // 注意：收藏按钮由 renderAlbum() 动态生成，状态检查与事件绑定必须在渲染完成后执行。
    //（原实现在脚本加载时立即执行，此时按钮尚不存在，查询/绑定均不生效，属于已修复的死代码；
    //  原实现的添加收藏分支还引用了作用域内不存在的 album 变量，会抛 ReferenceError）
    // 收藏按钮的两种状态只在这里切换：页面加载时查到“已收藏”与点击收藏后得到完全相同的按钮
    //（原来加载时只加了 btn-warning、没去掉 btn-outline-warning，实心星与底色同为 --warning，星形看不见）
    function setWishlistButton(btn, starred) {
        var icon = btn.querySelector('i');
        if (icon) icon.className = starred ? 'bi bi-star-fill' : 'bi bi-star';
        btn.classList.toggle('btn-warning', starred);
        btn.classList.toggle('btn-outline-warning', !starred);
        // 切换按钮：名称固定为“收藏”，状态只由 aria-pressed 表达（名称跟着变会读成“取消收藏，已按下”）；title 说明点击效果
        btn.title = starred ? '取消收藏' : '收藏';
        btn.setAttribute('aria-label', '收藏');
        btn.setAttribute('aria-pressed', starred ? 'true' : 'false');
    }

    function initWishlist(album) {
        // 检查当前条目是否已收藏（15s 超时）
        window.apiFetch('/api/wishlist/' + albumId, { timeoutMs: 15000 })
            .then(function (data) {
                if (data.status === 'ok' && data.item) {
                    var b = document.getElementById('wishlist-toggle-btn');
                    if (b) setWishlistButton(b, true);
                }
            })
            .catch(function () { /* 静默 */ });

        // 绑定收藏按钮（直接在元素上绑；按钮随 renderAlbum 重建，不会累积监听器）
        var btn = document.getElementById('wishlist-toggle-btn');
        if (!btn) return;
        btn.addEventListener('click', function () {
            var isStarred = btn.classList.contains('btn-warning');
            if (isStarred) {
                window.apiFetch('/api/wishlist/' + albumId, { method: 'DELETE', timeoutMs: 15000 })
                    .then(function (data) {
                        if (data.status === 'ok') {
                            setWishlistButton(btn, false);
                            if (typeof showToast === 'function') showToast('已取消收藏', 'info');
                        } else {
                            if (typeof showToast === 'function') showToast(data.message || '取消收藏失败', 'danger');
                        }
                    })
                    .catch(function (err) {
                        if (err && err.name === 'AbortError') return; // pagehide 中止，静默
                        if (typeof showToast === 'function') showToast(toastErr(err, '取消收藏失败'), 'danger');
                    });
            } else {
                var titleEl = document.querySelector('.card-title');
                var title = (album && album.title) || (titleEl ? titleEl.textContent.trim() : albumId);
                var author = (album && album.author) || '';
                var coverUrl = (album && album.cover) || '';
                window.apiFetch('/api/wishlist', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        album_id: albumId,
                        title: title,
                        author: author,
                        cover_url: coverUrl
                    }),
                    timeoutMs: 15000
                })
                .then(function (data) {
                    if (data.status === 'ok') {
                        setWishlistButton(btn, true);
                        if (typeof showToast === 'function') showToast('已添加收藏', 'success');
                    } else {
                        if (typeof showToast === 'function') showToast(data.message || '收藏失败', 'danger');
                    }
                })
                .catch(function (err) {
                    if (err && err.name === 'AbortError') return; // pagehide 中止，静默
                    if (typeof showToast === 'function') showToast(toastErr(err, '收藏失败'), 'danger');
                });
            }
        });
    }

    // ══════════════════════════════════════════════════════════
    //  标签管理功能
    // ══════════════════════════════════════════════════════════

    // 标签区的按钮与输入框：#album-content 常驻于模板，委托监听器只绑定一次
    container.addEventListener('click', function (event) {
        var target = event.target.closest('#album-tags-manager [data-action]');
        if (!target || target.disabled) return;
        var action = target.getAttribute('data-action');
        if (action === 'remove-tag') {
            removeAlbumTag(albumId, target.dataset.tag);
        } else if (action === 'add-tag') {
            addCustomTag(albumId);
        } else if (action === 'sync-tags') {
            syncAlbumTags(albumId);
        }
    });
    container.addEventListener('keydown', function (event) {
        // 输入法组字中的回车只是确认候选词，不提交
        if (event.key !== 'Enter' || event.isComposing || event.target.id !== 'tag-add-input') return;
        event.preventDefault();
        addCustomTag(albumId);
    });

    // 加载专辑标签（含来源信息，15s 超时）
    function loadAlbumTags(albumId) {
        var box = document.getElementById('album-tags-manager');
        if (!box) return;
        box.innerHTML = '<div class="text-muted small"><i class="bi bi-arrow-repeat"></i> 加载标签中...</div>';

        window.apiFetch('/api/library/' + encodeURIComponent(albumId) + '/tags', { timeoutMs: 15000 })
            .then(function (data) {
                if (data.status !== 'ok') {
                    box.innerHTML = '<div class="text-muted small">标签服务暂不可用</div>';
                    return;
                }
                renderTagManager(data.tags || []);
            })
            .catch(function (err) {
                if (err && err.name === 'AbortError') return; // pagehide 中止，静默
                box.innerHTML = '<div class="text-muted small">标签服务暂不可用</div>';
            });
    }

    function createEl(tag, className, text) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
    }

    function actionButton(className, action, iconClass, label, title) {
        var button = createEl('button', className);
        button.type = 'button';
        button.setAttribute('data-action', action);
        if (title) button.title = title;
        button.appendChild(createEl('i', 'bi ' + iconClass)).setAttribute('aria-hidden', 'true');
        button.appendChild(document.createTextNode(' ' + label));
        return button;
    }

    // 标签名是远程/用户数据：只经 textContent / dataset / setAttribute 进入 DOM
    function renderTagManager(tags) {
        var box = document.getElementById('album-tags-manager');
        if (!box) return;

        var area = createEl('div', 'tag-edit-area');

        var heading = createEl('div', 'mb-1');
        var strong = createEl('strong');
        strong.appendChild(createEl('i', 'bi bi-tags')).setAttribute('aria-hidden', 'true');
        strong.appendChild(document.createTextNode(' 本地标签'));
        heading.appendChild(strong);
        heading.appendChild(document.createTextNode(' '));
        heading.appendChild(createEl('span', 'text-muted small', '（点击 × 删除）'));
        area.appendChild(heading);

        // 标签列表
        var list = createEl('div', 'd-flex flex-wrap gap-1 mb-2');
        list.id = 'local-tags-list';
        if (tags.length === 0) {
            list.appendChild(createEl('span', 'text-muted small', '暂无标签'));
        } else {
            tags.forEach(function (tag) {
                var name = String(tag.tag);
                var chip = createEl('span', (tag.source === 'user' ? 'tag-source-user' : 'tag-source-auto') + ' badge tag-chip me-1', name);
                var remove = createEl('button', 'tag-edit-btn');
                remove.type = 'button';
                remove.setAttribute('data-action', 'remove-tag');
                remove.dataset.tag = name;
                remove.title = '删除标签';
                remove.setAttribute('aria-label', '删除标签 ' + name);
                remove.appendChild(createEl('span', '', '×')).setAttribute('aria-hidden', 'true');
                chip.appendChild(remove);
                list.appendChild(chip);
            });
        }
        area.appendChild(list);

        // 添加标签
        var row = createEl('div', 'tag-edit-row');
        var input = createEl('input', 'form-control form-control-sm');
        input.type = 'text';
        input.id = 'tag-add-input';
        input.placeholder = '输入标签名称...';
        input.maxLength = 50;
        input.setAttribute('aria-label', '新标签名称');
        row.appendChild(input);
        row.appendChild(actionButton('btn btn-outline-success btn-sm', 'add-tag', 'bi-plus-lg', '添加'));
        row.appendChild(actionButton('btn btn-outline-info btn-sm', 'sync-tags', 'bi-arrow-repeat', '同步', '从 18comic 同步最新标签'));
        area.appendChild(row);

        box.replaceChildren(area);
    }

    // 添加自定义标签
    function addCustomTag(albumId) {
        var input = document.getElementById('tag-add-input');
        if (!input) return;
        var tag = input.value.trim();
        if (!tag) {
            if (typeof showToast === 'function') showToast('请输入标签名称', 'warning');
            return;
        }
        if (tag.length > 50) {
            if (typeof showToast === 'function') showToast('标签名称不能超过 50 个字符', 'warning');
            return;
        }

        var btn = document.querySelector('#album-tags-manager [data-action="add-tag"]');
        if (btn) btn.disabled = true;

        window.apiFetch('/api/library/' + encodeURIComponent(albumId) + '/tags', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ tags: [tag] }),
            timeoutMs: 15000
        })
        .then(function (data) {
            if (data.status === 'ok') {
                if (typeof showToast === 'function') showToast('标签已添加', 'success');
                input.value = '';
                loadAlbumTags(albumId);
            } else {
                if (typeof showToast === 'function') showToast(data.message || '添加标签失败', 'danger');
            }
        })
        .catch(function (err) {
            if (err && err.name === 'AbortError') return; // pagehide 中止，静默
            if (typeof showToast === 'function') showToast(toastErr(err, '添加标签失败'), 'danger');
        })
        .finally(function () {
            if (btn) btn.disabled = false;
        });
    }

    // 删除标签
    function removeAlbumTag(albumId, tag) {
        if (!tag) return;
        if (!confirm('确定要删除标签 "' + tag + '" 吗？')) return;
        window.apiFetch('/api/library/' + encodeURIComponent(albumId) + '/tags', {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ tags: [tag] }),
            timeoutMs: 15000
        })
        .then(function (data) {
            if (data.status === 'ok') {
                if (typeof showToast === 'function') showToast('标签已删除', 'success');
                loadAlbumTags(albumId);
            } else {
                if (typeof showToast === 'function') showToast(data.message || '删除标签失败', 'danger');
            }
        })
        .catch(function (err) {
            if (err && err.name === 'AbortError') return; // pagehide 中止，静默
            if (typeof showToast === 'function') showToast(toastErr(err, '删除标签失败'), 'danger');
        });
    }

    // 同步标签（从 18comic）
    function syncAlbumTags(albumId) {
        var btn = document.querySelector('#album-tags-manager [data-action="sync-tags"]');
        if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> 同步中...'; }

        window.apiFetch('/api/library/' + encodeURIComponent(albumId) + '/tags/sync', {
            method: 'POST',
            timeoutMs: 15000
        })
        .then(function (data) {
            if (data.status === 'ok') {
                if (typeof showToast === 'function') showToast('同步完成，同步了 ' + (data.synced || 0) + ' 个标签', 'success');
                loadAlbumTags(albumId);
            } else {
                if (typeof showToast === 'function') showToast(data.message || '同步失败', 'danger');
            }
        })
        .catch(function (err) {
            if (err && err.name === 'AbortError') return; // pagehide 中止，静默
            if (typeof showToast === 'function') showToast(toastErr(err, '同步失败'), 'danger');
        })
        .finally(function () {
            if (btn) { btn.disabled = false; btn.innerHTML = '<i class="bi bi-arrow-repeat" aria-hidden="true"></i> 同步'; }
        });
    }

    loadAlbum();

    // 从 bfcache 返回（如 详情 → 下载管理 → 后退）：重新判断是否已下载、重新读检查新章节的状态；
    // 离开时详情请求被 pagehide 中止、页面仍停在加载中的，重新加载
    window.addEventListener('pageshow', function (event) {
        if (!event.persisted) return;
        if (rendered) {
            refreshOfflineStatus();
            refreshUpdateStatus();
        } else if (errorDiv.classList.contains('d-none')) {
            loadAlbum();
        }
    });
})(albumId);
