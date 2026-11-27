const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const { runInNewContext } = require('node:vm');

async function run() {
  const source = readFileSync(join(__dirname, '../../static/js/atelier-shell.js'), 'utf8');
  const document = new EventTarget();
  const window = new EventTarget();
  const scheme = { content: 'dark' };
  const confirmed = [];
  const warnings = [];
  const requests = [];
  const storage = new Map([['ui_theme', 'paper']]);
  const themes = ['sage', 'mint', 'paper', 'linen', 'apricot', 'night', 'pixel', 'cyber', 'anime'];
  const darkThemes = new Set(['night', 'cyber']);
  const themeDefaultFx = {
    sage: 'calm', mint: 'calm', paper: 'calm', linen: 'calm', apricot: 'calm', night: 'calm',
    pixel: 'pixel', cyber: 'cyber', anime: 'anime',
  };
  const stones = Object.fromEntries(themes.map(theme => {
    const stone = new EventTarget();
    stone.dataset = { setTheme: theme, scheme: darkThemes.has(theme) ? 'dark' : 'light' };
    stone.setAttribute = (key, value) => { stone[key] = value; };
    return [theme, stone];
  }));
  const fxLabel = { textContent: '动效开' };
  const fxToggle = new EventTarget();
  fxToggle.setAttribute = (key, value) => { fxToggle[key] = value; };
  fxToggle.querySelector = () => fxLabel;

  document.documentElement = {
    dataset: { theme: 'night', bsTheme: 'dark', fxEnabled: 'true', fxPreset: 'auto', fx: 'calm' },
  };
  document.querySelector = () => scheme;
  document.querySelectorAll = () => Object.values(stones);
  document.getElementById = id => (id === 'shell-fx-toggle' ? fxToggle : null);
  document.addEventListener('atelier-theme', event => confirmed.push(event.detail.theme));
  window.showToast = (...args) => warnings.push(args);
  class ThemeEvent extends Event {
    constructor(type, options) { super(type); this.detail = options.detail; }
  }
  const fetch = (url, options) => new Promise(resolve => {
    assert.equal(options.method, 'POST');
    const body = JSON.parse(options.body);
    if (url === '/api/settings/reset') {
      requests.push({
        url,
        body,
        finish: ok => resolve({
          ok,
          json: async () => ({
            status: 'ok',
            settings: { ui_theme: 'sage', ui_fx_enabled: 'true', ui_fx_preset: 'auto' },
          }),
        }),
      });
      return;
    }
    assert.equal(url, '/api/settings');
    requests.push({
      url,
      body,
      theme: body.ui_theme,
      finish: ok => resolve({ ok, json: async () => ({ status: 'ok', settings: body }) }),
    });
  });
  runInNewContext(source, {
    window,
    document,
    AbortSignal,
    CustomEvent: ThemeEvent,
    fetch,
    localStorage: {
      getItem: key => (storage.has(key) ? storage.get(key) : null),
      setItem: (key, value) => storage.set(key, value),
    },
  });
  const click = theme => stones[theme].dispatchEvent(new Event('click'));
  const flush = () => new Promise(resolve => setImmediate(resolve));
  const paint = (theme, fxEnabled = true, fxPreset = 'auto') => {
    const activeFx = !fxEnabled ? 'none' : (fxPreset === 'auto' ? (themeDefaultFx[theme] || 'calm') : fxPreset);
    assert.deepEqual(document.documentElement.dataset, {
      theme,
      bsTheme: darkThemes.has(theme) ? 'dark' : 'light',
      fxEnabled: String(fxEnabled),
      fxPreset,
      fx: activeFx,
    });
    assert.equal(scheme.content, document.documentElement.dataset.bsTheme);
    assert.deepEqual(themes.filter(key => stones[key]['aria-pressed'] === 'true'), [theme]);
    assert.equal(fxToggle['aria-pressed'], String(fxEnabled));
    assert.equal(fxLabel.textContent, fxEnabled ? '动效开' : '动效关');
  };

  assert.equal(storage.get('ui_theme'), 'night');
  assert.equal(requests.length, 0);
  click('sage'); click('paper'); click('cyber');
  paint('cyber');
  assert.deepEqual(requests.map(req => req.theme), ['sage']);
  requests[0].finish(true); await flush();
  assert.deepEqual(requests.map(req => req.theme), ['sage', 'cyber']);
  requests[1].finish(true); await flush();
  assert.deepEqual(confirmed, ['cyber']);
  assert.equal(storage.get('ui_theme'), 'cyber');
  click('night'); paint('night');
  requests[2].finish(false); await flush();
  paint('cyber'); assert.equal(warnings.length, 1);

  // Mix-and-match: anime theme + pixel FX preset + one-click toggle off
  window.atelierAppearance.setTheme('anime');
  requests[3].finish(true); await flush();
  paint('anime', true, 'auto');
  window.atelierAppearance.setFxPreset('pixel');
  requests[4].finish(true); await flush();
  paint('anime', true, 'pixel');
  fxToggle.dispatchEvent(new Event('click'));
  requests[5].finish(true); await flush();
  paint('anime', false, 'pixel');

  window.atelierAppearance.setTheme('night');
  window.atelierAppearance.setFxPreset('cyber');
  const resetPromise = window.atelierAppearance.resetAppearance();
  assert.equal(requests.length, 7);
  requests[6].finish(true);
  await flush();
  assert.deepEqual(requests[7].body, { ui_fx_preset: 'cyber' });
  requests[7].finish(true);
  await flush();
  assert.deepEqual(requests[8].body, { scope: 'appearance' });
  requests[8].finish(true);
  assert.equal(await resetPromise, true);
  paint('sage', true, 'auto');

  for (const theme of ['paper', 'unknown']) document.dispatchEvent(new ThemeEvent('atelier-theme', { detail: { theme } }));
  paint('paper', true, 'auto');
  storage.set('ui_theme', 'pixel');
  window.dispatchEvent(Object.assign(new Event('pageshow'), { persisted: true }));
  paint('pixel', true, 'auto');
  storage.set('ui_theme', 'night');
  window.dispatchEvent(Object.assign(new Event('storage'), { key: 'ui_theme', newValue: 'night' }));
  paint('night', true, 'auto'); assert.equal(requests.length, 9);
}
run().catch(error => { console.error(error); process.exitCode = 1; });
