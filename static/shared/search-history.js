(function () {
  'use strict';

  const container = document.getElementById('search-history-container');
  const toggle = document.getElementById('search-history-toggle');
  const panel = document.getElementById('search-history-panel');
  const tags = document.getElementById('search-history-tags');
  const status = document.getElementById('search-history-status');
  const eye = toggle.querySelector('.bi');
  const key = 'jmconsole.searchHistorySession';
  const endpoint = '/api/search-history';
  let session = container.dataset.session;
  let requestVersion = 0;
  let revealed = false;

  function remember(open) {
    revealed = open;
    try {
      if (open) localStorage.setItem(key, session);
      else localStorage.removeItem(key);
    } catch (_) { /* The toggle still works in this page without storage. */ }
  }

  function syncPreference() {
    try { revealed = localStorage.getItem(key) === session; } catch (_) {}
  }

  function display(open) {
    panel.hidden = !open;
    toggle.setAttribute('aria-expanded', String(open));
    toggle.title = open ? '隐藏搜索历史' : '显示搜索历史';
    eye.classList.toggle('bi-eye', !open);
    eye.classList.toggle('bi-eye-slash', open);
    if (!open) {
      ++requestVersion;
      tags.replaceChildren();
      status.textContent = '';
    }
  }

  async function load(userOpened = false) {
    if (!revealed || document.hidden) return;
    const version = ++requestVersion;
    if (panel.hidden) {
      status.textContent = '正在加载…';
      display(true);
    }
    try {
      const data = await window.apiFetch(endpoint, {
        cache: 'no-store', timeoutMs: 5000, headers: { 'X-JM-History-Session': session },
      });
      if (version !== requestVersion) return;
      if (data.session !== session) {
        session = data.session;
        display(false);
        // A deliberate click on a tab left open across restart may reveal the new session.
        if (userOpened && session) {
          remember(true);
          return load();
        }
        revealed = false;
        return;
      }
      tags.innerHTML = data.items.map(function (item) {
        const value = escapeHtmlAttr(item.keyword);
        return '<span class="search-history-tag badge bg-light text-dark border px-3 py-2">'
          + '<button type="button" class="search-history-text" data-history-search="' + value + '">' + escapeHtml(item.keyword) + '</button>'
          + '<button type="button" class="search-history-del" data-history-delete="' + value + '" aria-label="删除搜索历史 ' + value + '"><i class="bi bi-x-circle-fill" aria-hidden="true"></i></button>'
          + '</span>';
      }).join('') + (data.items.length
        ? '<button type="button" class="btn btn-outline-danger btn-sm" data-history-clear title="清空全部" aria-label="清空全部搜索历史"><i class="bi bi-trash" aria-hidden="true"></i></button>'
        : '');
      status.textContent = data.items.length ? '' : '暂无搜索历史';
    } catch (_) {
      if (version === requestVersion) {
        tags.replaceChildren();
        status.textContent = '搜索历史暂时无法读取';
      }
    }
  }

  toggle.addEventListener('click', function () {
    const open = panel.hidden;
    remember(open);
    if (open) load(true);
    else display(false);
  });

  tags.addEventListener('click', async function (event) {
    const button = event.target.closest('button');
    if (!button) return;
    if (button.hasAttribute('data-history-search')) {
      document.getElementById('search-input').value = button.dataset.historySearch;
      document.getElementById('search-btn').click();
      return;
    }
    const keyword = button.dataset.historyDelete;
    if (!confirm(keyword ? '确定删除这条搜索历史？' : '确定清空全部搜索历史？')) return;
    try {
      await window.apiFetch(keyword ? endpoint + '/' + encodeURIComponent(keyword) : endpoint + '/clear', {
        method: keyword ? 'DELETE' : 'POST', timeoutMs: 15000,
      });
      load();
    } catch (_) {
      if (revealed && !panel.hidden) status.textContent = '操作失败，请重试';
    }
  });

  function restore() {
    syncPreference();
    if (revealed) load();
    else display(false);
  }
  window.addEventListener('storage', (event) => { if (event.key === key) restore(); });
  window.addEventListener('pagehide', () => display(false));
  window.addEventListener('pageshow', (event) => { if (event.persisted) restore(); });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) display(false);
    else restore();
  });
  // Cover tabs that stay open and in view while the desktop program restarts.
  window.setInterval(async function () {
    if (panel.hidden || document.hidden) return;
    const version = requestVersion;
    let current;
    try {
      const response = await fetch(endpoint, { method: 'HEAD', cache: 'no-store', signal: AbortSignal.timeout(3000) });
      if (response.ok) current = response.headers.get('X-JM-History-Session');
    } catch (_) {}
    if (version !== requestVersion || current === session) return;
    if (current) session = current;
    revealed = false;
    display(false);
  }, 5000);

  restore();
})();
