// Run through playwright-cli run-code --filename, against ui_reader_fixture.py only.
async (page) => {
  const base = 'http://127.0.0.1:5010';
  const check = (ok, message) => { if (!ok) throw new Error(message); };
  const metrics = async () => (await (await page.request.get(base + '/test/metrics')).json()).search;
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.unroute('**/api/preview-img/**');
  await page.setViewportSize({ width: 1329, height: 912 });
  await page.goto(base + '/search');
  await page.evaluate(() => sessionStorage.removeItem('jm-reader-page:900001'));
  await page.getByLabel('关键词或车号').fill('sample');
  await page.getByRole('button', { name: '搜索', exact: false }).click();
  await page.locator('.reader-link').first().waitFor();
  check(await page.locator('.reader-link').count() === 12, 'Missing reading buttons');
  check(await page.locator('a a, a button').count() === 0, 'Interactive elements nested inside links');
  await page.getByRole('button', { name: '第 2 页', exact: true }).click();
  await page.waitForFunction(() => location.search.includes('page=2') && document.querySelectorAll('.reader-link').length === 12);
  const queryUrl = page.url();
  const requestsBeforeBack = await metrics();
  const item = page.locator('.card-title').getByRole('link', { name: 'Sample comic 9', exact: true });
  await item.scrollIntoViewIfNeeded();
  const oldScroll = await page.evaluate(() => scrollY);
  await item.click();
  await page.waitForURL('**/album/900009');
  await page.getByText('Sample detail', { exact: true }).waitFor();
  await page.goBack();
  await page.locator('.reader-link').first().waitFor();
  await page.waitForFunction(y => Math.abs(scrollY - y) < 40, oldScroll);
  check(page.url() === queryUrl, 'Back lost query/page parameters');
  check(await metrics() === requestsBeforeBack, 'Back issued another upstream search');
  await page.reload();
  await page.locator('.reader-link').first().waitFor();
  check(await metrics() === requestsBeforeBack, 'Reload did not reuse snapshot');
  await page.evaluate(() => scrollTo(0, 0));
  await page.screenshot({ path: 'output/playwright/search-reading-desktop.png', animations: 'disabled' });

  let failOnce = true;
  await page.route('**/api/preview-img/**', async route => {
    if (failOnce && route.request().url().split('?')[0].endsWith('/001.png')) {
      failOnce = false;
      await route.abort();
    } else await route.continue();
  });
  await page.locator('a.reader-link[href="/read/900001"]').click();
  await page.waitForURL('**/read/900001');
  await page.locator('#reader-page-1 .reader-page-error:not(.d-none)').waitFor();
  await page.locator('#reader-page-1').getByRole('button', { name: '重新加载图片' }).click();
  await page.waitForFunction(() => {
    const img = document.querySelector('#reader-page-1 img');
    return img && img.complete && img.naturalWidth > 0 && !img.classList.contains('d-none');
  });
  check(await page.locator('.reader-page').count() < 45, 'Reader rendered every page immediately');
  check(await page.locator('.reader-page figcaption').count() === 1, 'Only the first page should have a caption');
  check(await page.locator('#reader-page-1 figcaption').count() === 1, 'First page caption is missing');
  check(!(await page.locator('#reader-tools').isVisible()), 'Reading controls should start hidden');
  check(await page.locator('.reader-toolbar').evaluate(el => getComputedStyle(el).position) === 'static', 'Title banner must not be sticky');
  await page.screenshot({ path: 'output/playwright/continuous-reader-desktop.png', animations: 'disabled' });
  await page.keyboard.press('m');
  await page.locator('#reader-tools').waitFor({ state: 'visible' });
  await page.keyboard.press('Escape');
  check(!(await page.locator('#reader-tools').isVisible()), 'Escape must hide controls');
  await page.locator('#reader-page-1').click({ position: { x: 8, y: 8 } });
  await page.locator('#reader-tools').waitFor({ state: 'visible' });
  await page.getByLabel('跳转页码', { exact: true }).fill('39');
  await page.getByRole('button', { name: '跳转', exact: true }).click();
  await page.locator('#reader-page-39').waitFor();
  check(await page.locator('.reader-page').count() >= 39, 'Jump did not load later pages');
  check(!(await page.locator('#reader-tools').isVisible()), 'Jump should hide controls again');
  await page.waitForFunction(() => document.querySelector('.reader-toolbar').getBoundingClientRect().bottom < 0);
  await page.waitForFunction(() => {
    const figure = document.getElementById('reader-page-39');
    const image = figure.querySelector('img');
    return image.complete && image.naturalWidth > 0 && Math.abs(figure.getBoundingClientRect().top - 16) < 4;
  });
  await page.screenshot({ path: 'output/playwright/reader-unobstructed-desktop.png', animations: 'disabled' });
  await page.keyboard.press('m');
  await page.locator('#reader-tools').waitFor({ state: 'visible' });
  await page.getByRole('button', { name: '一键到底', exact: true }).click();
  await page.waitForFunction(() => {
    const last = document.getElementById('reader-page-45');
    return last && last.querySelector('img').complete && last.querySelector('img').naturalWidth > 0
      && Math.abs(last.getBoundingClientRect().bottom - innerHeight) < 4;
  });
  check(await page.locator('.reader-page').count() === 45, 'Bottom button did not render the final batch');
  check(await page.locator('.reader-page figcaption').count() === 1, 'Later batches must not repeat captions');
  check(!(await page.locator('#reader-tools').isVisible()), 'Bottom button should hide the tools');
  await page.screenshot({ path: 'output/playwright/reader-bottom.png', animations: 'disabled' });
  await page.keyboard.press('m');
  await page.getByRole('button', { name: '一键到顶', exact: true }).click();
  await page.waitForFunction(() => scrollY === 0);
  check(!(await page.locator('#reader-tools').isVisible()), 'Top button should hide the tools');
  await page.keyboard.press('m');
  await page.locator('#reader-back').click();
  await page.locator('.reader-link').first().waitFor();
  check(page.url() === queryUrl, 'Reader back did not return to original results');
  check(await metrics() === requestsBeforeBack, 'Reader back refetched search results');

  // 未下载的漫画：阅读按钮是在线样式，点击后 /read 转到在线阅读（fixture 的在线接口是离线桩，只回错误）
  await page.locator('a.reader-link[href="/read/900002"][data-read-state="online"]').waitFor();
  await page.locator('a.reader-link[href="/read/900002"]').click();
  await page.waitForURL('**/online/900002');
  await page.locator('#reader-empty:not(.d-none)').waitFor();
  check(await page.locator('[data-source="online"]').count() > 0, 'Undownloaded album did not open online reading');
  await page.goBack();
  await page.locator('.reader-link').first().waitFor();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.evaluate(() => scrollTo(0, 0));
  check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'Search has mobile horizontal overflow');
  await page.screenshot({ path: 'output/playwright/search-reading-mobile.png', animations: 'disabled' });
  await page.locator('a.reader-link[href="/read/900001"]').click();
  await page.locator('#reader-page-1').waitFor();
  await page.keyboard.press('m');
  await page.locator('#reader-tools').waitFor({ state: 'visible' });
  check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'Tools have mobile horizontal overflow');
  await page.screenshot({ path: 'output/playwright/reader-tools-mobile.png', animations: 'disabled' });
  await page.getByLabel('跳转页码', { exact: true }).fill('1');
  await page.getByRole('button', { name: '跳转', exact: true }).click();
  check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'Reader has mobile horizontal overflow');
  await page.screenshot({ path: 'output/playwright/continuous-reader-mobile.png', animations: 'disabled' });
  check(errors.length === 0, 'Browser JS errors: ' + errors.join('; '));
  return { status: 'passed', checks: 'result restore, reader navigation, lazy batches, retry, mobile layout, hidden tools, first-page-only caption, top/bottom icons and last-page alignment' };
}
