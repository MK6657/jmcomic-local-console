(() => {
  'use strict';

  const root = document.documentElement;
  const fallbackThemes = ['sage', 'mint', 'paper', 'linen', 'apricot', 'night', 'pixel', 'cyber', 'anime'];
  const darkThemes = new Set(['night', 'cyber']);
  const themeDefaultFx = {
    sage: 'calm', mint: 'calm', paper: 'calm', linen: 'calm', apricot: 'calm', night: 'calm',
    pixel: 'pixel', cyber: 'cyber', anime: 'anime',
  };
  const fxPresets = new Set(['auto', 'calm', 'pixel', 'cyber', 'anime']);
  const storageKey = 'ui_theme';
  const fxEnabledKey = 'ui_fx_enabled';
  const fxPresetKey = 'ui_fx_preset';

  const stones = document.querySelectorAll('.mineral-dock [data-set-theme]');
  const fxToggle = document.getElementById('shell-fx-toggle');
  stones.forEach(stone => {
    const key = stone.dataset.setTheme;
    if (!key) return;
    if (stone.dataset.scheme === 'dark') darkThemes.add(key);
    else if (stone.dataset.scheme === 'light') darkThemes.delete(key);
    if (stone.dataset.defaultFx) themeDefaultFx[key] = stone.dataset.defaultFx;
  });
  const classicSwitch = document.querySelector('a.atelier-classic[href="/ui/classic"]');
  if (classicSwitch && typeof classicSwitch.addEventListener === 'function') {
    classicSwitch.addEventListener('click', () => {
      if (!window.location) return;
      const cur = (window.location.pathname || '') + (window.location.search || '');
      if (cur && cur !== '/downloads' && !cur.startsWith('/ui/')) {
        classicSwitch.href = '/ui/classic?next=' + encodeURIComponent(cur);
      }
    });
  }
  const themes = new Set(
    stones.length
      ? Array.from(stones, stone => stone.dataset.setTheme).filter(Boolean)
      : fallbackThemes
  );
  const scheme = document.querySelector('meta[name="color-scheme"]');

  let confirmed = {
    theme: root.dataset.theme || 'sage',
    fxEnabled: root.dataset.fxEnabled !== 'false',
    fxPreset: fxPresets.has(root.dataset.fxPreset) ? root.dataset.fxPreset : 'auto',
  };
  let desired = { ...confirmed };
  let pendingSave = null;

  function resolveFx(state) {
    if (!state.fxEnabled) return 'none';
    if (state.fxPreset === 'auto') return themeDefaultFx[state.theme] || 'calm';
    return fxPresets.has(state.fxPreset) ? state.fxPreset : 'calm';
  }

  function paint(state) {
    root.dataset.theme = state.theme;
    root.dataset.bsTheme = darkThemes.has(state.theme) ? 'dark' : 'light';
    if (scheme) scheme.content = root.dataset.bsTheme;
    root.dataset.fxEnabled = String(state.fxEnabled);
    root.dataset.fxPreset = state.fxPreset;
    root.dataset.fx = resolveFx(state);
    stones.forEach(stone => stone.setAttribute('aria-pressed', String(stone.dataset.setTheme === state.theme)));
    if (fxToggle) {
      fxToggle.setAttribute('aria-pressed', String(state.fxEnabled));
      fxToggle.title = state.fxEnabled ? '交互动效已开启（点击一键关闭）' : '交互动效已关闭（点击一键开启）';
      const label = fxToggle.querySelector('span');
      if (label) label.textContent = state.fxEnabled ? '动效开' : '动效关';
    }
  }

  function remember(state) {
    try {
      localStorage.setItem(storageKey, state.theme);
      localStorage.setItem(fxEnabledKey, String(state.fxEnabled));
      localStorage.setItem(fxPresetKey, state.fxPreset);
    } catch (_) {}
  }

  function announce(state) {
    document.dispatchEvent(new CustomEvent('atelier-theme', { detail: { theme: state.theme } }));
    document.dispatchEvent(new CustomEvent('atelier-appearance', {
      detail: {
        theme: state.theme,
        fxEnabled: state.fxEnabled,
        fxPreset: state.fxPreset,
        activeFx: resolveFx(state),
      },
    }));
  }

  document.addEventListener('atelier-theme', event => {
    const theme = event.detail && event.detail.theme;
    if (!themes.has(theme)) return;
    if (confirmed.theme === theme && desired.theme === theme) return;
    confirmed = { ...confirmed, theme };
    desired = { ...desired, theme };
    paint(confirmed);
    remember(confirmed);
  });

  function diffPayload(target, base) {
    const patch = {};
    if (target.theme !== base.theme) patch.ui_theme = target.theme;
    if (target.fxEnabled !== base.fxEnabled) patch.ui_fx_enabled = target.fxEnabled ? 'true' : 'false';
    if (target.fxPreset !== base.fxPreset) patch.ui_fx_preset = target.fxPreset;
    return patch;
  }

  async function commit(nextPartial) {
    const candidate = {
      theme: nextPartial.theme !== undefined && themes.has(nextPartial.theme) ? nextPartial.theme : desired.theme,
      fxEnabled: nextPartial.fxEnabled !== undefined ? Boolean(nextPartial.fxEnabled) : desired.fxEnabled,
      fxPreset: nextPartial.fxPreset !== undefined && fxPresets.has(nextPartial.fxPreset) ? nextPartial.fxPreset : desired.fxPreset,
    };
    if (!Object.keys(diffPayload(candidate, desired)).length) return pendingSave;
    desired = candidate;
    paint(desired);
    if (!pendingSave) pendingSave = persistAppearance().finally(() => { pendingSave = null; });
    return pendingSave;
  }

  async function persistAppearance() {
    while (Object.keys(diffPayload(desired, confirmed)).length) {
      const sending = { ...desired };
      const patch = diffPayload(sending, confirmed);
      try {
        const response = await fetch('/api/settings', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(patch),
          keepalive: true,
          signal: AbortSignal.timeout(15000),
        });
        if (!response.ok) throw new Error();
        const result = await response.json();
        if (result.status !== 'ok' || !result.settings) throw new Error();
        if (patch.ui_theme && result.settings.ui_theme !== patch.ui_theme) throw new Error();
        if (patch.ui_fx_enabled && String(result.settings.ui_fx_enabled) !== patch.ui_fx_enabled) throw new Error();
        if (patch.ui_fx_preset && result.settings.ui_fx_preset !== patch.ui_fx_preset) throw new Error();
        confirmed = sending;
      } catch (_) {
        if (Object.keys(diffPayload(desired, sending)).length) continue;
        desired = { ...confirmed };
        paint(confirmed);
        if (window.showToast) window.showToast('外观设置未能保存，已恢复上次选择。', 'warning');
      }
    }
    remember(confirmed);
    announce(confirmed);
  }

  async function resetAppearance() {
    try {
      if (pendingSave) await pendingSave;
      const response = await fetch('/api/settings/reset', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ scope: 'appearance' }),
        signal: AbortSignal.timeout(15000),
      });
      if (!response.ok) throw new Error();
      const result = await response.json();
      if (result.status !== 'ok' || !result.settings) throw new Error();
      const s = result.settings;
      confirmed = {
        theme: themes.has(s.ui_theme) ? s.ui_theme : 'sage',
        fxEnabled: String(s.ui_fx_enabled) !== 'false',
        fxPreset: fxPresets.has(s.ui_fx_preset) ? s.ui_fx_preset : 'auto',
      };
      desired = { ...confirmed };
      paint(confirmed);
      remember(confirmed);
      announce(confirmed);
      if (window.showToast) window.showToast('已还原默认外观与动效配置', 'success');
      return true;
    } catch (_) {
      if (window.showToast) window.showToast('还原默认外观失败，请稍后重试', 'danger');
      return false;
    }
  }

  function syncFromSettings(s) {
    if (!s || typeof s !== 'object') return;
    const next = {
      theme: themes.has(s.ui_theme) ? s.ui_theme : confirmed.theme,
      fxEnabled: s.ui_fx_enabled !== undefined ? String(s.ui_fx_enabled) !== 'false' : confirmed.fxEnabled,
      fxPreset: fxPresets.has(s.ui_fx_preset) ? s.ui_fx_preset : confirmed.fxPreset,
    };
    confirmed = next;
    desired = { ...next };
    paint(confirmed);
    remember(confirmed);
    announce(confirmed);
  }

  window.atelierAppearance = {
    setTheme: theme => commit({ theme }),
    setFxEnabled: enabled => commit({ fxEnabled: Boolean(enabled) }),
    setFxPreset: preset => commit({ fxPreset: preset }),
    resetAppearance,
    syncFromSettings,
    whenSaved: () => pendingSave,
    getState: () => ({ ...desired, activeFx: resolveFx(desired) }),
  };

  paint(confirmed);
  if (themes.has(confirmed.theme)) remember(confirmed);

  stones.forEach(stone => stone.addEventListener('click', () => commit({ theme: stone.dataset.setTheme })));
  if (fxToggle) {
    fxToggle.addEventListener('click', () => commit({ fxEnabled: !desired.fxEnabled }));
  }

  function syncStored() {
    try {
      const savedTheme = localStorage.getItem(storageKey);
      const savedEnabled = localStorage.getItem(fxEnabledKey);
      const savedPreset = localStorage.getItem(fxPresetKey);
      const next = {
        theme: themes.has(savedTheme) ? savedTheme : confirmed.theme,
        fxEnabled: savedEnabled == null ? confirmed.fxEnabled : savedEnabled !== 'false',
        fxPreset: fxPresets.has(savedPreset) ? savedPreset : confirmed.fxPreset,
      };
      if (Object.keys(diffPayload(next, confirmed)).length) {
        confirmed = next;
        desired = { ...next };
        paint(confirmed);
        announce(confirmed);
      }
    } catch (_) {}
  }
  window.addEventListener('storage', event => {
    if (event.key === storageKey || event.key === fxEnabledKey || event.key === fxPresetKey) syncStored();
  });
  window.addEventListener('pageshow', event => {
    if (event.persisted && !pendingSave) syncStored();
  });

  const dock = document.getElementById('reader-tools');
  if (dock && window.ResizeObserver) {
    let lastDockH = -1;
    new ResizeObserver(() => {
      const h = dock.offsetHeight;
      if (h !== lastDockH) {
        lastDockH = h;
        root.style.setProperty('--atelier-reader-dock-h', `${h}px`);
      }
    }).observe(dock);
  }

  // ── Reader & Preview Floating Quick-Expand Action FAB (#reader-fab) ──
  const readerFab = document.getElementById('reader-fab');
  if (readerFab) {
    const fabToggle = readerFab.querySelector('.reader-fab-toggle');
    const fabPanel = readerFab.querySelector('.reader-fab-panel');
    const toasts = document.getElementById('toast-container');
    let fabMode = null; // null | 'hover' | 'click'
    let fabCloseTimer = null;

    function setFabOpen(mode) {
      clearTimeout(fabCloseTimer);
      fabMode = mode || null;
      readerFab.classList.toggle('is-open', Boolean(fabMode));
      if (fabToggle) fabToggle.setAttribute('aria-expanded', fabMode ? 'true' : 'false');
    }

    // Prevent clicks/taps on FAB from toggling reader tools or flipping preview pages
    ['pointerdown', 'click'].forEach(evt => {
      readerFab.addEventListener(evt, e => e.stopPropagation());
    });

    if (fabToggle) {
      fabToggle.addEventListener('click', () => {
        if (fabMode === 'hover') setFabOpen('click');
        else setFabOpen(fabMode ? null : 'click');
      });
    }

    readerFab.addEventListener('pointerenter', event => {
      if (event.pointerType !== 'mouse') return;
      clearTimeout(fabCloseTimer);
      if (!fabMode) setFabOpen('hover');
    });
    readerFab.addEventListener('pointerleave', event => {
      if (event.pointerType !== 'mouse' || fabMode !== 'hover') return;
      clearTimeout(fabCloseTimer);
      fabCloseTimer = setTimeout(() => {
        if (fabMode === 'hover') setFabOpen(null);
      }, 260);
    });

    document.addEventListener('keydown', event => {
      if (event.key === 'Escape' && fabMode) {
        const inside = readerFab.contains(document.activeElement);
        setFabOpen(null);
        if (inside && fabToggle) fabToggle.focus();
      }
    });
    document.addEventListener('pointerdown', event => {
      if (fabMode && !readerFab.contains(event.target) && !toasts?.contains(event.target)) setFabOpen(null);
    }, true);
    readerFab.addEventListener('focusout', event => {
      if (!fabMode) return;
      const next = event.relatedTarget;
      if (next && (readerFab.contains(next) || toasts?.contains(next))) return;
      if (next) setFabOpen(null);
    });
    window.addEventListener('pagehide', () => setFabOpen(null));

    if (fabPanel) {
      ['wheel', 'touchmove'].forEach(name => {
        fabPanel.addEventListener(name, event => event.stopPropagation(), { passive: true });
      });
      fabPanel.addEventListener('click', event => {
        const btn = event.target.closest('button[data-fab-action]');
        if (!btn) return;
        const action = btn.dataset.fabAction;
        if (action === 'top') {
          setFabOpen(null);
          const readerTop = document.getElementById('reader-top');
          if (readerTop && !readerTop.disabled) {
            readerTop.click();
          } else {
            window.scrollTo({ top: 0, behavior: 'instant' });
          }
        } else if (action === 'bottom') {
          setFabOpen(null);
          const readerBottom = document.getElementById('reader-bottom');
          if (readerBottom && !readerBottom.disabled) {
            readerBottom.click();
          } else {
            const maxY = Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
            window.scrollTo({ top: maxY, behavior: 'instant' });
          }
        } else if (action === 'tools') {
          const trigger = document.getElementById('reader-tools-toggle') || document.getElementById('preview-tools-toggle');
          if (trigger) trigger.click();
          setFabOpen(null);
        } else if (action === 'back') {
          setFabOpen(null);
          const origin = document.getElementById('reader-origin') || document.getElementById('preview-back');
          if (origin && typeof origin.click === 'function') {
            origin.click();
            return;
          }
          if (window.quickNav && typeof window.quickNav.goBack === 'function' && window.quickNav.goBack(event)) return;
          window.location.href = (origin && origin.getAttribute('href')) || '/';
        }
      });
    }
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
      const response = await fetch('/api/system/health?activity=1', { cache: 'no-store', signal: controller.signal });
      const result = await response.json();
      if (!response.ok || result.status !== 'ok' || !Number.isInteger(result.jobs?.running) || !Number.isInteger(result.jobs?.queued)) throw new Error();
      if (request === controller) {
        const activeCount = result.jobs.running;
        activity.textContent = `${activeCount} 进行中 · ${result.jobs.queued} 排队`;
        card.classList.toggle('is-busy', activeCount > 0 || result.jobs.queued > 0);
      }
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
  if (!request && !timer) {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', () => { if (!request && !timer) refresh(); }, { once: true });
    } else {
      refresh();
    }
  }
})();
