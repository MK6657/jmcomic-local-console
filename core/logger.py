"""
日志系统
========

文件布局（runtime/logs/）：
  app.log                    当前主日志（INFO+）
  app.log.YYYY-MM-DD         按天轮转的历史主日志（当天超过单文件上限时提前轮转为 app.log.YYYY-MM-DD.N）
  error.log                  当前错误日志（WARNING+）
  error.log.YYYY-MM-DD       历史错误日志
  launcher.log               launcher.py 重定向的子进程 stdout/stderr（启动崩溃等未经 logging 的输出）
  launcher.log.YYYY-MM-DD    由 launcher.py 在启动前轮转

行格式（与 routes/api_system.py 日志查看器的约定，见 LOG_HEADER_RE）：
  [YYYY-mm-dd HH:MM:SS] [LEVEL] [logger] message {"request_id": "req_ab12cd34", ...}
  JSON 附加字段可选；消息里的换行写成字面 \\n / \\r，traceback 等续行保证不以 "[" 开头，
  所以任何记录内容都无法伪造出一条新的日志头。request_id 只出现在属于某个请求（或下载任务）的记录上；
  合并了多个请求的去重摘要不写 request_id，而是写 "request_ids": [...]（被合并的各个请求，最多 50 个）。

设计要点：
- 应用使用独立日志器 "jmconsole"（propagate=False）。jmcomic 库的日志器 "jmcomic" 被接管：移除它自带的
  stdout handler（否则每行都会经 launcher 的重定向再写一遍 launcher.log），常规进度被丢弃，
  重试/失败/异常经同一套 脱敏 + 去重 写入本模块的文件。第三方库的 WARNING+ 经根日志器进入同一组文件。
- 去重：按 (logger, level, 归一化模板, 异常类型与位置) 合并窗口期内的重复消息。模板归一化噪声数字
  （页码、耗时、计数、路径里的数字……）。只有写成 job_id= / album_id= / photo_id= 等 *_id= 形式的值保留在模板里，
  这样写的不同任务/漫画/章节的消息不会合并——记录编号时务必写成 xxx_id=；其他位置的编号（例如
  "Job job_xxx"、请求路径 /api/online-img/<photo_id>/<n> 里的数字）与页码一样按噪声合并。
  第一条完整记录；窗口内的重复只计数；窗口结束后为该模板单独输出一条同级别摘要：
      ↑ 同类消息 N 次已合并（HH:MM:SS–HH:MM:SS），最后一条：<最后一条消息>
  摘要在该模板再次出现、后台线程定期刷新（30 秒）、进程退出时，或 launcher 强制结束服务前
  （POST /api/system/logs/flush）写出，绝不附加到其他消息上。
- 保留：start_log_maintenance()（由 app.main() 调用）先把已到轮转时间的活动文件归档（长时间没有新记录的
  error.log 也不例外），再删除 7 天前的历史日志、执行总大小上限；之后每天及每次轮转后自动再执行。
  导入本模块不会删除或重命名任何文件。
"""
import atexit
import json
import logging
import os
import random
import re
import string
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path

from .path_utils import get_app_root

# ── 路径 ──────────────────────────────────────────────
LOG_DIR = get_app_root() / "runtime" / "logs"
LOG_FILE = LOG_DIR / "app.log"
ERROR_LOG_FILE = LOG_DIR / "error.log"
LAUNCHER_LOG_NAME = "launcher.log"
# 正在写入的文件：清理时不按年龄/总量删除（launcher.log 的例外见 run_log_maintenance）
ACTIVE_LOG_NAMES = frozenset({LOG_FILE.name, ERROR_LOG_FILE.name, LAUNCHER_LOG_NAME})

# ── 配置 ─────────────────────────────────────────────
MAX_LOG_AGE_DAYS = 7          # 历史日志保留 7 天（按修改时间）
MAX_TOTAL_SIZE_MB = 100       # 日志目录总上限，超出时从最旧的历史文件开始删除
MAX_ACTIVE_FILE_MB = 20       # 单个活动文件上限，超出时提前轮转
FLUSH_INTERVAL_SECONDS = 30   # 后台线程刷新去重摘要的间隔

APP_LOGGER_NAME = "jmconsole"
JM_LIBRARY_LOGGER_NAME = "jmcomic"

LOG_FORMAT = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# ── 敏感信息脱敏 ─────────────────────────────────────
SENSITIVE_KEYS = {
    "cookie", "token", "password", "secret", "authorization",
    "proxy_password", "apikey", "api_key", "private_key",
    "access_key", "refresh_token", "credential",
    "session", "jwt", "bearer",
}
_UPPERCASE_PLACEHOLDER_KEYS = {
    "cookie", "token", "authorization", "apikey", "api_key", "refresh_token",
}
_sanitize_cache = None  # (keys snapshot, compiled rules)


def _sanitize_rules():
    """预编译脱敏规则（顺序与语义同旧实现：按 key 长度降序，每个 key 两条规则）。"""
    global _sanitize_cache
    keys = frozenset(SENSITIVE_KEYS)
    cache = _sanitize_cache
    if cache is None or cache[0] != keys:
        rules = []
        for key in sorted(keys, key=lambda k: (-len(k), k)):
            placeholder = key.upper() if key in _UPPERCASE_PLACEHOLDER_KEYS else "****"
            rules.append((
                key,
                # 匹配 key=value 或 key: value 模式
                re.compile(rf'(\b{key}\s*[:=]\s*)([^\s,}}]+)', re.IGNORECASE),
                rf'\g<1>{placeholder}',
                # 匹配 JSON-like "key": "value"
                re.compile(rf'[\'"]{key}[\'"]\s*:\s*[\'"]([^\'"]+)[\'"]', re.IGNORECASE),
                rf'"{key}": "{placeholder}"',
            ))
        cache = _sanitize_cache = (keys, tuple(rules))
    return cache[1]


def sanitize(msg: str) -> str:
    """脱敏处理：替换日志中的敏感信息"""
    if not msg:
        return msg
    # 快速路径：两条规则都要求 key 字面出现（忽略大小写）。casefold 覆盖 IGNORECASE 的
    # 特殊等价字符（如 ſ→s、K→k），只有土耳其无点 ı 需要单独折叠成 i。
    folded = msg.casefold().replace("ı", "i")
    for key, kv_pattern, kv_repl, json_pattern, json_repl in _sanitize_rules():
        if key not in folded:
            continue
        msg = kv_pattern.sub(kv_repl, msg)
        msg = json_pattern.sub(json_repl, msg)
    return msg


# ── Request ID 追踪 ──────────────────────────────────
_request_id_var = threading.local()


def generate_request_id() -> str:
    """生成短请求 ID，如 req_a3f2k7x9"""
    return 'req_' + ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))


def current_request_id():
    """当前记录所属的 request_id：Flask 请求上下文中的 ID，或本线程经 set_request_id() 显式设置的 ID
    （下载任务线程、bind_request_id() 包装的工作线程）。其他情况（启动主线程、调度器、日志维护线程……）
    返回 None：这些记录不属于任何请求，不带 request_id，日志查看器的“只看此请求”也就不会把
    一个线程的全部历史当成同一个请求。"""
    try:
        from flask import g, has_request_context
        if has_request_context():
            rid = getattr(g, "_request_id", None)
            if not rid:
                rid = g._request_id = generate_request_id()
            return rid
    except Exception:
        pass
    return getattr(_request_id_var, "value", None) or None


def get_request_id() -> str:
    """兼容旧接口：返回当前请求/任务的 request_id；都没有时为本线程显式分配一个。
    日志记录本身只使用 current_request_id()，不会凭空生成线程级 ID。"""
    return current_request_id() or set_request_id()


def set_request_id(rid: str = "") -> str:
    """显式设置本线程（请求上下文中同时设置 Flask g）的 request_id，返回该 ID。
    app.py 在每个请求开始时调用；下载任务线程在任务开始时调用，结束时 clear_request_id()。"""
    if not rid:
        rid = generate_request_id()
    _request_id_var.value = rid
    try:
        from flask import g, has_request_context
        if has_request_context():
            g._request_id = rid
    except Exception:
        pass
    return rid


def clear_request_id():
    """清除本线程显式设置的 request_id。"""
    _request_id_var.value = None


def bind_request_id(fn):
    """把当前 request_id 带进另一个线程执行的 fn（线程池里的 jmcomic 调用），让库的重试/失败记录
    也能按请求串起来。当前没有 request_id 时原样返回 fn。"""
    rid = current_request_id()
    if not rid:
        return fn

    def run(*args, **kwargs):
        previous = getattr(_request_id_var, "value", None)
        _request_id_var.value = rid
        try:
            return fn(*args, **kwargs)
        finally:
            _request_id_var.value = previous

    return run


# ── 重复日志合并 ─────────────────────────────────────

# 各级别的合并窗口（秒），0 = 不合并。窗口从该模板第一条完整记录开始计时。
DEDUP_WINDOWS = {
    logging.ERROR:   30,
    logging.WARNING: 60,
    logging.INFO:    120,
    logging.DEBUG:   300,
}
DEDUP_MAX_ENTRIES = 512            # 同时跟踪的模板数上限（LRU；被挤出的模板会先写出摘要）
SUMMARY_PREFIX = "↑ 同类消息"
TEMPLATE_MAX_CHARS = 300           # 只用消息前 300 字符计算模板，限制内存与耗时
LAST_MESSAGE_MAX_CHARS = 1000      # 摘要里“最后一条”的最大长度
EXTERNAL_MESSAGE_MAX_CHARS = 4000  # 第三方库单条消息上限（如 jmcomic 会整段打印响应体）
SUMMARY_MAX_REQUEST_IDS = 50       # 一条摘要最多列出的被合并请求 ID（保留最近的）

_SUMMARY_ATTR = "_jm_dedup_summary"  # 摘要记录：直接放行，不参与计数
_CLEAN_ATTR = "_jm_clean"            # 已脱敏（LogContext 或网关已处理）

_TEMPLATE_SUBSTITUTIONS = (
    (re.compile(r"req_[0-9a-z]{8}"), "req_#"),
    (re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"), "#"),
    (re.compile(r"(?<![0-9A-Za-z])[0-9a-fA-F]{8,}(?![0-9A-Za-z])"), "#"),
    (re.compile(r"\d+"), "#"),
)


# 身份字段：job_id= / album_id= / photo_id= … 的值原样保留在模板里（request_id 除外——它每个请求都不同，
# 正是需要合并的噪声）。否则不同任务/漫画的失败会合并成一条，被抑制的那些 ID 在日志里就再也找不到了。
_IDENTITY_FIELD = re.compile(r"(?<![0-9A-Za-z_])(?!request_id=)[A-Za-z][0-9A-Za-z_]*_id=([^\s,;，；。)）\]}]+)")


def _normalize_noise(text: str) -> str:
    for pattern, placeholder in _TEMPLATE_SUBSTITUTIONS:
        text = pattern.sub(placeholder, text)
    return text


def message_template(message: str) -> str:
    """把消息归一化成模板：request_id / UUID / 长十六进制串 / 数字 → 占位符，*_id= 的值除外。

    例：'取图 photo_id=324751 page=10 失败' 与 '... page=17 失败' 得到同一个模板；
    'job_id=job_1 失败' 与 'job_id=job_2 失败' 是两个模板。
    """
    text = message[:TEMPLATE_MAX_CHARS]
    parts, last = [], 0
    for match in _IDENTITY_FIELD.finditer(text):
        parts.append(_normalize_noise(text[last:match.start(1)]))
        parts.append(match.group(1))
        last = match.end(1)
    parts.append(_normalize_noise(text[last:]))
    return "".join(parts)


def _exception_signature(record: logging.LogRecord):
    """带异常的记录按 (异常类型, 最内层帧 文件:行号) 区分：同一消息下不同的异常（例如同一路由上的
    ValueError 和 PermissionError）各自完整记录一次 traceback，相同异常的重复仍然合并。"""
    info = record.exc_info
    if not isinstance(info, tuple) or len(info) < 3 or info[0] is None:
        return None
    exc_type, _, tb = info[:3]
    last = None
    while tb is not None:
        last, tb = tb, tb.tb_next
    where = f"{last.tb_frame.f_code.co_filename}:{last.tb_lineno}" if last is not None else ""
    return (f"{getattr(exc_type, '__module__', '')}.{getattr(exc_type, '__qualname__', exc_type)}", where)


def _hms(timestamp: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(timestamp))


def _clean_external(record: logging.LogRecord):
    """第三方库（jmcomic / Flask / waitress / py.warnings …）的记录：截断 + 脱敏 + 关联 request_id。"""
    try:
        message = record.getMessage()
    except Exception:
        return  # 参数与格式不匹配：交给 handler 的 handleError 报告
    if len(message) > EXTERNAL_MESSAGE_MAX_CHARS:
        cut = len(message) - EXTERNAL_MESSAGE_MAX_CHARS
        message = f"{message[:EXTERNAL_MESSAGE_MAX_CHARS]}…(已截断 {cut} 字符)"
    record.msg = sanitize(message)
    record.args = None
    if getattr(record, "log_extra", None) is None:
        rid = current_request_id()
        if rid:
            record.log_extra = {"request_id": rid}
    record.__dict__[_CLEAN_ATTR] = True


class _Pending:
    __slots__ = ("logger_name", "levelno", "start", "count", "first_dup", "last_dup", "last_message",
                 "request_ids", "request_ids_omitted", "without_request_id")

    def __init__(self, logger_name: str, levelno: int, start: float):
        self.logger_name = logger_name
        self.levelno = levelno
        self.start = start
        self.count = 0
        self.first_dup = start
        self.last_dup = start
        self.last_message = ""
        # 被合并的记录分属哪些请求/任务（去重后按最近出现排序，最多 SUMMARY_MAX_REQUEST_IDS 个）
        self.request_ids: OrderedDict = OrderedDict()
        self.request_ids_omitted = 0     # 超出上限、没能列出的请求 ID 个数
        self.without_request_id = False  # 是否也合并了不属于任何请求的记录

    def add_request_id(self, rid):
        if not rid:
            self.without_request_id = True
            return
        ids = self.request_ids
        if rid in ids:
            ids.move_to_end(rid)
            return
        ids[rid] = None
        if len(ids) > SUMMARY_MAX_REQUEST_IDS:
            ids.popitem(last=False)
            self.request_ids_omitted += 1

    def summary_payload(self) -> dict:
        """摘要的 JSON 附加字段。只有全部被合并的记录都来自同一个请求时才写单个 request_id；
        涉及多个请求时写 request_ids 列表——日志查看器的“只看此请求”按子串搜索（含附加字段），
        每个被合并的请求都能找到这条摘要，而不是把别的请求的记录算到最后一个请求头上。"""
        payload = {"merged": self.count}
        ids = list(self.request_ids)
        if len(ids) == 1 and not self.without_request_id and not self.request_ids_omitted:
            payload["request_id"] = ids[0]
        elif ids:
            payload["request_ids"] = ids
            if self.request_ids_omitted:
                payload["request_ids_omitted"] = self.request_ids_omitted
        return payload


class LogDeduplicator(logging.Filter):
    """所有 handler 共用的一个“网关”过滤器：脱敏第三方记录 + 按模板合并重复消息。

    - 键为 (logger 名, 级别, 模板, 异常签名)，不同级别/不同 logger/不同异常永不合并。
    - 第一条完整放行；窗口内的重复被抑制并计数；窗口结束后为该模板单独写一条同级别摘要。
    - 同一条记录会依次经过 app.log / error.log 的 handler：第一次的判定结果缓存在记录上，
      所以两份文件看到的内容一致，计数也只加一次。
    - 线程安全；锁内只做字典操作，摘要在锁外写出；摘要记录带标记直接放行，不会递归。
    """

    def __init__(self, windows=None, max_entries: int = DEDUP_MAX_ENTRIES,
                 adopted=None, on_pending=None, clock=time.time):
        super().__init__()
        self._windows = dict(DEDUP_WINDOWS if windows is None else windows)
        self._max_entries = max(1, int(max_entries))
        # adopted=None：不限制；否则只有这些 logger（及其子 logger）的 INFO 能进入文件，
        # 其余第三方库只接收 WARNING+。
        self._adopted = None if adopted is None else set(adopted)
        self._on_pending = on_pending
        self._clock = clock
        self._entries: OrderedDict = OrderedDict()
        self._lock = threading.Lock()
        self._decision_attr = f"_jm_gate_{id(self)}"

    # ── 配置 ──
    def adopt(self, name: str):
        if self._adopted is not None:
            self._adopted.add(name)

    def _is_adopted(self, name: str) -> bool:
        adopted = self._adopted
        return adopted is None or name in adopted or name.partition(".")[0] in adopted

    def _window(self, levelno: int) -> float:
        window = self._windows.get(levelno)
        if window is None:
            window = 0
            for level in sorted(self._windows):
                if levelno >= level:
                    window = self._windows[level]
        return window

    # ── 过滤 ──
    def filter(self, record: logging.LogRecord) -> bool:
        data = record.__dict__
        if data.get(_SUMMARY_ATTR):
            return True
        decision = data.get(self._decision_attr)
        if decision is None:
            try:
                decision = self._decide(record)
            except Exception:
                decision = True  # 过滤器自身出错时宁可多记，也不丢日志
            data[self._decision_attr] = decision
        return decision

    def _decide(self, record: logging.LogRecord) -> bool:
        if record.levelno < logging.WARNING and not self._is_adopted(record.name):
            return False
        if not record.__dict__.get(_CLEAN_ATTR):
            _clean_external(record)
        window = self._window(record.levelno)
        if window <= 0:
            return True
        message = record.getMessage()
        key = (record.name, record.levelno, message_template(message), _exception_signature(record))
        now = self._clock()
        due = []
        newly_pending = False
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and now - entry.start < window:
                entry.count += 1
                if entry.count == 1:
                    entry.first_dup = now
                    newly_pending = True
                entry.last_dup = now
                entry.last_message = message[:LAST_MESSAGE_MAX_CHARS]
                extra = getattr(record, "log_extra", None)
                entry.add_request_id(extra.get("request_id") if isinstance(extra, dict) else None)
                self._entries.move_to_end(key)
                suppress = True
            else:
                if entry is not None:
                    del self._entries[key]
                    if entry.count:
                        due.append(entry)
                self._entries[key] = _Pending(record.name, record.levelno, now)
                while len(self._entries) > self._max_entries:
                    _, evicted = self._entries.popitem(last=False)
                    if evicted.count:
                        due.append(evicted)
                suppress = False
        # 先写上一轮的摘要，再放行本条（同一模板在文件里的顺序：首条 → 摘要 → 新一轮首条）
        for entry in due:
            self._emit_summary(entry)
        if newly_pending and self._on_pending is not None:
            try:
                self._on_pending()
            except Exception:
                pass
        return not suppress

    # ── 摘要 ──
    def flush(self, force: bool = False, now: float | None = None) -> int:
        """写出窗口已结束的摘要并结束这些窗口。返回写出的摘要数。

        force=True（进程退出、launcher 强制结束服务前）：另外立即写出所有已有重复计数的摘要，窗口未结束也写。
        还没有重复（计数为 0）的模板保持窗口不变——否则调用一次之后，最近记录过的每条消息都会再完整记录一遍。"""
        now = self._clock() if now is None else now
        due = []
        with self._lock:
            for key, entry in list(self._entries.items()):
                if now - entry.start >= self._window(entry.levelno):
                    del self._entries[key]
                    if entry.count:
                        due.append(entry)
                elif force and entry.count:
                    del self._entries[key]
                    due.append(entry)
        for entry in due:
            self._emit_summary(entry)
        return len(due)

    def pending_count(self) -> int:
        with self._lock:
            return sum(1 for entry in self._entries.values() if entry.count)

    @staticmethod
    def _emit_summary(entry: _Pending):
        text = (f"{SUMMARY_PREFIX} {entry.count} 次已合并"
                f"（{_hms(entry.first_dup)}–{_hms(entry.last_dup)}），"
                f"最后一条：{entry.last_message}")
        payload = entry.summary_payload()
        try:
            logger = logging.getLogger(entry.logger_name)
            record = logger.makeRecord(
                entry.logger_name, entry.levelno, "(dedup)", 0, text, None, None,
                extra={"log_extra": payload, _SUMMARY_ATTR: True, _CLEAN_ATTR: True},
            )
            logger.handle(record)
        except Exception:
            pass


DedupFilter = LogDeduplicator  # 旧名称兼容


# ── jmcomic 库日志取舍 ───────────────────────────────
# jmcomic 通过 jm_log(topic, msg[, e]) → logging.getLogger("jmcomic") 记录，topic 在 record.topic。
#  - 丢弃（INFO 常规进度）：每个请求的 URL（api/html/req 等 topic）、image.before/after、
#    photo.before/after、album.before/after、plugin.invoke、jmv、entity、dir_rule、module.*、option.*、
#    album.comment 等。下载一本就会产生成百上千行，而本应用的任务日志已记录开始/完成/失败。
#  - 保留并提升为 WARNING：topic 含 error / fail / exception / retry / fallback / wrong_usage /
#    empty / scramble —— 请求重试、域名回退、图片/章节下载失败、插件异常、scramble 解析失败，
#    都是排查“下载失败 / 在线阅读取图失败”的关键信息。
#  - 保留为 INFO：api.update_domain.success、api.setting（极少出现，但会改变后续请求行为）。
#  - 库本身以 WARNING/ERROR 记录的（例如带异常对象的 jm_log）一律保留，含 traceback。
#  - 没有 topic 的记录（插件直接调用 jm_logger）保留原级别。
_JM_PROBLEM_TOPIC = re.compile(r"error|fail|exception|retry|fallback|wrong_usage|empty|scramble",
                               re.IGNORECASE)
_JM_INFO_TOPICS = ("api.update_domain.success", "api.setting")


class JmLibraryFilter(logging.Filter):
    """挂在 "jmcomic" 日志器上（logger 级，先于网关执行）：按 topic 取舍并调整级别。"""

    def filter(self, record: logging.LogRecord) -> bool:
        data = record.__dict__
        if data.get(_SUMMARY_ATTR) or data.get("_jm_library_seen"):
            return True
        topic = str(data.get("topic") or "")
        if record.levelno < logging.WARNING and topic:
            if _JM_PROBLEM_TOPIC.search(topic):
                record.levelno = logging.WARNING
                record.levelname = logging.getLevelName(logging.WARNING)
            elif not topic.startswith(_JM_INFO_TOPICS):
                return False
        if topic:
            try:
                record.msg = f"【{topic}】{record.getMessage()}"
                record.args = None
            except Exception:
                pass
        data["_jm_library_seen"] = True
        return True


# ── Formatter ────────────────────────────────────────

# 日志头（LOG_FORMAT + DATE_FORMAT 写出的首行）。日志查看器用它切分条目；续行永远不会匹配它。
LOG_HEADER_RE = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] \[([A-Z]+)\] \[([^\]\r\n]*)\](?: (.*))?$",
    re.DOTALL,
)
_LINE_BREAKS = re.compile(r"\r\n?|\n")
_CONTINUATION_BRACKET = re.compile(r"\n\[")


def _escape_line_breaks(text: str) -> str:
    """消息正文单行化：换行写成字面 \\r / \\n。请求路径、远端标题、第三方库输出里的换行
    （例如 URL 中的 %0A）因此无法在文件里伪造出一条 "[时间] [ERROR] ..." 日志。"""
    if "\n" not in text and "\r" not in text:
        return text
    return text.replace("\r", "\\r").replace("\n", "\\n")


def _safe_continuation(text: str) -> str:
    """traceback / stack 续行：统一换行符，并给以 "[" 开头的行加一个前导空格，使其不可能是日志头。"""
    text = _LINE_BREAKS.sub("\n", text)
    if text.startswith("["):
        text = " " + text
    return _CONTINUATION_BRACKET.sub("\n [", text)


class StructuredFormatter(logging.Formatter):
    """行格式见模块说明：消息（单行化）后追加 JSON payload（record.log_extra），traceback 作为续行。"""

    def format(self, record: logging.LogRecord) -> str:
        record.message = _escape_line_breaks(record.getMessage())
        if self.usesTime():
            record.asctime = self.formatTime(record, self.datefmt)
        text = self.formatMessage(record)
        extra = getattr(record, "log_extra", None)
        if extra and isinstance(extra, dict):
            try:
                text = f"{text} {json.dumps(extra, ensure_ascii=False, default=str)}"
            except Exception:
                pass
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            text = f"{text}\n{_safe_continuation(record.exc_text)}"
        if record.stack_info:
            text = f"{text}\n{_safe_continuation(self.formatStack(record.stack_info))}"
        return text

    def formatException(self, ei) -> str:
        return sanitize(super().formatException(ei))


# ── 按天轮转 + 单文件大小上限 ─────────────────────────

def _day_bounds(timestamp: float):
    """(该时刻所在日期 YYYY-MM-DD, 次日本地零点的时间戳)"""
    lt = time.localtime(timestamp)
    next_midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + 1, 0, 0, 0, 0, 0, -1))
    return time.strftime("%Y-%m-%d", lt), next_midnight


def rotation_target(base: str, day: str) -> str:
    """<base>.<day>；当天已有归档时用 <base>.<day>.N，N = 现有最大序号 + 1（序号越大越新）。

    不复用中间空出的名字：一天写满 100MB 时，总大小上限会先删掉当天最早的 <base>.<day>、.1……
    如果下一次轮转取“第一个空闲的名字”，最新的内容就会落到 <base>.<day> 或 .1，排在更旧的 .3、.4 后面，
    日志查看器（按日期、序号倒序读取）会先读旧文件，搜索预算也耗在旧内容上，最新的记录反而看不到。"""
    candidate = f"{base}.{day}"
    folder, prefix = os.path.split(candidate)
    numbered = re.compile(re.escape(prefix) + r"(?:\.(\d{1,6}))?")
    highest = None
    try:
        names = os.listdir(folder or ".")
    except OSError:
        names = []
    for name in names:
        match = numbered.fullmatch(name)
        if match:
            number = int(match.group(1) or 0)
            highest = number if highest is None or number > highest else highest
    number = 0 if highest is None else highest + 1
    while True:
        target = candidate if number == 0 else f"{candidate}.{number}"
        if not os.path.exists(target):
            return target
        number += 1


class DailyRotatingFileHandler(logging.FileHandler):
    """活动文件 + 按天轮转的 <name>.YYYY-MM-DD，超过 max_bytes 时当天提前轮转。

    与标准 TimedRotatingFileHandler 的区别：
    - 轮转文件以“内容所属日期”命名；重启跨天时（活动文件最后修改于更早日期）首条记录就会轮转。
    - 目标文件已存在时加序号，不覆盖、不跳过。
    - Windows 上文件被其他进程/句柄占用时重命名会失败：继续写活动文件，5 分钟后再试，
      不会每条日志都重试、也不会丢日志。
    - 不按数量删除旧文件，保留策略统一由 run_log_maintenance() 执行。
    """

    ROTATION_RETRY_SECONDS = 300

    def __init__(self, filename, max_bytes: int = 0, on_rotate=None,
                 clock=time.time, encoding: str = "utf-8"):
        self.max_bytes = max_bytes
        self.on_rotate = on_rotate
        self._clock = clock
        self._retry_at = 0.0
        super().__init__(filename, mode="a", encoding=encoding, delay=False)
        try:
            info = os.stat(self.baseFilename)
            started = info.st_mtime if info.st_size > 0 else clock()
        except OSError:
            started = clock()
        self._content_day, self._day_end = _day_bounds(started)

    def _rotation_reason(self):
        now = self._clock()
        if now < self._retry_at:
            return None
        if now >= self._day_end:
            return "day"
        if self.max_bytes > 0 and self.stream is not None:
            try:
                if self.stream.tell() >= self.max_bytes:
                    return "size"
            except (OSError, ValueError):
                pass
        return None

    def rotate_now(self) -> bool:
        """立即轮转（持有 handler 锁）。返回是否把活动文件归档成了 <name>.<日期>[.N]。"""
        with self.lock:
            return self._rotate()

    def rotate_if_due(self) -> bool:
        """到了轮转时间（跨日或超过大小上限）且活动文件非空时立即轮转（持有 handler 锁），返回是否归档了文件。

        emit() 只在有新记录时检查；长时间没有新记录的活动文件（只收 WARNING+ 的 error.log、空闲时的 app.log）
        由后台维护调用本方法按天归档，归档后的文件按修改时间参与 7 天保留。"""
        with self.lock:
            if not self._rotation_reason():
                return False
            try:
                if os.path.getsize(self.baseFilename) <= 0:
                    return False
            except OSError:
                return False
            return self._rotate()

    def _rotate(self) -> bool:
        now = self._clock()
        if self.stream is not None:
            stream, self.stream = self.stream, None
            try:
                stream.flush()
                stream.close()
            except (OSError, ValueError):
                pass
        rotated = renamed = False
        try:
            if os.path.exists(self.baseFilename) and os.path.getsize(self.baseFilename) > 0:
                os.rename(self.baseFilename, rotation_target(self.baseFilename, self._content_day))
                renamed = True
            rotated = True
        except OSError:
            rotated = False  # 被占用：继续追加到活动文件，稍后重试
        try:
            self.stream = self._open()
        except OSError:
            self.stream = None  # emit() 会在保护下再尝试打开
        if rotated:
            self._content_day, self._day_end = _day_bounds(now)
            self._retry_at = 0.0
            if renamed and self.on_rotate is not None:
                try:
                    self.on_rotate()
                except Exception:
                    pass
        else:
            self._retry_at = now + self.ROTATION_RETRY_SECONDS
        return renamed

    def emit(self, record: logging.LogRecord):
        try:
            if self._rotation_reason():
                self._rotate()
        except Exception:
            pass  # 轮转问题绝不影响写日志
        if self.stream is None and (self.mode != "w" or not getattr(self, "_closed", False)):
            # 轮转后重新打开失败（权限/占用）时 FileHandler.emit 会不加保护地再打开一次，OSError 会一路抛进
            # 调用 log.info() 的业务代码。这里先在保护下打开：失败只按 logging 的惯例报告，本条记录丢弃。
            try:
                self.stream = self._open()
            except OSError:
                self.handleError(record)
                return
        super().emit(record)


# ── 保留策略 ─────────────────────────────────────────

# 日志文件：*.log，或 *.log.<数字开头的后缀>（app.log.2026-09-25、app.log.2026-09-25.1、
# launcher.log.2026-09-01、app.legacy.20260925_190707.log …）。其它文件一律不碰。
_LOG_NAME_RE = re.compile(r".+\.log(?:\.\d[\w.-]*)?")


def is_log_file_name(name: str) -> bool:
    return bool(_LOG_NAME_RE.fullmatch(name))


def _is_own_output(path: Path) -> bool:
    """path 是否就是本进程的 stdout/stderr（由 launcher 启动时，launcher.log 一直被服务自己写着）。"""
    try:
        target = os.stat(path)
    except OSError:
        return False
    for fd in (1, 2):
        try:
            if os.path.samestat(os.fstat(fd), target):
                return True
        except (OSError, ValueError):
            continue
    return False


def run_log_maintenance(log_dir=None, now: float | None = None,
                        max_age_days: float | None = None,
                        max_total_mb: float | None = None) -> dict:
    """删除超过 max_age_days 的历史日志；总大小超过 max_total_mb 时从最旧的历史日志删起。

    - 只处理 log_dir 下（不递归）的日志文件，按修改时间判断年龄。
    - 永不删除活动文件 app.log / error.log，永不删除非日志文件。活动文件里的过期内容由
      rotate_due_log_files() 先归档成历史文件，再按年龄删除。
    - launcher.log 由 launcher 在每次启动前轮转；直接用 python app.py 启动时没人轮转它，所以当它最后一次
      写入也早于保留期（内容全部过期）且不是本进程正在写的 stdout/stderr 时，按历史文件删除。
    - 被占用无法删除的文件（Windows）跳过，记入 failed，下次再试。
    """
    log_dir = Path(LOG_DIR if log_dir is None else log_dir)
    now = time.time() if now is None else now
    max_age_days = MAX_LOG_AGE_DAYS if max_age_days is None else max_age_days
    max_total_mb = MAX_TOTAL_SIZE_MB if max_total_mb is None else max_total_mb
    result = {"deleted": [], "failed": [], "freed_bytes": 0, "total_bytes": 0}
    try:
        entries = list(os.scandir(log_dir))
    except OSError:
        return result
    files = []
    for entry in entries:
        try:
            if not is_log_file_name(entry.name) or not entry.is_file(follow_symlinks=False):
                continue
            info = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        files.append((entry.name, Path(entry.path), info.st_size, info.st_mtime))

    def delete(item) -> bool:
        name, path, size, _ = item
        try:
            path.unlink()
        except FileNotFoundError:
            return True
        except OSError:
            result["failed"].append(name)
            return False
        result["deleted"].append(name)
        result["freed_bytes"] += size
        return True

    cutoff = now - max_age_days * 86400

    def expired(item) -> bool:
        name, path, _, mtime = item
        if mtime >= cutoff:
            return False
        if name == LAUNCHER_LOG_NAME:
            return not _is_own_output(path)
        return name not in ACTIVE_LOG_NAMES

    remaining = []
    for item in files:
        if expired(item) and delete(item):
            continue
        remaining.append(item)

    total = sum(item[2] for item in remaining)
    limit = max_total_mb * 1024 * 1024
    if total > limit:
        for item in sorted((i for i in remaining if i[0] not in ACTIVE_LOG_NAMES), key=lambda i: i[3]):
            if total <= limit:
                break
            if item[0] in result["failed"]:
                continue
            if delete(item):
                total -= item[2]
    result["total_bytes"] = total
    return result


# ── 后台线程：定期刷新摘要 + 每日清理 ─────────────────

class LogHousekeeper:
    """守护线程：每 interval 秒写出到期的去重摘要、重新接管库日志器；
    启用维护后，每天（跨日或发生轮转后）执行一次保留策略。"""

    def __init__(self, dedup=None, interval: float = FLUSH_INTERVAL_SECONDS, periodic=None):
        self.dedup = dedup
        self.interval = interval
        self._periodic = periodic
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        self._stopped = False
        self._maintenance = None
        self._maintenance_day = None
        self._maintenance_due = False

    def ensure_started(self):
        if self._thread is not None or self._stopped:
            return
        with self._lock:
            if self._thread is None and not self._stopped:
                thread = threading.Thread(target=self._run, name="log-housekeeper", daemon=True)
                self._thread = thread
                thread.start()

    def enable_maintenance(self, task):
        self._maintenance = task
        self._maintenance_day = time.strftime("%Y-%m-%d")
        self.ensure_started()

    def request_maintenance(self):
        """轮转后调用（在 handler 锁内）：只置标志并唤醒线程，不在此处写日志或删文件。"""
        self._maintenance_due = True
        if self._thread is not None:
            self._wake.set()

    def _run(self):
        while True:
            self._wake.wait(self.interval)
            self._wake.clear()
            if self._stopped:
                return
            self.tick()

    def tick(self):
        if self.dedup is not None:
            try:
                self.dedup.flush()
            except Exception:
                pass
        if self._periodic is not None:
            try:
                self._periodic()
            except Exception:
                pass
        task = self._maintenance
        if task is not None:
            today = time.strftime("%Y-%m-%d")
            if self._maintenance_due or today != self._maintenance_day:
                self._maintenance_due = False
                self._maintenance_day = today
                try:
                    task()
                except Exception:
                    pass

    def stop(self, flush: bool = True, timeout: float = 2.0):
        self._stopped = True
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        if flush and self.dedup is not None:
            try:
                self.dedup.flush(force=True)
            except Exception:
                pass


# ── LogContext 包装器 ────────────────────────────────

class LogContext:
    """日志上下文包装：自动脱敏 + 追加 request_id + 支持 extra（写成行尾 JSON）"""

    def __init__(self, logger: logging.Logger):
        self._logger = logger

    def _log(self, level: int, msg, extra: dict | None = None, exc_info=None):
        if not self._logger.isEnabledFor(level):
            return
        log_extra = dict(extra or {})
        rid = current_request_id()
        if rid:
            log_extra['request_id'] = rid
        record_extra = {_CLEAN_ATTR: True}
        if log_extra:
            record_extra['log_extra'] = log_extra
        self._logger.log(level, sanitize(str(msg)), exc_info=exc_info, extra=record_extra)

    def info(self, msg: str, extra: dict | None = None):
        self._log(logging.INFO, msg, extra)

    def warning(self, msg: str, extra: dict | None = None, exc_info=None):
        self._log(logging.WARNING, msg, extra, exc_info)

    def error(self, msg: str, extra: dict | None = None, exc_info=None):
        self._log(logging.ERROR, msg, extra, exc_info)

    def exception(self, msg: str, extra: dict | None = None):
        """在 except 块中调用：ERROR + 完整 traceback。"""
        self._log(logging.ERROR, msg, extra, True)

    def critical(self, msg: str, extra: dict | None = None, exc_info=None):
        self._log(logging.CRITICAL, msg, extra, exc_info)

    def debug(self, msg: str, extra: dict | None = None):
        self._log(logging.DEBUG, msg, extra)


# ── 装配 ─────────────────────────────────────────────

_setup_lock = threading.RLock()


def _ensure_housekeeper():
    _housekeeper.ensure_started()


def _request_maintenance():
    _housekeeper.request_maintenance()


_dedup = LogDeduplicator(adopted={APP_LOGGER_NAME, JM_LIBRARY_LOGGER_NAME},
                         on_pending=_ensure_housekeeper)
_jm_library_filter = JmLibraryFilter()


def _build_handlers(log_dir: Path, on_rotate=None):
    """app.log（INFO+）、error.log（WARNING+）；控制台 handler 只在交互式终端或文件日志不可用时添加，
    否则经 launcher 重定向后每条 WARNING 会在 launcher.log 里再出现一次。"""
    formatter = StructuredFormatter(LOG_FORMAT, datefmt=DATE_FORMAT)
    handlers, problems = [], []
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        problems.append(f"日志目录创建失败，文件日志已禁用: {e}")
    else:
        for path, level in ((log_dir / LOG_FILE.name, logging.INFO),
                            (log_dir / ERROR_LOG_FILE.name, logging.WARNING)):
            try:
                handler = DailyRotatingFileHandler(
                    path, max_bytes=MAX_ACTIVE_FILE_MB * 1024 * 1024, on_rotate=on_rotate)
            except OSError as e:
                problems.append(f"日志文件 {path.name} 打开失败（已降级到控制台）: {e}")
                continue
            handler.setLevel(level)
            handlers.append(handler)
    stderr = sys.stderr
    try:
        interactive = bool(stderr is not None and stderr.isatty())
    except Exception:
        interactive = False
    if stderr is not None and (interactive or len(handlers) < 2):
        console = logging.StreamHandler(stderr)
        console.setLevel(logging.WARNING)
        handlers.append(console)
    for handler in handlers:
        handler.setFormatter(formatter)
        handler._jm_owned = True
    return handlers, problems


def adopt_logger(logger, level: int | None = logging.INFO, filters=()) -> logging.Logger:
    """让一个日志器只写入本模块的文件：移除它的控制台 handler（stdout/stderr），挂上共享 handler，
    propagate=False，并让其 INFO 记录通过网关。可重复调用。"""
    if isinstance(logger, str):
        logger = logging.getLogger(logger)
    with _setup_lock:
        for handler in list(logger.handlers):
            if getattr(handler, "_jm_owned", False):
                continue
            if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
                logger.removeHandler(handler)
        for handler in _shared_handlers:
            if handler not in logger.handlers:
                logger.addHandler(handler)
        for flt in filters:
            if flt not in logger.filters:
                logger.addFilter(flt)
        logger.propagate = False
        if level is not None and logger.level != level:
            logger.setLevel(level)
        _dedup.adopt(logger.name)
    return logger


def claim_library_loggers():
    """接管 jmcomic 库日志器并把共享 handler 挂到根日志器（第三方库 WARNING+）。

    与导入顺序无关：jmcomic 的 setup_default_jm_logger() 只在其日志器没有 handler 时才添加
    StreamHandler(stdout)。先导入本模块 → 它看到我们的 handler 就不再添加；先导入 jmcomic →
    这里移除它加的 handler。后台线程每 30 秒再调用一次，兜住 enable_pretty_log() 之类的重新添加。
    根日志器已有 handler 时，waitress 的 logging.basicConfig() 也不会再添加 stderr handler。
    """
    adopt_logger(JM_LIBRARY_LOGGER_NAME, level=logging.INFO, filters=(_jm_library_filter,))
    root = logging.getLogger()
    with _setup_lock:
        for handler in _shared_handlers:
            if handler not in root.handlers:
                root.addHandler(handler)


def setup_logger(name: str = APP_LOGGER_NAME) -> logging.Logger:
    """返回写入 app.log / error.log 的应用日志器（不向根日志器传播）。"""
    return adopt_logger(name, level=logging.INFO)


def rotate_due_log_files() -> int:
    """把已到轮转时间的活动文件（app.log / error.log）立即归档，返回归档的文件数。

    轮转平时只在写入新记录时检查；error.log 只收 WARNING+，安静几天后它的旧内容既不会被轮转也不会被
    保留策略删除，日志查看器还会把几周前的错误当成“最新”。维护任务（启动时、每天、每次轮转后）先调用这里。"""
    rotated = 0
    for handler in list(_shared_handlers):
        if isinstance(handler, DailyRotatingFileHandler):
            try:
                rotated += bool(handler.rotate_if_due())
            except Exception:
                pass
    return rotated


def _run_maintenance_and_report() -> dict:
    rotate_due_log_files()
    result = run_log_maintenance()
    if result["deleted"]:
        log.info(
            f"日志清理：删除 {len(result['deleted'])} 个超过 {MAX_LOG_AGE_DAYS} 天或超出总上限的日志文件，"
            f"释放 {result['freed_bytes'] / 1024 / 1024:.1f}MB",
            extra={"files": result["deleted"][:20]},
        )
    if result["failed"]:
        log.info(f"日志清理：{len(result['failed'])} 个文件正被占用，下次再试",
                 extra={"files": result["failed"][:20]})
    return result


def start_log_maintenance() -> dict:
    """应用启动时调用（app.main()）：立即归档到期的活动文件并执行一次保留策略，之后每天及每次轮转后自动执行。"""
    result = _run_maintenance_and_report()
    _housekeeper.enable_maintenance(_run_maintenance_and_report)
    return result


def flush_log_summaries(force: bool = False) -> int:
    """写出去重摘要（force=True：包括窗口尚未结束的；还没有重复的模板窗口不受影响），返回写出的条数。
    launcher 用 taskkill /F 结束服务时 atexit 不会执行，所以它会先经 POST /api/system/logs/flush 调用这里。"""
    return _dedup.flush(force=force)


def shutdown_logging():
    """停止后台线程并写出所有未输出的去重摘要（atexit 自动调用）。"""
    _housekeeper.stop(flush=True)


_hooks_installed = False


def install_process_hooks():
    """未捕获异常（主线程/子线程）与 warnings 写入 error.log；原有行为（打印到 stderr）保留。"""
    global _hooks_installed
    with _setup_lock:
        if _hooks_installed:
            return
        _hooks_installed = True
    previous_sys_hook = sys.excepthook
    previous_thread_hook = threading.excepthook

    def _sys_hook(exc_type, exc, tb):
        if not issubclass(exc_type, KeyboardInterrupt):
            try:
                log.critical(f"未捕获异常，进程即将退出: {exc_type.__name__}: {exc}",
                             exc_info=(exc_type, exc, tb))
            except Exception:
                pass
        previous_sys_hook(exc_type, exc, tb)

    def _thread_hook(args):
        if args.exc_type is not SystemExit:
            name = args.thread.name if args.thread is not None else "?"
            try:
                log.error(f"线程 {name} 未捕获异常: {args.exc_type.__name__}: {args.exc_value}",
                          exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
            except Exception:
                pass
        previous_thread_hook(args)

    sys.excepthook = _sys_hook
    threading.excepthook = _thread_hook
    logging.captureWarnings(True)


_shared_handlers, _setup_problems = _build_handlers(LOG_DIR, on_rotate=_request_maintenance)
for _handler in _shared_handlers:
    _handler.addFilter(_dedup)
_housekeeper = LogHousekeeper(_dedup, periodic=claim_library_loggers)

# ── 全局日志实例 ─────────────────────────────────────
_logger = setup_logger()
log = LogContext(_logger)
claim_library_loggers()
for _problem in _setup_problems:
    log.warning(_problem)
atexit.register(shutdown_logging)
