"""Opt-in Windows acceptance: pass an extracted JMComic.exe. No build or upstream requests."""
import argparse
from contextlib import closing, contextmanager
import hashlib
from http.client import HTTPConnection
import io
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from zipfile import ZipFile

ALBUM, A, B = '900100', 'fixture_paused_A', 'fixture_completed_B'
CH_A, CH_B = '900101', '900102'
COLORS = [(16, 160, 32), (32, 64, 224)]  # B, in page order. A is red.
PROXY = 'http://127.0.0.1:9'
TABLES = {'task_page_receipts', 'published_tasks', 'album_maintenance'}
MARKER = '.lifecycle-check.json'


def require(value, message):
    if not value:
        raise RuntimeError(message)


def paths(home):
    return dict(JMCONSOLE_HOME=str(home), JMCONSOLE_DOWNLOAD_DIR=str(home / 'downloads'),
                JMCONSOLE_CACHE_DIR=str(home / 'cache'), JMCONSOLE_BACKUP_DIR=str(home / 'backups'))


def environment(home):
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(('JMCONSOLE_', 'PYTHON', '_PYI', 'PYINSTALLER'))
           and not k.upper().endswith('_PROXY') and k.upper() not in ('VIRTUAL_ENV', '__PYVENV_LAUNCHER__')}
    return dict(env, **paths(home), PATH=str(Path(os.environ['SystemRoot']) / 'System32'), PYINSTALLER_RESET_ENVIRONMENT='1')


def schema(home):
    uri = (home / 'runtime/data/app.db').as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        version, = conn.execute('PRAGMA user_version').fetchone()
        require(version == 5, f'expected new v5 database initialized by candidate, got v{version}')
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
        require(TABLES <= tables, f'missing v5 tables: {TABLES - tables}')


def seed_fixture(home, nonce):
    """Only this short-lived subprocess imports the source publication writer. No server or SDK requests."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from core.release_lifecycle import _guard_network, _verify_root
    _verify_root(home, nonce)
    require(all(os.environ.get(k) == v for k, v in paths(home).items()), 'fixture paths not isolated')
    schema(home)
    _guard_network()
    from PIL import Image
    from core import database as db, publication
    from core.download_checkpoint import DownloadCheckpoint
    from core.download_layout import chapter_output_dir
    from core.settings import update_settings
    from core.task_commands import finish_download

    downloads = home / 'downloads'
    require(not any(downloads.iterdir()), 'expected fresh downloads')
    update_settings(dict(auto_update_check=False, schedule_enabled=True, schedule_start=0, schedule_end=0,
                         skip_existing=False))
    fixture = {'pages': {}}
    for job, pid, colors in [(A, CH_A, [(224, 32, 16)]), (B, CH_B, COLORS)]:
        class Chapter(list):
            name = 'Chapter'

        photo = Chapter([1, 2])
        photo.photo_id = pid
        db.insert_job(job, ALBUM, 'Fixture', [pid])
        require(db.transition_job_status(job, ['queued'], 'running'), 'fixture task did not start')
        receipt = DownloadCheckpoint(job, db.get_job(job))
        work = publication.Workspace(downloads, job, ALBUM, downloads / 'Fixture', receipt)
        receipt.own(work.work)
        chapter = chapter_output_dir(work.work, photo)
        for i, color in enumerate(colors, 1):
            page = chapter / f'{i:05d}.webp'
            with Image.new('RGB', (24, 16), color) as image:
                image.save(page, lossless=True)
            receipt.confirm(page)
            if job == A:
                fixture['private'] = page.relative_to(downloads).as_posix()
                fixture['receipt'] = [page.relative_to(work.work).as_posix(), hashlib.sha256(page.read_bytes()).hexdigest()]
            else:
                fixture['pages'][page.relative_to(work.work).as_posix()] = hashlib.sha256(page.read_bytes()).hexdigest()
        if job == A:
            require(db.transition_job_status(job, ['running'], 'paused', output_path=str(work.work),
                                             done_pages=1, total_pages=2), 'fixture A did not pause')
        else:
            manifest = work.seal([photo], lambda: None)
            target, evidence = work.project(manifest, SimpleNamespace(author='Fixture'), {}, lambda: None)
            require(finish_download(job, 'completed', photo_ids=[pid], total_pages=2, done_pages=2,
                                    output_path=str(target), publication=evidence), 'fixture B did not publish')
            work.release_pages(manifest)
    return fixture


def request(port, path, *, post=None, status=200):
    # HTTPConnection always connects directly to this literal IP; it neither uses system proxies nor follows redirects.
    require(port in range(5000, 5004) and path.startswith(('/api/', '/read/')), 'invalid loopback request')
    with closing(HTTPConnection('127.0.0.1', port, timeout=5)) as http:
        headers = {'Connection': 'close'}
        if post is not None:
            headers['Content-Type'] = 'application/json'
        http.request('GET' if post is None else 'POST', path, None if post is None else json.dumps(post), headers)
        response = http.getresponse()
        require(response.status == status, f'{path}: expected HTTP {status}, got {response.status}')
        data = response.read(2**20 + 1)
        require(len(data) <= 2**20, f'{path}: response too large for fixture')
        return data


def identity(home, proc, token):
    data = home / 'runtime/data'
    record = json.loads((data / 'flask.json').read_bytes())
    require(proc.poll() is None and record['pid'] == proc.pid == int((data / 'flask.pid').read_text())
            and record['launch_token'] == record['shutdown_token'] == token
            and record['shutdown_protocol'] == 'windows-event-v1', 'unverified process/nonce; refusing contact')
    port = record['port']
    health = json.loads(request(port, '/api/system/health'))
    roots = [os.path.normcase(p) for p in paths(home).values()]
    expected = hashlib.sha256(json.dumps(['jmconsole-instance-v1', token, *roots]).encode('ascii')).hexdigest()
    require(health['status'] == 'ok' and health['application'] == 'jmcomic-local-console'
            and health['pid'] == proc.pid and health['instance_id'] == expected, 'unverified service identity')
    return record


def private_receipt(home, fixture):
    # Otherwise a 404 for A might merely mean its page was deleted on startup.
    page = home / 'downloads' / fixture['private']
    require(hashlib.sha256(page.read_bytes()).hexdigest() == fixture['receipt'][1], 'paused work page changed')
    with closing(sqlite3.connect((home / 'runtime/data/app.db').as_uri() + '?mode=ro', uri=True)) as conn:
        rows = conn.execute('SELECT page,digest FROM task_page_receipts WHERE job_id=?', (A,)).fetchall()
        require(rows == [tuple(fixture['receipt'])], 'paused page receipt lost or changed')


@contextmanager
def foreground(command, home, nonce, processes):
    token = secrets.token_hex(16)
    with (home / 'worker.log').open('ab') as log:
        proc = subprocess.Popen([*command, '_lifecycle-worker', str(home), nonce], cwd=home,
                                env=dict(environment(home), JMCONSOLE_LAUNCH_TOKEN=token), stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
    processes.append(proc)
    data = home / 'runtime/data'
    deadline = time.monotonic() + 35
    while not (data / 'flask.json').exists():
        require(proc.poll() is None, f'candidate exited on startup: {proc.returncode}')
        require(time.monotonic() < deadline, 'candidate readiness timed out')
        time.sleep(.1)
    # Startup writes a small JSON record before entering the HTTP loop; allow a partial write.
    while True:
        try:
            record = identity(home, proc, token)
            break
        except (OSError, json.JSONDecodeError):
            require(proc.poll() is None and time.monotonic() < deadline, 'candidate identity timed out')
            time.sleep(.1)
    try:
        yield record
    finally:
        require(identity(home, proc, token) == record, 'identity changed before stop')
        # Shared Win32 event primitive only; it does not look up ports, processes or instances.
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        try:
            from core.process_control import request_shutdown
        finally:
            sys.path.pop(0)
        require(request_shutdown(token), 'owned shutdown event not found')
        require(proc.wait(timeout=25) == 0, 'candidate did not exit cleanly')
        require(not any((data / f'flask.{ext}').exists() for ext in ('pid', 'json')), 'stale identity files')


def publication_checks(port, fixture):
    import pikepdf

    folder = 'Fixture/'
    names = [folder + relative for relative in fixture['pages']]
    digests = list(fixture['pages'].values())
    for job, status in [(A, 'paused'), (B, 'completed')]:
        row = json.loads(request(port, f'/api/jobs/{job}'))['job']
        require((row['album_id'], row['status']) == (ALBUM, status), f'{job}: unexpected task state')
    require(b'data-source="local"' in request(port, f'/read/{ALBUM}'), 'reader did not select local pages')
    pages = json.loads(request(port, f'/api/preview/{ALBUM}'))['pages']
    require([(p['photo_id'], p['page']) for p in pages] == [(CH_B, 1), (CH_B, 2)], 'reader leaked or lost a page')
    for page, name, digest in zip(pages, names, digests):
        url = '/api/preview-img/' + name
        require(page['url'] == url and hashlib.sha256(request(port, url)).hexdigest() == digest,
                f'reader bytes differ: {name}')
    request(port, '/api/preview-img/' + fixture['private'], status=404)
    for fmt in ('zip', 'pdf'):
        request(port, f'/api/export/{A}/{fmt}', post={}, status=400)
    for base in (f'/api/export/{B}', f'/api/library/{ALBUM}/export'):
        with ZipFile(io.BytesIO(request(port, base + '/zip', post={}))) as archive:
            # ZIP can also include the chapter marker. No other chapters, nested archives or hidden pages.
            members = archive.namelist()
            require(set(members) <= {*names, folder + f'Chapter__{CH_B}/.jm-chapter.json'}
                    and [n for n in members if n in names] == names, f'{base}: unexpected ZIP members')
            for name, digest in zip(names, digests):
                info = archive.getinfo(name)
                require(info.file_size < 4096 and hashlib.sha256(archive.read(info)).hexdigest() == digest,
                        f'{base}: wrong ZIP page: {name}')
        with pikepdf.Pdf.open(io.BytesIO(request(port, base + '/pdf', post={}))) as doc:
            require(len(doc.pages) == len(COLORS), f'{base}: wrong PDF page count')
            for page, rgb in zip(doc.pages, COLORS):
                obj, = page.get_images().values()
                with pikepdf.PdfImage(obj).as_pil_image() as img:
                    require(img.size == (24, 16), f'{base}: wrong PDF image size')
                    # The PDF writer may transcode to JPEG. Check every channel of every pixel in page order.
                    limits = img.convert('RGB').getextrema()
                    require(all(v - 3 <= lo <= hi <= v + 3 for (lo, hi), v in zip(limits, rgb)),
                            f'{base}: wrong PDF page contents')


@contextmanager
def disposable_home():
    home = Path(tempfile.mkdtemp(prefix='jmcomic-lifecycle-')).resolve()
    nonce, processes = secrets.token_hex(32), []
    try:
        (home / MARKER).write_text(json.dumps({'format': 1, 'nonce': nonce}), encoding='utf-8')
        yield home, nonce, processes
    finally:
        live = [p.pid for p in processes if p.poll() is None]
        if live:
            print(f'Unverified exit: preserved temporary home {home}; owned PID(s) {live}', file=sys.stderr)
        else:
            shutil.rmtree(home)


def exercise(command, fingerprint):
    with disposable_home() as (home, nonce, processes):
        # No source schema initializer: an old executable must fail here, before a fixture is written.
        with foreground(command, home, nonce, processes) as record:
            require(record['code_fingerprint'] == fingerprint, 'server fingerprint differs from selected program')
            schema(home)
        child = subprocess.run([sys.executable, '-I', str(Path(__file__).resolve()), '_seed', str(home), nonce],
                               cwd=home, env=environment(home), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               timeout=30, check=True)
        fixture = json.loads(child.stdout.splitlines()[-1])
        for restart in (False, True):
            with foreground(command, home, nonce, processes) as record:
                require(record['code_fingerprint'] == fingerprint, 'unexpected server after restart')
                port = record['port']
                if not restart:
                    saved = json.loads(request(port, '/api/settings', post={'proxy': PROXY, 'timeout': '47'}))
                    require(saved['saved'] and not saved['warnings'] and saved['settings']['proxy'] == PROXY,
                            'ordinary proxy save failed')
                settings = json.loads(request(port, '/api/settings'))['settings']
                require(settings['proxy'] == PROXY and settings['timeout'] == '47'
                        and settings['auto_update_check'] == 'false', 'settings readback failed')
                require('proxy' not in json.loads(request(port, '/api/settings/export')), 'settings export exposed proxy')
                publication_checks(port, fixture)
            private_receipt(home, fixture)
        schema(home)


def bundle_hashes(exe):
    require(exe.name.lower() == 'jmcomic.exe' and exe.is_file(), 'expected extracted JMComic.exe')
    manifest = (exe.parent / '_internal/release.json').read_bytes()
    with exe.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256')
    exe_sha = digest.hexdigest()
    digest.update(manifest)  # frozen startup record hashes EXE followed by release.json
    return exe_sha, digest.hexdigest(), json.loads(manifest)['source_sha256']


@contextmanager
def observe_shutdown(token):
    """Hold the real manual-reset event across launch. Waiting with zero timeout never signals it."""
    import ctypes
    from ctypes import wintypes
    import _winapi
    open_event = ctypes.WinDLL('kernel32', use_last_error=True).OpenEventW
    open_event.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    open_event.restype = wintypes.HANDLE
    handle = open_event(0x00100000, False, 'Local\\JMConsoleShutdown_' + token)  # SYNCHRONIZE only
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    def check():
        require(_winapi.WaitForSingleObject(handle, 0) == _winapi.WAIT_TIMEOUT, 'old service shutdown event signaled')
    try:
        check()
        yield
        check()
    finally:
        _winapi.CloseHandle(handle)


def check_busy_launch(exe, home, proc, record):
    port, token = record['port'], record['launch_token']
    def paused_only():
        require(identity(home, proc, token) == record, 'original server identity changed')
        health = json.loads(request(port, '/api/system/health?activity=1'))
        require(health['jobs'] == dict(paused=1, queued=0, running=0, executors=0) and health['jm_active_clients'] == 0,
                'expected paused task to be the only upgrade blocker')
        job = json.loads(request(port, f'/api/jobs/{A}'))['job']
        require(job['job_id'] == A and job['status'] == 'paused', 'original task is no longer paused')

    paused_only()
    # webbrowser's first choice is a non-networking recorder; no desktop browser or user profile needed.
    python = Path(sys._base_executable).resolve(strict=True).as_posix()
    require(';' not in python and '%s' not in python, 'interpreter path not representable in BROWSER')
    (home / 'record_browser.py').write_text(
        "import sys\ntry:\n    with open('browser-calls', 'a', encoding='utf-8') as log: print(sys.argv[1], file=log)\n"
        "except OSError: pass\n", encoding='utf-8')  # even on write failure, never fall back to the real browser
    browser = f'"{python}" -I -S record_browser.py %s'
    # launch_flask opens launcher.log for append before Popen. An accidental spawn attempt must fail
    # here rather than start an unguarded server. This does not block attach or the real shutdown event.
    guard = home / 'runtime/logs/launcher.log'
    guard.touch(exist_ok=False)
    guard.chmod(stat.S_IREAD)
    try:
        with observe_shutdown(token):
            launch = subprocess.run([str(exe), 'launch'], cwd=home, env=dict(environment(home), BROWSER=browser),
                                    stdin=subprocess.DEVNULL, capture_output=True, timeout=35,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
        require(launch.returncode == 0, f'ordinary launch failed (exit {launch.returncode}); did not attach')
        output = launch.stdout.decode('utf-8')
        require('程序或运行依赖已更新' in output and '已暂缓升级：排队 0、运行 0、暂停 1' in output
                and f'已打开既有实例（PID {proc.pid}）' in output, 'missing explicit outdated/paused/attach decision')
        require((home / 'browser-calls').read_text(encoding='utf-8').splitlines() == [f'http://127.0.0.1:{port}'],
                'launcher did not open exactly the original loopback URL')
        require(guard.stat().st_size == 0, 'launcher wrote a server-start record')
        paused_only()
    finally:
        guard.chmod(stat.S_IWRITE)


def busy_upgrade(exe, candidate_fp, previous):
    with disposable_home() as (home, nonce, processes):
        synthetic = previous is None
        if synthetic:
            # Only copy packaged program resources. Never copy runtime, downloads or a personal instance.
            # JSON whitespace changes the fingerprint computed by the old process itself; PID/tokens are real.
            previous = home / 'old-bundle' / exe.name
            shutil.copytree(exe.parent / '_internal', previous.parent / '_internal')
            shutil.copyfile(exe, previous)
            with (previous.parent / '_internal/release.json').open('ab') as manifest:
                manifest.write(b'\n')
        old_sha, old_fp, _ = bundle_hashes(previous)
        require(old_fp != candidate_fp, 'old and candidate fingerprints match; upgrade path cannot be tested')
        command = [str(previous)]
        with foreground(command, home, nonce, processes) as record:
            require(record['code_fingerprint'] == old_fp, 'running code does not match supplied old bundle')
        db = (home / 'runtime/data/app.db').as_uri()
        with closing(sqlite3.connect(db + '?mode=rw', uri=True)) as connection, connection:
            old_schema, = connection.execute('PRAGMA user_version').fetchone()
            connection.execute('INSERT INTO jobs(job_id,album_id,title,selected_photo_ids,status) VALUES(?,?,?,?,?)',
                               (A, ALBUM, 'Busy upgrade fixture', '[]', 'paused'))
            connection.executemany('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)',
                                   [('auto_update_check', 'false'), ('schedule_enabled', 'true'),
                                    ('schedule_start', '0'), ('schedule_end', '0')])
        with foreground(command, home, nonce, processes) as record:
            require(record['code_fingerprint'] == old_fp, 'old service fingerprint changed')
            check_busy_launch(exe, home, processes[-1], record)
        with closing(sqlite3.connect(db + '?mode=ro', uri=True)) as connection:
            require(connection.execute('PRAGMA user_version').fetchone() == (old_schema,), 'launcher upgraded live DB')
        return dict(mode='synthetic-old-fingerprint' if synthetic else 'two-bundles', old_exe_sha256=old_sha,
                    old_fingerprint=old_fp, launcher_fingerprint=candidate_fp, old_schema=old_schema,
                    paused=1, original_pid=record['pid'], shutdown_signaled=False, replacement_started=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('executable', type=Path)
    parser.add_argument('--previous-exe', type=Path, help='use this extracted old EXE as server (default: synthetic old fingerprint)')
    parser.add_argument('--busy-upgrade-only', action='store_true', help='only exercise ordinary launch against a paused outdated server')
    args = parser.parse_args()
    require(sys.platform == 'win32', 'requires Windows')
    exe = args.executable.resolve(strict=True)
    exe_sha, fingerprint, source_sha = bundle_hashes(exe)
    checks = []
    report = dict(exe=str(exe), exe_sha256=exe_sha, source_sha256=source_sha, checks=checks)
    if not args.busy_upgrade_only:
        exercise([str(exe)], fingerprint)
        report['schema'] = 5
        checks.extend(['fresh_schema', 'paused_A_private', 'reader_B_only', 'task_and_album_zip_pdf',
                       'proxy_save', 'restart_readback', 'verified_exits', 'temp_cleanup'])
    previous = args.previous_exe.resolve(strict=True) if args.previous_exe else None
    report['busy_upgrade'] = busy_upgrade(exe, fingerprint, previous)
    checks.append('outdated_launcher_preserved_paused_service')
    print(json.dumps(dict(report, status='ok')))


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '_seed':
        print(json.dumps(seed_fixture(Path(sys.argv[2]), sys.argv[3])))
    else:
        main()
