import assert from 'node:assert/strict';

const settings = new URL(import.meta.url).searchParams;
const origin = 'http://127.0.0.1:' + (settings.get('port') || '5010');
const metrics = await fetch(origin + '/test/metrics');
assert.equal(typeof (await metrics.json()).search, 'number', 'Start tests/ui_reader_fixture.py first');
const task = settings.has('space') ? await taskSpace(Number(settings.get('space'))) : await taskSpace('Atelier shelf verification');
const page = task.page('p1');
const viewport = width => page.cdp('Emulation.setDeviceMetricsOverride', {width, height: 844, mobile: false, deviceScaleFactor: 1});

await viewport(800);
await page.goto(origin + '/search?keyword=coast&sort=pictures&page_size=20&page=3&view=catalog');
await page.waitForSelector('.album-card');
const before = await page.evaluate(() => {
  const row = document.querySelectorAll('.album-card')[10];
  row.scrollIntoView({block: 'center', behavior: 'instant'});
  return {id: row.dataset.albumId, top: row.getBoundingClientRect().top};
});
await page.click('.album-card .card-title a >> nth=10');
await page.waitForURL(origin + '/album/' + before.id);
await page.evaluate(() => history.back());
await page.waitForSelector('.album-card');
await page.waitForFunction(expected => {
  const row = document.querySelectorAll('.album-card')[10];
  return row && Math.abs(row.getBoundingClientRect().top - expected) < 3;
}, before.top, {timeout: 5000});
const restored = await page.evaluate(() => [document.querySelector('#sort-select').value,
  document.querySelector('.page-link[aria-current]').textContent, document.querySelector('.atelier-collection').dataset.layout]);
assert.deepEqual(restored, ['pictures', '3', 'catalog']);

await page.goto(origin + '/library?status=readable');
await page.waitForSelector('.library-card .shelf-cover img');
await page.evaluate(() => {
  const image = document.querySelector('.library-card .shelf-cover img');
  image.loading = 'eager';
  image.src = 'data:image/png;base64,AA==';
});
await page.waitForFunction(() => {
  const cover = document.querySelector('.library-card .shelf-cover');
  return cover.querySelector('img').hidden && cover.querySelector('.shelf-ticket').offsetHeight > 0;
});

await viewport(390);
await page.goto(origin + '/wishlist');
await page.waitForSelector('.item-checkbox');
await page.click('.item-checkbox >> nth=0');
for (const mode of ['shelf', 'catalog', 'shelf']) {
  await page.click('button[data-mode=' + mode + ']');
  const state = await page.evaluate(() => {
    const list = document.querySelector('.table-responsive');
    return [document.querySelectorAll('.item-checkbox:checked').length, !!document.querySelector('#batch-actions').offsetHeight,
      list.scrollWidth <= list.clientWidth, document.documentElement.scrollWidth <= innerWidth,
      document.querySelector('[data-mode][aria-pressed=true]').dataset.mode];
  });
  assert.deepEqual(state, [1, true, true, true, mode]);
}
await page.reload();
assert.equal(await page.evaluate(() => document.querySelector('.atelier-collection').dataset.layout), 'shelf');

await viewport(1440);
await page.goto(origin + '/');
await page.fill('#hero-keyword', 'JM900011');
await page.click('#hero-download-btn');
await page.waitForURL(origin + '/album/900011');
console.log({space: task.spaceId, searchReturn: before.id, wishlist: 'selection retained, both layouts fit 390px', home: 'JM number opens detail'});
