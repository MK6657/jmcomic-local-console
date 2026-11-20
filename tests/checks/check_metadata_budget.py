"""Opt-in loopback libcurl/SDK retry proof for metadata's shared total deadline."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import logging
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main():
    with tempfile.TemporaryDirectory(prefix="jmcomic-metadata-budget-") as temporary:
        home = Path(temporary)
        for name, child in (("JMCONSOLE_HOME", ""), ("JMCONSOLE_DOWNLOAD_DIR", "downloads"),
                            ("JMCONSOLE_CACHE_DIR", "cache"), ("JMCONSOLE_BACKUP_DIR", "backups")):
            os.environ[name] = str(home / child)
        original_connect = socket.socket.connect
        def local_connect(sock, address):
            assert isinstance(address, tuple) and ipaddress.ip_address(address[0]).is_loopback
            return original_connect(sock, address)
        socket.socket.connect = local_connect
        from jmcomic import JmModuleConfig
        JmModuleConfig.DOMAIN_HTML_LIST = ["html-budget.invalid"]
        import app
        from core import jm_service, background_calls
        from core.settings import update_settings
        from core.upstream import UpstreamTimeout
        logging.disable(logging.CRITICAL)
        app.create_app()
        update_settings({"client_type": "api", "retry_times": 2, "timeout": 5})
        release = threading.Event()
        counts = {"/slow": 0, "/ok": 0}
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                assert self.path in counts
                counts[self.path] += 1
                status = 500 if self.path == "/slow" and counts[self.path] == 1 else 200
                self.send_response(status)
                self.send_header("Content-Length", "2" if status == 200 else "0")
                self.end_headers()
                if status == 200:
                    if self.path == "/slow":
                        release.wait(3)
                    try:
                        self.wfile.write(b"ok")
                    except OSError:
                        pass
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = None
        try:
            client, _ = jm_service.get_client()
            root = client.get_root_postman()
            root.session.trust_env, root.session.proxies = False, {}
            for method in ("get", "post"):
                original = getattr(root, method)
                def request(url, *args, _original=original, **kwargs):
                    assert url.startswith(f"http://127.0.0.1:{server.server_port}/")
                    return _original(url, *args, allow_redirects=False, **kwargs)
                setattr(root, method, request)
            def require_ok(response, is_image):
                if response.status_code >= 400:
                    raise ConnectionError("synthetic upstream 500")
                return response
            client.raise_if_resp_should_retry = require_ok
            started = time.monotonic()
            try:
                jm_service._call_with_timeout("loopback", 0.4, "fixture deadline", client.get,
                    f"http://127.0.0.1:{server.server_port}/slow")
            except UpstreamTimeout:
                pass
            else:
                raise AssertionError("stalled metadata request did not time out")
            elapsed = time.monotonic() - started
            assert elapsed < 1.5 and counts["/slow"] == 2
            response = jm_service._call_with_timeout("loopback", 1, "fixture deadline", client.get,
                f"http://127.0.0.1:{server.server_port}/ok")
            assert response.status_code == 200 and response.content == b"ok"
            print(json.dumps({"shared_deadline_seconds": 0.4, "elapsed_seconds": round(elapsed, 3),
                              "slow_requests": counts["/slow"], "next_request_status": response.status_code,
                              "upstream_requests": 0}), flush=True)
        finally:
            release.set()
            if client is not None:
                jm_service.close_client(client)
            assert background_calls.stop(3)
            server.shutdown()
            server.server_close()
            thread.join(2)
            app.shutdown_logging()
            logging.shutdown()


if __name__ == "__main__":
    main()
