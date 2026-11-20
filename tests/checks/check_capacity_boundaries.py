"""Opt-in real loopback SSE/pagination check, or a disposable browser fixture."""
import argparse
from contextlib import closing
import http.client
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
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main(serve_ui=False):
    with tempfile.TemporaryDirectory(prefix="jmcomic-capacity-check-") as temporary:
        home = Path(temporary)
        for key, child in (("JMCONSOLE_HOME", ""), ("JMCONSOLE_DOWNLOAD_DIR", "downloads"),
                           ("JMCONSOLE_CACHE_DIR", "cache"), ("JMCONSOLE_BACKUP_DIR", "backups")):
            os.environ[key] = str(home / child)
        original_connect = socket.socket.connect
        def loopback(self, address):
            assert isinstance(address, tuple) and ipaddress.ip_address(address[0]).is_loopback
            return original_connect(self, address)
        socket.socket.connect = loopback
        from curl_cffi.curl import Curl
        def forbidden(*args, **kwargs):
            raise AssertionError("capacity checks do not access upstream")
        Curl.perform = forbidden
        from jmcomic import JmModuleConfig
        JmModuleConfig.DOMAIN_HTML_LIST = ["html-capacity.invalid"]
        import app
        from core import database as db
        from core.http_server import ManagedHTTPServer
        from core.progress import ProgressManager
        from routes import api_jobs
        logging.disable(logging.CRITICAL)
        application = app.create_app()
        assert db.DB_PATH.resolve().is_relative_to(home.resolve())
        # Even browser clicks can only enqueue into this disposable database.
        app.job_manager.schedule_next = lambda: None
        with closing(db.get_db()) as connection:
            with connection:
                connection.executemany("INSERT INTO jobs(job_id,album_id,title,status) VALUES(?,?,?,?)", [
                    (f"capacity_{kind}_{i:03d}", str(900000 + offset + i), f"合成任务 {kind} {i:03d}", kind)
                    for kind, count, offset in (("completed", 130, 0), ("failed", 70, 1000), ("queued", 5, 2000))
                    for i in range(count)])
        manager = ProgressManager(max_streams=2, max_streams_per_job=1)
        api_jobs.progress_manager = manager
        trackers = [manager.create_tracker(name) for name in ("capacity_one", "capacity_two", "capacity_three")]
        stop = threading.Event()
        token = uuid.uuid4().hex
        @application.post("/__capacity_test/stop/" + token)
        def stop_fixture():
            stop.set()
            return {"status": "ok"}
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        server = ManagedHTTPServer(application, listener, threads=4)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        opened = []
        def request(path):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            connection.request("GET", path)
            response = connection.getresponse()
            payload = response.read()
            connection.close()
            return response.status, json.loads(payload)
        try:
            if serve_ui:
                print(json.dumps({"url": f"http://127.0.0.1:{port}/downloads",
                                  "stop_url": f"http://127.0.0.1:{port}/__capacity_test/stop/{token}"}), flush=True)
                stop.wait(600)
            else:
                for tracker in trackers[:2]:
                    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    connection.request("GET", f"/api/jobs/{tracker.job_id}/events")
                    response = connection.getresponse()
                    assert response.status == 200
                    opened.append((connection, response))
                assert manager.active_stream_count() == 2
                status, rejected = request("/api/jobs/capacity_three/events")
                assert status == 429 and rejected["reason"] == "sse_limit"
                status, health = request("/api/system/health")
                assert status == 200 and health["status"] == "ok"
                status, page = request("/api/jobs?status=completed&page=2&page_size=50")
                assert status == 200 and page["total"] == 130 and len(page["jobs"]) == 50
                assert page["counts"]["failed"] == 70
                print(json.dumps({"http_workers": 4, "active_sse": 2, "excess_sse_status": 429,
                                  "health_while_streams_open": 200, "completed_total": 130,
                                  "page_two_rows": 50, "upstream_requests": 0}), flush=True)
        finally:
            manager.close_all()
            for connection, response in opened:
                response.close()
                connection.close()
            assert server.stop(timeout=5)
            thread.join(2)
            assert not thread.is_alive() and manager.active_stream_count() == 0
            app.shutdown_logging()
            logging.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve-ui", action="store_true")
    main(parser.parse_args().serve_ui)
