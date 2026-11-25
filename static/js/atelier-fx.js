(() => {
  'use strict';

  // Zero-stutter GPU FX engine (pre-allocated recycled node pool + deferred rAF WAAPI).
  // Zero createElement/remove on click, zero forced reflows, zero idle timers/compositor layers.
  const root = document.documentElement;
  const reducedMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)');
  const INTERACTIVE_SELECTOR = [
    '.btn',
    '.stone-btn',
    '.shell-fx-toggle',
    '.pref-palette',
    '.pref-fx-card',
    '.desk-tile',
    '.sidebar-nav .nav-link',
    '.shelf-view button',
    '.tag-cloud-item',
    '.folio-filters a',
    '.folio-history',
    '.transfer-ledger a',
    '.quick-nav-item',
    '.quick-nav-toggle',
    '.reader-fab-toggle',
    '.reader-fab-btn',
    '.reader-fab-link',
    '.page-link',
    '.export-run-btn',
    '.record-cleanup summary',
  ].join(',');

  const POOL_SIZE = 4;
  let layer = null;
  let pool = null;
  let anims = null;
  let gens = null;
  let pending = null;
  let rafId = 0;
  let lastBurst = 0;

  function activeMode() {
    if (reducedMotion && reducedMotion.matches) return 'none';
    return root.dataset.fx || 'none';
  }

  function ensurePool() {
    if (layer && layer.isConnected && pool) return pool;
    layer = document.createElement('div');
    layer.className = 'atelier-fx-layer';
    layer.setAttribute('aria-hidden', 'true');
    pool = new Array(POOL_SIZE);
    anims = new Array(POOL_SIZE);
    gens = new Uint32Array(POOL_SIZE);
    const frag = document.createDocumentFragment();
    for (let i = 0; i < POOL_SIZE; i++) {
      const node = document.createElement('span');
      node.className = 'fx-node';
      node.style.visibility = 'hidden';
      frag.appendChild(node);
      pool[i] = node;
      anims[i] = null;
    }
    layer.appendChild(frag);
    document.body.appendChild(layer);
    return pool;
  }

  function hideAll() {
    if (!pool) return;
    for (let i = 0; i < POOL_SIZE; i++) {
      gens[i]++;
      if (anims[i]) {
        try { anims[i].cancel(); } catch (_) {}
        anims[i] = null;
      }
      pool[i].style.visibility = 'hidden';
    }
  }

  function playSlots(specs, x, y) {
    const nodes = ensurePool();
    const count = Math.min(specs.length, POOL_SIZE);
    for (let i = count; i < POOL_SIZE; i++) {
      gens[i]++;
      if (anims[i]) {
        try { anims[i].cancel(); } catch (_) {}
        anims[i] = null;
      }
      nodes[i].style.visibility = 'hidden';
    }
    for (let i = 0; i < count; i++) {
      const spec = specs[i];
      const node = nodes[i];
      if (typeof node.animate !== 'function') continue;
      const gen = ++gens[i];
      if (anims[i]) {
        try { anims[i].cancel(); } catch (_) {}
      }
      node.className = 'fx-node ' + spec.cls;
      if (spec.color) node.style.setProperty('--fx-c', spec.color);
      else node.style.removeProperty('--fx-c');
      node.style.visibility = 'visible';
      const anim = node.animate(spec.frames(x, y), {
        duration: spec.duration,
        easing: spec.easing,
        fill: 'forwards',
      });
      anims[i] = anim;
      anim.onfinish = () => {
        if (gens[i] === gen) {
          node.style.visibility = 'hidden';
          anims[i] = null;
        }
      };
    }
  }

  function buildSpecs(mode) {
    if (mode === 'calm') {
      return [{
        cls: 'fx-calm-ring',
        duration: 360,
        easing: 'cubic-bezier(.22, 1, .36, 1)',
        frames: (x, y) => [
          { opacity: 0.78, transform: `translate3d(${x}px, ${y}px, 0) scale(.35)` },
          { opacity: 0, transform: `translate3d(${x}px, ${y}px, 0) scale(2.15)` },
        ],
      }];
    }
    if (mode === 'pixel') {
      const dirs = [[-22, -18, 'var(--accent)'], [22, -18, 'var(--ok)'], [-20, 20, 'var(--warn)'], [20, 20, 'var(--danger)']];
      return dirs.map(([dx, dy, color]) => ({
        cls: 'fx-pixel-bit',
        color,
        duration: 340,
        easing: 'steps(5, end)',
        frames: (x, y) => [
          { opacity: 1, transform: `translate3d(${x}px, ${y}px, 0) scale(1.05)` },
          { opacity: 1, offset: 0.72 },
          { opacity: 0, transform: `translate3d(${x + dx}px, ${y + dy}px, 0) scale(.5)` },
        ],
      }));
    }
    if (mode === 'cyber') {
      return [
        {
          cls: 'fx-cyber-ring',
          duration: 340,
          easing: 'cubic-bezier(.16, 1, .3, 1)',
          frames: (x, y) => [
            { opacity: 0.92, transform: `translate3d(${x}px, ${y}px, 0) rotate(45deg) scale(.35)` },
            { opacity: 0, transform: `translate3d(${x}px, ${y}px, 0) rotate(90deg) scale(2.05)` },
          ],
        },
        {
          cls: 'fx-cyber-tick',
          duration: 310,
          easing: 'cubic-bezier(.16, 1, .3, 1)',
          frames: (x, y) => [
            { opacity: 0.95, transform: `translate3d(${x}px, ${y}px, 0) scaleX(.6)` },
            { opacity: 0, transform: `translate3d(${x + 28}px, ${y}px, 0) scaleX(1.35)` },
          ],
        },
        {
          cls: 'fx-cyber-tick',
          duration: 310,
          easing: 'cubic-bezier(.16, 1, .3, 1)',
          frames: (x, y) => [
            { opacity: 0.95, transform: `translate3d(${x}px, ${y}px, 0) scaleX(.6)` },
            { opacity: 0, transform: `translate3d(${x - 28}px, ${y}px, 0) scaleX(1.35)` },
          ],
        },
      ];
    }
    if (mode === 'anime') {
      const pts = [
        ['fx-anime-petal', -26, -22, -55, 'var(--accent)'],
        ['fx-anime-star',   24, -24,  70, '#F5B841'],
        ['fx-anime-petal', -20,  22,  50, '#F28CB1'],
        ['fx-anime-star',   26,  18, -65, '#7EB8F0'],
      ];
      return pts.map(([cls, dx, dy, rot, color]) => ({
        cls,
        color,
        duration: 420,
        easing: 'cubic-bezier(.22, 1, .36, 1)',
        frames: (x, y) => [
          { opacity: 0.94, transform: `translate3d(${x}px, ${y}px, 0) rotate(0deg) scale(.6)` },
          { opacity: 1, offset: 0.35, transform: `translate3d(${x + Math.round(dx * 0.5)}px, ${y + Math.round(dy * 0.5)}px, 0) rotate(${Math.round(rot * 0.55)}deg) scale(1.12)` },
          { opacity: 0, transform: `translate3d(${x + dx}px, ${y + dy}px, 0) rotate(${rot}deg) scale(.45)` },
        ],
      }));
    }
    return null;
  }

  function flushBurst() {
    rafId = 0;
    const job = pending;
    pending = null;
    if (!job || activeMode() === 'none') return;
    const specs = buildSpecs(job.mode);
    if (specs) playSlots(specs, job.x, job.y);
  }

  // Passive pointerdown performs ZERO DOM mutations; burst starts cleanly on the next rAF.
  document.addEventListener('pointerdown', event => {
    if (event.button !== 0) return;
    const mode = activeMode();
    if (mode === 'none') return;
    const target = event.target;
    if (!target || !target.closest) return;
    if (target.closest('input, textarea, select')) return;
    const hit = target.closest(INTERACTIVE_SELECTOR);
    if (!hit || hit.disabled || hit.getAttribute('aria-disabled') === 'true') return;
    const now = performance.now();
    if (now - lastBurst < 90) return;
    lastBurst = now;
    pending = { mode, x: Math.round(event.clientX), y: Math.round(event.clientY) };
    if (!rafId) rafId = requestAnimationFrame(flushBurst);
  }, { capture: true, passive: true });

  // Clean up pool immediately when FX is turned off (no whole-page switching animation).
  document.addEventListener('atelier-appearance', () => {
    if (activeMode() === 'none') {
      pending = null;
      if (rafId) {
        cancelAnimationFrame(rafId);
        rafId = 0;
      }
      hideAll();
    } else {
      ensurePool();
    }
  });

  if (activeMode() !== 'none') ensurePool();

  // Click-to-copy JM 车号 badge on detail page
  document.addEventListener('click', event => {
    const seal = event.target && event.target.closest ? event.target.closest('.album-id-seal') : null;
    if (!seal) return;
    const match = String(seal.textContent || '').match(/\d+/);
    if (!match) return;
    const idText = match[0];
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(idText).then(() => {
        if (window.showToast) window.showToast('已复制车号 ' + idText, 'success');
      }).catch(() => {});
    }
  });

  // Quick `/` shortcut to focus page's primary search box; Esc blurs focused search box cleanly
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') {
      const active = document.activeElement;
      if (active && active.matches && active.matches('#hero-keyword, #search-input, #library-search, #wishlist-search')) {
        active.blur();
      }
      return;
    }
    if (event.defaultPrevented || event.key !== '/' || event.ctrlKey || event.metaKey || event.altKey || event.isComposing) return;
    if (document.querySelector && document.querySelector('.modal.show, dialog[open]')) return;
    const tag = document.activeElement && document.activeElement.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || (document.activeElement && document.activeElement.isContentEditable)) return;
    const searchInput = document.querySelector('#hero-keyword, #search-input, #library-search, #wishlist-search');
    if (!searchInput || searchInput.disabled) return;
    event.preventDefault();
    searchInput.focus();
    if (typeof searchInput.select === 'function') searchInput.select();
  });
})();
