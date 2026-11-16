"""Explicit portable-build smoke check, isolated from every everyday instance.

Call this once from a fresh process before importing application/path modules.
The desktop entry point prints the returned summary and exits with exit_code.
No scheduler, production instance lock or upstream request is started here.
"""
from contextlib import contextmanager
import io
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import threading
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

_PATH_KEYS = ("JMCONSOLE_HOME", "JMCONSOLE_DOWNLOAD_DIR", "JMCONSOLE_CACHE_DIR", "JMCONSOLE_BACKUP_DIR")


class _CleanupError(RuntimeError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _application_already_loaded() -> bool:
    return any(name == "app" or name.startswith("routes.")
               or (name.startswith("core.") and name != __name__)
               for name in sys.modules)


@contextmanager
def _temporary_environment(root: Path):
    previous = {name: os.environ.get(name) for name in _PATH_KEYS}
    configured = dict(zip(_PATH_KEYS, map(str, (root, root / "downloads", root / "cache", root / "backups"))))
    os.environ.update(configured)
    try:
        yield configured
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _loopback_url(url, port):
    parsed = urlsplit(str(url))
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.port != port or parsed.username is not None or parsed.password is not None):
        raise RuntimeError("self-check allows only its own loopback listener")


def _guard_transport(root, port):
    """Guard the SDK root so neither retries nor redirects reach an upstream."""
    for method in ("get", "post"):
        original = getattr(root, method)
        def guarded(url, *args, _original=original, **kwargs):
            _loopback_url(url, port)
            kwargs["allow_redirects"] = False
            return _original(url, *args, **kwargs)
        setattr(root, method, guarded)


def _require(condition):
    if not condition:
        raise AssertionError("self-check verification failed")


def _image_checks():
    from PIL import Image
    import img2pdf
    image = Image.new("RGB", (8, 8), "white")
    png, webp, gif = io.BytesIO(), io.BytesIO(), io.BytesIO()
    image.save(png, "PNG")
    image.save(webp, "WEBP")
    with Image.open(io.BytesIO(webp.getvalue())) as opened:
        opened.verify()
        _require(opened.format == "WEBP")
    image.save(gif, "GIF", save_all=True, append_images=[Image.new("RGB", (8, 8), "black")],
               duration=80, loop=0)
    with Image.open(io.BytesIO(gif.getvalue())) as opened:
        _require(opened.n_frames == 2)
        opened.seek(1)
        opened.load()
    _require(img2pdf.convert(png.getvalue()).startswith(b"%PDF-"))
    return png.getvalue()


def _dependency_checks():
    import certifi
    from Crypto.Cipher import AES
    certificate = Path(certifi.where())
    _require(certificate.is_file() and certificate.stat().st_size > 0)
    key, block = bytes(range(16)), bytes(reversed(range(16)))
    encrypted = AES.new(key, AES.MODE_ECB).encrypt(block)
    _require(encrypted != block and AES.new(key, AES.MODE_ECB).decrypt(encrypted) == block)


def _close_application_logging():
    module = sys.modules.get("core.logger")
    if module is None:
        return
    housekeeper = getattr(module, "_housekeeper", None)
    thread = getattr(housekeeper, "_thread", None)
    actions = [module.shutdown_logging,
               lambda: _require(thread is None or not thread.is_alive())]
    loggers = [logging.getLogger()] + [logger for logger in logging.Logger.manager.loggerDict.values()
                                       if isinstance(logger, logging.Logger)]
    def detach(handler):
        for logger in loggers:
            logger.removeHandler(handler)
    for handler in getattr(module, "_shared_handlers", ()):
        actions.extend((lambda handler=handler: detach(handler), handler.flush, handler.close))
    _cleanup(actions)


def _cleanup(actions):
    """Attempt every release even if an earlier release failed."""
    errors = []
    for action in actions:
        try:
            action()
        except Exception as error:
            errors.append(error)
    if errors:
        raise _CleanupError("self-check cleanup failed") from errors[0]


def _close_channels(socket_map):
    _cleanup([channel.close for channel in list(socket_map.values())]
             + [lambda: _require(not socket_map)])


def _cleanup_runtime(client, server, thread, socket_map, server_errors):
    actions = []
    if client is not None:
        # This client belongs only to this check. Do not use the application
        # helper here: it deliberately logs/swallows session-close failures.
        actions.append(lambda: client.get_root_postman().session.close())
    service = sys.modules.get("core.jm_service")
    reader = sys.modules.get("core.online_reader")
    if service is not None:
        actions.append(service.invalidate_option_cache)
    if reader is not None:
        actions.append(reader.clear_cache)
    if server is not None:
        actions.append(lambda: server.task_dispatcher.shutdown(timeout=3))
    actions.append(lambda: _close_channels(socket_map))
    if thread is not None:
        actions.extend((lambda: thread.join(timeout=3), lambda: _require(not thread.is_alive())))
    if server is not None:
        actions.append(lambda: _require(not server.task_dispatcher.threads))
    actions.extend((lambda: _require(not server_errors), _close_application_logging))
    _cleanup(actions)


def _run_checks(root: Path, checks: dict) -> None:
    """Imports intentionally live here, after instance isolation is installed."""
    server = thread = client = None
    socket_map, server_errors = {}, []
    try:
        from app import create_app
        from flask import Response
        from waitress import create_server
        from PIL import Image
        from jmcomic import JmModuleConfig
        from . import database, image_download, jm_service, path_utils, sdk_adapter

        config = path_utils.get_path_config()
        _require(all(path.is_relative_to(root) for path in (
            config.home, config.data, config.logs, config.downloads, config.cache, config.backups)))
        checks["isolated_paths"] = True
        payload = _image_checks()
        checks["pillow_webp_gif_pdf"] = True
        _dependency_checks()
        checks["crypto_ca_bundle"] = True
        application = create_app()
        _require(Path(database.DB_PATH).is_relative_to(root))

        @application.get("/__release_check__/image.png")
        def release_check_image():
            return Response(payload, mimetype="image/png")

        server = create_server(application, host="127.0.0.1", port=0, threads=2,
                               map=socket_map, asyncore_loop_timeout=0.1, channel_timeout=5)
        port = int(server.effective_port)
        def serve():
            try:
                server.run()
            except BaseException as error:
                server_errors.append(type(error).__name__)
        thread = threading.Thread(target=serve, name="release-check-http", daemon=True)
        thread.start()
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        def fetch(path):
            with opener.open(Request(f"http://127.0.0.1:{port}{path}", headers={"Connection": "close"}), timeout=5) as response:
                _require(response.status == 200)
                return response.read(4 * 1024 * 1024)
        for path in ("/", "/settings"):
            _require(b"JMComic" in fetch(path))
        for path in ("/static/css/style.css", "/static/js/settings.js",
                     "/static/vendor/bootstrap/css/bootstrap.min.css",
                     "/static/vendor/bootstrap-icons/fonts/bootstrap-icons.woff2"):
            _require(len(fetch(path)) > 100)
        health = json.loads(fetch("/api/system/health"))
        _require(health["status"] == "ok" and health["application"] == "jmcomic-local-console"
                 and health["pid"] == os.getpid())
        settings = json.loads(fetch("/api/settings"))
        _require(settings["status"] == "ok" and Path(settings["settings"]["download_root"]) == config.downloads)
        patch = {"timeout": "47", "retry_times": "0", "schedule_start": "0", "auto_update_check": "false"}
        save = Request(f"http://127.0.0.1:{port}/api/settings", data=json.dumps(patch).encode(),
                       headers={"Content-Type": "application/json", "Connection": "close"}, method="POST")
        with opener.open(save, timeout=5) as response:
            saved = json.load(response)
            _require(response.status == 200 and saved["status"] == "ok" and saved["saved"] is True)
        loaded = json.loads(fetch("/api/settings"))["settings"]
        _require(all(loaded[key] == value and database.get_setting(key) == value for key, value in patch.items()))
        checks["settings_save_readback"] = True
        checks["waitress_pages_assets_api"] = True
        local = f"127.0.0.1:{port}"
        JmModuleConfig.DOMAIN_API_LIST = [local]
        JmModuleConfig.DOMAIN_HTML_LIST = [local]
        JmModuleConfig.DOMAIN_IMAGE_LIST = [local]
        option = sdk_adapter.build_option()
        client = sdk_adapter.new_client(option, domain_list=[local], proxies={}, trust_env=False, timeout=5)
        _guard_transport(client.get_root_postman(), port)
        destination = config.downloads / "self-check.webp"
        image_download.download_image(client, img_url=f"http://{local}/__release_check__/image.png",
            img_save_path=str(destination), scramble_id=None, decode_image=False, timeout=5, attempts=1)
        with Image.open(destination) as converted:
            _require(converted.format == "WEBP" and converted.size == (8, 8))
            converted.verify()
        _require(not server_errors)
        _require(jm_service.get_active_client_count() == 0)
        from .job_manager import job_manager
        from . import scheduler, update_checker
        _require(job_manager._scheduler_thread is None and job_manager.get_running_count() == 0
                 and scheduler._scheduler_thread is None and not update_checker.runtime_status()["running"])
        _require(sys.modules["app"]._mutex_handle is None
                 and not sys.modules["core.instance_lock"]._owned)
        checks["sdk_curl_image_conversion"] = True
        checks["no_task_scheduler"] = True
    finally:
        _cleanup_runtime(client, server, thread, socket_map, server_errors)


def run_self_check() -> dict:
    """Return a compact JSON-safe report; callers must exit after this command.

    Exit code 1 includes cleanup failure. Success means the current package passed
    local checks, not that an upstream service or real content download was tested.
    """
    checks = {}
    if _application_already_loaded():
        return {"status": "error", "exit_code": 1, "checks": checks,
                "error": "self-check requires a fresh process before application imports"}
    stage = "isolation"
    try:
        with tempfile.TemporaryDirectory(prefix="jmcomic-release-check-") as temporary:
            with _temporary_environment(Path(temporary)):
                stage = "local_checks"
                _run_checks(Path(temporary), checks)
        checks["cleanup"] = True
        return {"status": "ok", "exit_code": 0, "checks": checks, "upstream_requests": 0}
    except Exception as error:
        return {"status": "error", "exit_code": 1, "checks": checks,
                "error": f"{stage}: {type(error).__name__}"}
