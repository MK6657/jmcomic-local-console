"""Offline Windows download acceptance. Pass the release directory (or its JMComic/JMComic.exe).

The normal mode runs the unmodified extracted JMComic.exe serve. --source-control
exercises the same fixture and assertions with a source directory or source ZIP in a private mutex;
it never counts as a release pass. Exit 2 means the ordinary mutex was busy.
"""
import argparse
from collections import Counter
from contextlib import closing, contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import shutil
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit
from zipfile import ZipFile

from PIL import Image

from check_vnext_portable import bundle_hashes, environment, identity, paths, request, require

ROOT = Path(__file__).resolve().parents[2]
ALBUM = '810001'
PHOTOS = ('810011', '810012')
TITLE = '典藏 日本語 Atlas 📚 ' * 12
CHAPTER = '新章 カタカナ Journey 📖 ' * 9
BOOK, CDN = 'book.atelier.invalid', 'pages.atelier.invalid'
DISCOVERY = 'jm365.work'
DOWNLOAD_ROOT_UNITS = 162
SIZE = {(pid, page): (21 + chapter, 15 + page) for chapter, pid in enumerate(PHOTOS) for page in (1, 2)}


def units(path):
    return len(str(path).encode('utf-16-le')) // 2


def album_html():
    episodes = ''.join(f'<a data-album="{pid}">第{index}話{CHAPTER}{index}</a>'
                       for index, pid in enumerate(PHOTOS, 1))
    return (f'<span class="number">書目：JM{ALBUM}</span><div id="book-name">{TITLE}</div>{episodes}'
            '<h2>敘述：QA</h2><span class="pagecount">頁:4</span><span>上架日期 : 2026-01-01</span>'
            '<span>更新日期 : 2026-01-02</span><span id="albim_likes_1">0</span><span>4</span>\n<span>次觀看</span>'
            '<span itemprop="author" data-type="author"><a>Fixture</a></span>'
            '<span itemprop="genre" data-type="tags"><a>test</a></span>'
            '<span itemprop="author" data-type="actor"></span><span itemprop="author" data-type="works"></span>'
            '<script>var scramble_id = 999999;</script>').encode()


def photo_html(pid):
    return (f'<meta property="og:url" content="https://{BOOK}/photo/{pid}"><meta name="keywords" content="qa">'
            f'<title>{CHAPTER}{PHOTOS.index(pid) + 1}|QA</title><script>var series_id = {ALBUM}; '
            f'var sort = {PHOTOS.index(pid) + 1}; var scramble_id = 999999; '
            'var page_arr = ["00001.png","00002.png"]; </script>'
            f'<img src="https://{CDN}/media/albums/blank.png"><img data-original="https://{CDN}/media/photos/'
            f'{pid}/00001.png" id="album_photo_1" data-page="0">').encode()


@contextmanager
def fixture(temp):
    openssl = shutil.which('openssl')
    require(openssl, 'QA driver needs openssl on PATH (Git for Windows includes it)')
    subprocess.run([openssl, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1', '-subj', '/CN=Atelier QA',
                    '-addext', f'subjectAltName=DNS:{DISCOVERY},DNS:{BOOK},DNS:{CDN}', '-keyout', 'key.pem',
                    '-out', 'cert.pem'], cwd=temp, check=True, capture_output=True, timeout=20)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(temp / 'cert.pem', temp / 'key.pem')
    tls.set_alpn_protocols(['http/1.1'])
    responses = {(BOOK, '/album/' + ALBUM): ('text/html; charset=utf-8', album_html())}
    for pid in PHOTOS:
        responses[BOOK, '/photo/' + pid] = ('text/html; charset=utf-8', photo_html(pid))
    for (pid, page), size in SIZE.items():
        image = io.BytesIO()
        with Image.new('RGB', size, (20 * page, 90, 150)) as pixels:
            pixels.save(image, 'PNG')
        responses[CDN, f'/media/photos/{pid}/{page:05d}.png'] = ('image/png', image.getvalue())
    hits, denied = Counter(), []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_CONNECT(self):
            host = self.path.partition(':')[0]
            if self.path != host + ':443' or host not in (BOOK, CDN, DISCOVERY):
                denied.append('unexpected CONNECT')
                self.send_error(403)
                return
            self.send_response_only(200)
            self.end_headers()
            with tls.wrap_socket(self.connection, server_side=True) as secure:
                bound = type('BoundFixture', (Handler,), {'tunnel': host})
                bound(secure, self.client_address, self.server)

        def do_GET(self):
            host = getattr(self, 'tunnel', '')
            path = urlsplit(self.path).path
            if self.headers.get('Host', '').split(':')[0] != host:
                denied.append('host mismatch')
                self.send_error(403)
                return
            if (host, path) == (DISCOVERY, '/3YeBdF'):
                self.send_response(302)
                self.send_header('Location', 'https://' + BOOK + '/')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            if (host, path) not in responses:
                denied.append('unexpected GET ' + path)
                self.send_error(404)
                return
            mime, content = responses[host, path]
            hits[path] += 1
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f'http://127.0.0.1:{server.server_port}', hits, denied
        finally:
            server.shutdown()
            thread.join(3)


def extract(release, temp, report):
    archive = release / 'JMComic-windows-x64.zip'
    sums = [line.split(maxsplit=1) for line in (release / 'SHA256SUMS.txt').read_text('ascii').splitlines()]
    expected = [entry[0].lower() for entry in sums if len(entry) == 2 and entry[1].lstrip('*') == archive.name]
    with archive.open('rb') as stream:
        report['zip_sha256'] = hashlib.file_digest(stream, 'sha256').hexdigest()
    require(expected == [report['zip_sha256']], 'release ZIP checksum mismatch')
    dest = temp / '便携包 Portable' / '資料の書庫' / '归档 Archive'
    while units(dest / 'JMComic/downloads') + units('/保存 Edition') + 2 <= DOWNLOAD_ROOT_UNITS:
        dest /= '保存 Edition'
    remaining = DOWNLOAD_ROOT_UNITS - units(dest / 'JMComic/downloads')
    require(remaining >= 2, 'TEMP path already too deep for the fixed release regression; use a shorter TEMP')
    dest /= '書' * (remaining - 1)
    with ZipFile(archive) as package:
        for member in package.infolist():
            path = PurePosixPath(member.filename)
            require(path.parts and path.parts[0] == 'JMComic' and not {'..', 'downloads', 'runtime'} & set(path.parts)
                    and '\\' not in member.filename and ':' not in member.filename, 'unexpected release ZIP member')
        package.extractall(dest)
    exe = dest / 'JMComic/JMComic.exe'
    exe_hash, fingerprint, source = bundle_hashes(exe)
    require(json.loads((release / 'release.json').read_bytes())['source_sha256'] == source, 'manifest mismatch')
    report.update(exe_sha256=exe_hash, bundle_source_sha256=source, download_root_units=units(exe.parent / 'downloads'),
                  album_title_units=units(TITLE), chapter_title_units=units(CHAPTER))
    return exe, fingerprint


def source_tree(path, temp):
    if path.is_dir():
        return path.resolve()
    target = temp / 'source'
    with ZipFile(path) as archive:
        for name in archive.namelist():
            parts = PurePosixPath(name).parts
            require(parts and parts[0] == 'atelier-edition' and '..' not in parts and ':' not in name and '\\' not in name,
                    'unexpected source ZIP member')
        archive.extractall(target)
    return target / 'atelier-edition'


class Busy(Exception):
    pass


@contextmanager
def serve(exe, temp, proxy, fingerprint, source):
    home = exe.parent
    token = secrets.token_hex(16)
    env = environment(home)
    env.update(SSL_CERT_FILE=str(temp / 'cert.pem'), CURL_CA_BUNDLE=str(temp / 'cert.pem'),
               REQUESTS_CA_BUNDLE=str(temp / 'cert.pem'), HTTP_PROXY=proxy, HTTPS_PROXY=proxy,
               ALL_PROXY=proxy, NO_PROXY='', TEMP=str(temp), TMP=str(temp), JMCONSOLE_LAUNCH_TOKEN=token)
    if source:
        (temp / 'source-worker-nonce').write_text(token, encoding='ascii')
        env['PYTHONPATH'] = os.pathsep.join(path for path in sys.path if 'site-packages' in Path(path).parts)
        command = [sys._base_executable, str(Path(__file__).resolve()), '--source-worker', token, str(source)]
    else:
        env.pop('JMCONSOLE_HOME')
        env.pop('JMCONSOLE_DOWNLOAD_DIR')
        command = [str(exe), 'serve']
    log = temp / 'worker.log'
    with log.open('wb') as out:
        proc = subprocess.Popen(command, cwd=temp, env=env, stdin=subprocess.DEVNULL,
                                stdout=out, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                if not source and b'Another JMComic instance' in log.read_bytes():
                    raise Busy('ordinary service mutex occupied; rerun in a free Windows logon session')
                raise RuntimeError('service exited before readiness: ' + log.read_bytes()[-560:].decode('utf-8', 'replace'))
            try:
                record = identity(home, proc, token)
            except (OSError, json.JSONDecodeError):
                time.sleep(.1)
                continue
            require(source or record['code_fingerprint'] == fingerprint, 'wrong EXE fingerprint')
            break
        else:
            raise RuntimeError('timeout waiting for owned process')
        try:
            yield record['port']
        finally:
            require(identity(home, proc, token) == record, 'identity changed before shutdown')
            sys.path.insert(0, str(ROOT))
            from core.process_control import request_shutdown
            require(request_shutdown(token) and proc.wait(timeout=25) == 0, 'owned service did not exit cleanly')
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def source_worker(token, source):
    from importlib.metadata import version
    temp = Path(tempfile.gettempdir()).resolve()
    home = Path(os.environ['JMCONSOLE_HOME']).resolve()
    require(temp.name.startswith('atelier-paths-') and home.is_relative_to(temp) and home.name == 'JMComic'
            and all(os.environ.get(key) == value for key, value in paths(home).items())
            and not (home / 'runtime').exists() and token == os.environ['JMCONSOLE_LAUNCH_TOKEN']
            and len(token) == 32 and (temp / 'source-worker-nonce').read_text('ascii') == token,
            'source-worker needs its own fresh, nonce-marked temporary bundle')
    sys.path.insert(0, str(source))
    from core.instance_lock import maintenance_lock
    from core import database
    import app
    require(Path(app.__file__).resolve() == source / 'app.py', 'source-worker imported the wrong application')
    (temp / 'source-deps.json').write_text(json.dumps({name: version(name) for name in ('jmcomic', 'curl-cffi', 'pillow')}),
                                           encoding='ascii')
    app._MUTEX_NAME = 'Local\\AtelierDownloadPathQA_' + token
    database._migration_lock = lambda: maintenance_lock(app._MUTEX_NAME)
    return app.main()


def verify_pages(port, job_id, home, row, report):
    root = home / 'downloads'
    output = Path(row['output_path']).resolve()
    require(output.is_relative_to(root) and (output / '.jm-view.json').is_file(), 'no public projection')
    require(json.loads((output / '.jm-view.json').read_bytes())['job'] == job_id, 'wrong public generation')
    require(output.name.endswith('_' + ALBUM), 'album path lost ID')
    with closing(sqlite3.connect((home / 'runtime/data/app.db').as_uri() + '?mode=ro', uri=True)) as conn:
        snapshot, manifest = conn.execute('SELECT snapshot,manifest FROM published_tasks WHERE album_id=? AND job_id=?',
                                          (ALBUM, job_id)).fetchone()
    snapshot = (root / snapshot).resolve()
    require(snapshot.is_relative_to(root / '.jmcomic-work'), 'unexpected snapshot location')
    names, digests = [], []
    entries = json.loads(manifest)
    require([entry['photo_id'] for entry in entries] == list(PHOTOS), 'wrong published chapter IDs')
    for entry in entries:
        pid, folder = entry['photo_id'], entry['folder']
        require(Path(folder).name == folder and folder.endswith('__' + pid), 'chapter folder lost ID')
        require(entry['files'] == ['00001.webp', '00002.webp'], 'wrong published filenames')
        for page, filename in enumerate(entry['files'], 1):
            name = f'{folder}/{filename}'
            payload = (snapshot / name).read_bytes()
            digest = hashlib.sha256(payload).hexdigest()
            require(digest == entry['digests'][filename] and (output / name).read_bytes() == payload, 'publication differs')
            with Image.open(io.BytesIO(payload)) as image:
                image.load()
                require((image.format, image.size) == ('WEBP', SIZE[pid, page]), 'wrong fixture page or conversion')
            names.append(name)
            digests.append(digest)
    report['longest_public_file_units'] = max(units(output / name) for name in names)
    report['published_pages'] = len(names)
    report['stage'] = 'read'
    require(b'data-source="local"' in request(port, '/read/' + ALBUM), 'reader did not open local edition')
    preview = json.loads(request(port, '/api/preview/' + ALBUM))
    require(preview['total_pages'] == 4 and [(p['photo_id'], p['offset']) for p in preview['pages']] ==
            [(pid, number) for pid in PHOTOS for number in range(2)], 'wrong reader order or membership')
    require([hashlib.sha256(request(port, page['url'])).hexdigest() for page in preview['pages']] == digests,
            'reader bytes differ from published pages')
    report['reader_pages'] = 4
    for scope, path in [('task', f'/api/export/{job_id}/zip'), ('album', f'/api/library/{ALBUM}/export/zip')]:
        report['stage'] = scope + '_zip'
        with ZipFile(io.BytesIO(request(port, path, post={}))) as archive:
            require(archive.testzip() is None, 'ZIP CRC failure')
            images = [name for name in archive.namelist() if name.endswith('.webp')]
            require(images == [output.name + '/' + name for name in names], 'wrong ZIP order or membership')
            require([hashlib.sha256(archive.read(name)).hexdigest() for name in images] == digests, 'ZIP content mismatch')
            markers = [name for name in archive.namelist() if name.endswith('/.jm-chapter.json')]
            require(sorted(json.loads(archive.read(name))['photo_id'] for name in markers) == sorted(PHOTOS),
                    'ZIP chapter identities do not match')
        report[scope + '_zip_pages'] = len(images)


def check(release, source, report):
    require(os.name == 'nt', 'Windows required')
    if release.is_file():
        require(release.name.lower() == 'jmcomic.exe' and release.parent.name == 'JMComic', 'expected release/JMComic/JMComic.exe')
        given_exe = release
        release = release.parents[1]
    else:
        given_exe = None
    with tempfile.TemporaryDirectory(prefix='atelier-paths-') as scratch:
        temp = Path(scratch).resolve()
        exe, fingerprint = extract(release, temp, report)
        if given_exe:
            require(bundle_hashes(given_exe)[1] == fingerprint, 'supplied EXE differs from archived EXE')
        if source is not None:
            source = source_tree(source, temp)
            sys.path.insert(0, str(ROOT))
            from tools.build_windows import source_fingerprint
            report['tested_source_sha256'] = source_fingerprint(source)
            report['source_matches_bundle'] = report['tested_source_sha256'] == report['bundle_source_sha256']
        with fixture(temp) as (proxy, hits, denied):
            report['stage'] = 'startup'
            try:
                with serve(exe, temp, proxy, fingerprint, source) as port:
                    if source:
                        versions = json.loads((temp / 'source-deps.json').read_bytes())
                        pinned = json.loads((exe.parent / '_internal/release.json').read_bytes())['packages']
                        report['source_worker_packages'] = versions
                        require(all(version == pinned[name] for name, version in versions.items()),
                                'source-control requires release versions of jmcomic, curl-cffi and Pillow; use the build venv')
                    def api(path, **kwargs):
                        return json.loads(request(port, path, **kwargs))
                    settings = api('/api/settings', post=dict(client_type='html', proxy=proxy, auto_update_check=False,
                                   schedule_enabled=False, timeout=8, retry_times=0, photo_threads=1, image_threads=1))
                    require(settings['saved'] and not settings['warnings'] and settings['settings']['proxy'] == proxy
                            and Path(settings['settings']['download_root']) == exe.parent / 'downloads', 'fixture settings not active')
                    report['stage'] = 'download'
                    job_id = api('/api/jobs', post={'album_id': ALBUM, 'title': TITLE}, status=201)['job_id']
                    end = time.monotonic() + 60
                    while time.monotonic() < end:
                        row = api('/api/jobs/' + job_id)['job']
                        if row['status'] not in ('queued', 'running'):
                            break
                        time.sleep(.15)
                    report.update(job_status=row['status'], done_pages=row['done_pages'], total_pages=row['total_pages'])
                    require((row['status'], row['done_pages'], row['total_pages']) == ('completed', 4, 4),
                            'download did not complete: ' + str(row.get('error_message') or row['status'])[:900])
                    report['stage'] = 'publication'
                    verify_pages(port, job_id, exe.parent, row, report)
                    require(not denied and hits == Counter(['/album/' + ALBUM, *('/photo/' + pid for pid in PHOTOS),
                            *(f'/media/photos/{pid}/{page:05d}.png' for pid, page in SIZE)]), 'unexpected SDK requests')
            finally:
                report.update(fixture_gets=dict(hits), image_gets=sum(n for path, n in hits.items() if path.startswith('/media/')),
                              rejected_fixture_requests=denied)
                if source:
                    require(source_fingerprint(source) == report['tested_source_sha256'], 'source changed during the run')
    report.update(stage='complete', status='ok')


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--source-worker':
        raise SystemExit(source_worker(sys.argv[2], Path(sys.argv[3])))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('release', type=Path)
    parser.add_argument('--source-control', type=Path, nargs='?', const=ROOT, metavar='TREE_OR_SOURCE_ZIP',
                        help='run this source tree/ZIP (default checkout); does not pass the EXE')
    args = parser.parse_args()
    result = dict(mode='source-control' if args.source_control else 'release-exe', status='failed', stage='extract')
    exit_code = 0
    try:
        check(args.release.resolve(), args.source_control, result)
    except Busy as error:
        result.update(status='blocked', error=str(error))
        exit_code = 2
    except Exception as error:
        result.update(error=f'{type(error).__name__}: {error}')
        exit_code = 1
    print(json.dumps(result, ensure_ascii=True))
    raise SystemExit(exit_code)
