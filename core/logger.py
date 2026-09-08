"""
日志系统模块 — 完整版
======================
特性：双文件轮转 + request_id 追踪 + 重复压缩 + 结构化 JSON payload + 自动清理

文件布局：
  logs/
  ├── app.log              当前日志（INFO+）
  ├── app.log.2026-07-02   历史日志（按天轮转）
  ├── error.log            当前错误日志（WARNING+）
  ├── error.log.2026-07-02 历史错误日志
  └── app.legacy.log       升级前旧日志（自动迁移）
"""
import json
import logging
import os
import random
import re
import string
import threading
import time
from collections import OrderedDict
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from .path_utils import get_app_root

# ── 路径 ──────────────────────────────────────────────
LOG_DIR = get_app_root() / "runtime" / "logs"
LOG_FILE = LOG_DIR / "app.log"
ERROR_LOG_FILE = LOG_DIR / "error.log"

# ── 配置（可通过 settings 表覆盖） ────────────────────
MAX_LOG_AGE_DAYS = 30       # 保留 30 天
MAX_TOTAL_SIZE_MB = 100     # 总上限 100MB

# ── 敏感信息脱敏 ─────────────────────────────────────
SENSITIVE_KEYS = {
    "cookie", "token", "password", "secret", "authorization",
    "proxy_password", "apikey", "api_key", "private_key",
    "access_key", "refresh_token", "credential",
    "session", "jwt", "bearer",
}


def sanitize(msg: str) -> str:
    """脱敏处理：替换日志中的敏感信息"""
    if not msg:
        return msg
    sorted_keys = sorted(SENSITIVE_KEYS, key=len, reverse=True)
    for key in sorted_keys:
        placeholder = key.upper() if key in (
            "cookie", "token", "authorization", "apikey",
            "api_key", "refresh_token",
        ) else "****"
        # 匹配 key=value 或 key: value 模式
        msg = re.sub(
            rf'(\b{key}\s*[:=]\s*)([^\s,}}]+)',
            rf'\g<1>{placeholder}',
            msg,
            flags=re.IGNORECASE,
        )
        # 匹配 JSON-like "key": "value"
        msg = re.sub(
            rf'[\'"]{key}[\'"]\s*:\s*[\'"]([^\'"]+)[\'"]',
            rf'"{key}": "{placeholder}"',
            msg,
            flags=re.IGNORECASE,
        )
    return msg


# ── Request ID 追踪 ──────────────────────────────────
_request_id_var = threading.local()


def generate_request_id() -> str:
    """生成短请求 ID，如 req_a3f2k7x9"""
    return 'req_' + ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))


def get_request_id() -> str:
    """获取当前请求/线程的 request_id（Flask g 优先 → threading.local 兜底）"""
    try:
        from flask import g, has_request_context
        if has_request_context():
            if not hasattr(g, '_request_id'):
                g._request_id = generate_request_id()
            return g._request_id
    except Exception:
        pass
    if not hasattr(_request_id_var, 'value') or not _request_id_var.value:
        _request_id_var.value = generate_request_id()
    return _request_id_var.value


def set_request_id(rid: str = ""):
    """显式设置 request_id（用于后台线程启动时）"""
    if not rid:
        rid = generate_request_id()
    _request_id_var.value = rid


# ── 重复日志压缩 Filter ──────────────────────────────

# 各级别的压缩窗口（秒），0 = 不压缩
DEDUP_WINDOWS = {
    logging.ERROR:   30,
    logging.WARNING: 60,
    logging.INFO:    120,
    logging.DEBUG:   300,
}


class DedupFilter(logging.Filter):
    """重复日志压缩过滤器。

    相同 (level, 消息) 在窗口期内重复出现时，抑制输出并计数；
    新消息到来时，在日志末尾追加 "(重复 xN)" 标记。
    """

    def __init__(self, windows: dict[int, int] | None = None):
        super().__init__()
        self._windows = windows or DEDUP_WINDOWS
        self._cache: OrderedDict = OrderedDict()
        self._lock = threading.Lock()
        self._max_entries = 1000

    def filter(self, record: logging.LogRecord) -> bool:
        window = self._windows.get(record.levelno, 120)
        if window <= 0:
            return True  # 不压缩

        key = (record.levelno, record.getMessage())
        now = time.time()

        with self._lock:
            if key in self._cache:
                last_ts, count = self._cache[key]
                if now - last_ts < window:
                    self._cache[key] = (now, count + 1)
                    return False  # 压缩掉当前这条
            # 新消息到来，检查上一条是否有被压缩的
            self._flush_pending(record)
            self._cache[key] = (now, 1)
            # LRU 淘汰
            while len(self._cache) > self._max_entries:
                self._cache.popitem(last=False)
            return True

    def _flush_pending(self, record: logging.LogRecord):
        """检查刚结束的压缩轮次，追加计数到日志（每次最多刷 5 条防阻塞）"""
        flushed = 0
        for (lvl, msg), (last_ts, count) in list(self._cache.items()):
            if flushed >= 5:
                break
            elapsed = time.time() - last_ts
            window = self._windows.get(lvl, 120)
            if count > 1 and elapsed >= window:
                flushed += 1
                if flushed == 1:
                    # 第 1 条附加到当前日志消息
                    extra = f" (重复 {count} 次, 最后一次 {datetime.fromtimestamp(last_ts).strftime('%H:%M:%S')})"
                    record.msg = msg + extra
                    record.args = None
                del self._cache[(lvl, msg)]


# ── 结构化日志 Formatter ─────────────────────────────

class StructuredFormatter(logging.Formatter):
    """在日志行尾追加 JSON payload（仅当 extra 中有 log_extra 时）。"""

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        # 追加 JSON payload
        extra = getattr(record, 'log_extra', None)
        if extra and isinstance(extra, dict):
            try:
                suffix = json.dumps(extra, ensure_ascii=False, default=str)
                return f"{base} {suffix}"
            except Exception:
                pass
        return base


# ── LogContext 包装器 ────────────────────────────────

class LogContext:
    """日志上下文管理器，自动脱敏 + 追加 request_id + 支持 log_extra"""

    def __init__(self, logger: logging.Logger):
        self._logger = logger

    def _log(self, level: int, msg: str, extra: dict | None = None, *args, **kwargs):
        rid = get_request_id()
        log_extra = dict(extra or {})
        if rid:
            log_extra['request_id'] = rid
        safe_msg = sanitize(str(msg))
        self._logger.log(level, safe_msg, extra={'log_extra': log_extra} if log_extra else None,
                         *args, **kwargs)

    def info(self, msg: str, extra: dict | None = None):
        self._log(logging.INFO, msg, extra)

    def warning(self, msg: str, extra: dict | None = None):
        self._log(logging.WARNING, msg, extra)

    def error(self, msg: str, extra: dict | None = None):
        self._log(logging.ERROR, msg, extra)

    def debug(self, msg: str, extra: dict | None = None):
        self._log(logging.DEBUG, msg, extra)


# ── 日志系统初始化 ───────────────────────────────────

def _migrate_old_log():
    """启动时将旧式单文件 app.log 重命名为带日期的归档文件（仅一次）。"""
    old = LOG_DIR / "app.log"
    if not old.exists():
        return
    # 判断是否已经被 TimedRotatingFileHandler 接管（有轮转文件存在）
    rotated_exists = any(f.name.startswith("app.log.") for f in LOG_DIR.iterdir() if f.is_file())
    if rotated_exists:
        return  # 已经是新系统
    # 仅迁移纯 INFO/WARNING/ERROR 格式的日志文件（无 .1 .2 后缀）
    legacy = LOG_DIR / f"app.legacy.{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    try:
        os.rename(str(old), str(legacy))
        print(f"[日志] 已迁移旧日志 -> {legacy.name}")
    except OSError:
        pass  # 迁移失败不阻塞启动


def _check_total_size():
    """检查日志总大小，超过上限时删除最旧的文件。"""
    log_files = []
    for f in LOG_DIR.iterdir():
        if f.is_file() and '.log' in f.name and f.name.startswith(('app', 'error')):
            log_files.append(f)
    total = sum(f.stat().st_size for f in log_files if f.exists())
    if total > MAX_TOTAL_SIZE_MB * 1024 * 1024:
        # 按修改时间排序，删除最旧的
        log_files.sort(key=lambda f: f.stat().st_mtime)
        for f in log_files:
            if f.name in ('app.log', 'error.log'):
                continue  # 不删当前文件
            try:
                size = f.stat().st_size
                f.unlink()
                total -= size
                print(f"[日志] 总大小超限，已删除旧文件: {f.name}")
                if total <= MAX_TOTAL_SIZE_MB * 1024 * 1024:
                    break
            except OSError:
                pass


def setup_logger(name: str = "jmcomic") -> logging.Logger:
    """设置并返回日志器（双文件：app.log INFO+ / error.log WARNING+）。

    文件日志初始化失败时降级到 stderr，保证应用仍可启动。
    """
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    formatter = StructuredFormatter(
        "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # ── 总是添加控制台 Handler（WARNING+）—— 最底层保障 ──
    console = logging.StreamHandler()
    console.setLevel(logging.WARNING)
    console.setFormatter(formatter)
    logger.addHandler(console)

    # ── 文件日志（降级友好）──
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning(f"日志目录创建失败，文件日志已禁用: {e}")
        return logger

    # 迁移旧日志 + 检查总大小（失败不阻断）
    _migrate_old_log()
    _check_total_size()

    try:
        # ── 主日志 Handler（INFO+，保留 30 天） ──
        main_handler = TimedRotatingFileHandler(
            filename=str(LOG_FILE),
            when='midnight',
            interval=1,
            backupCount=MAX_LOG_AGE_DAYS,
            encoding='utf-8',
            delay=False,
        )
        main_handler.setLevel(logging.INFO)
        main_handler.setFormatter(formatter)
        main_handler.addFilter(DedupFilter())
        logger.addHandler(main_handler)
    except OSError as e:
        logger.warning(f"主日志文件打开失败（已降级到控制台）: {e}")

    try:
        # ── 错误日志 Handler（WARNING+，保留 30 天） ──
        error_handler = TimedRotatingFileHandler(
            filename=str(ERROR_LOG_FILE),
            when='midnight',
            interval=1,
            backupCount=MAX_LOG_AGE_DAYS,
            encoding='utf-8',
            delay=False,
        )
        error_handler.setLevel(logging.WARNING)
        error_handler.setFormatter(formatter)
        error_handler.addFilter(DedupFilter())
        logger.addHandler(error_handler)
    except OSError as e:
        logger.warning(f"错误日志文件打开失败（已降级到控制台）: {e}")

    return logger


# ── 全局日志实例 ─────────────────────────────────────
_logger = setup_logger()
log = LogContext(_logger)
