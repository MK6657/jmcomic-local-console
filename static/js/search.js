/**
 * JMComic 搜索页 JavaScript — 搜索 + 渲染 + 分页
 * 使用 Bootstrap alert 替代原生 alert()
 * 依赖: utils.js (window.apiFetch — 自带 AbortController/超时/pagehide 清理；window.escapeHtml / escapeHtmlAttr)
 * 动态内容不使用内联 onclick：按钮带 data-* 属性，由容器统一委托处理
 */
(function () {
  'use strict';

  var searchForm = document.getElementById('search-form');
  var searchInput = document.getElementById('search-input');
  var searchBtn = document.getElementById('search-btn');
  var sortSelect = document.getElementById('sort-select');
  var pageSizeSelect = document.getElementById('page-size-select');
  var resultsDiv = document.getElementById('search-results');
  var paginationDiv = document.getElementById('pagination');
  var statusDiv = document.getElementById('search-status');

  if (!searchForm || !resultsDiv) return;

  var escapeHtml = window.escapeHtml;
  var escapeHtmlAttr = window.escapeHtmlAttr;

  var currentQuery = '';
  var currentPage = 1;
  var currentSort = 'latest';
  var currentPageSize = 20;
  var lastResults = null;
  var requestSerial = 0;
  var snapshotKey = 'jm-search-state-v1';
  // 快照里的结果用多久（按取到结果的时间算）：经导航/后退回来与顶部导航“回到上次的搜索”一致（nav-memory.js）；
  // 刷新（F5）是想看新结果，超过 30 分钟就重新搜索
  var SNAPSHOT_MAX_AGE = (window.navMemory && window.navMemory.MAX_AGE) || 12 * 60 * 60 * 1000;
  var RELOAD_MAX_AGE = 30 * 60 * 1000;
  var lastFetchedAt = 0;      // lastResults 取到的时间
  var pendingRestore = null;  // 快照里没有可用结果（离开时还在加载 / 已过期）时，重新请求后回到的位置
  var restoring = null;       // 正要回到的快照：结果画出、滚回去之前离开或刷新，沿用它的位置，不拿页面顶部覆盖

  /** leaving：离开页面（pagehide）时为 true，记“看到的位置”；加载、换页时记当前所在的位置 */
  function saveSearchState(leaving) {
    if (!currentQuery) return;
    var url = new URL(window.location.href);
    url.searchParams.set('keyword', currentQuery);
    url.searchParams.set('sort', currentSort);
    url.searchParams.set('page_size', currentPageSize);
    url.searchParams.set('page', currentPage);
    var nm = window.navMemory;
    var keep = restoring;
    var keepRaw = !!keep && typeof keep.rawScrollY === 'number';
    var here = keep
      ? { y: keepRaw ? keep.rawScrollY : (keep.scrollY || 0), listTop: keepRaw ? keep.rawResultsTop : keep.resultsTop }
      : { y: Math.round(window.scrollY), listTop: nm ? nm.listTopNow() : null };
    var seen = keep ? { y: keep.scrollY || 0, listTop: keep.resultsTop } : (leaving && nm ? nm.place() : here);
    var snapshot = {
      url: url.pathname + url.search, query: currentQuery, sort: currentSort,
      pageSize: currentPageSize, page: currentPage, data: lastResults, fetchedAt: lastFetchedAt,
      // 看到哪里了：为了点顶部导航刚滚回最上方时，记的是之前停留的位置（nav-memory.js）；
      // 连同当时结果区的位置（上方的搜索历史后加载、高度不同时，按结果区换算）
      scrollY: seen.y, resultsTop: seen.listTop,
      rawScrollY: here.y, rawResultsTop: here.listTop,  // 刷新时回到的位置：就是刷新前所在的位置
      savedAt: Date.now()
    };
    try {
      history.replaceState(Object.assign({}, history.state, { jmSearch: snapshot }), '', snapshot.url);
    } catch (_) {
      try { history.replaceState(null, '', snapshot.url); } catch (ignored) {}
    }
    try { sessionStorage.setItem(snapshotKey, JSON.stringify(snapshot)); } catch (_) {}
  }

  /**
   * 当前地址的快照。后退/前进、往返缓存：优先这条历史记录自己的快照（同一地址可能有好几条历史记录，各有各的位置）；
   * 刷新：历史记录里的和 sessionStorage 里的取较新的（刷新时 pagehide 写进历史记录的那次会丢）；
   * 经导航新打开的历史记录没有自己的快照，用 sessionStorage 里的。
   */
  function snapshotForHere(reload) {
    var here = location.pathname + location.search;
    var own = history.state && history.state.jmSearch;
    var session = null;
    try { session = JSON.parse(sessionStorage.getItem(snapshotKey)); } catch (_) {}
    own = own && typeof own === 'object' && own.url === here ? own : null;
    session = session && typeof session === 'object' && session.url === here ? session : null;
    if (own && session && reload) return (session.savedAt || 0) > (own.savedAt || 0) ? session : own;
    return own || session;
  }

  /** fromCache：页面从往返缓存（bfcache）恢复 */
  function restoreSearchState(fromCache) {
    var params = new URLSearchParams(window.location.search);
    currentQuery = params.get('keyword') || '';
    currentSort = ['latest', 'views', 'likes'].indexOf(params.get('sort')) >= 0 ? params.get('sort') : 'latest';
    currentPage = Math.max(1, Math.min(500, parseInt(params.get('page'), 10) || 1));
    currentPageSize = [20, 50, 100].indexOf(Number(params.get('page_size'))) >= 0 ? Number(params.get('page_size')) : 20;
    if (!currentQuery) return false;
    searchInput.value = currentQuery;
    sortSelect.value = currentSort;
    pageSizeSelect.value = String(currentPageSize);
    var nm = window.navMemory;
    var reload = !fromCache && !!nm && nm.navigationType() === 'reload';
    var saved = snapshotForHere(reload);
    // 没有这个地址的结果快照（之后又搜过别的），但这里是快捷导航“返回”的目标：重新搜索，画出后回到跳走前的位置
    if (!saved && nm && nm.hasReturn && nm.hasReturn()) saved = { url: location.pathname + location.search };
    restoring = saved;
    var fetchedAt = saved ? (saved.fetchedAt || saved.savedAt) : 0;
    var age = Date.now() - fetchedAt;
    if (saved && saved.data && age >= 0 && age < (reload ? RELOAD_MAX_AGE : SNAPSHOT_MAX_AGE)) {
      lastResults = saved.data;
      lastFetchedAt = fetchedAt;
      renderResults(lastResults);
      scrollToSaved(saved, reload);
    } else {
      pendingRestore = saved ? { saved: saved, reload: reload } : null;
      fetchResults(currentPage);
    }
    return true;
  }

  /**
   * 回到快照记下的位置：刷新回到刷新前所在处，其余回到“看到的位置”；在结果区内时按结果区现在的位置换算。
   * 经快捷导航“返回”到这里时，回到跳走前那一刻所在的位置（nav-memory.js takeReturn）。
   */
  function scrollToSaved(saved, reload) {
    var raw = reload && typeof saved.rawScrollY === 'number';
    var y = raw ? saved.rawScrollY : (saved.scrollY || 0);
    var listTop = raw ? saved.rawResultsTop : saved.resultsTop;
    var back = window.navMemory && window.navMemory.takeReturn ? window.navMemory.takeReturn() : null;
    if (back) {
      y = back.y;
      listTop = back.listTop;
    }
    requestAnimationFrame(function () {
      requestAnimationFrame(function () {
        if (restoring !== saved) return; // 这期间已经开始了新的搜索/翻页
        restoring = null;
        if (window.navMemory) {
          window.navMemory.scrollBack(y, listTop);
        } else {
          window.scrollTo({ top: y, behavior: 'instant' }); // instant：避免 Bootstrap 的平滑滚动
        }
      });
    });
  }

  window.addEventListener('pagehide', function () {
    saveSearchState(true);
    requestSerial += 1;
  });
  resultsDiv.addEventListener('click', function (event) {
    var action = event.target.closest('[data-action]');
    if (action) {
      if (action.getAttribute('data-action') === 'download') quickDownload(action);
      else if (action.getAttribute('data-action') === 'wishlist') toggleWishlist(action);
      return;
    }
    if (event.target.closest('a, button, input')) return;
    var card = event.target.closest('[data-album-url]');
    if (card) window.location.href = card.getAttribute('data-album-url');
  });
  paginationDiv.addEventListener('click', function (event) {
    var button = event.target.closest('button[data-page]');
    if (!button || button.disabled) return;
    fetchResults(Number(button.getAttribute('data-page')));
    // 翻页后回到结果顶部（instant，避免平滑滚动动画）
    if (resultsDiv.getBoundingClientRect().top < 0) resultsDiv.scrollIntoView({ block: 'start', behavior: 'instant' });
  });

  // ── 全局 alert 管理 ──

  function showGlobalAlert(message, type) {
    type = type || 'danger';
    var container = document.getElementById('global-alert-container');
    if (!container) return;

    container.style.display = 'block';
    container.innerHTML = '<div class="alert alert-' + type + ' alert-dismissible fade show mb-3" role="alert">'
      + escapeHtml(message)
      + '<button type="button" class="btn-close" data-bs-dismiss="alert" aria-label="关闭"></button>'
      + '</div>';

    // 自动隐藏（5秒后）
    setTimeout(function () {
      container.style.display = 'none';
      container.innerHTML = '';
    }, 5000);
  }

  // ── 异常 → Toast 文案（服务端错误/超时显示具体消息，网络错误显示通用文案） ──
  function toastErr(err, fallback) {
    if (err && (err.status || err.isTimeout)) return err.message || fallback || '操作失败';
    return '网络错误';
  }

  // ── 搜索 ──

  function doSearch() {
    var q = searchInput.value.trim();
    if (!q) {
      showGlobalAlert('请输入关键词', 'warning');
      searchInput.focus();
      return;
    }

    currentQuery = q;
    currentPage = 1;
    currentSort = sortSelect.value;
    currentPageSize = parseInt(pageSizeSelect.value) || 20;

    fetchResults();
  }

  function fetchResults(page) {
    if (page) currentPage = page;
    currentSort = sortSelect.value;
    currentPageSize = parseInt(pageSizeSelect.value) || 20;

    var serial = ++requestSerial;
    var restore = pendingRestore; // 只给恢复时的这一次请求用；之后的搜索/翻页从头看
    pendingRestore = null;
    if (!restore) restoring = null;
    lastResults = null;
    saveSearchState();
    paginationDiv.innerHTML = '';
    var url = '/api/search?q=' + encodeURIComponent(currentQuery)
      + '&page=' + currentPage
      + '&page_size=' + currentPageSize
      + '&sort=' + encodeURIComponent(currentSort);

    resultsDiv.innerHTML = '<div class="text-center py-4"><div class="spinner-border text-primary" role="status"><span class="visually-hidden">搜索中...</span></div><p class="mt-2 text-muted">搜索中...</p></div>';
    statusDiv.classList.add('d-none');
    // 清除全局错误
    var alertContainer = document.getElementById('global-alert-container');
    if (alertContainer) {
      alertContainer.style.display = 'none';
      alertContainer.innerHTML = '';
    }

    // abortKey：快速翻页/连续搜索时自动中止上一次未完成的搜索请求，避免占满连接池
    window.apiFetch(url, { timeoutMs: 30000, abortKey: 'search-results' })
      .then(function (data) {
        if (serial !== requestSerial) return;
        if (data.status !== 'ok') throw new Error(data.message || '搜索失败');
        lastResults = data;
        lastFetchedAt = Date.now();
        renderResults(data);
        if (restore) scrollToSaved(restore.saved, restore.reload);
        saveSearchState();
      })
      .catch(function (err) {
        if (serial !== requestSerial) return;
        if (err && err.name === 'AbortError') return; // pagehide/新搜索中止，静默
        // 检查是否网络错误（TypeError 通常表示网络问题）
        if (err instanceof TypeError && err.message === 'Failed to fetch') {
          resultsDiv.innerHTML = '<div class="alert alert-danger" role="alert">'
            + '<i class="bi bi-wifi-off me-2"></i>网络连接失败，请检查网络状态后重试'
            + '</div>';
        } else {
          // 保持原有按 HTTP 状态码分类的提示文案
          var msg = err.message;
          if (err && err.status) {
            if (err.status >= 500) {
              msg = '服务器错误 (' + err.status + ')，请稍后重试';
            } else if (err.status === 404) {
              msg = '搜索接口未找到 (404)';
            } else if (err.status === 429) {
              msg = '请求过于频繁，请稍后再试 (429)';
            } else {
              msg = '搜索失败 (' + err.status + ')';
            }
          }
          resultsDiv.innerHTML = '<div class="alert alert-danger" role="alert">'
            + '<i class="bi bi-exclamation-circle me-2"></i>' + escapeHtml(msg)
            + '</div>';
        }
      });
  }

  // ── 渲染 ──

  var _wishlistCache = {};

  function renderResults(data) {
    var items = data.items || [];
    var total = data.total || 0;
    var totalPages = Math.ceil(total / currentPageSize);
    var albumIds = items.map(function (item) { return item.album_id; });

    // 批量查询收藏状态（15s 超时 + abortKey 去重，防止页面跳转/翻页时堆积占用浏览器连接池）
    if (albumIds.length > 0) {
      window.apiFetch('/api/wishlist/check', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ album_ids: albumIds }),
        timeoutMs: 15000,
        abortKey: 'search-wishlist-check'
      })
      .then(function (checkData) {
        if (checkData.status === 'ok' && checkData.result) {
          _wishlistCache = checkData.result;
          updateWishlistButtons();
        }
      })
      .catch(function (err) {
        if (err && err.name === 'AbortError') return; // 中止请求静默
        console.warn('批量查询收藏状态失败');
      });
    }

    // 状态
    if (total > 0) {
      statusDiv.classList.remove('d-none');
      statusDiv.textContent = '共找到 ' + total + ' 个结果';
    } else {
      statusDiv.classList.add('d-none');
    }

    if (items.length === 0) {
      resultsDiv.innerHTML = '<div class="text-center text-muted py-5"><i class="bi bi-inbox" style="font-size:3rem;"></i><p class="mt-3">没有找到相关结果</p></div>';
      paginationDiv.innerHTML = '';
      return;
    }

    // 渲染卡片（网格布局，每行4个）——整个卡片可点击；封面链接与标题同址，移出 Tab 顺序避免重复
    var html = '<div class="row row-cols-1 row-cols-sm-2 row-cols-lg-3 row-cols-xl-4 g-3 mb-4">';
    items.forEach(function (item) {
      var albumUrl = '/album/' + encodeURIComponent(item.album_id);
      html += '<div class="col">';
      html += '<div class="card album-card h-100" data-album-url="' + albumUrl + '" data-album-id="' + escapeHtmlAttr(item.album_id) + '">';
      // 已下载标记：盖在封面左上角，由 refreshReadable 按 /api/preview/available 的结果显示
      html += '<span class="offline-badge offline-badge--cover" hidden><i class="bi bi-check-circle-fill" aria-hidden="true"></i>已下载 · 可离线阅读</span>';
      html += '<a href="' + albumUrl + '" class="card-cover-link" tabindex="-1" aria-hidden="true">';

      // 封面
      if (item.cover_url) {
        html += '<img src="' + escapeHtmlAttr(item.cover_url) + '" class="card-img-top" alt="" loading="lazy" decoding="async">';
      } else {
        html += '<div class="cover-placeholder"><i class="bi bi-image"></i></div>';
      }

      html += '</a><div class="card-body d-flex flex-column">';
      html += '<h6 class="card-title" title="' + escapeHtmlAttr(item.title) + '"><a class="text-decoration-none text-reset" href="' + albumUrl + '">' + escapeHtml(item.title) + '</a></h6>';
      html += '<div class="mb-2 small text-muted">';
      if (item.author) html += '<div><i class="bi bi-person"></i> ' + escapeHtml(item.author) + '</div>';
      if (item.album_id) html += '<div><i class="bi bi-hash"></i> ' + escapeHtml(item.album_id) + '</div>';
      html += '</div>';

      // 标签
      if (item.tags && item.tags.length > 0) {
        html += '<div class="mb-2">';
        item.tags.slice(0, 5).forEach(function (tag) {
          html += '<span class="badge bg-light text-dark me-1">' + escapeHtml(tag) + '</span>';
        });
        html += '</div>';
      }

      // 按钮：详情 / 阅读为链接；下载 / 收藏由 resultsDiv 的委托处理（data-action）
      html += '<div class="mt-auto"><div class="d-flex gap-2 mb-2">';
      html += '<a href="' + albumUrl + '" class="btn btn-outline-primary btn-sm flex-fill"><i class="bi bi-info-circle"></i> 详情</a>';
      // 阅读：已下载打开本地文件，否则在线阅读；判断结果回来前是中性样式（refreshReadable → setCardReadable）
      html += window.readLink.html(item.album_id, undefined, 'btn-sm flex-fill reader-link');
      html += '</div><div class="d-flex gap-2">';
      html += '<button type="button" class="btn btn-success btn-sm flex-fill" data-action="download" data-album-id="' + escapeHtmlAttr(item.album_id) + '"><i class="bi bi-download"></i> 下载</button>';
      html += '<button type="button" class="btn btn-sm wishlist-btn btn-outline-warning" data-action="wishlist" data-album-id="' + escapeHtmlAttr(item.album_id) + '" data-title="' + escapeHtmlAttr(item.title) + '" data-author="' + escapeHtmlAttr(item.author) + '" data-cover="' + escapeHtmlAttr(item.cover_url) + '" title="收藏" aria-label="收藏"><i class="bi bi-star"></i></button>';
      html += '</div></div>';

      html += '</div></div></div>';
    });
    html += '</div>';
    resultsDiv.innerHTML = html;

    // 已下载标记每次渲染都重新判断（含从快照/bfcache 恢复），不沿用快照里的旧状态
    refreshReadable();

    // 分页
    renderPagination(currentPage, totalPages);
  }

  // ── 已下载（本地可读）标记 ──
  // 与详情/下载管理/收藏/资源库共用服务端同一判定（core.local_availability）

  function setCardReadable(card, readable) {
    var badge = card.querySelector('.offline-badge--cover');
    if (badge) badge.hidden = !readable;
    var link = card.querySelector('.reader-link');
    if (link) window.readLink.apply(link, readable);
  }

  function refreshReadable() {
    var cards = Array.prototype.slice.call(resultsDiv.querySelectorAll('.album-card[data-album-id]'));
    var ids = [];
    cards.forEach(function (card) {
      var id = card.getAttribute('data-album-id');
      if (/^[0-9]{1,20}$/.test(id) && ids.indexOf(id) < 0) ids.push(id);
    });
    if (ids.length === 0) return;
    ids = ids.slice(0, 200);
    // abortKey：翻页/重新搜索时中止上一页的判断，避免旧结果套到新卡片上
    window.apiFetch('/api/preview/available', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ album_ids: ids }),
      timeoutMs: 15000,
      abortKey: 'search-readable'
    })
    .then(function (data) {
      if (data.status !== 'ok') return;
      var readable = {};
      (data.readable || []).forEach(function (id) { readable[String(id)] = true; });
      cards.forEach(function (card) {
        var id = card.getAttribute('data-album-id');
        // 只更新本次请求覆盖、且仍在页面上的卡片
        if (ids.indexOf(id) >= 0 && resultsDiv.contains(card)) setCardReadable(card, !!readable[id]);
      });
    })
    .catch(function (err) {
      if (err && err.name === 'AbortError') return; // 翻页/pagehide 中止，静默
      console.warn('查询本地下载状态失败');
    });
  }

  function pageButton(page, label, ariaLabel, disabled, current) {
    return '<button type="button" class="page-link" data-page="' + page + '"'
      + (ariaLabel ? ' aria-label="' + ariaLabel + '"' : '') + (current ? ' aria-current="page"' : '')
      + (disabled ? ' disabled' : '') + '>' + label + '</button>';
  }

  function renderPagination(page, totalPages) {
    if (totalPages <= 1) {
      paginationDiv.innerHTML = '';
      return;
    }

    var html = '<nav aria-label="搜索结果分页"><ul class="pagination justify-content-center">';

    // 上一页
    html += '<li class="page-item' + (page <= 1 ? ' disabled' : '') + '">' + pageButton(page - 1, '&laquo;', '上一页', page <= 1) + '</li>';

    // 页码
    var start = Math.max(1, page - 2);
    var end = Math.min(totalPages, start + 4);
    start = Math.max(1, end - 4);

    if (start > 1) {
      html += '<li class="page-item">' + pageButton(1, '1') + '</li>';
      if (start > 2) html += '<li class="page-item disabled"><span class="page-link">...</span></li>';
    }

    for (var p = start; p <= end; p++) {
      html += '<li class="page-item' + (p === page ? ' active' : '') + '">' + pageButton(p, p, '第 ' + p + ' 页', false, p === page) + '</li>';
    }

    if (end < totalPages) {
      if (end < totalPages - 1) html += '<li class="page-item disabled"><span class="page-link">...</span></li>';
      html += '<li class="page-item">' + pageButton(totalPages, totalPages, '末页') + '</li>';
    }

    // 下一页
    html += '<li class="page-item' + (page >= totalPages ? ' disabled' : '') + '">' + pageButton(page + 1, '&raquo;', '下一页', page >= totalPages) + '</li>';

    html += '</ul></nav>';
    paginationDiv.innerHTML = html;
  }

  // ── 卡片操作 ──

  function quickDownload(btn) {
    var albumId = btn.getAttribute('data-album-id');
    var title = '';
    // 从按钮所在卡片获取标题
    var card = btn.closest('.album-card') || btn.closest('.card');
    if (card) {
      var titleEl = card.querySelector('.card-title');
      if (titleEl) title = titleEl.textContent || '';
    }
    btn.disabled = true; // 防止连点重复建任务
    window.apiFetch('/api/jobs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ album_id: albumId, photo_ids: [], title: title }),
      timeoutMs: 15000
    })
    .then(function (data) {
      if (data.status === 'ok') {
        // 优先使用全局 Toast，fallback 到页面内的 alert
        if (typeof showToast === 'function') {
          showToast('✅ 已加入下载队列', 'success');
        } else {
          showGlobalAlert('✅ 已加入下载队列', 'success');
        }
      } else {
        var msg = '❌ ' + (data.message || '创建任务失败');
        if (typeof showToast === 'function') {
          showToast(msg, 'danger');
        } else {
          showGlobalAlert(msg, 'danger');
        }
      }
    })
    .catch(function (err) {
      if (err && err.name === 'AbortError') return; // pagehide 中止，静默
      var msg = (err && (err.status || err.isTimeout)) ? ('❌ ' + (err.message || '创建任务失败')) : '网络错误';
      if (typeof showToast === 'function') {
        showToast(msg, 'danger');
      } else {
        showGlobalAlert(msg === '网络错误' ? '网络错误，请检查网络连接' : msg, 'danger');
      }
    })
    .finally(function () { btn.disabled = false; });
  }

  // ── 收藏操作 ──

  function setWishlistButton(btn, starred) {
    btn.querySelector('i').className = starred ? 'bi bi-star-fill' : 'bi bi-star';
    btn.classList.toggle('btn-warning', starred);
    btn.classList.toggle('btn-outline-warning', !starred);
    btn.title = starred ? '取消收藏' : '收藏';
    btn.setAttribute('aria-label', btn.title);
  }

  function toggleWishlist(btn) {
    var albumId = btn.getAttribute('data-album-id');
    var isStarred = btn.querySelector('i').classList.contains('bi-star-fill');

    if (isStarred) {
      // 取消收藏
      window.apiFetch('/api/wishlist/' + encodeURIComponent(albumId), { method: 'DELETE', timeoutMs: 15000 })
        .then(function (data) {
          if (data.status === 'ok') {
            setWishlistButton(btn, false);
            if (typeof showToast === 'function') showToast('已取消收藏', 'info');
            delete _wishlistCache[albumId];
          } else {
            if (typeof showToast === 'function') showToast(data.message || '取消收藏失败', 'danger');
          }
        })
        .catch(function (err) {
          if (err && err.name === 'AbortError') return; // pagehide 中止，静默
          if (typeof showToast === 'function') showToast(toastErr(err, '取消收藏失败'), 'danger');
        });
    } else {
      // 添加收藏
      window.apiFetch('/api/wishlist', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          album_id: albumId,
          title: btn.getAttribute('data-title') || '',
          author: btn.getAttribute('data-author') || '',
          cover_url: btn.getAttribute('data-cover') || ''
        }),
        timeoutMs: 15000
      })
      .then(function (data) {
        if (data.status === 'ok') {
          setWishlistButton(btn, true);
          if (typeof showToast === 'function') showToast('已添加收藏', 'success');
          _wishlistCache[albumId] = 'none';
        } else {
          if (typeof showToast === 'function') showToast(data.message || '收藏失败', 'danger');
        }
      })
      .catch(function (err) {
        if (err && err.name === 'AbortError') return; // pagehide 中止，静默
        if (typeof showToast === 'function') showToast(toastErr(err, '收藏失败'), 'danger');
      });
    }
  }

  function updateWishlistButtons() {
    resultsDiv.querySelectorAll('.wishlist-btn').forEach(function (btn) {
      setWishlistButton(btn, _wishlistCache[btn.getAttribute('data-album-id')] !== undefined);
    });
  }

  // ── 事件绑定 ──

  if (searchBtn) searchBtn.addEventListener('click', doSearch);

  // 拦截表单提交（Enter 键），防止页面重载
  if (searchForm) {
    searchForm.addEventListener('submit', function (e) { e.preventDefault(); });
  }

  if (searchInput) searchInput.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') {
      e.preventDefault();
      doSearch();
    }
  });
  if (sortSelect) sortSelect.addEventListener('change', function () {
    if (currentQuery) fetchResults(1);
  });
  if (pageSizeSelect) pageSizeSelect.addEventListener('change', function () {
    if (currentQuery) fetchResults(1);
  });

  // ── 搜索历史 ──
  // 关键词只放在 data-* 属性里（escapeHtmlAttr 对属性值是安全的）；拼进内联 JS 字符串则会被
  // encodeURIComponent 不编码的 ' ( ) 打断，形成可存储的脚本注入。

  var historyContainer = document.getElementById('search-history-container');
  var historyTags = document.getElementById('search-history-tags');

  function loadSearchHistory() {
    if (!historyContainer || !historyTags) return;

    window.apiFetch('/api/search-history', { timeoutMs: 15000 })
      .then(function (data) {
        if (data.status !== 'ok' || !data.items || data.items.length === 0) {
          historyContainer.classList.add('d-none');
          return;
        }
        // 渲染去重后的标签 + 删除按钮
        var html = '';
        data.items.forEach(function (item) {
          var kwAttr = escapeHtmlAttr(item.keyword);
          html += '<span class="search-history-tag badge bg-light text-dark border px-3 py-2">'
            + '<button type="button" class="search-history-text" data-history-search="' + kwAttr + '">' + escapeHtml(item.keyword) + '</button>'
            + '<button type="button" class="search-history-del" data-history-delete="' + kwAttr + '" title="删除" aria-label="删除搜索历史 ' + kwAttr + '"><i class="bi bi-x-circle-fill" aria-hidden="true"></i></button>'
            + '</span>';
        });
        // 清空按钮
        html += '<button type="button" class="btn btn-outline-danger btn-sm" data-history-clear title="清空全部" aria-label="清空全部搜索历史"><i class="bi bi-trash" aria-hidden="true"></i></button>';
        historyTags.innerHTML = html;
        historyContainer.classList.remove('d-none');
      })
      .catch(function (err) {
        if (err && err.name === 'AbortError') return; // pagehide 中止，静默（保留现有 UI）
        historyContainer.classList.add('d-none');
      });
  }

  if (historyTags) historyTags.addEventListener('click', function (event) {
    var el = event.target.closest('[data-history-search], [data-history-delete], [data-history-clear]');
    if (!el) return;
    if (el.hasAttribute('data-history-search')) {
      // 点击历史关键词重新搜索
      searchInput.value = el.getAttribute('data-history-search');
      doSearch();
    } else if (el.hasAttribute('data-history-delete')) {
      deleteSearchHistory(el.getAttribute('data-history-delete'));
    } else {
      clearSearchHistory();
    }
  });

  // ── 删除单条搜索历史 ──
  function deleteSearchHistory(keyword) {
    if (!confirm('确定删除 "' + keyword + '" 的搜索历史？')) return;
    window.apiFetch('/api/search-history/' + encodeURIComponent(keyword), { method: 'DELETE', timeoutMs: 15000 })
      .then(function (data) {
        if (data.status === 'ok') {
          if (typeof showToast === 'function') showToast('已删除', 'info');
          loadSearchHistory();
        }
      })
      .catch(function (err) {
        if (err && err.name === 'AbortError') return; // pagehide 中止，静默
        console.warn('操作搜索历史失败');
      });
  }

  // ── 清空全部搜索历史 ──
  function clearSearchHistory() {
    if (!confirm('确定清空全部搜索历史？')) return;
    window.apiFetch('/api/search-history/clear', { method: 'POST', timeoutMs: 15000 })
      .then(function (data) {
        if (data.status === 'ok') {
          if (typeof showToast === 'function') showToast('已清空', 'info');
          loadSearchHistory();
        }
      })
      .catch(function (err) {
        if (err && err.name === 'AbortError') return; // pagehide 中止，静默
        console.warn('操作搜索历史失败');
      });
  }

  // URL is the source of truth; snapshots preserve results across both reload and bfcache.
  restoreSearchState(false);
  window.addEventListener('pageshow', function (event) {
    if (event.persisted) {
      // 重新渲染快照，renderResults 会重新判断已下载标记
      restoreSearchState(true);
    } else if (!currentQuery && searchInput.value.trim()) {
      // Older history entries may restore only the form after scripts have executed.
      doSearch();
    }
  });
  // 切回本标签页（例如在另一个标签页下载完成后）时刷新已下载标记
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'visible') refreshReadable();
  });

  // ── 页面加载时获取搜索历史 ──
  loadSearchHistory();

})();
