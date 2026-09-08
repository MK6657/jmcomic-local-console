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
  await page.screenshot({ path: 'output/playwright/continuous-reader-desktop.png', animations: 'disabled' });
  await page.getByLabel('跳转页码', { exact: true }).fill('39');
  await page.getByRole('button', { name: '跳转', exact: true }).click();
  await page.locator('#reader-page-39').waitFor();
  check(await page.locator('.reader-page').count() >= 39, 'Jump did not load later pages');
  await page.locator('#reader-back').click();
  await page.locator('.reader-link').first().waitFor();
  check(page.url() === queryUrl, 'Reader back did not return to original results');
  check(await metrics() === requestsBeforeBack, 'Reader back refetched search results');

  await page.locator('a.reader-link[href="/read/900002"]').click();
  await page.locator('#reader-empty:not(.d-none)').waitFor();
  check((await page.locator('#reader-message').innerText()).includes('不会自动下载'), 'Missing local-only empty explanation');
  await page.goBack();
  await page.locator('.reader-link').first().waitFor();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.evaluate(() => scrollTo(0, 0));
  check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'Search has mobile horizontal overflow');
  await page.screenshot({ path: 'output/playwright/search-reading-mobile.png', animations: 'disabled' });
  await page.locator('a.reader-link[href="/read/900001"]').click();
  await page.locator('#reader-page-1').waitFor();
  await page.getByLabel('跳转页码', { exact: true }).fill('1');
  await page.getByRole('button', { name: '跳转', exact: true }).click();
  check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'Reader has mobile horizontal overflow');
  await page.screenshot({ path: 'output/playwright/continuous-reader-mobile.png', animations: 'disabled' });
  check(errors.length === 0, 'Browser JS errors: ' + errors.join('; '));
  return { status: 'passed', checks: 'result restore, page/scroll retention, reader navigation, lazy batches, image retry, empty state, mobile layout' };
}
