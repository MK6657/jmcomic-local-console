"""Flask 应用入口 - 含 PID 单实例锁 + 防僵尸端口"""
import ctypes
import json
import os
import socket
import sys
import time
from pathlib import Path

# 使用 Windows 命名互斥体实现原子级单实例锁（防止双进程竞争，参见 THREAD_SAFETY_REVIEW.md P1-3）
_MUTEX_NAME = "Local\\JMComicDownloader_AppInstance"

# 确保项目根目录在 sys.path 中
# 打包后：sys._MEIPASS，开发时：项目根目录
from core.path_utils import get_resource_root, get_app_root, ensure_dirs
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
_PID_FILE = get_app_root() / "runtime" / "data" / "flask.pid"
_PORT_FILE = get_app_root() / "runtime" / "data" / "flask.json"
_DEFAULT_PORT = 5000
_FALLBACK_PORTS = [5001, 5002, 5003]


_mutex_handle = None


def _acquire_lock():
    """A live named mutex cannot be stale; protect startup before any port is bound."""
    global _mutex_handle
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    mutex = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
    error = ctypes.get_last_error()
    if not mutex:
        raise ctypes.WinError(error)
    if error == 183:
        kernel32.CloseHandle(mutex)
        raise SystemExit("Another JMComic instance is already running or starting.")
    _mutex_handle = mutex
    _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PID_FILE.write_text(str(os.getpid()))
    return mutex


def _bind_preemptive() -> socket.socket:
    """创建预绑定的 SO_REUSEADDR socket，绕过僵尸端口。

    策略：默认端口 → 3 次重试 → fallback 端口 → 报错
    返回已处于 LISTEN 状态的 socket 对象。

    注意（Windows 行为）：
    - 先用 connect() 检查端口是否真实被占，避免 SO_REUSEADDR 下
      静默绑定到另一个进程已占用的端口（Windows 上两个 REUSEADDR
      socket 可以绑定同一端口）。
    - 确认空闲后用 SO_REUSEADDR 绑定以支持快速重启（避免 TIME_WAIT）。
    """
    host = "127.0.0.1"

    def _check_port(port: int) -> bool:
        """用 connect 探测端口是否真实被占（不受 SO_REUSEADDR 影响）"""
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True  # 端口被占用
        except (ConnectionRefusedError, TimeoutError, OSError):
            return False  # 端口空闲

    # 尝试默认端口（带重试）
    for attempt in range(3):
        # 先确认端口没有被占
        if _check_port(_DEFAULT_PORT):
            log.warning(f"端口 {_DEFAULT_PORT} 被占（connect 检测），等待重试 ({attempt+1}/3)...")
            if attempt < 2:
                time.sleep(3)
                continue
            log.warning(f"端口 {_DEFAULT_PORT} connect 检测仍被占，尝试绑定以确认")
        # 用无 REUSEADDR 的 socket 做一次独占绑定检测
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind((host, _DEFAULT_PORT))
            probe.close()
        except OSError as e:
            probe.close()
            if attempt < 2:
                log.warning(f"端口 {_DEFAULT_PORT} 绑定被拒，等待重试 ({attempt+2}/3)...")
                time.sleep(3)
                continue
            else:
                log.warning(f"端口 {_DEFAULT_PORT} 最终不可用: {e}")
                break
        # 独占检测通过，用 SO_REUSEADDR 创建真正的 listener
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, _DEFAULT_PORT))
            sock.listen()
            log.info(f"绑定端口 {host}:{_DEFAULT_PORT}")
            return sock
        except OSError as e:
            sock.close()
            log.warning(f"端口 {_DEFAULT_PORT} REUSEADDR 绑定异常: {e}")
            if attempt < 2:
                time.sleep(3)
                continue
            break
    # fallback 端口
    for fb in _FALLBACK_PORTS:
        if _check_port(fb):
            log.warning(f"Fallback 端口 {fb} 也被占用，跳过")
            continue
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind((host, fb))
            probe.close()
        except OSError:
            probe.close()
            continue
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, fb))
            sock.listen()
            log.warning(f"重要：端口 {_DEFAULT_PORT} 被占，降级到 {fb}")
            return sock
        except OSError:
            sock.close()
            continue
    raise RuntimeError(f"无可用端口（尝试了 {_DEFAULT_PORT}, {_FALLBACK_PORTS} 均被占）")


def _release_lock():
    """释放锁文件和端口记录"""
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
    if _mutex_handle is not None:
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(_mutex_handle))
        _mutex_handle = None

from flask import Flask

# ── 初始化日志系统 ─────────────────────────────────────
# 导入即配置好 app.log / error.log（不删除任何文件）；过期日志清理在 main() 中启动。
from core.logger import (
    log, MAX_LOG_AGE_DAYS, MAX_TOTAL_SIZE_MB,
    adopt_logger, claim_library_loggers, install_process_hooks, start_log_maintenance,
)
log.info("=== JMComic 下载控制台 启动 ===")

# ── 先导入核心模块（确保全局实例就绪） ─────────────────────
import core.database as db
from core.job_manager import job_manager
from core.scheduler import start as start_scheduler, stop as stop_scheduler
from core import update_checker  # 检查新章节：只有 main() 启动后台线程（create_app 从不启动）

# ── jmcomic 库全局优化（必须在任何 client 创建前设置）──
from jmcomic import JmModuleConfig
# 库日志由 core.logger 接管：去掉 jmcomic 自带的 stdout handler（不再重复进 launcher.log），
# 逐请求/逐图片的常规进度被丢弃，只有重试/失败/异常经去重 + 脱敏写入 app.log / error.log。
JmModuleConfig.FLAG_ENABLE_JM_LOG = True
claim_library_loggers()  # 与导入顺序无关：若 jmcomic 先于 core.logger 被导入，这里移除它的 stdout handler
JmModuleConfig.FLAG_API_CLIENT_AUTO_UPDATE_DOMAIN = False  # 跳过启动时的域名更新请求
JmModuleConfig.FLAG_API_CLIENT_REQUIRE_COOKIES = False     # 公开内容无需 cookie，避免 /setting 请求用过期域名失败

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
    # 静态资源强缓存 1 年；配合 asset_url() 的 ?v=mtime 版本参数，
    # 文件更新后 URL 变化 → 浏览器立即拉新，未更新则命中缓存（不再每次页面加载全量重下）
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 31536000

    _static_root = Path(app.static_folder)

    @app.template_global()
    def asset_url(filename: str) -> str:
        """带版本参数的静态资源 URL：/static/<file>?v=<mtime>。

        本地应用 stat() 开销微秒级，每次请求实时取 mtime，
        保证开发/热替换 JS 后浏览器立即生效，同时享受一年强缓存。
        """
        try:
            version = int((_static_root / filename).stat().st_mtime)
        except OSError:
            version = 0
        return f"/static/{filename}?v={version}"

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
        if request.path.startswith("/api/") and request.is_json:
            if not isinstance(request.get_json(silent=True), dict):
                return jsonify(status="error", message="请求体需为 JSON 对象"), 400
        elif (request.path.startswith("/api/") and request.content_length
              and request.mimetype != "multipart/form-data"):
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
    @app.errorhandler(403)
    def _handle_403(e):
        from flask import request, jsonify, render_template
        log.warning(f"拒绝访问 path={request.path}")
        if request.path.startswith('/api/'):
            return jsonify({"status": "error", "message": "拒绝访问"}), 403
        return render_template("403.html", title="拒绝访问"), 403

    @app.errorhandler(404)
    def _handle_404(e):
        from flask import request, jsonify, render_template
        if request.path.startswith('/api/'):
            return jsonify({"status": "error", "message": "接口不存在"}), 404
        return render_template("404.html", title="页面未找到"), 404

    @app.errorhandler(500)
    def _handle_500(e):
        from flask import request, jsonify, render_template
        log.error(f"服务器内部错误 path={request.path} error={e}")
        if request.path.startswith('/api/'):
            return jsonify({"status": "error", "message": "服务器内部错误"}), 500
        return render_template("500.html", title="服务器错误"), 500

    # ── 请求日志中间件 ──────────────────────────────────
    # 成功（2xx / 304）的静态资源与 launcher 的健康检查不写访问日志，否则每次打开页面都会刷出几十行；
    # 其余请求照常记录，4xx/5xx 一律记录（5xx 为 WARNING，同时进入 error.log）。
    _quiet_paths = frozenset({"/favicon.ico", "/api/system/health"})

    @app.after_request
    def _log_request(response):
        """记录 HTTP 请求的 method、path、状态码、耗时"""
        try:
            from flask import request
            status = response.status_code
            path = request.path
            if (200 <= status < 300 or status == 304) and (
                    path.startswith("/static/") or path in _quiet_paths):
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
    return {
        "pid": os.getpid(),
        "port": port,
        "timestamp": time.time(),
        "launch_token": os.environ.get("JMCONSOLE_LAUNCH_TOKEN", ""),
        "code_fingerprint": _CODE_FINGERPRINT,
    }


def main():
    # 日志：未捕获异常（含启动失败）写入 error.log
    install_process_hooks()

    # 获取单实例锁
    try:
        _acquire_lock()
    except SystemExit as exc:
        # 另一实例仍在运行/启动中（“重启未生效”的常见原因）：写入日志，而不只是打印到 launcher.log
        log.error(f"启动中止：{exc}")
        raise

    # 清理 7 天前/超出总上限的历史日志，之后每天及每次轮转后自动清理
    start_log_maintenance()

    # 初始化数据库
    db.init_db()

    # 应用重启后，将所有 stranded 状态（running/paused）的任务回退为 queued，
    # 因为之前的下载线程已经不存在了。
    stranded = db.get_jobs_by_status("running") + db.get_jobs_by_status("paused")
    for j in stranded:
        db.update_job(j["job_id"], status="queued")
        album_id = j.get("album_id")
        if album_id:
            db.update_wishlist_download_status(album_id, "queued")
    if stranded:
        log.info(f"已回退 {len(stranded)} 个孤立任务 (running/paused) → queued")

    # ── 信号处理器：优雅关闭 ──
    def _signal_handler(signum, frame):
        log.warning(f"收到信号 {signum}，开始优雅关闭...")
        job_manager.stop()
        stop_scheduler()
        update_checker.stop()
        _release_lock()
        sys.exit(0)

    import signal
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # 启动日志 — 打印关键配置摘要
    log.info(
        f"启动配置 PORT={_DEFAULT_PORT} PID={os.getpid()} "
        f"DOWNLOAD_ROOT={db.get_setting('download_root', 'downloads/')} "
        f"MAX_LOG_AGE={MAX_LOG_AGE_DAYS}d MAX_LOG_SIZE={MAX_TOTAL_SIZE_MB}MB"
    )

    # 启动 Flask 开发服务器
    app = create_app()
    try:
        # 优先使用 waitress（生产级 WSGI，不会卡死）
        try:
            from waitress import serve
            log.info("使用 waitress 服务器")
            sock = _bind_preemptive()
            actual_port = sock.getsockname()[1]
            # Do not start/resume downloads when no server port can be acquired.
            job_manager.start()
            start_scheduler()
            # 检查新章节的后台线程（启动 5–7 分钟后才开始第一次检查；只取章节列表，从不下载）
            update_checker.start()
            # 记录实际端口
            _PORT_FILE.write_text(json.dumps(_startup_record(actual_port)))
            log.info(f"服务已启动 → http://127.0.0.1:{actual_port}")
            serve(app, sockets=[sock], threads=128)
        except ImportError:
            raise RuntimeError("运行依赖缺失，请执行 python -m pip install -r requirements.txt") from None
    finally:
        job_manager.stop()
        stop_scheduler()
        update_checker.stop()
        _release_lock()


if __name__ == "__main__":
    main()
