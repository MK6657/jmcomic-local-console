"""Disposable loopback regression: real libcurl + SDK image decoding, no upstream."""
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
app.create_app()
settings.update_settings({"timeout": 5, "retry_times": 0, "image_threads": 1,
                          "photo_threads": 1, "auto_update_check": False})
payload = io.BytesIO()
Image.new("RGB", (16, 16), "blue").save(payload, "PNG")
payload = payload.getvalue()
release = threading.Event()
requests = []
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        requests.append(self.path)
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if "/bad/" in self.path:
            release.wait(20)
            return
        self.wfile.write(payload)
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
    def __init__(self, album_id):
        self.download_url = f"http://{host}/media/photos/{'bad' if album_id=='101' else 'good'}/00001.png"
class Photo(list):
    name = "Chapter"
    def __init__(self, album_id):
        super().__init__([Page(album_id)])
        self.photo_id, self.page_arr = album_id, [1]
class Album(list):
    name, author, tags = "Loopback fixture", "Fixture", []
    def __init__(self, album_id):
        super().__init__([Photo(album_id)])
        self.album_id = album_id
option = build_option()
def get_client(shared=True):
    client = new_client(option)
    root = client.get_root_postman()
    def local_only(method):
        def request(url, *args, **kwargs):
            assert urlsplit(url).netloc == host, "Smoke requests must stay on the loopback fixture"
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
start = time.monotonic()
manager.schedule_next()
first_status = None
try:
    limit = start + 16
    while time.monotonic() < limit:
        manager.schedule_next()
        first_status, second_status = db.get_job(bad)["status"], db.get_job(good)["status"]
        if first_status == "failed" and second_status == "completed" and manager.get_running_count() == 0:
            break
        time.sleep(0.05)
    else:
        raise AssertionError("stalled queue did not advance")
    good_path = Path(db.get_job(good)["output_path"])
    images = list(good_path.rglob("*.webp"))
    assert len(images) == 1
    with Image.open(images[0]) as image:
        image.verify()
    print(json.dumps({"bad_status": first_status, "good_status": second_status,
                      "seconds": round(time.monotonic()-start, 3),
                      "bad_requests": sum('/bad/' in path for path in requests),
                      "good_requests": sum('/good/' in path for path in requests),
                      "remaining_executors": manager.get_running_count(),
                      "image_verified": True, "upstream_requests": 0}))
finally:
    release.set()
    assert manager.stop(timeout=3)
    server.shutdown()
    server.server_close()
    thread.join(2)
    for handler in getattr(sys.modules.get("core.logger"), "_shared_handlers", []):
        handler.close()
    temporary.cleanup()
