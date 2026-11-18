(() => {
  'use strict';

  const root = document.documentElement;
  const themes = new Set(['paper', 'sage', 'linen', 'night']);
  const storageKey = 'ui_theme';
  const stones = document.querySelectorAll('.mineral-dock [data-set-theme]');
  const scheme = document.querySelector('meta[name="color-scheme"]');
  let confirmed = root.dataset.theme;
  let desired = confirmed;
  let saving = false;

  function paint(theme) {
    root.dataset.theme = theme;
    root.dataset.bsTheme = theme === 'night' ? 'dark' : 'light';
    if (scheme) scheme.content = root.dataset.bsTheme;
    stones.forEach(stone => stone.setAttribute('aria-pressed', String(stone.dataset.setTheme === theme)));
  }

  function remember(theme) {
    try { localStorage.setItem(storageKey, theme); } catch (_) {}
  }

  function announce(theme) {
    document.dispatchEvent(new CustomEvent('atelier-theme', {detail: {theme}}));
  }

  document.addEventListener('atelier-theme', event => {
    const theme = event.detail && event.detail.theme;
    if (!themes.has(theme)) return;
    confirmed = desired = theme;
    paint(theme);
    remember(theme);
  });

  async function choose(theme) {
    if (!themes.has(theme) || theme === desired) return;
    desired = theme;
    paint(theme);
    if (saving) return;
    saving = true;
    try {
      while (desired !== confirmed) {
        const sending = desired;
        try {
          const response = await fetch('/api/settings', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ui_theme: sending}), keepalive: true,
            signal: AbortSignal.timeout(15000)
          });
          if (!response.ok) throw new Error();
          const result = await response.json();
          if (result.status !== 'ok' || result.settings?.ui_theme !== sending) throw new Error();
          confirmed = sending;
        } catch (_) {
          if (desired !== sending) continue;
          desired = confirmed;
          if (window.showToast) window.showToast('纸墨色调未能保存，已恢复上次选择。', 'warning');
        }
      }
      announce(confirmed);
    } finally { saving = false; }
  }

  if (themes.has(confirmed)) remember(confirmed);
  stones.forEach(stone => stone.addEventListener('click', () => choose(stone.dataset.setTheme)));
  function syncStored() {
    try {
      const saved = localStorage.getItem(storageKey);
      if (themes.has(saved) && saved !== confirmed) announce(saved);
    } catch (_) {}
  }
  window.addEventListener('storage', event => {
    if (event.key === storageKey) syncStored();
  });
  window.addEventListener('pageshow', event => {
    if (event.persisted && !saving) syncStored();
  });

  const dock = document.getElementById('reader-tools');
  if (dock && window.ResizeObserver) {
    new ResizeObserver(() => {
      root.style.setProperty('--atelier-reader-dock-h', `${dock.offsetHeight}px`);
    }).observe(dock);
  }

  const activity = document.getElementById('atelier-activity');
  if (!activity) return;
  const card = activity.closest('a');
  let timer = null;
  let request = null;
  function stop() {
    clearTimeout(timer);
    if (request) request.abort();
    request = null;
  }
  async function refresh() {
    stop();
    if (document.hidden || !card.getClientRects().length) return;
    const controller = new AbortController();
    request = controller;
    const deadline = setTimeout(() => controller.abort(), 8000);
    try {
      const response = await fetch('/api/system/health?activity=1', {cache: 'no-store', signal: controller.signal});
      const result = await response.json();
      if (!response.ok || result.status !== 'ok' || !Number.isInteger(result.jobs?.running) || !Number.isInteger(result.jobs?.queued)) throw new Error();
      if (request === controller) activity.textContent = `${result.jobs.running} 进行中 · ${result.jobs.queued} 排队`;
    } catch (_) {
      if (request === controller) activity.textContent = '暂时无法读取';
    } finally {
      clearTimeout(deadline);
      if (request === controller) {
        request = null;
        card.title = card.ariaLabel = `传输中心：${activity.textContent}`;
        timer = setTimeout(refresh, 30000);
      }
    }
  }
  window.addEventListener('pagehide', stop);
  window.addEventListener('pageshow', refresh);
  document.addEventListener('visibilitychange', refresh);
  window.matchMedia('(max-width: 640px)').addEventListener('change', refresh);
})();
