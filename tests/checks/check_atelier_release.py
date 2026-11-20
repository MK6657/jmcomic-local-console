"""Verify the actual EXE's separate demo, shipped renderers and exports. Loopback and synthetic files only."""
import contextlib
import hashlib
from http.client import HTTPConnection
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from zipfile import ZipFile

import pikepdf


@contextlib.contextmanager
def server(executable, temp):
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(('JMCONSOLE_', 'PYTHON', '_PYI', 'PYINSTALLER'))}
    env.update(LOCALAPPDATA=str(temp), JMCONSOLE_HOME=str(temp / 'must-remain-absent'),
               PATH=str(Path(os.environ['SystemRoot']) / 'System32'), PYINSTALLER_RESET_ENVIRONMENT='1')
    log = temp / 'worker.txt'
    with log.open('wb') as out:
        process = subprocess.Popen([str(executable), 'demo', '--no-browser'], cwd=temp, env=env,
                                   stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            text = log.read_bytes().decode('utf-8', 'replace')
            if process.poll() is not None:
                raise RuntimeError('Demo exited before binding: ' + text[-1200:])
            match = re.search(r'http://127\.0\.0\.1:(\d+)/downloads', text)
            if match:
                break
            time.sleep(.12)
        else:
            raise RuntimeError('No loopback demo URL before deadline')
        yield int(match[1])
    finally:
        if process.poll() is None:
            process.terminate()  # The disposable, explicitly spawned demo; never a shared service.
        process.wait(timeout=15)


def exchange(port, path, method='GET', payload=None, *, expected=200, cookie=None):
    conn = HTTPConnection('127.0.0.1', port, timeout=30)
    try:
        headers = {'Cookie':cookie} if cookie else {}
        if payload is not None:
            headers['Content-Type'] = 'application/json'
        conn.request(method, path, json.dumps(payload) if payload is not None else None, headers)
        r = conn.getresponse()
        data = r.read()
        assert r.status == expected, (path, expected, r.status, data[:180])
        return dict(r.getheaders()), data
    finally:
        conn.close()


def verify(executable):
    with tempfile.TemporaryDirectory(prefix='Atelier 临时书阁 ') as d:
        temp = Path(d)
        with server(executable, temp) as port:
            def data(path): return json.loads(exchange(port, path)[1])
            pages = ['/', '/search?keyword=海岸', '/library', '/wishlist', '/downloads', '/settings',
                     '/album/901001', '/read/901003', '/online/901002', '/preview/901003',
                     '/preview/job/job_atelier_901001_ch2']
            from urllib.parse import quote
            for path in pages:
                headers, html = exchange(port, quote(path, safe='/?=&'))
                assert b'data-theme="paper"' in html and b'atelier.css' in html, path
                assert 'frame-ancestors' in headers['Content-Security-Policy'] and "connect-src 'self'" in headers['Content-Security-Policy']
            for asset in ('transfer.css','transfer.js','LICENSE-vue.txt'):
                assert exchange(port, '/static/atelier-ui/'+asset)[1]
            assert data('/api/search?q=901001')['items'][0]['album_id'] == '901001'
            assert data('/api/online/901002')['total_pages'] == 16
            h, webp = exchange(port, data('/api/online/901002')['pages'][0]['url'])
            assert h['Content-Type'].startswith('image/webp') and webp[8:12] == b'WEBP'
            assert data('/api/preview/901003')['source'] == 'archive'
            contents = data('/api/preview/901001')['pages']
            published = [hashlib.sha256(exchange(port, p['url'])[1]).hexdigest() for p in contents]
            assert len(published) == 12
            with ZipFile(io.BytesIO(exchange(port,'/api/export/job_atelier_901001_ch2/zip','POST')[1])) as z:
                assert z.testzip() is None
                members = [n for n in z.namelist() if not n.endswith('/')]
                markers = [n for n in members if n.endswith('/.jm-chapter.json')]
                assert len(markers) == 1 and str(json.loads(z.read(markers[0]))['photo_id']) == '90100102'
                images = [n for n in members if n not in markers]
                assert [n.rsplit('/',1)[-1] for n in images] == [f'{i:05}.webp' for i in range(1,13)]
                exported = [hashlib.sha256(z.read(n)).hexdigest() for n in images]
                assert exported == published
            with pikepdf.open(io.BytesIO(exchange(port,'/api/export/job_atelier_901001_ch2/pdf','POST')[1])) as doc:
                assert len(doc.pages) == len(published)
            exchange(port, '/api/settings','POST', {'ui_theme':'night'})
            exchange(port, '/api/settings','POST', {'retry_times':'0'})
            exchange(port, '/api/settings','POST', {'timeout':'49'})
            h, _ = exchange(port,'/ui/classic',expected=303)
            classic = exchange(port,'/downloads',cookie=h['Set-Cookie'].split(';')[0])[1]
            assert b'/static/classic/js/downloads.js?' in classic
            exchange(port, '/api/jobs','POST', {'album_id':'901001'}, expected=409)
        with server(executable, temp) as port:
            s = json.loads(exchange(port, '/api/settings')[1])['settings']
            assert (s['ui_theme'],s['retry_times'],s['timeout']) == ('night','0','49')
            assert b'data-theme="night"' in exchange(port, '/read/901003')[1]
        assert not (temp/'must-remain-absent').exists()
    return dict(status='ok', exe_pages=len(pages), task_zip_matches_all_published_pages=len(published),
                task_pdf_pages=len(published), offline_catalog=True, archive_only_reader=True,
                classic_fallback=True, restart_preserved=['ui_theme','retry_times','timeout'])


if __name__=='__main__':
    print(json.dumps(verify(Path(sys.argv[1]).resolve()), ensure_ascii=True))
