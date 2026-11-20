import re
import shutil
import subprocess
from pathlib import Path

import pytest
from flask import render_template


ROOT = Path(__file__).resolve().parents[1]
THEMES = ('sage', 'mint', 'paper', 'linen', 'apricot', 'night', 'pixel', 'cyber', 'anime')
DARK_THEMES = {'night', 'cyber'}


@pytest.mark.parametrize('theme', THEMES)
def test_saved_palette_is_in_first_html_response(client, theme):
    client.set_cookie('jmconsole_edition', 'atelier')
    assert client.post('/api/settings', json={'ui_theme': theme}).get_json()['saved']
    for url in ('/', '/settings', '/library', '/online/42', '/preview/42', '/unknown-page'):
        response = client.get(url)
        assert response.status_code == (404 if url == '/unknown-page' else 200)
        html = response.get_data(as_text=True)
        root_tag = re.search(r'<html\b[^>]*>', html).group()
        assert f'data-theme="{theme}"' in root_tag
        assert f'data-bs-theme="{"dark" if theme in DARK_THEMES else "light"}"' in root_tag
        assert 'data-fx="' in root_tag
        assert html.index('css/atelier.css?v=') < html.index('</head>')
        assert html.index('css/atelier-fx.css?v=') < html.index('</head>')
        assert re.findall(r'data-set-theme="(\w+)" aria-pressed="true"', html) == [theme]
        assert 'href="/ui/classic"' in html
        assert ('id="quick-nav"' not in html) == url.startswith(('/online/', '/preview/'))


def test_mix_and_match_fx_and_appearance_reset(client):
    client.set_cookie('jmconsole_edition', 'atelier')
    # Mix anime theme with cyber FX preset
    res = client.post('/api/settings', json={'ui_theme': 'anime', 'ui_fx_enabled': 'true', 'ui_fx_preset': 'cyber'})
    assert res.status_code == 200 and res.get_json()['saved']
    root_tag = re.search(r'<html\b[^>]*>', client.get('/').get_data(as_text=True)).group()
    assert 'data-theme="anime"' in root_tag and 'data-fx="cyber"' in root_tag and 'data-fx-preset="cyber"' in root_tag

    # One-click FX off forces data-fx="none"
    res = client.post('/api/settings', json={'ui_fx_enabled': 'false'})
    assert res.status_code == 200 and res.get_json()['saved']
    root_tag = re.search(r'<html\b[^>]*>', client.get('/').get_data(as_text=True)).group()
    assert 'data-fx="none"' in root_tag and 'data-fx-enabled="false"' in root_tag

    # Reset appearance restores defaults without touching unrelated settings
    assert client.post('/api/settings', json={'timeout': '55'}).status_code == 200
    reset_resp = client.post('/api/settings/reset', json={'scope': 'appearance'})
    assert reset_resp.status_code == 200
    body = reset_resp.get_json()
    assert body['status'] == 'ok' and body['scope'] == 'appearance'
    assert body['settings']['ui_theme'] == 'sage'
    assert body['settings']['ui_fx_enabled'] == 'true'
    assert body['settings']['ui_fx_preset'] == 'auto'
    assert body['settings']['timeout'] == '55'

    # Reset all restores timeout too (preserving proxy by default)
    full_reset = client.post('/api/settings/reset', json={'scope': 'all'})
    assert full_reset.status_code == 200
    assert full_reset.get_json()['settings']['timeout'] == '30'


@pytest.mark.parametrize('status', (403, 404, 500))
def test_error_pages_distinguish_demo_from_real_instance(client, status):
    with client.application.test_request_context('/'):
        for demo in (True, False):
            html = render_template(f'{status}.html', atelier_demo=demo, ui_theme='night')
            sidebar = html.split('<aside', 1)[1].split('</aside>', 1)[0]
            assert ('离线样书 · 演示实例' in sidebar) == demo
            assert f'{status} /' in html and 'href="/"' in html
            assert 'data-theme="night"' in html


def _luminance(color):
    channels = [int(color[offset:offset + 2], 16) / 255 for offset in (0, 2, 4)]
    linear = [value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4 for value in channels]
    return sum(weight * value for weight, value in zip((.2126, .7152, .0722), linear))


@pytest.mark.parametrize('theme', THEMES)
def test_palette_ink_contrast_on_all_paper_surfaces(theme):
    sheet = (ROOT / 'static/css/atelier.css').read_text(encoding='utf-8')
    palette = re.search(r'\[data-theme="' + theme + r'"\]\s*\{([^}]+)\}', sheet).group(1)
    values = {name: _luminance(color) for name, color in re.findall(r'--([\w-]+):\s*#([\da-fA-F]{6})\b', palette)}
    for surface in ('bg-canvas', 'bg-card', 'bg-subtle', 'bg-sidebar', 'bg-elevated', 'bg-tint', 'ok-bg', 'warn-bg', 'danger-bg'):
        for ink in ('ink', 'ink-2', 'ink-3', 'accent', 'ok', 'warn', 'danger'):
            dark, light = sorted((values[ink], values[surface]))
            assert (light + .05) / (dark + .05) >= (10 if ink == 'ink' else 4.5), (ink, surface)


def test_shell_theme_interactions():
    node = shutil.which('node')
    if not node:
        pytest.skip('requires Node.js')
    subprocess.run([node, str(ROOT / 'tests/js/atelier_shell_state.cjs')], check=True, timeout=10)


def test_reader_and_preview_expose_collapsible_quick_fab(client, monkeypatch):
    from core import local_availability
    monkeypatch.setattr(local_availability, 'is_readable', lambda album_id: True)
    for url in ('/read/42', '/online/42', '/preview/42'):
        html = client.get(url).get_data(as_text=True)
        assert 'id="reader-fab"' in html
        assert 'id="reader-fab-panel"' in html
        assert 'data-fab-action="top"' in html and 'data-fab-action="bottom"' in html
        assert 'href="/"' in html and 'href="/settings"' in html
        # Keep existing quick-nav separation contract intact
        assert 'id="quick-nav"' not in html and 'quick-nav-item' not in html


def test_low_overhead_assets_and_theme_registry_alignment():
    from core.ui_themes import THEME_ORDER, UI_THEMES
    assert THEME_ORDER == THEMES and UI_THEMES == set(THEMES)
    atelier_css = re.sub(r'/\*.*?\*/', '', (ROOT / 'static/css/atelier.css').read_text(encoding='utf-8'), flags=re.S)
    fx_css = re.sub(r'/\*.*?\*/', '', (ROOT / 'static/css/atelier-fx.css').read_text(encoding='utf-8'), flags=re.S)
    reader_css = re.sub(r'/\*.*?\*/', '', (ROOT / 'static/css/reader.css').read_text(encoding='utf-8'), flags=re.S)
    assert 'feTurbulence' not in atelier_css
    assert 'backdrop-filter' not in reader_css
    # High-FPS GPU invariant: atelier-fx.css must never transition box-shadow or filter
    assert not re.search(r'transition:[^;]*(?:box-shadow|filter)', fx_css)
    # Click burst particles must never carry blurred box-shadows, clip-path masks, or idle will-change
    particle_block = fx_css.split('.atelier-fx-layer', 1)[1]
    assert 'box-shadow' not in particle_block
    assert 'clip-path' not in particle_block
    assert 'will-change' not in particle_block
    fx_js = (ROOT / 'static/js/atelier-fx.js').read_text(encoding='utf-8')
    assert 'POOL_SIZE' in fx_js and 'requestAnimationFrame' in fx_js and 'is-fx-switching' not in fx_js
    transfer_js = (ROOT / 'static/atelier-ui/transfer.js').read_bytes()
    assert len(transfer_js) < 4096, f'transfer.js should stay lightweight, got {len(transfer_js)} bytes'


def test_image_format_and_pack_format_settings_controls(client):
    from core.jm_service import _page_extension

    html = client.get('/settings').get_data(as_text=True)
    assert 'id="image_format"' in html and 'value="webp"' in html and 'value="jpg"' in html and 'value="png"' in html
    assert 'id="pack_format"' in html and 'value="cbz"' in html and 'value="zip"' in html

    for fmt, ext in (('png', '.png'), ('jpg', '.jpg'), ('webp', '.webp')):
        res = client.post('/api/settings', json={'image_format': fmt})
        assert res.status_code == 200 and res.get_json()['settings']['image_format'] == fmt
        assert _page_extension({'image_format': fmt}) == ext
        assert _page_extension({'image_format': fmt}, gif=True) == '.webp'

    bad = client.post('/api/settings', json={'image_format': 'bmp'})
    assert bad.status_code == 400


def test_cbz_export_uses_zip_stored_images_only(client, tmp_path, monkeypatch):
    import io
    import zipfile
    from PIL import Image
    import core.database as db
    from core import path_guard
    from routes import api_export

    root = tmp_path / 'downloads'
    album_dir = root / 'CBZ_Test_909001'
    chapter_dir = album_dir / '001_Ch1'
    chapter_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(path_guard, 'DOWNLOAD_ROOT', root)
    monkeypatch.setattr(api_export, 'DOWNLOAD_ROOT', root)

    def write_img(path, fmt, color):
        buf = io.BytesIO()
        Image.new('RGB', (16, 20), color).save(buf, format=fmt)
        path.write_bytes(buf.getvalue())

    write_img(chapter_dir / '00001.webp', 'WEBP', (20, 120, 90))
    write_img(chapter_dir / '00002.png', 'PNG', (40, 80, 160))
    write_img(chapter_dir / '00003.jpg', 'JPEG', (180, 60, 100))
    (chapter_dir / '.jm-chapter.json').write_text('{"format": 2}', encoding='utf-8')

    job_id = 'cbztest1'
    db.insert_job(job_id, '909001', 'CBZ_Test', [])
    db.transition_job_status(job_id, ['queued'], 'completed', output_path=str(album_dir), total_pages=3, done_pages=3)

    for endpoint in ('/api/library/909001/export/cbz', f'/api/export/{job_id}/cbz'):
        resp = client.post(endpoint)
        assert resp.status_code == 200
        assert 'application/vnd.comicbook+zip' in resp.headers.get('Content-Type', '')
        assert '.cbz' in resp.headers.get('Content-Disposition', '')
        raw = resp.get_data()
        resp.close()
        with zipfile.ZipFile(io.BytesIO(raw), 'r') as zf:
            assert zf.testzip() is None
            names = zf.namelist()
            assert names == ['001_Ch1/00001.webp', '001_Ch1/00002.png', '001_Ch1/00003.jpg']
            assert all(info.compress_type == zipfile.ZIP_STORED for info in zf.infolist())


def test_versioned_static_assets_emit_immutable_cache_control(client):
    resp = client.get('/static/css/atelier.css?v=12345')
    assert resp.status_code == 200
    cc = resp.headers.get('Cache-Control', '')
    assert 'immutable' in cc and 'max-age=31536000' in cc
    vendor = client.get('/static/vendor/bootstrap/css/bootstrap.min.css')
    assert vendor.status_code == 200 and 'immutable' in vendor.headers.get('Cache-Control', '')
