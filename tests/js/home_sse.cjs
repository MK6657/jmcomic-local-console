const assert = require('node:assert/strict');
const path = require('node:path');
const events = new EventTarget();
const sources = [], timers = new Map();
const row = {dataset: {status: 'running'}, getAttribute: () => 'book', querySelector: () => ({textContent: 'Fixture'})};
const badge = {closest: () => row, classList: {replace() {}}};
const bar = {parentElement: {hidden: false}};
global.window = global;
global.addEventListener = events.addEventListener.bind(events);
global.document = Object.assign(new EventTarget(), {
  visibilityState: 'visible',
  querySelectorAll: selector => selector === '[data-status="running"]' && row.dataset.status === 'running' ? [row] : [],
  getElementById: id => ({'pct-book': badge, 'progress-book': bar})[id] || null,
});
global.EventSource = class extends EventTarget {
  constructor(url) { super(); this.url = url; sources.push(this); }
  close() { this.closed = true; }
};
global.setTimeout = callback => { const id = Symbol(); timers.set(id, callback); return id; };
global.clearTimeout = id => timers.delete(id);
global.showToast = () => {};
require(path.join(__dirname, '../../static/js/sse-client.js'));
require(path.join(__dirname, '../../static/js/home.js'));

document.dispatchEvent(new Event('DOMContentLoaded'));
assert.equal(sources[0].url, '/api/jobs/book/events');
sources[0].onerror();
assert.equal(timers.size, 1);
function visibility(state) {
  document.visibilityState = state;
  document.dispatchEvent(new Event('visibilitychange'));
}
visibility('hidden');
assert.equal(timers.size, 0);
visibility('visible');
const current = sources[1];
function cancel(source) {
  source.dispatchEvent(new MessageEvent('canceled', {data: JSON.stringify({job_id: 'book', status: 'canceled'})}));
}
cancel(sources[0]);
assert.equal(row.dataset.status, 'running');
assert.ok(!current.closed);
current.onopen();
cancel(current);
assert.ok(current.closed);
assert.equal(row.dataset.status, 'canceled');
assert.equal(badge.textContent, '已取消');
assert.equal(bar.parentElement.hidden, true);
current.onerror();
events.dispatchEvent(new Event('pagehide'));
events.dispatchEvent(Object.assign(new Event('pageshow'), {persisted: true}));
visibility('hidden');
visibility('visible');
assert.equal(timers.size, 0);
assert.equal(sources.length, 2);
