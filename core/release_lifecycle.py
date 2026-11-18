"""Explicit two-process Windows lifecycle check using an isolated ordinary app.

No production instance is opened or stopped. Only a nonce-marked temporary root
may bypass the ordinary single-instance name. This module must be entered from a
fresh process; importing it alone does not initialize application paths or logging.
"""
from contextlib import closing, contextmanager
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

MARKER = ".lifecycle-check.json"
_TOKEN = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_PATH_KEYS = ("JMCONSOLE_HOME", "JMCONSOLE_DOWNLOAD_DIR", "JMCONSOLE_CACHE_DIR", "JMCONSOLE_BACKUP_DIR")
START_TIMEOUT = 35
STOP_TIMEOUT = 15


def _fresh_process():
    return not any(name in {"app", "launcher", "jmcomic_cli"} or name.startswith("routes.")
                   or (name.startswith("core.") and name != __name__) for name in sys.modules)


def _require(condition, message="lifecycle verification failed"):
    if not condition:
        raise RuntimeError(message)


def _paths(root):
    return dict(zip(_PATH_KEYS, map(str, (root, root / "downloads", root / "cache", root / "backups"))))


def _verify_root(root, token):
    """Refuse arbitrary paths before importing anything that can write an instance."""
    root = Path(root)
    _require(isinstance(token, str) and bool(_TOKEN.fullmatch(token)), "invalid lifecycle nonce")
    _require(root.is_absolute() and root.name.startswith("jmcomic-lifecycle-"), "invalid lifecycle root")
    temporary = Path(tempfile.gettempdir()).resolve()
    _require(root.parent.resolve() == temporary and root.resolve() == root, "root is not a direct temporary directory")
    for path in (root, root / MARKER):
        info = path.lstat()
        _require(not path.is_symlink() and not getattr(info, "st_file_attributes", 0) & 0x400,
                 "lifecycle root/marker cannot be a redirect")
    marker = root / MARKER
    _require(marker.stat().st_size < 4096, "invalid lifecycle marker")
    data = json.loads(marker.read_text(encoding="utf-8"))
    _require(data == {"format": 1, "nonce": token}, "lifecycle marker does not match")
    return root


@contextmanager
def _environment(root, launch_token=None):
    values = _paths(root)
    if launch_token is not None:
        values["JMCONSOLE_LAUNCH_TOKEN"] = launch_token
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _loopback(host):
    if isinstance(host, bytes):
        host = host.decode("ascii")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(str(host).split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _loopback_url(url):
    if isinstance(url, bytes):
        url = url.decode("ascii")
    parsed = urlsplit(str(url))
    _require(parsed.scheme in {"http", "https"} and _loopback(parsed.hostname)
             and parsed.username is None and parsed.password is None, "non-loopback curl request forbidden")


def _guard_network():
    """Limit Python sockets and raw libcurl to local addresses, including redirects."""
    connect, connect_ex, resolve = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo
    def checked_connect(sock, address):
        _require(isinstance(address, tuple) and _loopback(address[0]), "non-loopback socket forbidden")
        return connect(sock, address)
    def checked_connect_ex(sock, address):
        _require(isinstance(address, tuple) and _loopback(address[0]), "non-loopback socket forbidden")
        return connect_ex(sock, address)
    def checked_resolve(host, port, family=0, type=0, proto=0, flags=0):
        # Waitress validates a default wildcard listen address even when given
        # our already-bound loopback socket. Numeric passive lookups cannot
        # reach DNS; connect/connect_ex still reject these wildcard addresses.
        passive_numeric = bool(flags & socket.AI_PASSIVE) and host in {"0.0.0.0", "::"}
        _require(host is None or _loopback(host) or passive_numeric, "non-loopback DNS lookup forbidden")
        if passive_numeric:
            flags |= socket.AI_NUMERICHOST
        return resolve(host, port, family, type, proto, flags)
    socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo = checked_connect, checked_connect_ex, checked_resolve
    from curl_cffi import Curl, CurlOpt
    original_setopt, original_perform = Curl.setopt, Curl.perform
    urls = {}
    def setopt(curl, option, value):
        if option == CurlOpt.URL:
            _loopback_url(value)
            urls[id(curl)] = value
        elif option == CurlOpt.FOLLOWLOCATION and value:
            raise RuntimeError("curl redirects forbidden during lifecycle check")
        elif option == CurlOpt.PROXY and value:
            raise RuntimeError("curl proxies forbidden during lifecycle check")
        return original_setopt(curl, option, value)
    def perform(curl, *args, **kwargs):
        _require(id(curl) in urls, "curl request without checked URL forbidden")
        _loopback_url(urls[id(curl)])
        original_setopt(curl, CurlOpt.FOLLOWLOCATION, 0)
        return original_perform(curl, *args, **kwargs)
    Curl.setopt, Curl.perform = setopt, perform


def run_worker(root, token):
    """Private desktop dispatch target; validates capability before importing app."""
    _require(_fresh_process(), "lifecycle worker requires a fresh process")
    root = _verify_root(root, token)
    _require(all(os.environ.get(key) == value for key, value in _paths(root).items()),
             "lifecycle worker paths are not isolated")
    _guard_network()
    # Optional local HTML/CDN for a full download through the frozen SDK. Only
    # this nonce-checked worker reads this file; ordinary starts never do so.
    fixture = root / '.loopback-sdk-port'
    if fixture.exists():
        _require(fixture.is_file() and not fixture.is_symlink() and fixture.stat().st_size <= 5,
                 'invalid fixture port file')
        port = fixture.read_text('ascii')
        _require(port.isascii() and port.isdecimal() and 1 <= int(port) <= 65535, 'invalid fixture port')
        from jmcomic import JmModuleConfig
        from curl_cffi.requests import Session
        JmModuleConfig.DOMAIN_HTML_LIST = JmModuleConfig.DOMAIN_IMAGE_LIST = ['127.0.0.1:' + port]
        request = Session.request
        def local_request(session, *args, **kwargs):
            return request(session, *args, **dict(kwargs, allow_redirects=False))
        Session.request = local_request  # satisfy the existing no-redirect libcurl guard
    import app
    from core import database
    from core.instance_lock import maintenance_lock
    name = "Local\\JMConsoleLifecycle_" + token
    app._MUTEX_NAME = name
    database._migration_lock = lambda: maintenance_lock(name)
    return app.main()


def _worker_command(root, token):
    arguments = ["_lifecycle-worker", str(root), token]
    if getattr(sys, "frozen", False):
        return [sys.executable, *arguments]
    return [sys.executable, str(Path(__file__).resolve().parent.parent / "desktop.py"), *arguments]


def _start_worker(root, token, launch_token):
    environment = dict(os.environ, **_paths(root), JMCONSOLE_LAUNCH_TOKEN=launch_token,
                       PYTHONIOENCODING="utf-8:backslashreplace")
    if getattr(sys, "frozen", False):
        environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    # A file avoids pipe backpressure; it lives only in the disposable root and is
    # never included in a report (logs can contain local paths).
    with (root / "worker-output.log").open("ab") as output:
        process = subprocess.Popen(_worker_command(root, token), cwd=str(root), env=environment,
                                   stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, **kwargs)
    process.jm_launch_token = launch_token
    return process


def _wait_ready(process, launch_token):
    import launcher
    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        _require(process.poll() is None, "lifecycle worker exited during startup")
        # Windows venv python.exe can redirect to a child with a different PID.
        # The private token still binds its marker and matching health response
        # to this launch, while the Popen handle owns the whole invocation.
        server = launcher.get_running_server(expected_token=launch_token)
        if server is not None:
            return server
        time.sleep(0.1)
    raise TimeoutError("lifecycle worker readiness timed out")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _request(port, path, payload=None):
    _require(port in (5000, 5001, 5002, 5003) and path.startswith("/api/"), "invalid lifecycle HTTP target")
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Connection": "close"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    request = Request(f"http://127.0.0.1:{port}{path}", data=data,
                      headers=headers,
                      method="GET" if payload is None else "POST")
    with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=5) as response:
        _require(response.status == 200, "lifecycle API failed")
        result = json.loads(response.read(1024 * 1024))
    _require(result.get("status") == "ok", "lifecycle API did not confirm success")
    return result


def _stop_verified(process, port, record, root):
    import launcher
    token = getattr(process, "jm_launch_token", None)
    _require(isinstance(token, str) and record.get("launch_token") == token
             and record.get("shutdown_token") == token
             and record.get("shutdown_protocol") == "windows-event-v1"
             and isinstance(record.get("pid"), int) and record["pid"] > 0,
             "worker lacks verified graceful shutdown")
    launcher.stop_outdated_server(record["pid"], timeout=STOP_TIMEOUT, port=port, record=record)
    _require(process.wait(timeout=STOP_TIMEOUT) == 0, "lifecycle worker did not exit cleanly")
    _require(not (root / "runtime/data/flask.json").exists()
             and not (root / "runtime/data/flask.pid").exists(), "worker left service identity records")


def _cleanup_owned(process):
    if process.poll() is None:
        from core.process_control import request_shutdown
        token = getattr(process, "jm_launch_token", None)
        if isinstance(token, str) and request_shutdown(token):
            try:
                process.wait(timeout=STOP_TIMEOUT)
                return
            except subprocess.TimeoutExpired:
                pass
        if os.name == "nt":
            # A source venv redirector may have a child service. Only this
            # stored, still-live Popen PID and its owned tree may be reaped.
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
        else:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    _require(process.poll() is not None, "owned lifecycle worker is still alive")


def _seed_recovery(root):
    database = root / "runtime/data/app.db"
    with closing(sqlite3.connect(database)) as connection, connection:
        for job_id, album, state in (("lifecycle_paused", "900001", "paused"),
                                     ("lifecycle_running", "900002", "running")):
            connection.execute("INSERT INTO jobs (job_id,album_id,title,selected_photo_ids,status) VALUES (?,?,?,?,?)",
                               (job_id, album, "Synthetic lifecycle fixture", "[]", state))


def _check_recovery(root):
    with closing(sqlite3.connect((root / "runtime/data/app.db").as_uri() + "?mode=ro", uri=True)) as connection:
        rows = dict(connection.execute("SELECT job_id,status FROM jobs WHERE job_id LIKE 'lifecycle_%'"))
    _require(rows == {"lifecycle_paused": "paused", "lifecycle_running": "queued"}, "task recovery semantics changed")
    _require(not any((root / "downloads").iterdir()), "lifecycle check unexpectedly produced downloads")


def _run_supervisor(root, token, processes, checks):
    patch = {"timeout": "47", "retry_times": "0", "skip_existing": False, "auto_update_check": False,
             "schedule_enabled": True, "schedule_start": "0", "schedule_end": "0"}
    first_token = secrets.token_hex(16)
    first = _start_worker(root, token, first_token)
    processes.append(first)
    port, record = _wait_ready(first, first_token)
    _request(port, "/api/system/health")
    result = _request(port, "/api/settings", patch)
    _require(result["settings"]["timeout"] == "47" and result["settings"]["retry_times"] == "0")
    checks["real_app_settings_save"] = True
    _stop_verified(first, port, record, root)
    checks["graceful_exit_and_identity_cleanup"] = True
    _seed_recovery(root)
    second_token = secrets.token_hex(16)
    second = _start_worker(root, token, second_token)
    processes.append(second)
    port, record = _wait_ready(second, second_token)
    settings = _request(port, "/api/settings")["settings"]
    _require(settings["timeout"] == "47" and settings["retry_times"] == "0"
             and settings["auto_update_check"] == "false" and settings["schedule_enabled"] == "true")
    _check_recovery(root)
    checks["restart_persistence_and_task_recovery"] = True
    _stop_verified(second, port, record, root)


def run_lifecycle_check():
    """Return a compact report only after every owned worker actually exits."""
    checks, processes = {}, []
    if not _fresh_process():
        return {"status": "error", "exit_code": 1, "checks": checks, "error": "fresh process required"}
    temporary = tempfile.TemporaryDirectory(prefix="jmcomic-lifecycle-")
    root = Path(temporary.name).resolve()
    token = secrets.token_hex(32)
    try:
        marker = root / MARKER
        marker.write_text(json.dumps({"format": 1, "nonce": token}), encoding="utf-8")
        _verify_root(root, token)
        with _environment(root):
            # Import launcher only after paths are pinned to this disposable root.
            _run_supervisor(root, token, processes, checks)
        for process in processes:
            _require(process.poll() == 0, "worker process did not exit successfully")
        temporary.cleanup()
        checks["temporary_cleanup"] = True
        return {"status": "ok", "exit_code": 0, "checks": checks, "upstream_requests": 0}
    except BaseException as error:
        cleanup_failed = False
        for process in processes:
            try:
                _cleanup_owned(process)
            except BaseException:
                cleanup_failed = True
        if not cleanup_failed:
            try:
                temporary.cleanup()
            except BaseException:
                cleanup_failed = True
        if cleanup_failed:
            # Do not remove a live child's files. Detach automatic cleanup; the
            # generic error report intentionally does not disclose the temp path.
            temporary._finalizer.detach()
        return {"status": "error", "exit_code": 1, "checks": checks,
                "error": "cleanup failed" if cleanup_failed else type(error).__name__}
