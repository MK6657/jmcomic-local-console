const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const { runInNewContext } = require('node:vm');

async function run() {
  const source = readFileSync(join(__dirname, '../static/js/atelier-shell.js'), 'utf8');
  const document = new EventTarget();
  const window = new EventTarget();
  const scheme = {content: 'dark'};
  const confirmed = [];
  const warnings = [];
  const requests = [];
  const storage = new Map([['ui_theme', 'paper']]);
  const themes = ['paper', 'sage', 'linen', 'night'];
  const stones = Object.fromEntries(themes.map(theme => {
    const stone = new EventTarget();
    stone.dataset = {setTheme: theme};
    stone.setAttribute = (key, value) => { stone[key] = value; };
    return [theme, stone];
  }));
  document.documentElement = {dataset: {theme: 'night', bsTheme: 'dark'}};
  document.querySelector = () => scheme;
  document.querySelectorAll = () => Object.values(stones);
  document.getElementById = () => null;
  document.addEventListener('atelier-theme', event => confirmed.push(event.detail.theme));
  window.showToast = (...args) => warnings.push(args);
  class ThemeEvent extends Event {
    constructor(type, options) { super(type); this.detail = options.detail; }
  }
  const fetch = (url, options) => new Promise(resolve => {
    assert.equal(url, '/api/settings');
    assert.equal(options.method, 'POST');
    const body = JSON.parse(options.body);
    assert.deepEqual(Object.keys(body), ['ui_theme']);
    requests.push({theme: body.ui_theme, finish: ok => resolve({ok, json: async () => ({status: 'ok', settings: body})})});
  });
  runInNewContext(source, {window, document, AbortSignal, CustomEvent: ThemeEvent, fetch,
    localStorage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value)}});
  const click = theme => stones[theme].dispatchEvent(new Event('click'));
  const flush = () => new Promise(resolve => setImmediate(resolve));
  const paint = theme => {
    assert.deepEqual(document.documentElement.dataset, {theme, bsTheme: theme === 'night' ? 'dark' : 'light'});
    assert.equal(scheme.content, document.documentElement.dataset.bsTheme);
    assert.deepEqual(themes.filter(key => stones[key]['aria-pressed'] === 'true'), [theme]);
  };

  assert.equal(storage.get('ui_theme'), 'night');
  assert.equal(requests.length, 0);
  click('sage'); click('paper'); click('linen');
  paint('linen');
  assert.deepEqual(requests.map(req => req.theme), ['sage']);
  requests[0].finish(true); await flush();
  assert.deepEqual(requests.map(req => req.theme), ['sage', 'linen']);
  requests[1].finish(true); await flush();
  assert.deepEqual(confirmed, ['linen']);
  assert.equal(storage.get('ui_theme'), 'linen');
  click('night'); paint('night');
  requests[2].finish(false); await flush();
  paint('linen'); assert.equal(warnings.length, 1);

  for (const theme of ['paper', 'unknown']) document.dispatchEvent(new ThemeEvent('atelier-theme', {detail: {theme}}));
  paint('paper');
  storage.set('ui_theme', 'sage');
  window.dispatchEvent(Object.assign(new Event('pageshow'), {persisted: true}));
  paint('sage');
  storage.set('ui_theme', 'night');
  window.dispatchEvent(Object.assign(new Event('storage'), {key: 'ui_theme', newValue: 'night'}));
  paint('night'); assert.equal(requests.length, 3);
}
run().catch(error => { console.error(error); process.exitCode = 1; });
