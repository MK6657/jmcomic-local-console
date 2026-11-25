"""Flask 应用入口 - 含 PID 单实例锁 + 防僵尸端口"""
import json
import os
import secrets
import socket
import sys
import threading
import time
from pathlib import Path

# 确保项目根目录在 sys.path 中
# 打包后：sys._MEIPASS，开发时：项目根目录
from core.path_utils import get_resource_root, get_data_dir, get_download_root, ensure_dirs
from core.instance_lock import DEFAULT_MUTEX_NAME, acquire_instance_lock, InstanceBusyError
from core.interface import classic_ui, render_page

_MUTEX_NAME = DEFAULT_MUTEX_NAME


def _instance_lease():
    try:
        return acquire_instance_lock(_MUTEX_NAME)
    except InstanceBusyError:
        raise SystemExit("Another JMComic instance is already running or starting.") from None


_source_lease = _instance_lease() if __name__ == "__main__" else None
_root = get_resource_root()
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

# 确保运行时目录存在（data/ downloads/ logs/ 在 exe 同级）
ensure_dirs()

# 本进程启动时的程序文件指纹（内容哈希），写入 flask.json。start.bat（launcher.py）据此判断正在运行的
# 服务是否为当前代码——覆盖复制进来的更新保留旧的创建时间、修改时间也可能更早，只比时间会漏掉。
# 在导入其余模块之前计算：若之后文件又变了，指纹不一致只会让 launcher 多重启一次，而不会漏掉更新。
try:
    from launcher import code_fingerprint as _code_fingerprint
    _CODE_FINGERPRINT = _code_fingerprint()
except Exception:
    _CODE_FINGERPRINT = ""  # 缺失时 launcher 回退为比较修改时间

# ── PID 锁文件：防止多实例冲突 ──────────────────────────────────
_PID_FILE = get_data_dir() / "flask.pid"
_PORT_FILE = get_data_dir() / "flask.json"
_DEFAULT_PORT = 5000
_FALLBACK_PORTS = [5001, 5002, 5003]


_mutex_handle = None
_shutdown_control = None


def _acquire_lock(instance_lease=None):
    """A live named mutex cannot be stale; protect startup before any port is bound."""
    global _mutex_handle
    mutex = _instance_lease() if instance_lease is None else instance_lease
    _mutex_handle = mutex
    try:
        _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        _PID_FILE.write_text(str(os.getpid()))
    except BaseException:
        mutex.close()
        _mutex_handle = None
        raise
    return mutex


def _bind_preemptive() -> socket.socket:
    """Bind the actual listener atomically; Windows must own the address exclusively."""
    host = "127.0.0.1"

    for port in (_DEFAULT_PORT, *_FALLBACK_PORTS):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            option = socket.SO_EXCLUSIVEADDRUSE if os.name == "nt" else socket.SO_REUSEADDR
            sock.setsockopt(socket.SOL_SOCKET, option, 1)
            sock.bind((host, port))
            sock.listen()
            return sock
        except OSError as exc:
            sock.close()
            log.warning(f"无法绑定 {host}:{port}: {exc}")
    raise RuntimeError(f"无可用端口（尝试了 {_DEFAULT_PORT}, {_FALLBACK_PORTS} 均被占）")


def _release_lock(*, keep_process_lock=False):
    """Clear identity records; entry shutdown keeps the OS lease until exit."""
    global _mutex_handle
    try:
        if _PID_FILE.exists():
            pid_in_file = _PID_FILE.read_text().strip()
            if pid_in_file == str(os.getpid()):
                _PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    try:
        if _PORT_FILE.exists():
            record = json.loads(_PORT_FILE.read_text())
            if record.get("pid") == os.getpid():
                _PORT_FILE.unlink()
    except Exception:
        pass
    if _mutex_handle is not None and not keep_process_lock:
        _mutex_handle.close()
        _mutex_handle = None

from flask import Flask

# ── 初始化日志系统 ─────────────────────────────────────
# 导入即配置好 app.log / error.log（不删除任何文件）；过期日志清理在 main() 中启动。
from core.logger import (
    log, MAX_LOG_AGE_DAYS, MAX_TOTAL_SIZE_MB,
    adopt_logger, install_process_hooks, start_log_maintenance,
    shutdown_logging,
)
log.info("=== JMComic 下载控制台 启动 ===")

# ── 先导入核心模块（确保全局实例就绪） ─────────────────────
import core.database as db
from core.job_manager import job_manager
from core import scheduler, update_checker
from core.runtime import ApplicationRuntime
from core.http_server import ManagedHTTPServer
from core.process_control import create_shutdown_control, SHUTDOWN_PROTOCOL
from core.progress import progress_manager
from core import background_calls, online_reader
from core.tag_jobs import tag_jobs

background_services = (("requests", background_calls), ("reader", online_reader), ("tags", tag_jobs))

# ── jmcomic 库全局优化（必须在任何 client 创建前设置）──
from core.sdk_adapter import configure_sdk
# 库日志由 core.logger 接管：去掉 jmcomic 自带的 stdout handler（不再重复进 launcher.log），
# 逐请求/逐图片的常规进度被丢弃，只有重试/失败/异常经去重 + 脱敏写入 app.log / error.log。
configure_sdk()

# ── 注册蓝图 ──────────────────────────────────────────────
from routes.page_routes import page_bp
from routes.api_search import api_search_bp
from routes.api_album import api_album_bp
from routes.api_jobs import api_jobs_bp
from routes.api_settings import api_settings_bp
from routes.api_preview import api_preview_bp
from routes.api_online import api_online_bp
from routes.api_export import api_export_bp
from routes.api_wishlist import api_wishlist_bp
from routes.api_library import api_library_bp
from routes.api_updates import api_updates_bp
from routes.api_batch_downloads import api_batch_downloads_bp
from routes.api_system import api_system_bp


def create_app() -> Flask:
    # 确保数据库已初始化（生产环境 main() 中已调用，此处为绕过 main() 的入口提供保险）
    db.init_db()

    # 在 PyInstaller 打包环境下，Flask 的模板和静态文件路径指向 sys._MEIPASS
    app = Flask(
        __name__,
        template_folder=str(_root / "templates"),
        static_folder=str(_root / "static"),
    )

    # Flask 自身的异常日志（"Exception on /path [GET]" + traceback）写入 app.log / error.log，
    # 而不是只打印到 stderr（launcher.log）
    adopt_logger(app.logger)

    # 全局限制请求体大小，防止 DoS（最大 10MB）
    app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024
    # Public UI generation, independent of the launcher shutdown token.
    app.config["SEARCH_HISTORY_SESSION"] = secrets.token_hex(16)
    # 静态资源强缓存 1 年；配合 asset_url() 的 ?v=mtime 版本参数，
    # 文件更新后 URL 变化 → 浏览器立即拉新，未更新则命中缓存（不再每次页面加载全量重下）
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 31536000

    _static_root = Path(app.static_folder)
    _asset_mtime_cache: dict[tuple[bool, str], tuple[float, str, int]] = {}
    _asset_mtime_lock = threading.Lock()

    @app.template_global()
    def asset_url(filename: str) -> str:
        """带版本参数的静态资源 URL：/static/<file>?v=<mtime>。

        2 秒短 TTL 内存缓存避免每次页面渲染对同一静态资源重复 stat()，
        经典版优先使用 static/classic/ 下的定制副本，未覆盖的公共脚本自动回退到 static/。
        """
        is_classic = bool(classic_ui())
        cache_key = (is_classic, filename)
        now = time.monotonic()
        with _asset_mtime_lock:
            cached = _asset_mtime_cache.get(cache_key)
        if cached is not None and (now - cached[0]) < 2.0:
            return f"/static/{cached[1]}?v={cached[2]}"
        resolved = filename
        if is_classic and filename.startswith(('js/', 'css/')):
            classic_candidate = 'classic/' + filename
            if (_static_root / classic_candidate).is_file():
                resolved = classic_candidate
        try:
            version = int((_static_root / resolved).stat().st_mtime)
        except OSError:
            version = 0
        with _asset_mtime_lock:
            if len(_asset_mtime_cache) > 256:
                _asset_mtime_cache.clear()
            _asset_mtime_cache[cache_key] = (now, resolved, version)
        return f"/static/{resolved}?v={version}"

    @app.context_processor
    def _inject_ui_theme() -> dict:
        """直出当前持久化的主题与动效配置（异常兜底默认），实现零闪屏渲染。"""
        from core.ui_themes import (
            DEFAULT_UI_FX_PRESET,
            DEFAULT_UI_THEME,
            DARK_THEMES,
            FX_PRESET_SPECS,
            THEME_SPECS,
            is_dark_theme,
            normalize_fx_enabled,
            normalize_fx_preset,
            normalize_theme,
            resolve_fx,
        )
        try:
            from core.settings import get_settings
            saved = get_settings()
            theme = normalize_theme(saved.get("ui_theme"))
            fx_enabled = normalize_fx_enabled(saved.get("ui_fx_enabled"))
            fx_preset = normalize_fx_preset(saved.get("ui_fx_preset"))
        except Exception:
            theme = DEFAULT_UI_THEME
            fx_enabled = True
            fx_preset = DEFAULT_UI_FX_PRESET
        return {
            "ui_theme": theme,
            "ui_themes": THEME_SPECS,
            "ui_dark_themes": DARK_THEMES,
            "ui_theme_is_dark": is_dark_theme(theme),
            "ui_fx_enabled": fx_enabled,
            "ui_fx_preset": fx_preset,
            "ui_fx_presets": FX_PRESET_SPECS,
            "ui_fx_active": resolve_fx(theme, fx_preset, fx_enabled),
        }

    # ── 请求 ID + 计时初始化（每个请求执行一次） ──
    from core.logger import set_request_id, clear_request_id
    import time as _time

    @app.teardown_request
    def _end_request_id(_exc):
        # 请求结束后本线程不再属于该请求（SSE 用 stream_with_context，生成器结束后才会走到这里）
        clear_request_id()

    @app.before_request
    def _init_request():
        from flask import request, jsonify
        from urllib.parse import urlsplit
        set_request_id()
        request._request_start_time = _time.time()
        if app.config.get("SHUTTING_DOWN"):
            response = jsonify(status="error", error_kind="stopping", message="服务正在关闭，请稍后重试")
            response.status_code = 503
            response.headers["Retry-After"] = "1"
            return response
        # Loopback binding alone does not prevent browser-origin attacks/DNS rebinding.
        try:
            host = urlsplit("http://" + request.host).hostname
            origin = request.headers.get("Origin")
            foreign_origin = origin is not None and origin != request.host_url.rstrip("/")
            if host not in {"127.0.0.1", "localhost", "::1"} or foreign_origin:
                return jsonify(status="error", message="仅允许本机同源访问"), 403
            if request.headers.get("Sec-Fetch-Site") == "cross-site":
                return jsonify(status="error", message="拒绝跨站请求"), 403
        except ValueError:
            return jsonify(status="error", message="无效主机地址"), 403
        if request.path.startswith("/api/"):
            upload_endpoint = request.path in {"/api/settings/import", "/api/wishlist/import-file"}
            if request.is_json:
                try:
                    parsed_body = request.get_json(silent=True)
                except RecursionError:
                    return jsonify(status="error", message="请求体 JSON 嵌套过深"), 400
                if not isinstance(parsed_body, dict):
                    return jsonify(status="error", message="请求体需为 JSON 对象"), 400
            elif not (upload_endpoint and request.mimetype == "multipart/form-data"):
                has_body = bool(request.content_length) or (
                    bool(request.headers.get("Transfer-Encoding")) and bool(request.get_data(cache=True))
                )
                if has_body:
                    return jsonify(status="error", message="请使用 application/json 或文件上传"), 415

    # 页面路由
    app.register_blueprint(page_bp)
    # API 路由
    app.register_blueprint(api_search_bp)
    app.register_blueprint(api_album_bp)
    app.register_blueprint(api_jobs_bp)
    app.register_blueprint(api_settings_bp)
    app.register_blueprint(api_preview_bp)
    app.register_blueprint(api_online_bp)
    app.register_blueprint(api_export_bp)
    app.register_blueprint(api_wishlist_bp)
    app.register_blueprint(api_library_bp)
    app.register_blueprint(api_updates_bp)
    app.register_blueprint(api_batch_downloads_bp)  # 批量下载：先列清单，确认后才加入下载队列
    # 系统自检
    app.register_blueprint(api_system_bp)

    # ── 全局错误处理器 ──────────────────────────────────
    @app.errorhandler(400)
    @app.errorhandler(413)
    @app.errorhandler(415)
    def _handle_request_error(e):
        from flask import request, jsonify
        if not request.path.startswith("/api/"):
            return e
        messages = {
            400: "请求格式错误，请检查请求体",
            413: "请求体超过允许大小",
            415: "请使用 application/json 或文件上传",
        }
        return jsonify(status="error", message=messages[e.code]), e.code

    @app.errorhandler(403)
    def _handle_403(e):
        from flask import request, jsonify, render_template
        log.warning(f"拒绝访问 path={request.path}")
        if request.path.startswith('/api/'):
            return jsonify({"status": "error", "message": "拒绝访问"}), 403
        return render_page("403.html", title="拒绝访问"), 403

    @app.errorhandler(404)
    def _handle_404(e):
        from flask import request, jsonify, render_template
        if request.path.startswith('/api/'):
            return jsonify({"status": "error", "message": "接口不存在"}), 404
        return render_page("404.html", title="页面未找到"), 404

    @app.errorhandler(500)
    def _handle_500(e):
        from flask import request, jsonify, render_template
        log.error(f"服务器内部错误 path={request.path} error={e}")
        if request.path.startswith('/api/'):
            return jsonify({"status": "error", "message": "服务器内部错误"}), 500
        return render_page("500.html", title="服务器错误"), 500

    # ── 请求日志中间件 ──────────────────────────────────
    # 成功（2xx / 304）的静态资源与 launcher 的健康检查不写访问日志，否则每次打开页面都会刷出几十行；
    # 其余请求照常记录，4xx/5xx 一律记录（5xx 为 WARNING，同时进入 error.log）。
    @app.after_request
    def _security_headers(response):
        # Separate from logging so quiet paths and early error responses are covered.
        from flask import request
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
        if app.config.get('ATELIER_DEMO'):
            response.headers['Content-Security-Policy'] += "; img-src 'self' blob: data:; connect-src 'self'"
        if response.mimetype == 'text/html':
            response.vary.add('Cookie')
            if request.endpoint in ('page.index', 'page.search'):
                response.headers['Cache-Control'] = 'no-store'
            else:
                response.headers.setdefault('Cache-Control', 'no-cache')
        elif response.status_code == 200 and request.path.startswith("/static/"):
            ver = request.args.get("v", "")
            if (ver and ver != "0") or request.path.startswith("/static/vendor/"):
                response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    _quiet_paths = frozenset({"/favicon.ico", "/api/system/health"})

    @app.after_request
    def _log_request(response):
        """记录 HTTP 请求的 method、path、状态码、耗时"""
        try:
            from flask import request
            status = response.status_code
            path = request.path
            if (200 <= status < 300 or status == 304) and (
                    path.startswith("/static/") or path in _quiet_paths
                    or (request.method == "HEAD" and path == "/api/search-history")):
                return response
            duration_ms = round((_time.time() - request._request_start_time) * 1000) if hasattr(request, '_request_start_time') else -1
            # response.status 带原因短语（"404 NOT FOUND"），使去重模板能区分不同结果
            (log.warning if status >= 500 else log.info)(
                f"HTTP {request.method} {path} → {response.status} ({duration_ms}ms)",
                extra={
                    "method": request.method,
                    "path": path,
                    "status": status,
                    "duration_ms": duration_ms,
                },
            )
        except Exception:
            pass
        return response

    return app


def _startup_record(port: int) -> dict:
    """runtime/data/flask.json 的内容：launcher 用它核实服务身份、决定复用还是重启。"""
    record = {
        "pid": os.getpid(),
        "port": port,
        "timestamp": time.time(),
        "launch_token": os.environ.get("JMCONSOLE_LAUNCH_TOKEN", ""),
        "code_fingerprint": _CODE_FINGERPRINT,
    }
    if _shutdown_control is not None:
        record.update(shutdown_protocol=SHUTDOWN_PROTOCOL, shutdown_token=_shutdown_control.token)
    return record


class _ShutdownRequested(BaseException):
    """Unwind serving/startup once; cleanup runs in main's single finally block."""


def main(*, instance_lease=None):
    import signal
    global _shutdown_control
    install_process_hooks()
    runtime = ApplicationRuntime(job_manager, scheduler, update_checker, logger=log,
                                 additional_services=background_services)
    application = None
    sock = None
    http = None
    acquired = False
    maintenance_started = False
    received_signal = None
    serving = False
    old_handlers = {}

    def request_shutdown(signum, frame):
        nonlocal received_signal
        if received_signal is None:
            received_signal = signum
            if serving:
                raise _ShutdownRequested()

    def check_shutdown():
        if received_signal is not None:
            raise _ShutdownRequested()

    safe = True
    try:
        try:
            _acquire_lock(instance_lease)
            acquired = True
        except SystemExit as exc:
            log.error(f"启动中止：{exc}")
            raise
        for signum in (signal.SIGINT, signal.SIGTERM):
            old_handlers[signum] = signal.signal(signum, request_shutdown)
        _shutdown_control = create_shutdown_control(os.environ.get("JMCONSOLE_LAUNCH_TOKEN"))
        check_shutdown()
        application = create_app()  # Initializes schema, never recovers or starts jobs.
        check_shutdown()
        sock = _bind_preemptive()
        actual_port = sock.getsockname()[1]
        http = ManagedHTTPServer(application, sock)
        # Acquire a listener before changing interrupted task states or starting
        # any service that can make upstream requests or execute queued downloads.
        maintenance_started = True
        start_log_maintenance()
        log.info(
            f"启动配置 PORT={actual_port} PID={os.getpid()} "
            f"DOWNLOAD_ROOT={get_download_root()} "
            f"MAX_LOG_AGE={MAX_LOG_AGE_DAYS}d MAX_LOG_SIZE={MAX_TOTAL_SIZE_MB}MB"
        )
        check_shutdown()
        runtime.start()
        check_shutdown()
        _PORT_FILE.write_text(json.dumps(_startup_record(actual_port)))
        log.info(f"服务已启动 → http://127.0.0.1:{actual_port}（waitress）")
        serving = True
        check_shutdown()
        http.run()
    except _ShutdownRequested:
        pass
    finally:
        serving = False
        shutdown_deadline = time.monotonic() + runtime.shutdown_timeout + 3
        if application is not None:
            getattr(application, "config", {})["SHUTTING_DOWN"] = True
        if received_signal is not None:
            log.warning(f"收到信号 {received_signal}，开始优雅关闭...")
        streams_closed = True
        if http is not None:
            try:
                progress_manager.close_all()
            except Exception as exc:
                streams_closed = False
                log.error(f"关闭进度流失败: {type(exc).__name__}")
        control_stopped = True
        if _shutdown_control is not None:
            try:
                control_stopped = _shutdown_control.stop()
                if control_stopped:
                    _shutdown_control = None
            except Exception:
                control_stopped = False
            if not control_stopped:
                log.error("停机控制线程未排空或关闭失败，保留实例所有权")
        try:
            safe = runtime.stop(timeout=min(runtime.shutdown_timeout,
                                            max(0, shutdown_deadline - time.monotonic())))
            safe = safe and control_stopped
        except Exception as exc:
            safe = False
            log.error(f"后台运行时关闭失败: {type(exc).__name__}")
        finally:
            http_drained = True
            if http is not None:
                try:
                    http_drained = http.stop(timeout=min(3, max(0, shutdown_deadline - time.monotonic())))
                except Exception as exc:
                    http_drained = False
                    log.error(f"HTTP服务未排空: {type(exc).__name__}")
            if not safe and control_stopped:
                try:
                    safe = runtime.stop(timeout=max(0, shutdown_deadline - time.monotonic()))
                except Exception:
                    safe = False
            # HTTP finally blocks can return the last lease after Runtime's
            # first drain. Confirm any late connection cleanup in the same
            # overall budget, including failures registered by that release.
            for name, service in reversed(background_services):
                try:
                    safe = service.stop(timeout=max(0, shutdown_deadline - time.monotonic())) and safe
                except Exception as exc:
                    safe = False
                    log.error(f"HTTP排空后的连接清理失败 component={name} error_type={type(exc).__name__}")
            safe = safe and http_drained and streams_closed
            drained = safe
            if sock is not None:
                try:
                    sock.close()
                except Exception as exc:
                    safe = False
                    log.error(f"监听端口关闭失败: {type(exc).__name__}")
            if acquired and drained:
                try:
                    # Retain the OS lease until the process really exits, even
                    # after all owned services and HTTP workers report drained.
                    _release_lock(keep_process_lock=True)
                except Exception as exc:
                    safe = False
                    log.error(f"服务启动记录清理失败: {type(exc).__name__}")
            elif acquired:
                # A live executor may still own an HTTP request/file write. Keep
                # the mutex until this process actually exits, so a replacement
                # process cannot recover and write the same task concurrently.
                log.error("后台服务未排空，保留单实例锁直到进程退出；当前端口已关闭")
            try:
                if safe and maintenance_started:
                    shutdown_logging()
            finally:
                for signum, handler in old_handlers.items():
                    signal.signal(signum, handler)
    return 0 if safe else 1


if __name__ == "__main__":
    sys.exit(main(instance_lease=_source_lease))
