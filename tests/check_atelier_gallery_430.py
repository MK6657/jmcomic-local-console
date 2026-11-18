"""Opt-in source measurement: python -m pytest -qs tests/check_atelier_gallery_430.py.

Requires local Playwright Chromium. Pytest's socket/libcurl guards remain active. Waitress
serves only loopback; only SDK transport and search results are fixtures, never the reader APIs.
"""
import json
import threading
from pathlib import Path
from time import perf_counter
from urllib.parse import urlsplit

import pytest
from flask import jsonify
from waitress import create_server

from test_archive_reading import _job, comic, downloads, png
from test_atelier_gallery import chromium
from test_online_reader import FakeAlbum, FakePhoto, online


OBSERVE = '''{
  window.peakImages = 0;
  window.addEventListener('pageshow', event => { window.galleryPersisted = event.persisted; });
  if (/^\\/(read|online|preview)\\//.test(location.pathname)) {
    const sample = () => { window.peakImages = Math.max(window.peakImages, document.images.length); };
    for (const [proto, names] of [[Node.prototype, ['appendChild','insertBefore','removeChild','replaceChild']],
         [Element.prototype, ['append','prepend','remove','replaceWith','replaceChildren']]]) {
      for (const name of names) {
        const original = proto[name];
        proto[name] = function(...args) { const result = original.apply(this, args); sample(); return result; };
      }
    }
    new MutationObserver(sample).observe(document, {subtree:true, childList:true});
  }
}'''


@pytest.fixture(scope='module')
def loopback(app):
    hits = []
    gates = {}

    def wsgi(environ, start):
        hit = {'path': environ['PATH_INFO'], 'method': environ['REQUEST_METHOD'],
               'initiator': urlsplit(environ.get('HTTP_REFERER', '')).path}
        hits.append(hit)
        arrived = perf_counter()
        gate = gates.get(hit['path'].rsplit('/', 1)[0] + '/')
        if gate is not None:
            pending, full, release = gate
            pending.append(hit['path'])
            if len(pending) >= 6:
                full.set()
            if not release.wait(15):
                hit['gate_timeout'] = True

        def respond(status, headers, exc_info=None):
            hit.update(status=int(status.split()[0]), headers_ms=round((perf_counter() - arrived) * 1000, 2))
            headers.append(('Content-Security-Policy', "default-src 'self'; img-src 'self' data: blob:; "
                            "script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; font-src 'self' data:"))
            return start(status, headers, exc_info)
        return app(environ, respond)

    channels = {}
    server = create_server(wsgi, host='127.0.0.1', port=0, map=channels, threads=8, asyncore_loop_timeout=0.1)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.effective_port}', hits, gates
    finally:
        server.task_dispatcher.shutdown()
        server.asyncore.close_all(map=channels)
        thread.join(3)


def settle(page, mode, number):
    selector = '#preview-image' if mode == 'preview' else f'#reader-page-{number} img'
    page.wait_for_function('''([selector, width]) => document.querySelector(selector)?.naturalWidth === width &&
        Array.from(document.images).every(image => image.complete && image.naturalWidth > 0)''',
                           arg=[selector, 200 + number])
    page.evaluate('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')


def counts(page, hits, since, prefix, mode):
    def number(path):
        return int(path.rsplit('/', 1)[-1]) + (1 if mode == 'online' else 0)

    resources = page.evaluate('''prefix => performance.getEntriesByType('resource')
        .map(entry => new URL(entry.name).pathname).filter(path => path.startsWith(prefix))''', prefix)
    received = [hit for hit in hits[since:] if hit['path'].startswith(prefix)]
    assert all(hit['method'] == 'GET' and hit['status'] in (200, 304) for hit in received)
    return {'chromium_requests': len(resources), 'http_gets': len(received),
            'pages': sorted({number(hit['path']) for hit in received}),
            'img_peak': page.evaluate('peakImages'), 'img_settled': page.evaluate('document.images.length')}


@pytest.mark.parametrize('mode', ['read', 'preview', 'online'])
def test_430_measurement(chromium, loopback, client, downloads, online, monkeypatch, mode):
    base, hits, gates = loopback
    album_id = '940431' if mode == 'online' else '940430'
    images = [png((number % 256, number // 256, 53), (200 + number, 480)) for number in range(1, 431)]
    if mode == 'online':
        _, fake = online
        photo = FakePhoto('42', 430)
        fake.album = FakeAlbum([photo]); fake.get_photo_detail = lambda *args, **kwargs: photo
        fake.postman.answer = lambda url: images[int(Path(urlsplit(url).path).stem) - 1]
    else:
        folder = downloads / 'Gallery'
        comic(folder, {f'chapter__42/{number:03}.png': image for number, image in enumerate(images, 1)})
        _job('gallery', album_id, folder)

    def search():
        return jsonify(status='ok', page=3, page_size=20, total=100, items=[
            dict(album_id=album_id if rank == 47 else str(950000 + rank), title=f'Gallery {rank}',
                 author='Atelier', cover_url='/static/images/no-cover.svg', tags=[]) for rank in range(41, 61)])

    monkeypatch.setitem(client.application.view_functions, 'api_search.search', search)
    context = chromium.new_context(viewport={'width': 1280, 'height': 800}, service_workers='block')
    context.add_init_script(OBSERVE)
    page = context.new_page()
    page.set_default_timeout(8000)
    errors, external = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda req: external.append(req.url) if req.url.startswith(('http:', 'https:'))
            and not req.url.startswith(base + '/') else None)
    aborted = []
    page.on('requestfailed', lambda req: aborted.append(urlsplit(req.url).path) if req.failure == 'net::ERR_ABORTED' else None)
    gate_key = '/api/online-img/42/' if mode == 'online' else f'/api/preview-archive/{album_id}/'
    pending, full, release = [], threading.Event(), threading.Event()
    try:
        page.goto(base + '/search?keyword=gallery&page=3&page_size=20&sort=latest')
        link = page.locator(f'a.reader-link[href="/read/{album_id}"]')
        link.wait_for()
        page.wait_for_load_state('networkidle')
        link.evaluate("el => el.scrollIntoView({block:'center', behavior:'instant'})")
        origin_url = page.url
        origin_y = page.evaluate('scrollY')
        origin_link_y = link.bounding_box()['y']
        assert origin_y > 150
        key = 'jm-reader-page:' + ('online:' if mode == 'online' else '') + album_id
        page.evaluate("key => sessionStorage.setItem(key,'16')", key)
        checkpoint = len(hits)
        link.click()
        prefix = '/api/online-img/' if mode == 'online' else '/api/preview-archive/'
        if mode == 'preview':
            settle(page, 'read', 16)
            page.keyboard.press('m')
            checkpoint = len(hits)
            page.locator('#reader-paged').click()
        settle(page, mode, 16)
        start = counts(page, hits, checkpoint, prefix, mode)
        assert set(start['pages']).issubset(range(12, 21)) and 16 in start['pages']
        if mode == 'preview':
            entry = page.locator('#page-jump-input')
        else:
            page.keyboard.press('m')
            entry = page.locator('#reader-page-input')
        entry.fill('429')
        checkpoint = len(hits)
        page.evaluate('performance.clearResourceTimings(); window.peakImages = document.images.length')
        entry.press('Enter')
        settle(page, mode, 429)
        far = counts(page, hits, checkpoint, prefix, mode)
        assert 429 in far['pages'] and min(far['pages']) >= 422 and far['img_peak'] <= 10
        if mode == 'preview':
            page.locator('#btn-last').click()
        else:
            page.keyboard.press('End')
        settle(page, mode, 430)
        if mode == 'preview':
            assert page.locator('#btn-next').is_disabled() and page.locator('#page-current').inner_text() == '430'
        else:
            page.wait_for_function('Math.abs(document.querySelector("#reader-page-430").getBoundingClientRect().bottom - innerHeight) < 3')
            assert page.locator('#reader-next').is_disabled()
        if mode != 'preview':
            page.keyboard.press('m')
        gates[gate_key] = (pending, full, release)
        before_slow = len(hits)
        before_abort = len(aborted)
        entry.fill('256')
        entry.press('Enter')
        assert full.wait(4), f'only {len(pending)} real slow requests reached Waitress'
        if mode != 'preview':
            page.keyboard.press('m')
        back = page.locator('#preview-back' if mode == 'preview' else '#reader-back')
        held_at_click = len(pending)
        checkpoint = len(hits)
        since = page.evaluate('performance.timeOrigin + performance.now()')
        back.click()
        page.wait_for_url(origin_url)
        page.wait_for_function('y => Math.abs(scrollY - y) <= 2', arg=origin_y)
        return_y = page.evaluate('scrollY')
        assert abs(link.bounding_box()['y'] - origin_link_y) <= 2
        probe = page.evaluate('''async () => {
          const path = '/api/system/health?gallery_probe=' + crypto.randomUUID();
          const start = performance.now();
          const response = await fetch(path, {cache:'no-store'});
          const data = await response.json();
          return {status:response.status, ok:data.status, ms:+(performance.now()-start).toFixed(2)};
        }''')
        assert probe['status'] == 200
        assert not any(hit.get('gate_timeout') for hit in hits[before_slow:])
        canceled = [path for path in aborted[before_abort:] if path.startswith(gate_key)]
        assert set(pending).issubset(canceled), (pending, canceled)
        gates.pop(gate_key); release.set()
        after = hits[checkpoint:]
        assert not any(hit['path'] == '/api/search' for hit in after)
        restored = [hit for hit in after if hit['path'] == '/search' or hit['initiator'] == '/search']
        first_api = next(hit for hit in restored if hit['path'].startswith('/api/'))
        page.wait_for_load_state('networkidle')
        times = page.evaluate('''since => performance.getEntriesByType('resource')
          .filter(entry => performance.timeOrigin + entry.startTime >= since && new URL(entry.name).pathname.startsWith('/api/'))
          .map(entry => ({path:new URL(entry.name).pathname, ms:+entry.duration.toFixed(2), bytes:entry.transferSize}))''', since)
        first_timing = next(record for record in times if record['path'] == first_api['path'])
        assert not external and not errors
        print('GALLERY430 ' + json.dumps({'mode': mode, 'source': 'source / Waitress / loopback HTTP',
              'chromium': chromium.version, 'start16': start, 'jump429': far, 'end430_width': 630,
              'return': {'page': 3, 'rank': 47, 'from_y': origin_y, 'to_y': return_y,
                         'extra_searches': 0, 'bfcache': page.evaluate('window.galleryPersisted'),
                         'first_server_request': restored[0], 'first_api': first_api, 'first_api_timing': first_timing,
                         'health_probe': probe, 'held_before_return': held_at_click, 'aborted_image_requests': len(canceled)}}, sort_keys=True))
    finally:
        gates.pop(gate_key, None); release.set()
        context.close()
