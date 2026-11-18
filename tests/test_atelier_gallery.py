"""Headless Chromium + the Flask test client: real templates, JS, bytes and routes, no HTTP server."""
from pathlib import Path
from urllib.parse import urlsplit
import zipfile

import pytest

from test_archive_reading import _job, comic, damage_page, downloads, png
from test_online_reader import FakeAlbum, FakePhoto, online


@pytest.fixture(scope="module")
def chromium():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as engine:
        if not Path(engine.chromium.executable_path).exists():
            pytest.skip("needs the optional local Chromium test browser")
        browser = engine.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture
def gallery(chromium, client, downloads):
    folder = downloads / 'Gallery'
    entries = {f'chapter__42/{number:03}.png': png((number % 256, 35, 21), (10 + number, 500))
               for number in range(1, 431)}
    archive = comic(folder, entries, compression=zipfile.ZIP_STORED)
    damage_page(archive, entries['chapter__42/128.png'])
    _job('gallery', '930430', folder)
    page = chromium.new_page(viewport={'width': 390, 'height': 844})
    seen, errors = [], []

    def dispatch(route):
        request = route.request
        url = urlsplit(request.url)
        assert url.netloc == 'gallery.test'
        seen.append(url.path)
        response = client.open(url.path + ('?' + url.query if url.query else ''), method=request.method,
                               data=request.post_data_buffer, content_type=request.headers.get('content-type'))
        try:
            response.direct_passthrough = False
            route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.get_data())
        finally:
            response.close()

    page.route('**/*', dispatch)
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.set_default_timeout(6000)
    yield page, seen
    page.close()
    assert errors == []


def wait_sheet(page, number):
    page.wait_for_function('''number => {
      const sheet = document.getElementById('reader-page-' + number);
      return sheet && sheet.querySelector('img').naturalWidth === 10 + number &&
        document.getElementById('reader-range').value === String(number);
    }''', arg=number)


def test_real_archive_far_jump_mode_return_and_themes(gallery):
    page, seen = gallery
    page.goto('http://gallery.test/downloads')
    page.goto('http://gallery.test/read/930430?page=16')
    wait_sheet(page, 16)
    page.keyboard.press('m')
    page.locator('#reader-page-input').fill('429')
    page.locator('#reader-page-input').press('Enter')
    wait_sheet(page, 429)
    page.wait_for_function('Math.abs(document.querySelector("#reader-page-429").getBoundingClientRect().top - 16) < 3')
    assert page.locator('.reader-page').count() <= 7
    numbers = [int(path.rsplit('/', 1)[-1]) for path in seen if '/preview-archive/' in path]
    assert len(numbers) <= 12 and all(14 <= number <= 20 or 427 <= number <= 430 for number in numbers)
    page.keyboard.press('End')
    wait_sheet(page, 430)
    page.keyboard.press('m')
    page.locator('#reader-paged').click()
    page.wait_for_function('document.querySelector("#preview-image")?.naturalWidth === 440')
    assert page.locator('.thumb-item').count() == 9
    assert page.locator('.thumb-item[aria-current]').get_attribute('data-page') == '430'
    frame = page.locator('#image-wrapper')
    frame.click(position={'x': 20, 'y': 30})
    page.wait_for_function('document.querySelector("#preview-image").naturalWidth === 439')
    frame.click(position={'x': frame.bounding_box()['width'] - 20, 'y': 30})
    page.wait_for_function('document.querySelector("#preview-image").naturalWidth === 440')
    page.keyboard.press('ArrowLeft')
    for theme in ('sage', 'night', 'linen', 'paper'):
        page.locator(f'[data-set-theme="{theme}"]').click()
        assert page.locator('html').get_attribute('data-theme') == theme
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert page.locator('#reader-tools').evaluate('el => getComputedStyle(el).backgroundColor') != 'rgba(0, 0, 0, 0)'
    page.locator('#preview-continuous').click()
    wait_sheet(page, 429)
    page.keyboard.press('m')
    page.locator('#reader-back').click()
    page.wait_for_url('**/downloads')
    assert '/api/online/930430' not in seen


def test_real_bad_page_links_to_exact_online_offset(gallery, online):
    page, seen = gallery
    _, fake = online
    photo = FakePhoto('42', 430)
    fake.album = FakeAlbum([photo])
    fake.get_photo_detail = lambda photo_id, fetch_album=True: photo
    page.goto('http://gallery.test/preview/930430?page=128')
    link = page.locator('#no-image-hint a')
    assert link.get_attribute('href') == '/online/930430?chapter=42&offset=127'
    assert not any('/api/online' in path for path in seen)
    link.click()
    page.wait_for_function('document.querySelector("#reader-page-128 img")?.naturalWidth > 0')
    assert '/api/online-img/42/127' in seen and page.url.endswith('?page=128')
    assert page.locator('#reader-paged').count() == 0
    page.keyboard.press('ArrowRight')
    assert page.evaluate("sessionStorage.getItem('jm-reader-page:online:930430')") == '129'
    assert page.evaluate("sessionStorage.getItem('jm-reader-page:930430')") == '128'


@pytest.mark.parametrize('mode', ['read', 'preview'])
def test_hidden_gallery_cancels_then_resumes(gallery, mode):
    page, _ = gallery
    page.add_init_script('''{
      const original = window.fetch;
      window.held = [];
      window.hold = true;
      window.fetch = (url, options) => window.hold && String(url).includes('/api/preview-archive/')
        ? new Promise((resolve, reject) => {
            held.push(options.signal);
            options.signal.addEventListener('abort', () => reject(new DOMException('Stopped', 'AbortError')));
          }) : original(url, options);
    }''')
    page.goto(f'http://gallery.test/{mode}/930430?page=429')
    page.wait_for_function('held.length >= 3')
    page.evaluate('''() => {
      Object.defineProperty(document, 'hidden', {value:true, configurable:true});
      document.dispatchEvent(new Event('visibilitychange'));
    }''')
    assert page.evaluate('held.every(signal => signal.aborted)')
    page.evaluate('''() => {
      window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted:true}));
      window.hold = false; delete document.hidden;
      window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted:true}));
    }''')
    selector = '#reader-page-429 img' if mode == 'read' else '#preview-image'
    page.wait_for_function('selector => document.querySelector(selector)?.naturalWidth === 439', arg=selector)
