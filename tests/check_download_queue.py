"""Disposable loopback regression: real libcurl + SDK image decoding, no upstream."""
import argparse
from collections import Counter
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit

parser = argparse.ArgumentParser(description=__doc__)
scenario = parser.add_mutually_exclusive_group()
scenario.add_argument("--two-stalls", action="store_true", help="stall both bad pages instead of returning corrupt bytes for page 3")
scenario.add_argument("--pause", action="store_true", help="resume only after the waiting album finishes")
scenario.add_argument("--quick-resume", action="store_true", help="resume while the second page is still in flight")
args = parser.parse_args()
pause_run = args.pause or args.quick_resume
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
temporary = tempfile.TemporaryDirectory(prefix="jm-stalled-queue-")
for name, relative in (("JMCONSOLE_HOME", ""), ("JMCONSOLE_DOWNLOAD_DIR", "downloads"),
                       ("JMCONSOLE_CACHE_DIR", "runtime/cache"), ("JMCONSOLE_BACKUP_DIR", "runtime/backups")):
    os.environ[name] = str(Path(temporary.name) / relative)
import app
from core import database as db, jm_service, job_manager as job_module, settings, image_download
from core.sdk_adapter import build_option, new_client
from jmcomic import JmModuleConfig
from PIL import Image
logging.disable(logging.CRITICAL)
assert db.DB_PATH.resolve().is_relative_to(Path(temporary.name).resolve())
assert jm_service.DOWNLOAD_ROOT.resolve().is_relative_to(Path(temporary.name).resolve())
app.create_app()
settings.update_settings({"timeout": 5, "retry_times": 0, "image_threads": 1,
                          "photo_threads": 1, "max_running_jobs": 1, "auto_update_check": False,
                          "skip_existing": not pause_run})
payload = io.BytesIO()
Image.new("RGB", (16, 16), "blue").save(payload, "PNG")
payload = payload.getvalue()
release = threading.Event()
inflight = threading.Event()
requests = []
slow_path = "/media/photos/101/00001.png"
third_path = "/media/photos/101/00003.png"
second_path = "/media/photos/101/00002.png"
next_path = "/media/photos/102/00001.png"
faults = {second_path: "hold"} if pause_run else {slow_path: "stall", third_path: "stall" if args.two_stalls else "corrupt"}
stalled = []
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        requests.append(self.path)
        fault = faults.get(self.path)
        body = b"not an image" if fault == "corrupt" else payload
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if fault == "hold":
            inflight.set()
            assert release.wait(10)
        if fault == "stall":
            stalled.append(self.path)
            release.wait()
            return
        self.wfile.write(body)
    def log_message(self, *args):
        pass
server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
server.daemon_threads = True
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
host = f"127.0.0.1:{server.server_port}"
JmModuleConfig.DOMAIN_IMAGE_LIST = [host]
image_download._good_host = None
image_download._failed_hosts = {}
class Page:
    is_gif, scramble_id = False, None
    def __init__(self, album_id, name):
        self.download_url = f"http://{host}/media/photos/{album_id}/{name}"
class Photo(list):
    name = "Chapter"
    def __init__(self, album_id):
        self.photo_id = album_id
        self.page_arr = ["00001.png", "00002.png", "00003.png"] if album_id == "101" else ["00001.png"]
        super().__init__(Page(album_id, name) for name in self.page_arr)
class Album(list):
    name, author, tags = "Loopback fixture", "Fixture", []
    def __init__(self, album_id):
        super().__init__([Photo(album_id)])
        self.album_id = album_id
option = build_option()
def get_client(shared=True):
    assert not shared
    client = new_client(option, domain_list=[host])
    root = client.get_root_postman()
    root.session.trust_env = False
    def local_only(method):
        def request(url, *args, **kwargs):
            parts = urlsplit(url)
            assert (parts.scheme, parts.netloc) == ("http", host), "Smoke requests must stay on the loopback fixture"
            kwargs.update(proxy="", allow_redirects=False)
            return method(url, *args, **kwargs)
        return request
    root.get, root.post = local_only(root.get), local_only(root.post)
    client.get_album_detail = lambda album_id: Album(album_id)
    client.check_photo = lambda photo: None
    return client, option
jm_service.get_client = get_client
manager = job_module.JobManager()
job_module.job_manager = manager
bad = manager.create_job("101", "Stalled", [])
good = manager.create_job("102", "Healthy", [])


def wait_for(predicate):
    limit = start + 30
    while time.monotonic() < limit:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("loopback queue did not reach the expected state")


start = time.monotonic()
try:
    manager.start()
    if pause_run:
        assert inflight.wait(5)
        partial = db.get_job(bad)
        assert partial["done_pages"] == 1
        saved, = Path(partial["output_path"]).rglob("00001.webp")
        digest = hashlib.sha256(saved.read_bytes()).hexdigest()
        assert manager.pause_job(bad)[0]
        if args.quick_resume:
            assert manager.resume_job(bad)[0] and db.get_job(bad)["status"] == "queued"
        manager.schedule_next()
        assert manager.get_running_count() == 1 and db.get_job(good)["status"] == "queued"
        assert requests == [slow_path, second_path]
        release.set()
        if not args.quick_resume:
            wait_for(lambda: db.get_job(good)["status"] == "completed" and manager.get_running_count() == 0)
            partial = db.get_job(bad)
            assert (partial["status"], partial["done_pages"]) == ("paused", 2)
            assert manager.resume_job(bad)[0]
        first_status = "completed"
    else:
        first_status = "failed"
    wait_for(lambda: db.get_job(bad)["status"] == first_status and db.get_job(good)["status"] == "completed"
             and manager.get_running_count() == 0)
    first, second = db.get_job(bad), db.get_job(good)
    if pause_run:
        assert requests == [slow_path, second_path, next_path, third_path], requests
        final_page, = Path(first['output_path']).rglob('00001.webp')
        assert hashlib.sha256(final_page.read_bytes()).hexdigest() == digest
        assert len(db.get_all_jobs()) == 2 and first["done_pages"] == first["total_pages"] == 3
        expected_first = ["00001.webp", "00002.webp", "00003.webp"]
    else:
        assert requests == [slow_path, second_path, third_path, next_path], requests
        assert stalled == ([slow_path, third_path] if args.two_stalls else [slow_path])
        assert (first["done_pages"], first["total_pages"]) == (1, 3)
        assert all(f"第 {page} 页下载失败" in first["error_message"] for page in (1, 3))
        expected_first = ["00002.webp"]
    assert second["done_pages"] == second["total_pages"] == 1
    for job, expected in ((first, expected_first), (second, ["00001.webp"])):
        images = sorted(Path(job["output_path"]).rglob("*.webp"))
        assert [path.name for path in images] == expected
        for path in images:
            with Image.open(path) as image:
                assert image.format == "WEBP"
                image.load()
    mode = "quick_resume" if args.quick_resume else "pause" if args.pause else "two_stalls" if args.two_stalls else "stall_and_corrupt"
    print(json.dumps({"scenario": mode, ("first_status" if pause_run else "bad_status"): first["status"],
                      "good_status": second["status"],
                      "seconds": round(time.monotonic()-start, 3),
                      "requests_per_page": dict(Counter(requests)), "request_order": requests, "stalled_pages": stalled,
                      "remaining_executors": manager.get_running_count(),
                      "verified_images": len(expected_first) + 1, "upstream_requests": 0}))
finally:
    release.set()
    assert manager.stop(timeout=3)
    server.shutdown()
    server.server_close()
    thread.join(2)
    for handler in getattr(sys.modules.get("core.logger"), "_shared_handlers", []):
        handler.close()
    temporary.cleanup()
