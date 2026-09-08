/**
 * 漫画详情页 JS
 * 依赖: utils.js (window.escapeHtml, window.escapeHtmlAttr, window.apiFetch)
 * 依赖: base.html 内联 showToast
 * 变量: albumId 在模板中通过 <script>var albumId = ...</script> 定义
 *
 * 请求统一走 window.apiFetch（自带 AbortController/超时/pagehide 清理），
 * 解决多次页面跳转后残留请求占满浏览器连接池的问题
 */
(function (albumId) {
    'use strict';

    var container = document.getElementById('album-content');
    var spinner = document.getElementById('loading-spinner');
    var errorDiv = document.getElementById('error-message');

    // ── 异常 → Toast 文案（服务端错误/超时显示具体消息，网络错误显示通用文案） ──
    function toastErr(err, fallback) {
        if (err && (err.status || err.isTimeout)) return err.message || fallback || '操作失败';
        return '网络错误';
    }

    // 获取详情（30s 超时）
    window.apiFetch('/api/album/' + albumId, { timeoutMs: 30000 })
        .then(function (data) {
            if (data.status !== 'ok') {
                throw new Error(data.message || '获取失败');
            }
            renderAlbum(data.data);
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

    function renderAlbum(album) {
        spinner.classList.add('d-none');
        container.classList.remove('d-none');

        var html = '';

        // 左侧封面
        html += '<div class="col-12 col-md-4 col-lg-3 mb-4">';
        if (album.cover) {
            html += '<img src="' + window.escapeHtmlAttr(album.cover) + '" class="detail-cover shadow" alt="' + window.escapeHtmlAttr(album.title) + '">';
        } else {
            html += '<div class="placeholder-cover" style="width:100%;max-width:350px;height:450px;"><i class="bi bi-image" style="font-size:3rem;"></i></div>'
        }
        html += '</div>';

        // 右侧信息
        html += '<div class="col-12 col-md-8 col-lg-9">';
        html += '<div class="card mb-4"><div class="card-body">';
        html += '<h3 class="card-title">' + window.escapeHtml(album.title) + ' <button type="button" id="wishlist-toggle-btn" class="btn btn-sm btn-outline-warning ms-2" title="收藏"><i class="bi bi-star"></i></button></h3>';
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

        html += '</div></div>';

        // 章节列表
        html += '<div class="card">';
        html += '<div class="card-header d-flex justify-content-between align-items-center">';
        html += '<span><i class="bi bi-list-ol"></i> 章节列表 <span class="badge bg-secondary">' + (album.photos ? album.photos.length : 0) + '</span></span>';
        html += '<div class="form-check"><input type="checkbox" id="select-all-chapters" class="form-check-input"><label class="form-check-label" for="select-all-chapters">全选</label></div>';
        html += '</div>';

        html += '<div class="card-body p-0">';
        if (album.photos && album.photos.length > 0) {
            html += '<table class="table table-hover chapter-table mb-0"><thead><tr><th class="chapter-checkbox"><input type="checkbox" id="select-all-inline" class="form-check-input" title="全选"></th><th style="width:60px">序号</th><th>章节名</th><th style="width:80px">页数</th></tr></thead><tbody>';

            album.photos.forEach(function (photo, idx) {
                html += '<tr>';
                html += '<td class="chapter-checkbox"><input type="checkbox" name="chapters" value="' + window.escapeHtmlAttr(photo.photo_id) + '" class="form-check-input chapter-checkbox-item"></td>';
                html += '<td>' + (idx + 1) + '</td>';
                html += '<td>' + window.escapeHtml(photo.title || '-') + '</td>';
                html += '<td>' + window.escapeHtml(String(photo.page_count || '?')) + '</td>';
                html += '</tr>';
            });

            html += '</tbody></table>';
        } else {
            html += '<div class="text-center text-muted py-4"><i class="bi bi-journal-text" style="font-size:2rem;"></i><p class="mt-2 mb-0">暂无章节信息</p></div>';
        }
        html += '</div>';

        // 底部按钮
        html += '<div class="card-footer"><div class="d-flex gap-2">';
        html += '<button type="button" id="download-selected-btn" class="btn btn-primary"><i class="bi bi-download"></i> 下载选中章节</button>';
        html += '<button type="button" id="download-all-btn" class="btn btn-success"><i class="bi bi-download"></i> 下载全部</button>';
        html += '<a href="/search" class="btn btn-outline-secondary ms-auto"><i class="bi bi-arrow-left"></i> 返回搜索</a>';
        html += '</div></div>';

        html += '</div>'; // 章节卡片结束
        html += '</div>'; // 右侧列结束

        container.innerHTML = html;

        // 绑定事件
        bindEvents(album);
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

    // ── 收藏功能 ──
    // 注意：收藏按钮由 renderAlbum() 动态生成，状态检查与事件绑定必须在渲染完成后执行。
    //（原实现在脚本加载时立即执行，此时按钮尚不存在，查询/绑定均不生效，属于已修复的死代码；
    //  原实现的添加收藏分支还引用了作用域内不存在的 album 变量，会抛 ReferenceError）
    function initWishlist(album) {
        // 检查当前条目是否已收藏（15s 超时）
        window.apiFetch('/api/wishlist/' + albumId, { timeoutMs: 15000 })
            .then(function (data) {
                if (data.status === 'ok' && data.item) {
                    var b = document.getElementById('wishlist-toggle-btn');
                    if (b) {
                        var icon = b.querySelector('i');
                        icon.className = 'bi bi-star-fill';
                        b.classList.add('btn-warning');
                        b.title = '取消收藏';
                    }
                }
            })
            .catch(function () { /* 静默 */ });

        // 绑定收藏按钮（直接在元素上绑；按钮随 renderAlbum 重建，不会累积监听器）
        var btn = document.getElementById('wishlist-toggle-btn');
        if (!btn) return;
        btn.addEventListener('click', function () {
            var icon = btn.querySelector('i');
            var isStarred = icon.classList.contains('bi-star-fill');
            if (isStarred) {
                window.apiFetch('/api/wishlist/' + albumId, { method: 'DELETE', timeoutMs: 15000 })
                    .then(function (data) {
                        if (data.status === 'ok') {
                            icon.className = 'bi bi-star';
                            btn.classList.remove('btn-warning');
                            btn.classList.add('btn-outline-warning');
                            btn.title = '收藏';
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
                var title = titleEl ? titleEl.textContent.trim() : albumId;
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
                        icon.className = 'bi bi-star-fill';
                        btn.classList.remove('btn-outline-warning');
                        btn.classList.add('btn-warning');
                        btn.title = '取消收藏';
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

    // 加载专辑标签（含来源信息，15s 超时）
    function loadAlbumTags(albumId) {
        var container = document.getElementById('album-tags-manager');
        if (!container) return;
        container.innerHTML = '<div class="text-muted small"><i class="bi bi-arrow-repeat"></i> 加载标签中...</div>';

        window.apiFetch('/api/library/' + encodeURIComponent(albumId) + '/tags', { timeoutMs: 15000 })
            .then(function (data) {
                if (data.status !== 'ok') {
                    container.innerHTML = '<div class="text-muted small">标签服务暂不可用</div>';
                    return;
                }
                renderTagManager(albumId, data.tags || []);
            })
            .catch(function (err) {
                if (err && err.name === 'AbortError') return; // pagehide 中止，静默
                container.innerHTML = '<div class="text-muted small">标签服务暂不可用</div>';
            });
    }

    function renderTagManager(albumId, tags) {
        var container = document.getElementById('album-tags-manager');
        if (!container) return;

        var html = '<div class="tag-edit-area">';
        html += '<div class="mb-1"><strong><i class="bi bi-tags"></i> 本地标签</strong> <span class="text-muted small">（点击 ❌ 删除）</span></div>';

        // 标签列表
        html += '<div class="d-flex flex-wrap gap-1 mb-2" id="local-tags-list">';
        if (tags.length === 0) {
            html += '<span class="text-muted small">暂无标签</span>';
        } else {
            for (var i = 0; i < tags.length; i++) {
                var tag = tags[i];
                var sourceClass = tag.source === 'user' ? 'tag-source-user badge' : 'tag-source-auto badge';
                html += '<span class="' + sourceClass + ' me-1" style="font-size:0.75rem;padding:0.2rem 0.5rem;border-radius:10px;">'
                    + window.escapeHtml(tag.tag)
                    + ' <span class="tag-edit-btn" onclick="removeAlbumTag(\'' + encodeURIComponent(albumId) + '\',\'' + window.escapeHtmlAttr(tag.tag) + '\')" title="删除标签">&times;</span>'
                    + '</span>';
            }
        }
        html += '</div>';

        // 添加标签
        html += '<div class="tag-edit-row">';
        html += '<input type="text" id="tag-add-input" class="form-control form-control-sm" placeholder="输入标签名称..."'
            + ' onkeydown="if(event.key===\'Enter\') addCustomTag(\'' + encodeURIComponent(albumId) + '\')"'
            + ' maxlength="50">';
        html += '<button class="btn btn-outline-success btn-sm" onclick="addCustomTag(\'' + encodeURIComponent(albumId) + '\')"><i class="bi bi-plus-lg"></i> 添加</button>';
        html += '<button class="btn btn-outline-info btn-sm" onclick="syncAlbumTags(\'' + encodeURIComponent(albumId) + '\')" title="从 18comic 同步最新标签"><i class="bi bi-arrow-repeat"></i> 同步</button>';
        html += '</div>';

        html += '</div>'; // tag-edit-area
        container.innerHTML = html;
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

        var btn = input.nextElementSibling;
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
        var btn = document.querySelector('#album-tags-manager .btn-outline-info');
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
            if (btn) { btn.disabled = false; btn.innerHTML = '<i class="bi bi-arrow-repeat"></i> 同步'; }
        });
    }

    // 暴露给 onclick
    window.addCustomTag = addCustomTag;
    window.removeAlbumTag = removeAlbumTag;
    window.syncAlbumTags = syncAlbumTags;
    window.loadAlbumTags = loadAlbumTags;
})(albumId);
