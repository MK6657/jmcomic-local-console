/**
 * JMComic 搜索页 JavaScript — 搜索 + 渲染 + 分页
 * 使用 Bootstrap alert 替代原生 alert()
 * 依赖: utils.js (window.apiFetch — 自带 AbortController/超时/pagehide 清理)
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

  var currentQuery = '';
  var currentPage = 1;
  var currentSort = 'latest';
  var currentPageSize = 20;
  var lastResults = null;
  var requestSerial = 0;
  var snapshotKey = 'jm-search-state-v1';

  function saveSearchState() {
    if (!currentQuery) return;
    var url = new URL(window.location.href);
    url.searchParams.set('keyword', currentQuery);
    url.searchParams.set('sort', currentSort);
    url.searchParams.set('page_size', currentPageSize);
    url.searchParams.set('page', currentPage);
    var snapshot = {
      url: url.pathname + url.search, query: currentQuery, sort: currentSort,
      pageSize: currentPageSize, page: currentPage, data: lastResults,
      scrollY: window.scrollY, savedAt: Date.now()
    };
    try {
      history.replaceState(Object.assign({}, history.state, { jmSearch: snapshot }), '', snapshot.url);
    } catch (_) {
      try { history.replaceState(null, '', snapshot.url); } catch (ignored) {}
    }
    try { sessionStorage.setItem(snapshotKey, JSON.stringify(snapshot)); } catch (_) {}
  }

  function restoreSearchState() {
    var params = new URLSearchParams(window.location.search);
    currentQuery = params.get('keyword') || '';
    currentSort = ['latest', 'views', 'likes'].indexOf(params.get('sort')) >= 0 ? params.get('sort') : 'latest';
    currentPage = Math.max(1, Math.min(500, parseInt(params.get('page'), 10) || 1));
    currentPageSize = [20, 50, 100].indexOf(Number(params.get('page_size'))) >= 0 ? Number(params.get('page_size')) : 20;
    if (!currentQuery) return false;
    searchInput.value = currentQuery;
    sortSelect.value = currentSort;
    pageSizeSelect.value = String(currentPageSize);
    var saved = history.state && history.state.jmSearch;
    if (!saved) {
      try { saved = JSON.parse(sessionStorage.getItem(snapshotKey)); } catch (_) {}
    }
    if (saved && saved.url === location.pathname + location.search && saved.data
        && Date.now() - saved.savedAt < 30 * 60 * 1000) {
      lastResults = saved.data;
      renderResults(lastResults);
      requestAnimationFrame(function () {
        requestAnimationFrame(function () { window.scrollTo(0, saved.scrollY || 0); });
      });
    } else {
      fetchResults(currentPage);
    }
    return true;
  }

  window.addEventListener('pagehide', function () {
    saveSearchState();
    requestSerial += 1;
  });
  resultsDiv.addEventListener('click', function (event) {
    if (event.target.closest('a, button, input')) return;
    var card = event.target.closest('[data-album-url]');
    if (card) window.location.href = card.getAttribute('data-album-url');
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
        renderResults(data);
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

    // 渲染卡片（网格布局，每行4个）——整个卡片可点击
    var html = '<div class="row row-cols-1 row-cols-sm-2 row-cols-lg-3 row-cols-xl-4 g-3 mb-4">';
    items.forEach(function (item) {
      var albumUrl = '/album/' + encodeURIComponent(item.album_id);
      html += '<div class="col">';
      html += '<div class="card album-card h-100 shadow-sm" data-album-url="' + albumUrl + '">';
      html += '<a href="' + albumUrl + '" class="text-decoration-none text-reset">';

      // 封面
      if (item.cover_url) {
        html += '<img src="' + escapeHtmlAttr(item.cover_url) + '" class="card-img-top" alt="' + escapeHtmlAttr(item.title) + '" loading="lazy">';
      } else {
        html += '<div class="cover-placeholder"><i class="bi bi-image"></i></div>';
      }

      html += '</a><div class="card-body d-flex flex-column">';
      html += '<h6 class="card-title"><a class="text-decoration-none text-reset" href="' + albumUrl + '">' + escapeHtml(item.title) + '</a></h6>';
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

      // 按钮（需 stopPropagation 防止触发父 <a> 导航）
      html += '<div class="mt-auto"><div class="d-flex gap-2 mb-2">';
      html += '<a href="' + albumUrl + '" class="btn btn-outline-primary btn-sm flex-fill"><i class="bi bi-info-circle"></i> 详情</a>';
      html += '<a href="/read/' + encodeURIComponent(item.album_id) + '" class="btn btn-outline-primary btn-sm flex-fill reader-link"><i class="bi bi-book"></i> 阅读</a>';
      html += '</div><div class="d-flex gap-2">';
      html += '<button type="button" class="btn btn-success btn-sm flex-fill" data-album-id="' + escapeHtmlAttr(item.album_id) + '" onclick="event.stopPropagation();quickDownload(this)"><i class="bi bi-download"></i> 下载</button>';
      html += '<button type="button" class="btn btn-sm wishlist-btn btn-outline-warning" data-album-id="' + escapeHtmlAttr(item.album_id) + '" data-title="' + escapeHtmlAttr(item.title) + '" data-author="' + escapeHtmlAttr(item.author) + '" data-cover="' + escapeHtmlAttr(item.cover_url) + '" onclick="event.stopPropagation();toggleWishlist(this)" title="收藏"><i class="bi bi-star"></i></button>';
      html += '</div></div>';

      html += '</div></div></div>';
    });
    html += '</div>';
    resultsDiv.innerHTML = html;

    // 分页
    renderPagination(currentPage, totalPages);
  }

  function renderPagination(page, totalPages) {
    if (totalPages <= 1) {
      paginationDiv.innerHTML = '';
      return;
    }

    var html = '<nav><ul class="pagination justify-content-center">';

    // 上一页
    html += '<li class="page-item ' + (page <= 1 ? 'disabled' : '') + '">';
    html += '<button class="page-link" onclick="searchGoTo(' + (page - 1) + ')" aria-label="上一页">&laquo;</button></li>';

    // 页码
    var start = Math.max(1, page - 2);
    var end = Math.min(totalPages, start + 4);
    start = Math.max(1, end - 4);

    if (start > 1) {
      html += '<li class="page-item"><button class="page-link" onclick="searchGoTo(1)">1</button></li>';
      if (start > 2) html += '<li class="page-item disabled"><span class="page-link">...</span></li>';
    }

    for (var p = start; p <= end; p++) {
      html += '<li class="page-item ' + (p === page ? 'active' : '') + '">';
      html += '<button class="page-link" onclick="searchGoTo(' + p + ')" aria-label="第 ' + p + ' 页">' + p + '</button></li>';
    }

    if (end < totalPages) {
      if (end < totalPages - 1) html += '<li class="page-item disabled"><span class="page-link">...</span></li>';
      html += '<li class="page-item"><button class="page-link" onclick="searchGoTo(' + totalPages + ')" aria-label="末页">' + totalPages + '</button></li>';
    }

    // 下一页
    html += '<li class="page-item ' + (page >= totalPages ? 'disabled' : '') + '">';
    html += '<button class="page-link" onclick="searchGoTo(' + (page + 1) + ')" aria-label="下一页">&raquo;</button></li>';

    html += '</ul></nav>';
    paginationDiv.innerHTML = html;
  }

  // ── 全局分页函数 ──

  window.searchGoTo = function (page) {
    fetchResults(page);
  };

  window.quickDownload = function (btn) {
    if (!btn) return;
    var albumId = btn.getAttribute('data-album-id');
    var title = '';
    // 从按钮所在卡片获取标题
    var card = btn.closest('.album-card') || btn.closest('.card');
    if (card) {
      var titleEl = card.querySelector('.card-title');
      if (titleEl) title = titleEl.textContent || '';
    }
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
    });
  };

  // ── 收藏操作 ──

  window.toggleWishlist = function (btn) {
    var albumId = btn.getAttribute('data-album-id');
    var title = btn.getAttribute('data-title') || '';
    var author = btn.getAttribute('data-author') || '';
    var coverUrl = btn.getAttribute('data-cover') || '';
    var iconEl = btn.querySelector('i');
    var isStarred = iconEl.classList.contains('bi-star-fill');

    if (isStarred) {
      // 取消收藏
      window.apiFetch('/api/wishlist/' + encodeURIComponent(albumId), { method: 'DELETE', timeoutMs: 15000 })
        .then(function (data) {
          if (data.status === 'ok') {
            iconEl.className = 'bi bi-star';
            btn.classList.remove('btn-warning');
            btn.classList.add('btn-outline-warning');
            btn.title = '收藏';
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
          title: title || '',
          author: author || '',
          cover_url: coverUrl || ''
        }),
        timeoutMs: 15000
      })
      .then(function (data) {
        if (data.status === 'ok') {
          iconEl.className = 'bi bi-star-fill';
          btn.classList.remove('btn-outline-warning');
          btn.classList.add('btn-warning');
          btn.title = '取消收藏';
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
  };

  window.updateWishlistButtons = function () {
    document.querySelectorAll('.wishlist-btn').forEach(function (btn) {
      var albumId = btn.getAttribute('data-album-id');
      var iconEl = btn.querySelector('i');
      if (_wishlistCache[albumId] !== undefined) {
        iconEl.className = 'bi bi-star-fill';
        btn.classList.remove('btn-outline-warning');
        btn.classList.add('btn-warning');
        btn.title = '取消收藏';
      } else {
        iconEl.className = 'bi bi-star';
        btn.classList.remove('btn-warning');
        btn.classList.add('btn-outline-warning');
        btn.title = '收藏';
      }
    });
  };

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

  function loadSearchHistory() {
    var container = document.getElementById('search-history-container');
    var tagsDiv = document.getElementById('search-history-tags');
    var clearBtn = document.getElementById('clear-search-history');
    if (!container || !tagsDiv) return;

    window.apiFetch('/api/search-history', { timeoutMs: 15000 })
      .then(function (data) {
        if (data.status !== 'ok' || !data.items || data.items.length === 0) {
          container.classList.add('d-none');
          return;
        }
        // 渲染去重后的标签 + 删除按钮
        var html = '';
        data.items.forEach(function (item) {
          var kw = escapeHtml(item.keyword);
          var kwEnc = encodeURIComponent(item.keyword);
          html += '<span class="search-history-tag badge bg-light text-dark border px-3 py-2 me-1 mb-1" style="cursor:default;font-weight:normal;">'
            + '<span class="search-history-text" style="cursor:pointer;" onclick="searchHistoryClick(\'' + kwEnc + '\')">' + kw + '</span>'
            + ' <i class="bi bi-x-circle-fill text-danger search-history-del" style="cursor:pointer;" onclick="deleteSearchHistory(\'' + kwEnc + '\')" title="删除"></i>'
            + '</span>';
        });
        // 清空按钮
        html += '<button class="btn btn-outline-danger btn-sm ms-2" onclick="clearSearchHistory()" title="清空全部"><i class="bi bi-trash"></i></button>';
        tagsDiv.innerHTML = html;
        container.classList.remove('d-none');
        if (clearBtn) clearBtn.classList.remove('d-none');
      })
      .catch(function (err) {
        if (err && err.name === 'AbortError') return; // pagehide 中止，静默（保留现有 UI）
        container.classList.add('d-none');
      });
  }

  // ── 搜索历史点击重新搜索 ──
  window.searchHistoryClick = function (keyword) {
    searchInput.value = decodeURIComponent(keyword);
    doSearch();
  };

  // ── 删除单条搜索历史 ──
  window.deleteSearchHistory = function (keyword) {
    keyword = decodeURIComponent(keyword);
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
  };

  // ── 清空全部搜索历史 ──
  window.clearSearchHistory = function () {
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
  };

  // URL is the source of truth; snapshots preserve results across both reload and bfcache.
  restoreSearchState();
  window.addEventListener('pageshow', function (event) {
    if (event.persisted) {
      restoreSearchState();
    } else if (!currentQuery && searchInput.value.trim()) {
      // Older history entries may restore only the form after scripts have executed.
      doSearch();
    }
  });

  // ── 页面加载时获取搜索历史 ──
  loadSearchHistory();

  // ── 工具 ──

  function escapeHtml(text) {
    if (!text) return '';
    var d = document.createElement('div');
    d.textContent = text;
    return d.innerHTML;
  }

  function escapeHtmlAttr(text) {
    if (!text) return '';
    return escapeHtml(text).replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

})();
