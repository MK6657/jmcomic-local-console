"""Opt-in Windows probes, one fresh process and disposable instance per invocation.

See docs/archive/WINDOWS_VNEXT_MEASUREMENTS.md. No service, upstream, timing assertions or real library.
"""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import ctypes
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import logging
import os
from pathlib import Path
import random
import socket
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch
import zipfile

from check_pdf_memory import memory  # also makes the repo root importable


def image(number, fmt, size, mode='RGB'):
    from PIL import Image
    pixels = random.Random(number).randbytes(size * size * len(mode))
    with Image.frombytes(mode, (size, size), pixels) as page, io.BytesIO() as buf:
        page.save(buf, fmt)
        return buf.getvalue()


@contextmanager
def sample(roots):
    """Measure named-file logical lengths in the output/temp roots; short-lived files may be missed."""
    result = Counter()
    stop = threading.Event()

    def observe():
        total = 0
        for root in roots:
            for folder, _, names in os.walk(root):
                for name in names:
                    try:
                        total += os.stat(os.path.join(folder, name)).st_size
                    except FileNotFoundError:  # file renamed/removed during enumeration
                        continue
        result['disk_peak'] = max(result['disk_peak'], total)
        result['ws_peak'] = max(result['ws_peak'], memory().working)
        result['samples'] += 1

    def loop():
        while not stop.wait(.005):
            observe()

    observe()
    result['ws_start'] = result['ws_peak']
    with ThreadPoolExecutor(1) as pool:
        sampler = pool.submit(loop)
        start = time.perf_counter()
        try:
            yield result
        finally:
            result['seconds'] = round(time.perf_counter() - start, 4)
            stop.set()
            sampler.result()
            observe()


def backup(home):
    from core import backup as api, database as db
    root, output = home / 'downloads', home / 'backups'
    output.mkdir()
    for i in range(48):
        folder = root / f'chapter-{i // 8}'
        folder.mkdir(exist_ok=True)
        (folder / f'{i:05}.png').write_bytes(image(i, 'PNG', 512))
    counts = Counter()
    hash_file = api._hash_file

    def counted(path, budget, output=None):
        size, digest = hash_file(path, budget, output)
        if path.is_relative_to(root):
            counts['source_hash_bytes'] += size
            counts['source_hash_calls'] += 1
        return size, digest

    with patch.object(api, '_hash_file', counted), sample([output, home / 'tmp']) as obs:
        info = api.create_backup(db.DB_PATH, root, output / 'full.zip', include_downloads=True, service_stopped=True)
    files = info.manifest['resources']['files']
    assert info.includes_downloads and len(files) == 48
    with zipfile.ZipFile(info.path) as archive:
        assert len(archive.infolist()) == len(files) + 2
        manifest_bytes = archive.getinfo('manifest.json').file_size
    return dict(**obs, **counts, files=len(files), source_bytes=sum(f['size'] for f in files),
                db_bytes=info.manifest['database']['size'], zip_bytes=info.path.stat().st_size,
                manifest_bytes=manifest_bytes, schema=info.schema_version)


def pdf(home):
    from core import database as db
    from flask import Flask
    import pikepdf
    from routes import api_export
    folder = home / 'downloads' / 'Book'
    folder.mkdir()
    archive_path = folder / 'Book.cbz'
    with zipfile.ZipFile(archive_path, 'x', zipfile.ZIP_STORED) as archive:
        for i in range(32):
            is_png = i % 2 == 0
            data = image(i, 'PNG' if is_png else 'JPEG', 512, 'RGBA' if is_png else 'RGB')
            archive.writestr(f'{i:05}.{"png" if is_png else "jpg"}', data)
        source = sum(m.file_size for m in archive.infolist())
    db.insert_job('fixture', '123', 'Book', [])
    assert db.transition_job_status('fixture', ['queued'], 'completed', output_path=str(folder))
    app = Flask(__name__)
    app.register_blueprint(api_export.api_export_bp)
    client = app.test_client()
    maxima = Counter()

    class ObservedBudget(api_export.ExportBudget):
        def resize_staging(self, key, size):
            super().resize_staging(key, size)
            maxima['staging_peak'] = max(maxima['staging_peak'], self.staging_bytes)

    final = home / 'received.pdf'  # simulates the recipient; excluded from server temp usage
    with patch.object(api_export, 'ExportBudget', ObservedBudget), sample([home / 'tmp']) as obs:
        with client.post('/api/library/123/export/pdf', buffered=False) as response, final.open('wb') as stream:
            assert response.status_code == 200, response.get_json()
            for chunk in response.response:
                stream.write(chunk)
    with pikepdf.Pdf.open(final) as doc:
        assert len(doc.pages) == 32
    assert not list((home / 'tmp').iterdir())
    return dict(**obs, **maxima, pages=32, cbz_bytes=archive_path.stat().st_size,
                source_bytes=source, pdf_bytes=final.stat().st_size)


def file_sizes(folder):
    """FileStandardInfo.AllocationSize for the unnamed stream, excludes directory/MFT overhead."""
    import msvcrt

    class StandardInfo(ctypes.Structure):
        _fields_ = [('allocation', ctypes.c_int64), ('end', ctypes.c_int64), ('links', wintypes.DWORD),
                    ('deleting', ctypes.c_ubyte), ('directory', ctypes.c_ubyte)]

    query = ctypes.windll.kernel32.GetFileInformationByHandleEx
    query.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    size = Counter(logical=0, allocated=0, files=0)
    for path in folder.rglob('*'):
        if path.is_file():
            with path.open('rb') as handle:
                info = StandardInfo()
                if not query(msvcrt.get_osfhandle(handle.fileno()), 1, ctypes.byref(info), ctypes.sizeof(info)):
                    raise ctypes.WinError()
                assert info.links == 1  # this fixture has copies, not shared NTFS hardlinks
                size.update(logical=info.end, allocated=info.allocation, files=1)
    return dict(size)


def views(home):
    from core import album_maintenance as maintenance, database as db, publication
    from core.download_layout import chapter_output_dir
    from core.task_commands import finish_download
    root = home / 'downloads'
    shelf = root / 'Book'
    settings = dict(organize_mode='none', auto_pack='false', pack_format='cbz', delete_originals='false')
    result = []

    class Chapter(list):
        name = 'Chapter'

    for i in range(5):
        job = f'probe_{i}'
        photo = Chapter(range(1, 9))
        photo.photo_id = str(i + 1)
        db.insert_job(job, '123', 'Book', [photo.photo_id])
        assert db.transition_job_status(job, ['queued'], 'running')
        workspace = publication.Workspace(root, job, '123', shelf, SimpleNamespace(directory=None))
        folder = chapter_output_dir(workspace.work, photo)
        for n in photo:
            (folder / f'{n:05}.webp').write_bytes(image(i * 8 + n, 'WEBP', 384))
        manifest = workspace.seal([photo], lambda: None)
        target, evidence = workspace.project(manifest, SimpleNamespace(author='A'), settings, lambda: None)
        assert finish_download(job, 'completed', photo_ids=[photo.photo_id], total_pages=8, done_pages=8,
                               output_path=str(target), publication=evidence)
        workspace.release_pages(manifest)
        if i:
            work = maintenance.claim()
            assert work
            maintenance.run(work, threading.Event())
            assert maintenance.statuses(['123'])['123']['state'] == 'completed'
            assert maintenance.public_directory('123') == shelf
        with closing(db.get_db()) as conn:
            for table in ('jobs', 'resource_catalog'):
                assert {r[0] for r in conn.execute(f'SELECT output_path FROM {table}')} == {str(shelf)}
        old = root / publication.VIEWS
        count = len(list(old.glob('*/*/.jm-view.json')))
        assert count == i and len(list(shelf.rglob('*.webp'))) == (i + 1) * 8
        result.append(dict(old_views=count, old=file_sizes(old), shelf=file_sizes(shelf)))
    private = root / publication.WORK
    snapshots = Counter()
    for snapshot in private.glob('*/snapshot'):
        snapshots.update(file_sizes(snapshot))
    return dict(generations=result, snapshots=dict(snapshots), private_including_metadata=file_sizes(private))


def sdk(home):
    from core import jm_service, metadata_budget, sdk_adapter
    from curl_cffi import Curl, CurlOpt
    body = image(7, 'JPEG', 256)
    lock = threading.Condition()
    stats = Counter()

    class Server(ThreadingHTTPServer):
        def get_request(self):
            request = super().get_request()
            with lock:
                stats['accepts'] += 1
            return request

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        disable_nagle_algorithm = True

        def log_message(self, *args):
            pass

        def do_GET(self):
            with lock:
                stats['requests'] += 1
                stats['active'] += 1
                stats['peak_requests'] = max(stats['peak_requests'], stats['active'])
            try:
                time.sleep(.005)  # model a small, repeatable service delay
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            finally:
                with lock:
                    stats['active'] -= 1
                    lock.notify_all()

    result = {}
    with Server(('127.0.0.1', 0), Handler) as server:
        url = f'http://127.0.0.1:{server.server_port}/page.jpg'
        original = Curl.setopt

        def loopback(curl, option, value):
            if option == CurlOpt.URL and value not in (url, url.encode()):
                raise AssertionError('unexpected curl destination')
            return original(curl, option, value)

        def request(client):
            with metadata_budget.call_scope(metadata_budget.deadline_after(5)):
                response = sdk_adapter.get_image_once(client, url, 5)
                assert response.status_code == 200 and response.content == body

        def lane(mode):
            client = jm_service.new_image_client() if mode == 'image_lane' else None
            try:
                for _ in range(8):
                    if client:
                        request(client)
                    else:
                        fresh, _ = jm_service.get_client(shared=False)
                        try:
                            request(fresh)
                        finally:
                            jm_service.close_client(fresh)
            finally:
                if client:
                    jm_service.close_client(client)

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.object(Curl, 'setopt', loopback):
                for mode in ('per_page', 'image_lane', 'metadata_new_thread', 'metadata_same_thread'):
                    warm = jm_service.new_image_client()  # same warm option; no transport request
                    jm_service.close_client(warm)
                    with lock:
                        stats.clear()
                    start = time.perf_counter()
                    if mode.startswith('metadata'):
                        shared, _ = jm_service.get_client()
                        try:
                            for _ in range(32):
                                if mode == 'metadata_new_thread':
                                    with ThreadPoolExecutor(1) as pool:
                                        pool.submit(request, shared).result()
                                else:
                                    request(shared)
                        finally:
                            jm_service.close_client(shared)
                            jm_service.invalidate_option_cache()
                    else:
                        with ThreadPoolExecutor(4) as pool:
                            list(pool.map(lane, [mode] * 4))
                    with lock:
                        assert lock.wait_for(lambda: stats['active'] == 0, timeout=5)
                        assert stats['requests'] == 32
                        result[mode] = dict(accepts=stats['accepts'], peak_requests=stats['peak_requests'],
                                            seconds=round(time.perf_counter() - start, 4), requests=stats['requests'])
        finally:
            server.shutdown()
            thread.join()
    return dict(body_bytes=len(body), modes=result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('probe', choices=['backup', 'pdf', 'sdk', 'views'])
    args = parser.parse_args()
    if sys.platform != 'win32' or not __debug__:
        parser.error('run on Windows with Python assertions enabled')
    with tempfile.TemporaryDirectory(prefix='jm-measure-') as temporary:
        home = Path(temporary).resolve()
        for key, child in [('HOME', ''), ('DOWNLOAD_DIR', 'downloads'), ('CACHE_DIR', 'cache'), ('BACKUP_DIR', 'backups')]:
            os.environ['JMCONSOLE_' + key] = str(home / child)
        (home / 'tmp').mkdir()
        tempfile.tempdir = str(home / 'tmp')
        from curl_cffi import Curl
        from core.path_utils import ensure_dirs
        from core import database as db
        from core.logger import shutdown_logging
        logging.disable(logging.CRITICAL)
        try:
            ensure_dirs()
            assert db.DB_PATH.is_relative_to(home)
            db.init_db()
            def forbidden(*args, **kwargs):
                raise AssertionError('no external network in a measurement')
            with patch.object(socket.socket, 'connect', forbidden):
                if args.probe == 'sdk':
                    result = sdk(home)
                else:
                    with patch.object(Curl, 'perform', forbidden):
                        result = {'backup': backup, 'pdf': pdf, 'views': views}[args.probe](home)
            print(json.dumps({args.probe: result}))
        finally:
            shutdown_logging()
            logging.shutdown()


if __name__ == '__main__':
    main()
