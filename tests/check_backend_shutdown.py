"""Explicit loopback proof of reader/shared-call shutdown; no upstream content."""
from concurrent.futures import ThreadPoolExecutor
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
from types import SimpleNamespace
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def check():
    with tempfile.TemporaryDirectory(prefix="jmcomic-backend-drain-") as temporary:
        root_path = Path(temporary)
        for name, relative in (("JMCONSOLE_HOME", ""), ("JMCONSOLE_DOWNLOAD_DIR", "downloads"),
                               ("JMCONSOLE_CACHE_DIR", "cache"), ("JMCONSOLE_BACKUP_DIR", "backups")):
            os.environ[name] = str(root_path / relative)
        import app
        from core import background_calls, jm_service, online_reader as reader, settings
        from core.runtime import ApplicationRuntime
        from PIL import Image
        logging.disable(logging.CRITICAL)
        app.create_app()
        settings.update_settings({"timeout": "5", "auto_update_check": False})
        output = io.BytesIO()
        Image.new("RGB", (8, 8), "white").save(output, "PNG")
        payload = output.getvalue()
        release = threading.Event()
        entered = {path: threading.Event() for path in ("/reader.png", "/metadata")}
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                assert self.path in entered
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                entered[self.path].set()
                if release.wait(8):
                    try:
                        self.wfile.write(payload)
                    except OSError:
                        pass
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        host = f"127.0.0.1:{server.server_port}"
        state_lock = threading.Lock()
        active, closed = set(), []
        def guard(client, owner):
            transport = client.get_root_postman()
            transport.session.trust_env = False
            transport.session.proxies = {}
            for name in ("get", "post"):
                original = getattr(transport, name)
                def request(url, *args, _original=original, **kwargs):
                    assert urlsplit(url).netloc == host, "smoke requests must remain on loopback fixture"
                    kwargs["allow_redirects"] = False
                    with state_lock:
                        active.add(owner)
                    try:
                        return _original(url, *args, **kwargs)
                    finally:
                        with state_lock:
                            active.remove(owner)
                setattr(transport, name, request)
            close = transport.session.close
            def close_owned():
                with state_lock:
                    assert owner not in active, "cannot close an active curl request"
                    closed.append(owner)
                return close()
            transport.session.close = close_owned
            return client
        new_image_client = jm_service.new_image_client
        jm_service.new_image_client = lambda: guard(new_image_client(), "reader")
        reader._image_hosts = lambda: [host]
        class Photo(list):
            photo_id, name, page_arr = "900001", "Synthetic", ["1"]
        photo = Photo([SimpleNamespace(download_url=f"http://{host}/reader.png", scramble_id=None, is_gif=False)])
        runtime = ApplicationRuntime(app.job_manager, app.scheduler, app.update_checker,
                                     additional_services=app.background_services)
        pool = ThreadPoolExecutor(max_workers=1)
        reader_errors = []
        def fetch():
            try:
                reader.image_file(photo.photo_id, 0)
            except Exception as error:
                reader_errors.append(type(error).__name__)
        reader_thread = threading.Thread(target=fetch, name="reader-drain-fixture", daemon=True)
        try:
            runtime.start()
            reader._remember_photos([photo])
            shared, _ = jm_service.get_client()
            guard(shared, "metadata")
            future = jm_service._submit_client_call(pool, shared, shared.get_root_postman().get,
                                                    f"http://{host}/metadata", timeout=5)
            jm_service.close_client(shared)
            reader_thread.start()
            assert all(event.wait(3) for event in entered.values())
            begin = time.monotonic()
            assert runtime.stop(timeout=0.05) is False
            elapsed = time.monotonic() - begin
            assert elapsed < 0.5
            with state_lock:
                assert active == {"reader", "metadata"} and not closed
            assert not (root_path / "cache/online/900001/00001.webp").exists()
            release.set()
            reader_thread.join(6)
            assert not reader_thread.is_alive() and reader_errors == ["TimeoutError"]
            future.result(timeout=6)
            assert runtime.stop(timeout=3) is True
            assert reader.stop(timeout=0) is True
            assert background_calls.stop(timeout=1) is True
            assert not (root_path / "cache/online/900001/00001.webp").exists()
            with state_lock:
                assert not active and sorted(closed) == ["metadata", "reader"]
            result = {"first_stop_drained": False, "first_stop_seconds": round(elapsed, 3),
                      "inflight_clients_retained": True, "final_stop_drained": True,
                      "clients_closed_after_request_exit": True, "stopped_cache_not_published": True,
                      "upstream_requests": 0}
        finally:
            release.set()
            if reader_thread.ident is not None:
                reader_thread.join(6)
            pool.shutdown(wait=True)
            try:
                drained = runtime.stop(timeout=3)
            finally:
                server.shutdown()
                server.server_close()
                server_thread.join(2)
                from core import logger
                logger.shutdown_logging()
                for handler in logger._shared_handlers:
                    handler.close()
            assert drained
        return result


if __name__ == "__main__":
    print(json.dumps(check()))
