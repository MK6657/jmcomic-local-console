import re
import shutil
import subprocess
from pathlib import Path

import pytest
from flask import render_template


ROOT = Path(__file__).resolve().parents[1]
THEMES = ('paper', 'sage', 'linen', 'night')


@pytest.mark.parametrize('theme', THEMES)
def test_saved_palette_is_in_first_html_response(client, theme):
    client.set_cookie('jmconsole_edition', 'atelier')
    assert client.post('/api/settings', json={'ui_theme': theme}).get_json()['saved']
    for url in ('/', '/settings', '/library', '/online/42', '/preview/42', '/unknown-page'):
        response = client.get(url)
        assert response.status_code == (404 if url == '/unknown-page' else 200)
        html = response.get_data(as_text=True)
        assert f'data-theme="{theme}"' in re.search(r'<html\b[^>]*>', html).group()
        assert f'data-bs-theme="{"dark" if theme == "night" else "light"}"' in html
        assert html.index('css/atelier.css?v=') < html.index('</head>')
        assert re.findall(r'data-set-theme="(\w+)" aria-pressed="true"', html) == [theme]
        assert 'href="/ui/classic"' in html
        assert ('id="quick-nav"' not in html) == url.startswith(('/online/', '/preview/'))


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
    subprocess.run([node, str(ROOT / 'tests/atelier_shell_state.cjs')], check=True, timeout=10)
