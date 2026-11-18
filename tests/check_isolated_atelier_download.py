"""Exercise the frozen EXE's real queue and SDK at a 164-unit root even when a daily instance is open."""
from collections import Counter
from contextlib import ExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import secrets
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

from PIL import Image

import check_atelier_download_paths as qa
from check_vnext_portable import MARKER, bundle_hashes, foreground, request, require


@contextmanager
def https_fixture(temp):
    cert = temp / 'cert.pem'
    subprocess.run([shutil.which('openssl') or 'openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                    '-days', '1', '-subj', '/CN=127.0.0.1', '-addext', 'subjectAltName=IP:127.0.0.1',
                    '-keyout', str(temp / 'key.pem'), '-out', str(cert)], check=True, capture_output=True, timeout=20)
    hits = Counter()
    class Pages(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            path = self.path.split('?', 1)[0]
            hits[path] += 1
            if path not in responses:
                self.send_error(404)
                return
            mime, body = responses[path]
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with ThreadingHTTPServer(('127.0.0.1', 0), Pages) as server:
        port = server.server_port
        with patch.multiple(qa, BOOK=f'127.0.0.1:{port}', CDN=f'127.0.0.1:{port}'):
            responses = {'/album/' + qa.ALBUM: ('text/html; charset=utf-8', qa.album_html())}
            for pid in qa.PHOTOS:
                responses['/photo/' + pid] = ('text/html; charset=utf-8', qa.photo_html(pid))
            for (pid, n), size in qa.SIZE.items():
                data = io.BytesIO()
                with Image.new('RGB', size, (10, 100, 5 * n)) as im:
                    im.save(data, 'PNG')
                responses[f'/media/photos/{pid}/{n:05}.png'] = ('image/png', data.getvalue())
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert, temp / 'key.pem')
        server.socket = tls.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield port, cert, hits, responses
        finally:
            server.shutdown()
            thread.join(3)


def verify(exe=None):
    report = {'mode': 'isolated-release-exe' if exe else 'isolated-source-control'}
    # Launch the interpreter itself, not the Windows venv's PID-forwarding stub.
    cmd = [str(exe)] if exe else [sys._base_executable, '-c',
        'import runpy,sys; sys.path[:0]=sys.argv[1:3]; del sys.argv[1:3]; runpy.run_module("desktop",run_name="__main__")',
        str(qa.ROOT), next(p for p in sys.path if Path(p).name == 'site-packages')]
    with tempfile.TemporaryDirectory(prefix='sdk-path-') as scratch:
        temp = Path(scratch).resolve()
        name = 'jmcomic-lifecycle-' + secrets.token_hex(8)
        home = (temp / name).with_name(name + '書' * (164 - qa.units(temp / name / 'downloads')))
        home.mkdir()
        report['download_root_units'] = qa.units(home / 'downloads')
        require(report['download_root_units'] == 164, 'unexpected QA root length')
        nonce = secrets.token_hex(32)
        (home / MARKER).write_text(json.dumps(dict(format=1, nonce=nonce)), encoding='utf8')
        with https_fixture(temp) as (fixture_port, cert, hits, responses):
            (home / '.loopback-sdk-port').write_text(str(fixture_port), 'ascii')
            with patch.dict(os.environ, TEMP=str(temp), TMP=str(temp), CURL_CA_BUNDLE=str(cert), SSL_CERT_FILE=str(cert)), ExitStack() as cleanup:
                owned = []
                def reap():
                    for proc in owned:
                        if proc.poll() is None:
                            proc.terminate()
                            proc.wait(timeout=10)
                cleanup.callback(reap)
                with foreground(cmd, home, nonce, owned) as record:
                    if exe:
                        exe_hash, fingerprint, source = bundle_hashes(exe)
                        require(record['code_fingerprint'] == fingerprint, 'wrong EXE')
                        report.update(exe_sha256=exe_hash, source_sha256=source)
                    port = record['port']
                    settings = json.loads(request(port, '/api/settings', post=dict(client_type='html', timeout=8,
                        retry_times=0, auto_update_check=False, schedule_enabled=False, image_threads=1)))
                    require(settings['saved'], 'settings were not saved')
                    job = json.loads(request(port, '/api/jobs', post=dict(album_id=qa.ALBUM, title=qa.TITLE), status=201))['job_id']
                    deadline = time.monotonic() + 40
                    while time.monotonic() < deadline:
                        row = json.loads(request(port, '/api/jobs/' + job))['job']
                        if row['status'] not in ('queued', 'running'):
                            break
                        time.sleep(.15)
                    require((row['status'], row['done_pages'], row['total_pages']) == ('completed', 4, 4),
                            f"Synthetic task: {row['status']}: {row['error_message']}; fixture hits={dict(hits)}")
                    require(hits == Counter(responses.keys()), 'expected exactly one request per metadata document/image')
                    qa.verify_pages(port, job, home, row, report)
    report.update(status='ok', stage='complete', sdk_requests=sum(hits.values()))
    return report


if __name__ == '__main__':
    print(json.dumps(verify(Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else None)))
