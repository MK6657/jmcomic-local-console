// Playwright CLI check against the synthetic ui_reader_fixture.py server only.
async (page) => {
  const base = 'http://127.0.0.1:5010';
  const check = (ok, label) => { if (!ok) throw new Error(label); };
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.unroute('**/api/preview-img/**');
  await page.unroute('**/api/preview/900001');
  await page.route('**/api/preview/900001', route => route.fulfill({
    contentType: 'application/json', body: JSON.stringify({ status: 'ok', title: 'Synthetic 430-page book', total_pages: 430,
      pages: Array.from({ length: 430 }, (_, i) => ({ page: i + 1, chapter: 'Sample chapter',
        url: '/api/preview-img/Reader%20sample/001.png?fixturePage=' + (i + 1) })) })
  }));
  await page.setViewportSize({ width: 1329, height: 912 });
  await page.goto(base + '/search');
  await page.evaluate(() => sessionStorage.removeItem('jm-reader-page:900001'));
  await page.getByLabel('关键词或车号').fill('sample');
  await page.getByRole('button', { name: '搜索', exact: false }).click();
  await page.locator('.reader-link').first().waitFor();
  const searchUrl = page.url();
  await page.locator('a.reader-link[href="/read/900001"]').click();
  await page.locator('#reader-page-1').waitFor();
  await page.keyboard.press('m');
  await page.getByRole('link', { name: '单页翻页', exact: true }).click();
  await page.waitForURL('**/preview/900001?page=1');
  await page.waitForFunction(() => document.getElementById('page-total').textContent === '430');
  await page.waitForFunction(() => document.getElementById('preview-image').naturalWidth > 0);
  check(await page.locator('.thumb-item').count() === 100, 'Initial thumbnails must be batched');
  const layout = await page.locator('.thumb-item img').first().evaluate(img => ({
    fit: getComputedStyle(img).objectFit,
    width: img.getBoundingClientRect().width,
    frame: img.parentElement.getBoundingClientRect().width,
    bar: document.getElementById('thumbnail-bar').getBoundingClientRect().height,
    rail: document.getElementById('thumb-container').getBoundingClientRect().height
  }));
  check(layout.fit === 'contain' && layout.width <= layout.frame && layout.bar > layout.rail, 'Thumbnails are still clipped');
  await page.getByRole('button', { name: '向后浏览缩略图', exact: true }).click();
  check(await page.locator('#thumb-container').evaluate(el => el.scrollLeft) > 0, 'Thumbnail next arrow did not scroll');
  await page.locator('.thumb-item[data-page="25"]').click();
  await page.waitForFunction(() => document.getElementById('page-current').textContent === '25');
  await page.screenshot({ path: 'output/playwright/paged-full-thumbnails.png', animations: 'disabled' });
  await page.getByRole('link', { name: '连续滚动', exact: true }).click();
  await page.waitForURL('**/read/900001?page=25');
  await page.waitForFunction(() => {
    const figure = document.getElementById('reader-page-25');
    return figure && Math.abs(figure.getBoundingClientRect().top - 16) < 4;
  });
  await page.keyboard.press('m');
  await page.getByRole('link', { name: '单页翻页', exact: true }).click();
  await page.waitForURL('**/preview/900001?page=25');
  await page.waitForFunction(() => document.getElementById('page-current').textContent === '25');
  await page.locator('#btn-last').click();
  await page.waitForFunction(() => document.getElementById('page-current').textContent === '430');
  check(await page.locator('.thumb-item').count() === 430, 'Late-page thumbnails were not loaded');
  check(await page.locator('.thumb-item[aria-current="page"]').getAttribute('data-page') === '430', 'Last-page thumbnail not selected');
  check(await page.locator('#btn-next').isDisabled(), 'Next should be disabled on last page');
  await page.getByRole('button', { name: '上一页', exact: false }).click();
  await page.waitForFunction(() => document.getElementById('page-current').textContent === '429');
  await page.reload();
  await page.waitForFunction(() => document.getElementById('page-current').textContent === '429');
  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForFunction(() => {
    const selected = document.querySelector('.thumb-item.active').getBoundingClientRect();
    const rail = document.getElementById('thumb-container').getBoundingClientRect();
    return selected.left >= rail.left - 1 && selected.right <= rail.right + 1;
  });
  check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'Mobile preview overflows horizontally');
  await page.screenshot({ path: 'output/playwright/paged-mode-mobile.png', animations: 'disabled' });
  await page.goBack();
  await page.locator('.reader-link').first().waitFor();
  check(page.url() === searchUrl, 'Switching modes added unwanted back-history entries');
  check(errors.length === 0, 'Browser errors: ' + errors.join('; '));
  await page.unroute('**/api/preview/900001');
  return { status: 'passed', checks: '430-page thumbnail containment, horizontal browsing, page-preserving mode switch, last/previous/reload, mobile, back history' };
}
