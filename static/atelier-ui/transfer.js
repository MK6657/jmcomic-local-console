(() => {
  'use strict';

  // Zero-dependency transfer ledger (replaces 54 KB Vue runtime bundle).
  // Counts belong to the same server snapshot as the visible queue; no extra poll or SSE.
  const STATES = [
    ['running', '下载中'],
    ['queued', '排队'],
    ['paused', '已暂停'],
    ['completed', '已完成'],
  ];

  function validCounts(counts) {
    return Boolean(
      counts &&
      STATES.every(([key]) => Number.isSafeInteger(counts[key]) && counts[key] >= 0)
    );
  }

  function init() {
    const container = document.getElementById('atelier-transfer-summary');
    if (!container) return;

    let nav = null;
    const refs = Object.create(null);

    function ensureDom() {
      if (nav) return;
      nav = document.createElement('nav');
      nav.className = 'transfer-ledger';
      nav.setAttribute('aria-label', '全部任务状态统计');
      for (const [key, label] of STATES) {
        const link = document.createElement('a');
        link.href = '#' + key;
        link.dataset.state = key;
        const countEl = document.createElement('strong');
        countEl.textContent = '0';
        const labelEl = document.createElement('span');
        labelEl.textContent = label;
        link.append(countEl, labelEl);
        nav.appendChild(link);
        refs[key] = { link, countEl, label };
      }
      container.replaceChildren(nav);
    }

    function update(event) {
      const payload = event && event.detail;
      if (!payload || payload.status !== 'ok') return;
      if (!validCounts(payload.counts)) {
        if (nav) {
          nav.remove();
          nav = null;
        }
        return;
      }
      ensureDom();
      for (const [key] of STATES) {
        const count = payload.counts[key];
        const entry = refs[key];
        entry.countEl.textContent = String(count);
        entry.link.classList.toggle('is-active-count', count > 0);
        entry.link.setAttribute('aria-label', `${entry.label} ${count} 项，查看任务`);
      }
    }

    document.addEventListener('atelier-jobs-summary', update);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init, { once: true });
  } else {
    init();
  }
})();
