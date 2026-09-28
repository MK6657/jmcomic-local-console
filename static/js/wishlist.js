// ── wishlist.js — 收藏清单页面逻辑 ──
// 使用 window.apiFetch (来自 utils.js)，showToast 来自 base.html
// 请求统一走 apiFetch（自带 AbortController/超时/pagehide 清理）
// 表格行用 DOM API 构建：标题/作者来自 18comic 或导入文件，只走 textContent / dataset；
// 按钮事件全部委托（data-action），不拼接内联 onclick。
// 搜索词、下载状态筛选、排序与页码同步到地址栏：刷新、从阅读页返回后保持不变。
(function () {
  'use strict';

  var DEFAULT_SORT = 'added_at';
  var currentPage = 1;
  var pageSize = 50;
  var allItems = [];
  var currentQuery = '';
  var currentStatus = '';
  var currentSort = DEFAULT_SORT;
  var _refreshTimer = null;

  var tbody = document.getElementById('wishlist-body');
  var searchInput = document.getElementById('wishlist-search');
  var statusSelect = document.getElementById('wishlist-status');
  var sortSelect = document.getElementById('wishlist-sort');
  var selectAll = document.getElementById('select-all');

  // 下拉框文字（后面附上各状态的数量）
  var STATUS_LABELS = {
    '': '状态：全部',
    'none': '未下载',
    'missing': '下载过 · 本地文件不可用',
    'active': '排队中 / 下载中',
    'readable': '已下载内容 · 可离线阅读',
    'failed': '失败'
  };

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

  function badge(className, text, title) {
    var b = el('span', 'badge ' + className, text);
    if (title) b.title = title;
    return b;
  }

  // className：列的类名（style.css .wishlist-table 按列设置宽度与换行方式）
  function cell(child, text, className) {
    var td = el('td', className, text);
    if (child) td.appendChild(child);
    return td;
  }

  function showTableMessage(text, className) {
    var td = el('td', 'text-center py-4 ' + (className || 'text-muted'), text);
    td.colSpan = 7;
    var tr = el('tr');
    tr.appendChild(td);
    tbody.textContent = '';
    tbody.appendChild(tr);
  }

  // ── 地址栏状态 ──
  function setSelect(select, value, fallback) {
    select.value = value;
    if (select.value !== value) select.value = fallback; // 地址栏里的值不在选项中 → 默认
  }

  function readUrlState() {
    var params;
    try { params = new URLSearchParams(window.location.search); } catch (e) { return; }
    searchInput.value = (params.get('q') || '').trim();
    setSelect(statusSelect, params.get('status') || '', '');
    setSelect(sortSelect, params.get('sort') || DEFAULT_SORT, DEFAULT_SORT);
    currentQuery = searchInput.value;
    currentStatus = statusSelect.value;
    currentSort = sortSelect.value;
    var page = parseInt(params.get('page'), 10);
    currentPage = page > 0 ? page : 1;
  }

  function writeUrlState() {
    var params = new URLSearchParams();
    if (currentQuery) params.set('q', currentQuery);
    if (currentStatus) params.set('status', currentStatus);
    if (currentSort && currentSort !== DEFAULT_SORT) params.set('sort', currentSort);
    if (currentPage > 1) params.set('page', String(currentPage));
    var qs = params.toString();
    var url = window.location.pathname + (qs ? '?' + qs : '') + window.location.hash;
    if (url === window.location.pathname + window.location.search + window.location.hash) return;
    try { window.history.replaceState(window.history.state, '', url); } catch (e) { /* 忽略 */ }
  }

  function updateStatusCounts(counts) {
    if (!counts) return;
    var all = 0;
    Object.keys(counts).forEach(function (k) { all += counts[k] || 0; });
    for (var i = 0; i < statusSelect.options.length; i++) {
      var option = statusSelect.options[i];
      var label = STATUS_LABELS[option.value];
      if (label === undefined) continue;
      var n = option.value ? (counts[option.value] || 0) : all;
      option.textContent = label + '（' + n + '）';
    }
  }

  // ── 加载收藏列表 ──
  async function loadWishlist(page, opts) {
    opts = opts || {};
    page = page || currentPage;
    currentPage = page;
    writeUrlState();
    try {
      var url = '/api/wishlist?page=' + page + '&page_size=' + pageSize;
      if (currentQuery) url += '&q=' + encodeURIComponent(currentQuery);
      if (currentStatus) url += '&status=' + encodeURIComponent(currentStatus);
      if (currentSort) url += '&sort=' + encodeURIComponent(currentSort);
      // abortKey：翻页/自动刷新时自动中止上一次未完成的列表请求
      var data = await window.apiFetch(url, { timeoutMs: 15000, abortKey: 'wishlist-list' });
      if (data.status !== 'ok') {
        showTableMessage('加载失败: ' + (data.message || '未知错误'));
        return;
      }

      allItems = data.items || [];
      var total = data.total || 0;
      updateStatusCounts(data.group_counts);

      if (allItems.length === 0 && total > 0 && page > 1 && !opts.clamped) {
        // 移除/筛选后当前页超出末页（或地址栏里是旧页码）→ 回到最后一页
        return loadWishlist(Math.ceil(total / pageSize), { clamped: true });
      }
      currentPage = data.page || page;
      writeUrlState();

      if (allItems.length === 0) {
        showTableMessage((currentQuery || currentStatus) ? '没有符合当前搜索或筛选条件的收藏' : '收藏列表为空');
        document.getElementById('wishlist-pagination').classList.add('d-none');
        updateBatchActions();
        return;
      }

      renderRows(allItems);
      renderPagination(total, currentPage);
      updateBatchActions();
      // 从导航回到收藏：地址与离开时相同就回到原来的滚动位置（只在第一次画出列表时）
      if (window.navMemory) window.navMemory.restoreScroll();
    } catch (e) {
      if (e && e.name === 'AbortError') return; // pagehide/新请求中止，静默
      if (e && e.status) {
        // HTTP 错误：保持原有「加载失败: 服务端消息」文案
        showTableMessage('加载失败: ' + (e.message || '未知错误'));
      } else {
        showTableMessage('网络错误: ' + ((e && e.message) || ''), 'text-danger');
      }
    }
  }

  function renderRows(items) {
    // 自动刷新会重建表格：保留已勾选的行
    var keep = {};
    getSelectedIds().forEach(function (id) { keep[id] = true; });
    var focus = rememberKeyboardFocus();
    var frag = document.createDocumentFragment();
    for (var i = 0; i < items.length; i++) {
      frag.appendChild(buildRow(items[i], !!keep[String(items[i].album_id)]));
    }
    tbody.textContent = '';
    tbody.appendChild(frag);
    restoreKeyboardFocus(focus);
  }

  // 重建表格前记住键盘焦点（哪一行的第几个控件），重建后还回去。
  // 例如用键盘点“阅读”、再返回时列表会立即重新加载，焦点不会被甩回页面开头；鼠标点出来的焦点不管
  var FOCUSABLE = 'a[href], button, input';

  function rememberKeyboardFocus() {
    var active = document.activeElement;
    if (!active || !tbody.contains(active)) return null;
    try { if (!active.matches(':focus-visible')) return null; } catch (e) { return null; }
    var row = active.closest('tr[data-album-id]');
    if (!row) return null;
    return { id: row.dataset.albumId, index: Array.prototype.indexOf.call(row.querySelectorAll(FOCUSABLE), active) };
  }

  function restoreKeyboardFocus(saved) {
    if (!saved || saved.index < 0) return;
    var rows = tbody.querySelectorAll('tr[data-album-id]');
    for (var i = 0; i < rows.length; i++) {
      if (rows[i].dataset.albumId !== saved.id) continue;
      var target = rows[i].querySelectorAll(FOCUSABLE)[saved.index];
      if (target) target.focus();
      return;
    }
  }

  function buildRow(item, checked) {
    var albumId = String(item.album_id);
    var detailUrl = '/album/' + encodeURIComponent(albumId);
    var tr = el('tr');
    tr.dataset.albumId = albumId;

    var cb = el('input', 'item-checkbox');
    cb.type = 'checkbox';
    cb.value = albumId;
    cb.checked = checked;
    cb.setAttribute('aria-label', '选择 ' + (item.title || albumId));
    tr.appendChild(cell(cb, null, 'wishlist-col-check'));

    var idLink = el('a', null, albumId);
    idLink.href = detailUrl;
    idLink.target = '_blank';
    idLink.rel = 'noopener'; // 保留同源 referrer：详情页“返回”据此回到收藏页（带筛选条件）
    tr.appendChild(cell(idLink, null, 'wishlist-col-id'));

    // 标题/作者放进带最小宽度的块里：不带空格的长串在格内任意位置换行，窄屏也不会被挤成一字一行
    tr.appendChild(cell(el('div', 'wishlist-title', item.title || '-'), null, 'wishlist-col-title'));
    tr.appendChild(cell(el('div', 'wishlist-author', item.author || '-'), null, 'wishlist-col-author'));
    tr.appendChild(cell(statusBadges(item), null, 'wishlist-col-status'));
    tr.appendChild(cell(null, item.added_at ? new Date(item.added_at).toLocaleString() : '-', 'wishlist-col-added'));

    // 下载 / 详情 / 移除 / 阅读 每行都有，位置固定；“阅读”已下载时打开本地文件，否则在线阅读（utils.js readLink）
    var actions = el('div', 'wishlist-actions');
    var download = actionButton('btn-outline-success', 'bi-download', '下载', 'download');
    if (item.readable === true) {
      // 已有已下载内容（可能只是部分章节）：整部下载前先确认（downloadSingle）
      download.title = '下载（已有已下载内容：整部下载前会先确认）';
      download.setAttribute('aria-label', download.title);
    }
    actions.appendChild(download);
    var info = el('a', 'btn btn-sm btn-outline-info');
    info.href = detailUrl;
    info.target = '_blank';
    info.rel = 'noopener';
    info.title = '详情';
    info.setAttribute('aria-label', '详情');
    info.appendChild(icon('bi-info-circle'));
    actions.appendChild(info);
    actions.appendChild(actionButton('btn-outline-danger', 'bi-trash', '移除', 'remove'));
    var read = window.readLink.create(albumId, window.readLink.stateFor(item.readable, item.archive_problem), 'btn-sm');
    if (read) actions.appendChild(read);
    tr.appendChild(cell(actions, null, 'wishlist-col-actions'));
    return tr;
  }

  function actionButton(variant, iconName, label, action) {
    var btn = el('button', 'btn btn-sm ' + variant);
    btn.type = 'button';
    btn.dataset.action = action;
    btn.title = label;
    btn.setAttribute('aria-label', label);
    btn.appendChild(icon(iconName));
    return btn;
  }

  // 分组由服务端算好（status_group）：可读判定与资源库、详情等页面共用同一规则
  function statusBadges(item) {
    var box = el('div', 'wishlist-status');
    var group = item.status_group;
    if (group === 'readable') {
      // 与详情页、搜索页、资源库同一个“已下载”标记（style.css .offline-badge）。
      // 表格里只显示“可离线阅读”，徽章不断行也不把状态列撑宽；完整的“已下载内容 · 可离线阅读”在读屏文本和 title 里。
      // 本地可读不代表整部漫画都已下载（可能只下载了部分章节），所以不说“完整”
      var offline = el('span', 'offline-badge');
      offline.title = '已下载内容 · 可离线阅读：本地有已下载的内容，可以离线阅读；不一定是整部漫画';
      offline.appendChild(icon('bi-check-circle-fill'));
      offline.appendChild(el('span', 'visually-hidden', '已下载内容 · '));
      offline.appendChild(el('span', null, '可离线阅读'));
      box.appendChild(offline);
      // 只剩压缩包也能读：跟一个 CBZ / ZIP 标记（item.archive，与其他页面同一规则）
      if (item.archive) box.appendChild(window.localBadges.archive(item.archive));
      // 检查新章节已确认的新章节（item.update）：表格里短写“新章节 · N”（“有”“话”给读屏），章节有变动时“章节有变动”
      if (item.update && window.updateBadges) {
        var u = window.updateBadges.chip(item.update, { compact: true });
        if (u) box.appendChild(u);
      }
      if (item.activity) box.appendChild(activityBadge(item.activity)); // 可读，同时又在下载（更新/补章节）
    } else if (group === 'active') {
      box.appendChild(activityBadge(item.activity));
    } else if (group === 'failed') {
      box.appendChild(badge('bg-danger', '失败', '最近一次下载失败，可以重新下载'));
    } else if (item.files_missing) {
      // “下载过 · 本地文件不可用”：文件已删除 / 压缩包损坏 / 压缩包无可阅读图片（item.local_problem）
      box.appendChild(window.localBadges.problem(item.local_problem) || window.localBadges.problem('deleted'));
    } else {
      box.appendChild(badge('bg-secondary', '未下载'));
    }
    return box;
  }

  function activityBadge(activity) {
    if (activity === 'downloading') return badge('bg-primary', '下载中');
    if (activity === 'paused') return badge('bg-warning', '已暂停');
    return badge('bg-info text-dark', '排队中');
  }

  function renderPagination(total, page) {
    var nav = document.getElementById('wishlist-pagination');
    nav.classList.remove('d-none');
    var totalPages = Math.max(1, Math.ceil(total / pageSize));
    var ul = nav.querySelector('ul');
    ul.textContent = '';

    function pageItem(p, label, active) {
      var li = el('li', 'page-item' + (active ? ' active' : ''));
      var btn = el('button', 'page-link', label);
      btn.type = 'button';
      btn.dataset.page = String(p);
      if (active) btn.setAttribute('aria-current', 'page');
      li.appendChild(btn);
      ul.appendChild(li);
    }

    if (page > 1) pageItem(page - 1, '上一页', false);
    var start = Math.max(1, page - 2);
    var end = Math.min(totalPages, start + 4);
    start = Math.max(1, end - 4);
    for (var p = start; p <= end; p++) {
      pageItem(p, String(p), p === page);
    }
    if (page < totalPages) pageItem(page + 1, '下一页', false);
  }

  // ── 单个操作 ──
  function findItem(albumId) {
    for (var i = 0; i < allItems.length; i++) {
      if (String(allItems[i].album_id) === String(albumId)) return allItems[i];
    }
    return null;
  }

  // 定时下载开着、现在不在时间段内时，任务什么时候开始：“今天 23:00” / “明天 08:00”；否则 ''
  function scheduledStart(w) {
    if (!w || !w.enabled || w.open || w.never || w.invalid) return '';
    var h = Number(w.start);
    return (w.opens_tomorrow ? '明天' : '今天') + ' ' + (h < 10 ? '0' : '') + h + ':00';
  }

  // 一行的「下载」：整部下载（服务端不会给已在下载队列中的漫画重复建任务）。
  // 已有已下载内容的先确认：整部下载会按上游现在的全部章节下载，只要新章节应该用资源库的「下载新章节」
  async function downloadSingle(albumId) {
    var item = findItem(albumId);
    if (item && item.readable === true) {
      var name = item.title || albumId;
      if (!window.confirm('「' + name + '」已有已下载的内容（可能只是部分章节）。\n'
        + '整部下载会按上游现在的全部章节下载：开启了「跳过已存在的文件」、漫画还在原来的下载文件夹里时，已有的散图会跳过；'
        + '关闭了这个设置、按作者或扁平化整理过、或只剩压缩包的漫画，会重新下载全部图片。\n'
        + '只要新章节请用资源库的「下载新章节」。确定整部下载吗？')) return;
    }
    try {
      var data = await window.apiFetch('/api/wishlist/download', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ids: [albumId]}),
        timeoutMs: 60000,
      });
      if (data.status === 'ok') {
        var w = data.window || {};
        var start = scheduledStart(w);
        if (!(data.job_ids || []).length && (data.skipped || []).length) {
          toast('这部漫画已在下载队列中，没有重复创建任务', 'info');
        } else if (w.enabled && (w.never || w.invalid)) {
          toast('已加入下载队列；定时下载的时间段设置不对，任务不会自动开始', 'warning');
        } else if (start) {
          toast('已加入下载队列，' + start + ' 起开始（定时下载）', 'info');
        } else {
          toast('已加入下载队列', 'success');
        }
        loadWishlist(currentPage);
      } else {
        toast(data.message || '下载失败', 'danger');
      }
    } catch (e) {
      if (e && e.name === 'AbortError') return; // pagehide 中止，静默
      toast((e && e.status) ? (e.message || '下载失败') : ('网络错误: ' + e.message), 'danger');
    }
  }

  async function removeSingle(albumId) {
    if (!confirm('确定要移除该收藏吗？')) return;
    try {
      var data = await window.apiFetch('/api/wishlist/' + encodeURIComponent(albumId), { method: 'DELETE', timeoutMs: 15000 });
      if (data.status === 'ok') {
        toast('已移除收藏', 'success');
        loadWishlist(currentPage);
      } else {
        toast(data.message || '移除失败', 'danger');
      }
    } catch (e) {
      if (e && e.name === 'AbortError') return; // pagehide 中止，静默
      toast((e && e.status) ? (e.message || '移除失败') : ('网络错误: ' + e.message), 'danger');
    }
  }

  // ── 批量操作 ──
  function toggleSelectAll() {
    var checked = selectAll.checked;
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
    var boxes = document.querySelectorAll('.item-checkbox').length;
    var div = document.getElementById('batch-actions');
    if (ids.length > 0) {
      div.classList.remove('d-none');
      document.getElementById('selected-count').textContent = '已选择 ' + ids.length + ' 项';
    } else {
      div.classList.add('d-none');
    }
    selectAll.checked = boxes > 0 && ids.length === boxes;
    selectAll.indeterminate = ids.length > 0 && ids.length < boxes;
  }

  // 批量下载的确认窗口（js/batch-download.js）：先列出清单，确认后才加入下载队列；
  // 加入之后按原来的页码、筛选刷新列表（只重建表格，位置和勾选不变）
  function openBatchDialog(kind, albumIds, opener) {
    if (!window.batchDownloadDialog) return;
    window.batchDownloadDialog.open(kind, {
      albumIds: albumIds,
      opener: opener,
      onDone: function () { loadWishlist(currentPage); }
    });
  }

  // 下载选中的收藏：选中的里只下载“未下载”的（整部），其余的在窗口里列出原因
  function batchDownload() {
    var ids = getSelectedIds();
    if (ids.length === 0) return;
    openBatchDialog('selected_favourites', ids, document.getElementById('batch-download-btn'));
  }

  // 下载未下载的收藏：收藏页“未下载”里的（从未下载过的、下载被取消的），每部整部
  function downloadUndownloaded() {
    openBatchDialog('undownloaded_favourites', null, document.getElementById('wishlist-download-undownloaded-btn'));
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
      toast('移除完成：成功 ' + ok + '，失败 ' + fail, fail > 0 ? 'warning' : 'success');
      // 已移除的行不再保留勾选
      var cbs = document.querySelectorAll('.item-checkbox:checked');
      for (var j = 0; j < cbs.length; j++) cbs[j].checked = false;
      loadWishlist(currentPage);
    } catch (e) {
      if (e && e.name === 'AbortError') return; // pagehide 中止，静默
      toast('网络错误: ' + e.message, 'danger');
    }
  }

  // ── 批量导入 ──
  function showImportModal() {
    document.getElementById('import-raw').value = '';
    document.getElementById('import-result').classList.add('d-none');
    new bootstrap.Modal(document.getElementById('importModal')).show();
  }

  function importAlert(kind) {
    var resultDiv = document.getElementById('import-result');
    resultDiv.classList.remove('d-none');
    resultDiv.textContent = '';
    var alert = el('div', 'alert alert-' + kind + ' mb-0');
    resultDiv.appendChild(alert);
    return alert;
  }

  async function doImport() {
    var raw = document.getElementById('import-raw').value.trim();
    if (!raw) { toast('请输入车号', 'warning'); return; }

    var btn = document.getElementById('import-submit-btn');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> 导入中...';

    try {
      var data = await window.apiFetch('/api/wishlist/import', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({raw: raw}),
        timeoutMs: 60000,
      });
      if (data.status === 'ok') {
        var ok = importAlert('success');
        ok.appendChild(document.createTextNode('成功添加 '));
        ok.appendChild(el('strong', null, String(data.added)));
        ok.appendChild(document.createTextNode(' 个，跳过已存在 '));
        ok.appendChild(el('strong', null, String(data.skipped_existing)));
        ok.appendChild(document.createTextNode(' 个。'));
        var invalid = data.failed_validation || [];
        if (invalid.length > 0) {
          ok.appendChild(el('br'));
          ok.appendChild(document.createTextNode('非数字格式（已忽略）: ' + invalid.join(', ')));
        }
        var errors = data.errors || [];
        if (errors.length > 0) {
          ok.appendChild(el('br'));
          ok.appendChild(document.createTextNode('导入错误: ' + errors.map(function (e) {
            return e.album_id + ': ' + (e.message || e.error || '导入失败');
          }).join('; ')));
        }
        loadWishlist(1);
      } else {
        importAlert('danger').textContent = '导入失败: ' + (data.message || '未知错误');
      }
    } catch (e) {
      if (e && e.name === 'AbortError') {
        // pagehide 中止，静默（finally 仍会恢复按钮状态）
      } else if (e && e.status) {
        // HTTP 错误：保持原有「导入失败: 服务端消息」展示
        importAlert('danger').textContent = '导入失败: ' + e.message;
      } else {
        toast('导入请求失败: ' + e.message, 'danger');
      }
    } finally {
      btn.disabled = false;
      btn.textContent = '导入';
    }
  }

  // ── 搜索/筛选/排序 ──
  function searchWishlist() {
    currentQuery = searchInput.value.trim();
    currentStatus = statusSelect.value;
    currentSort = sortSelect.value;
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
      if (data.status === 'ok') {
        toast('导入成功: 新增 ' + data.added + ', 跳过 ' + data.skipped_existing, 'success');
        loadWishlist(1);
      } else {
        toast(data.message || '导入失败', 'danger');
      }
    } catch(e) {
      if (e && e.name === 'AbortError') { input.value = ''; return; } // pagehide 中止，静默
      toast((e && e.status) ? (e.message || '导入失败') : ('导入失败: ' + e.message), 'danger');
    }
    input.value = '';
  }

  function refreshList() {
    loadWishlist(currentPage);
  }

  // ── 自动刷新定时器（每 10 秒检查一次下载状态变化） ──
  // 页面隐藏或正用键盘操作表格按钮时跳过，免得重建表格把焦点弄丢；批量下载的确认窗口开着时也跳过
  function canRefreshNow() {
    return !document.hidden && !tbody.contains(document.activeElement)
      && !(window.batchDownloadDialog && window.batchDownloadDialog.isOpen());
  }

  function startAutoRefresh() {
    if (_refreshTimer) clearInterval(_refreshTimer);
    _refreshTimer = setInterval(function () {
      if (canRefreshNow()) loadWishlist(currentPage);
    }, 10000);
  }

  function stopAutoRefresh() {
    if (_refreshTimer) clearInterval(_refreshTimer);
    _refreshTimer = null;
  }

  // ── 事件绑定（取代内联 onclick；行内参数只从 data-* 读取） ──
  tbody.addEventListener('click', function (e) {
    var btn = e.target.closest('button[data-action]');
    if (!btn || !tbody.contains(btn)) return;
    var row = btn.closest('tr');
    var albumId = row ? row.dataset.albumId : '';
    if (!albumId) return;
    if (btn.dataset.action === 'download') downloadSingle(albumId);
    else if (btn.dataset.action === 'remove') removeSingle(albumId);
  });

  document.getElementById('wishlist-pagination').addEventListener('click', function (e) {
    var btn = e.target.closest('button[data-page]');
    if (!btn) return;
    var page = parseInt(btn.dataset.page, 10);
    if (page > 0) loadWishlist(page);
  });

  // 监听复选框变化
  document.addEventListener('change', function(e) {
    if (e.target.classList.contains('item-checkbox')) updateBatchActions();
  });

  selectAll.addEventListener('change', toggleSelectAll);
  searchInput.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') searchWishlist();
  });
  document.getElementById('wishlist-search-btn').addEventListener('click', searchWishlist);
  statusSelect.addEventListener('change', searchWishlist);
  sortSelect.addEventListener('change', searchWishlist);
  document.getElementById('wishlist-export-btn').addEventListener('click', exportWishlist);
  document.getElementById('wishlist-import-file-btn').addEventListener('click', function () {
    document.getElementById('import-file-input').click();
  });
  document.getElementById('import-file-input').addEventListener('change', function () {
    importWishlistFile(this);
  });
  document.getElementById('wishlist-batch-import-btn').addEventListener('click', showImportModal);
  document.getElementById('wishlist-refresh-btn').addEventListener('click', refreshList);
  document.getElementById('wishlist-download-undownloaded-btn').addEventListener('click', downloadUndownloaded);
  document.getElementById('batch-download-btn').addEventListener('click', batchDownload);
  document.getElementById('batch-remove-btn').addEventListener('click', batchRemove);
  document.getElementById('import-submit-btn').addEventListener('click', doImport);

  // ── 初始化 ──
  readUrlState();
  loadWishlist(currentPage);
  startAutoRefresh();

  // 离开页面（含进入 bfcache，例如点“阅读”后）停止定时器；
  // 从阅读页等处返回、页面从 bfcache 恢复时：立即重新加载（下载状态/可离线阅读可能已变），并恢复定时器
  window.addEventListener('pagehide', stopAutoRefresh);
  window.addEventListener('pageshow', function (event) {
    if (!event.persisted) return;
    loadWishlist(currentPage);
    startAutoRefresh();
  });
  // 切回本标签页时立即刷新（例如在别的标签页下载完成，或本地文件被移走）
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'visible' && _refreshTimer && canRefreshNow()) loadWishlist(currentPage);
  });

  // ── 供控制台/其他脚本调用（页面本身不再依赖这些全局函数） ──
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

})();
