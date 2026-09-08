// ── wishlist.js — 收藏清单页面逻辑 ──
// 使用 window.escapeHtml / window.apiFetch (来自 utils.js)
// 请求统一走 apiFetch（自带 AbortController/超时/pagehide 清理）
(function () {
  'use strict';

  var currentPage = 1;
  var pageSize = 50;
  var allItems = [];
  var currentQuery = '';
  var currentSort = 'added_at';
  var _refreshTimer = null;

  // ── 加载收藏列表 ──
  async function loadWishlist(page) {
    page = page || currentPage;
    var q = currentQuery;
    var sort = currentSort;
    var tbody;
    try {
      var url = '/api/wishlist?page=' + page + '&page_size=' + pageSize;
      if (q) url += '&q=' + encodeURIComponent(q);
      if (sort) url += '&sort=' + encodeURIComponent(sort);
      // abortKey：翻页/自动刷新时自动中止上一次未完成的列表请求
      var data = await window.apiFetch(url, { timeoutMs: 15000, abortKey: 'wishlist-list' });
      tbody = document.getElementById('wishlist-body');
      if (data.status !== 'ok') {
        // 显示错误状态在表格中
        if (tbody) tbody.innerHTML = '<tr><td colspan="7" class="text-center text-muted py-4">加载失败: ' + window.escapeHtml(data.message || '未知错误') + '</td></tr>';
        return;
      }

      allItems = data.items || [];
      var total = data.total || 0;
      currentPage = data.page || 1;

      if (allItems.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" class="text-center text-muted py-4">收藏列表为空' + (currentQuery ? '（搜索无结果）' : '') + '</td></tr>';
        document.getElementById('wishlist-pagination').classList.add('d-none');
        return;
      }

      var statusMap = {
        'none': '<span class="badge bg-secondary">未下载</span>',
        'queued': '<span class="badge bg-info text-dark">排队中</span>',
        'downloading': '<span class="badge bg-primary">下载中</span>',
        'completed': '<span class="badge bg-success">已完成</span>',
        'failed': '<span class="badge bg-danger">失败</span>',
      };

      var html = '';
      for (var i = 0; i < allItems.length; i++) {
        var item = allItems[i];
        html += '<tr>'
          + '<td><input type="checkbox" class="item-checkbox" value="' + window.escapeHtml(item.album_id) + '"></td>'
          + '<td><a href="/album/' + encodeURIComponent(item.album_id) + '" target="_blank" rel="noopener noreferrer">' + window.escapeHtml(item.album_id) + '</a></td>'
          + '<td>' + window.escapeHtml(item.title || '-') + '</td>'
          + '<td>' + window.escapeHtml(item.author || '-') + '</td>'
          + '<td>' + (statusMap[item.download_status] || window.escapeHtml(item.download_status) || '<span class="badge bg-secondary">未下载</span>') + '</td>'
          + '<td>' + (item.added_at ? new Date(item.added_at).toLocaleString() : '-') + '</td>'
          + '<td>'
          + '<button class="btn btn-sm btn-outline-success" onclick="downloadSingle(\'' + encodeURIComponent(item.album_id) + '\')" title="下载">'
          + '<i class="bi bi-download"></i>'
          + '</button>'
          + '<a href="/album/' + encodeURIComponent(item.album_id) + '" class="btn btn-sm btn-outline-info" target="_blank" rel="noopener noreferrer" title="详情">'
          + '<i class="bi bi-info-circle"></i>'
          + '</a>'
          + '<button class="btn btn-sm btn-outline-danger" onclick="removeSingle(\'' + encodeURIComponent(item.album_id) + '\')" title="移除">'
          + '<i class="bi bi-trash"></i>'
          + '</button>'
          + '</td>'
          + '</tr>';
      }
      tbody.innerHTML = html;

      // 分页
      renderPagination(total, page);
      updateBatchActions();
    } catch (e) {
      if (e && e.name === 'AbortError') return; // pagehide/新请求中止，静默
      tbody = document.getElementById('wishlist-body');
      if (!tbody) return;
      if (e && e.status) {
        // HTTP 错误：保持原有「加载失败: 服务端消息」文案
        tbody.innerHTML = '<tr><td colspan="7" class="text-center text-muted py-4">加载失败: ' + window.escapeHtml(e.message || '未知错误') + '</td></tr>';
      } else {
        tbody.innerHTML = '<tr><td colspan="7" class="text-center text-danger py-4">网络错误: ' + window.escapeHtml(e.message) + '</td></tr>';
      }
    }
  }

  function renderPagination(total, page) {
    var nav = document.getElementById('wishlist-pagination');
    nav.classList.remove('d-none');
    var totalPages = Math.max(1, Math.ceil(total / pageSize));
    var ul = nav.querySelector('ul');
    var html = '';
    if (page > 1) html += '<li class="page-item"><a class="page-link" href="#" onclick="loadWishlist(' + (page - 1) + ');return false;">上一页</a></li>';
    var start = Math.max(1, page - 2);
    var end = Math.min(totalPages, start + 4);
    start = Math.max(1, end - 4);
    for (var p = start; p <= end; p++) {
      html += '<li class="page-item' + (p === page ? ' active' : '') + '"><a class="page-link" href="#" onclick="loadWishlist(' + p + ');return false;">' + p + '</a></li>';
    }
    if (page < totalPages) html += '<li class="page-item"><a class="page-link" href="#" onclick="loadWishlist(' + (page + 1) + ');return false;">下一页</a></li>';
    ul.innerHTML = html;
  }

  // ── 单个操作 ──
  async function downloadSingle(albumId) {
    try {
      var data = await window.apiFetch('/api/wishlist/download', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ids: [albumId]}),
        timeoutMs: 60000,
      });
      if (data.status === 'ok') {
        if (typeof showToast === 'function') showToast('已创建下载任务', 'success');
        loadWishlist(currentPage);
      } else {
        if (typeof showToast === 'function') showToast(data.message || '下载失败', 'danger');
      }
    } catch (e) {
      if (e && e.name === 'AbortError') return; // pagehide 中止，静默
      if (typeof showToast === 'function') {
        showToast((e && e.status) ? (e.message || '下载失败') : ('网络错误: ' + e.message), 'danger');
      }
    }
  }

  async function removeSingle(albumId) {
    if (!confirm('确定要移除该收藏吗？')) return;
    try {
      var data = await window.apiFetch('/api/wishlist/' + encodeURIComponent(albumId), { method: 'DELETE', timeoutMs: 15000 });
      if (data.status === 'ok') {
        if (typeof showToast === 'function') showToast('已移除收藏', 'success');
        loadWishlist(currentPage);
      } else {
        if (typeof showToast === 'function') showToast(data.message || '移除失败', 'danger');
      }
    } catch (e) {
      if (e && e.name === 'AbortError') return; // pagehide 中止，静默
      if (typeof showToast === 'function') {
        showToast((e && e.status) ? (e.message || '移除失败') : ('网络错误: ' + e.message), 'danger');
      }
    }
  }

  // ── 批量操作 ──
  function toggleSelectAll() {
    var checked = document.getElementById('select-all').checked;
    var cbs = document.querySelectorAll('.item-checkbox');
    for (var i = 0; i < cbs.length; i++) {
      cbs[i].checked = checked;
    }
    updateBatchActions();
  }

  function getSelectedIds() {
    var checked = document.querySelectorAll('.item-checkbox:checked');
    var ids = [];
    for (var i = 0; i < checked.length; i++) {
      ids.push(checked[i].value);
    }
    return ids;
  }

  function updateBatchActions() {
    var ids = getSelectedIds();
    var div = document.getElementById('batch-actions');
    if (ids.length > 0) {
      div.classList.remove('d-none');
      document.getElementById('selected-count').textContent = '已选择 ' + ids.length + ' 项';
    } else {
      div.classList.add('d-none');
    }
  }

  async function batchDownload() {
    var ids = getSelectedIds();
    if (ids.length === 0) return;
    try {
      var data = await window.apiFetch('/api/wishlist/download', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ids: ids}),
        timeoutMs: 120000,
      });
      if (data.status === 'ok') {
        if (typeof showToast === 'function') showToast('已创建 ' + data.job_ids.length + ' 个下载任务', 'success');
        loadWishlist(currentPage);
      } else {
        if (typeof showToast === 'function') showToast(data.message || '批量下载失败', 'danger');
      }
    } catch (e) {
      if (e && e.name === 'AbortError') return; // pagehide 中止，静默
      if (typeof showToast === 'function') {
        showToast((e && e.status) ? (e.message || '批量下载失败') : ('网络错误: ' + e.message), 'danger');
      }
    }
  }

  async function batchRemove() {
    var ids = getSelectedIds();
    if (ids.length === 0 || !confirm('确定要移除 ' + ids.length + ' 项收藏吗？')) return;
    var ok = 0, fail = 0;
    try {
      // 并行删除所有选中项
      var results = await Promise.all(ids.map(function (id) {
        return window.apiFetch('/api/wishlist/' + encodeURIComponent(id), { method: 'DELETE', timeoutMs: 60000 })
          .then(function (data) { return data.status === 'ok'; })
          .catch(function (e) {
            // pagehide 中止的请求向上抛，交给外层守卫静默处理，
            // 不能计入"失败"数（否则 bfcache 恢复后弹出误导性汇总）
            if (e && e.name === 'AbortError') throw e;
            return false;
          });
      }));
      for (var i = 0; i < results.length; i++) {
        if (results[i]) ok++; else fail++;
      }
      if (typeof showToast === 'function') showToast('移除完成：成功 ' + ok + '，失败 ' + fail, fail > 0 ? 'warning' : 'success');
      loadWishlist(currentPage);
    } catch (e) {
      if (e && e.name === 'AbortError') return; // pagehide 中止，静默
      if (typeof showToast === 'function') showToast('网络错误: ' + e.message, 'danger');
    }
  }

  // ── 批量导入 ──
  function showImportModal() {
    document.getElementById('import-raw').value = '';
    document.getElementById('import-result').classList.add('d-none');
    new bootstrap.Modal(document.getElementById('importModal')).show();
  }

  async function doImport() {
    var raw = document.getElementById('import-raw').value.trim();
    if (!raw) { if (typeof showToast === 'function') showToast('请输入车号', 'warning'); return; }

    var btn = document.querySelector('#importModal .btn-primary');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> 导入中...';

    try {
      var data = await window.apiFetch('/api/wishlist/import', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({raw: raw}),
        timeoutMs: 60000,
      });
      var resultDiv = document.getElementById('import-result');
      resultDiv.classList.remove('d-none');
      if (data.status === 'ok') {
        var resultHtml = '<div class="alert alert-success mb-0">'
          + '成功添加 <strong>' + window.escapeHtml(data.added) + '</strong> 个，跳过已存在 <strong>' + window.escapeHtml(data.skipped_existing) + '</strong> 个。'
          + (data.failed_validation.length > 0 ? '<br>非数字格式（已忽略）: ' + window.escapeHtml(data.failed_validation.join(', ')) : '')
          + (data.errors.length > 0 ? '<br>导入错误: ' + data.errors.map(function(e) { return window.escapeHtml(e.album_id) + ': ' + window.escapeHtml(e.error); }).join('; ') : '')
          + '</div>';
        resultDiv.innerHTML = resultHtml;
        loadWishlist(1);
      } else {
        resultDiv.innerHTML = '<div class="alert alert-danger mb-0">导入失败: ' + window.escapeHtml(data.message) + '</div>';
      }
    } catch (e) {
      if (e && e.name === 'AbortError') {
        // pagehide 中止，静默（finally 仍会恢复按钮状态）
      } else if (e && e.status) {
        // HTTP 错误：保持原有「导入失败: 服务端消息」展示
        var errDiv = document.getElementById('import-result');
        if (errDiv) {
          errDiv.classList.remove('d-none');
          errDiv.innerHTML = '<div class="alert alert-danger mb-0">导入失败: ' + window.escapeHtml(e.message) + '</div>';
        }
      } else {
        if (typeof showToast === 'function') showToast('导入请求失败: ' + e.message, 'danger');
      }
    } finally {
      btn.disabled = false;
      btn.textContent = '导入';
    }
  }

  // ── 搜索/排序 ──
  function searchWishlist() {
    var q = document.getElementById('wishlist-search').value.trim();
    var sort = document.getElementById('wishlist-sort').value;
    currentQuery = q;
    currentSort = sort;
    loadWishlist(1);
  }

  // ── 导出收藏清单 ──
  function exportWishlist() {
    window.open('/api/wishlist/export');
  }

  // ── 导入收藏清单（文件） ──
  async function importWishlistFile(input) {
    var file = input.files[0];
    if (!file) return;
    var formData = new FormData();
    formData.append('file', file);
    try {
      var data = await window.apiFetch('/api/wishlist/import-file', { method: 'POST', body: formData, timeoutMs: 60000 });
      if (typeof showToast === 'function') {
        if (data.status === 'ok') {
          showToast('导入成功: 新增 ' + data.added + ', 跳过 ' + data.skipped_existing, 'success');
          loadWishlist(1);
        } else {
          showToast(data.message || '导入失败', 'danger');
        }
      }
    } catch(e) {
      if (e && e.name === 'AbortError') { input.value = ''; return; } // pagehide 中止，静默
      if (typeof showToast === 'function') {
        showToast((e && e.status) ? (e.message || '导入失败') : ('导入失败: ' + e.message), 'danger');
      }
    }
    input.value = '';
  }

  function refreshList() {
    loadWishlist(currentPage);
  }

  // ── 自动刷新定时器（每 10 秒检查一次下载状态变化） ──
  function startAutoRefresh() {
    if (_refreshTimer) clearInterval(_refreshTimer);
    _refreshTimer = setInterval(function () {
      if (!document.hidden) {
        loadWishlist(currentPage);
      }
    }, 10000);
  }

  // 监听复选框变化
  document.addEventListener('change', function(e) {
    if (e.target.classList.contains('item-checkbox')) updateBatchActions();
  });

  // ── 初始化 ──
  loadWishlist(1);
  startAutoRefresh();

  // 页面关闭时清理定时器
  window.addEventListener('beforeunload', function () {
    if (_refreshTimer) clearInterval(_refreshTimer);
  });

  // ── 将函数暴露到 window 供 onclick 调用 ──
  window.loadWishlist = loadWishlist;
  window.downloadSingle = downloadSingle;
  window.removeSingle = removeSingle;
  window.toggleSelectAll = toggleSelectAll;
  window.updateBatchActions = updateBatchActions;
  window.batchDownload = batchDownload;
  window.batchRemove = batchRemove;
  window.showImportModal = showImportModal;
  window.doImport = doImport;
  window.searchWishlist = searchWishlist;
  window.exportWishlist = exportWishlist;
  window.importWishlistFile = importWishlistFile;
  window.refreshList = refreshList;

  // ── 导出清单日志 ──
  console.log('[wishlist.js] IIFE 已执行，window 导出的函数:', Object.keys(window).filter(function (k) {
    return ['loadWishlist','downloadSingle','removeSingle','toggleSelectAll','updateBatchActions',
            'batchDownload','batchRemove','showImportModal','doImport','searchWishlist',
            'exportWishlist','importWishlistFile','refreshList'].indexOf(k) >= 0;
  }).join(', '));

})();
