"""
System API 路由
"""
import json
import time
import os
import socket
import threading
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from flask import Blueprint, jsonify, request
from core.database import get_db, get_all_jobs
from core.logger import log, LOG_DIR, MAX_TOTAL_SIZE_MB, MAX_LOG_AGE_DAYS
from core.validation import validate_numeric
from core.jm_service import get_active_client_count, invalidate_option_cache, clear_album_detail_cache
from core import online_reader
from core.settings import build_jmcomic_option
from urllib.parse import urlparse
import re as _re

api_system_bp = Blueprint("api_system", __name__)

# 进程启动时间（用于计算 uptime）
_START_TIME = time.time()


@api_system_bp.get("/api/system/diagnose")
def diagnose():
    """系统自检端点，返回健康状态和诊断信息"""
    try:
        now = time.time()
        uptime_seconds = int(now - _START_TIME)
        issues = []
        level = "healthy"

        # ── 日志系统状态 ──
        log_files = sorted(
            [f for f in LOG_DIR.iterdir() if f.is_file() and f.name.startswith(('app', 'error'))],
            key=lambda f: f.stat().st_mtime, reverse=True,
        )
        total_log_size = 0
        main_files = []
        error_files = []
        for f in log_files:
            sz = f.stat().st_size
            total_log_size += sz
            if f.name.startswith('app'):
                main_files.append({"name": f.name, "size_kb": round(sz / 1024, 1)})
            elif f.name.startswith('error'):
                error_files.append({"name": f.name, "size_kb": round(sz / 1024, 1)})

        log_status = {
            "main_log": {"files": main_files[:10], "count": len(main_files)},
            "error_log": {"files": error_files[:10], "count": len(error_files)},
            "total_size_mb": round(total_log_size / 1024 / 1024, 2),
            "max_size_mb": MAX_TOTAL_SIZE_MB,
            "max_age_days": MAX_LOG_AGE_DAYS,
        }
        if total_log_size > MAX_TOTAL_SIZE_MB * 1024 * 1024 * 0.9:
            issues.append(f"日志总大小 {log_status['total_size_mb']:.1f}MB，接近上限 {MAX_TOTAL_SIZE_MB}MB")
            level = "degraded"

        # ── 应用状态 ──
        db_ok = False
        try:
            conn = get_db()
            conn.execute("SELECT 1").fetchone()
            conn.close()
            db_ok = True
        except Exception:
            issues.append("数据库连接异常")
            level = "unhealthy"

        # ── 错误日志最近 24h 分析 ──
        _error_pattern = _re.compile(r'\b(ERROR|CRITICAL)\b')
        errors_24h = 0
        recent_errors = []
        cutoff = datetime.now().timestamp() - 86400
        _MAX_SCAN_SIZE = 10 * 1024 * 1024
        for f in log_files:
            if not f.name.startswith('error'):
                continue
            if f.stat().st_mtime < cutoff:
                continue
            if f.stat().st_size > _MAX_SCAN_SIZE:
                continue
            try:
                with open(f, 'r', encoding='utf-8', errors='replace') as fh:
                    for line in iter(fh.readline, ""):
                        if fh.tell() > _MAX_SCAN_SIZE:
                            break
                        if _error_pattern.search(line):
                            errors_24h += 1
                            if len(recent_errors) < 5:
                                recent_errors.append(line.strip()[:200])
            except Exception:
                pass

        if errors_24h > 20:
            issues.append(f"最近 24 小时错误 {errors_24h} 次（阈值 20）")
            if level == "healthy":
                level = "degraded"

        active_count = 0
        queued_count = 0
        try:
            all_jobs = get_all_jobs()
            active_count = sum(1 for j in all_jobs if j.get("status") == "running")
            queued_count = sum(1 for j in all_jobs if j.get("status") == "queued")
        except Exception:
            pass

        # ── jmcomic API 域名可达性检查 ──
        jm_domain = ""
        jm_reachable = False
        try:
            opt = build_jmcomic_option()
            domain_list = opt.get("client", {}).get("domain", [])
            if domain_list:
                jm_domain = domain_list[0]
            else:
                from jmcomic import JmModuleConfig
                defaults = JmModuleConfig.default_dict()
                domain_list = defaults.get("client", {}).get("domain", [])
                jm_domain = domain_list[0] if domain_list else ""
            if jm_domain:
                parsed = urlparse(jm_domain)
                host = parsed.hostname or jm_domain
                port = parsed.port or 443
                sock = socket.create_connection((host, port), timeout=3)
                sock.close()
                jm_reachable = True
        except Exception:
            jm_reachable = False
            if jm_domain:
                issues.append(f"jmcomic API 域名 {jm_domain} 不可达")
                if level == "healthy":
                    level = "degraded"

        return jsonify({
            "status": "ok",
            "health": level,
            "uptime": {
                "seconds": uptime_seconds,
                "since": datetime.fromtimestamp(_START_TIME).strftime("%Y-%m-%d %H:%M:%S"),
            },
            "log_system": log_status,
            "application": {
                "db_ok": db_ok,
                "active_jobs": active_count,
                "queued_jobs": queued_count,
            },
            "jmcomic": {
                "domain": jm_domain,
                "reachable": jm_reachable,
                "active_clients": get_active_client_count(),
            },
            "warnings": issues,
            "error_count_24h": errors_24h,
        })
    except Exception as e:
        log.exception(f"诊断过程异常 error={e}")
        return jsonify({"status": "error", "message": "诊断过程出错", "health": "unhealthy"}), 500


@api_system_bp.get("/api/system/health")
def health_ping():
    """轻量健康检查"""
    log.debug("API健康检查")
    db_ok = False
    try:
        conn = get_db()
        conn.execute("SELECT 1").fetchone()
        conn.close()
        db_ok = True
    except Exception:
        pass
    if not db_ok:
        return jsonify({"status": "error", "message": "数据库连接不可用", "db": "unreachable", "ts": datetime.now().isoformat()}), 503
    return jsonify({
        "status": "ok",
        "ts": datetime.now().isoformat(),
        "application": "jmcomic-local-console",
        "pid": os.getpid(),
        "jm_active_clients": get_active_client_count(),
    })


@api_system_bp.post("/api/system/clear-cache")
def clear_jm_comic_cache():
    """清除 jmcomic 客户端缓存（Option + Client）、详情缓存与在线阅读图片缓存，强制下一次请求重建。"""
    log.info("API清除 jmcomic 缓存")
    try:
        invalidate_option_cache()
        clear_album_detail_cache()
        online_reader.clear_cache()
        return jsonify({"status": "ok", "message": "jmcomic 缓存已清除"})
    except Exception as e:
        log.error(f"清除 jmcomic 缓存失败: {e}")
        return jsonify({"status": "error", "message": "清除缓存失败"}), 500


# ════════════════════════════════════════════════════════════════
# 日志查看器（设置页「日志与诊断」）
# ════════════════════════════════════════════════════════════════
# 只读取 LOG_DIR 下名字固定的 app.log / error.log（及其轮转副本 .YYYY-MM-DD[.N]），
# 从不接受用户传入的路径；从文件末尾分块倒读，每个请求最多读 _LOG_READ_CAP_BYTES 字节
# （带搜索词时 _LOG_QUERY_READ_CAP_BYTES），实际上限在响应的 read_limit_mb 里告诉前端。

import core.logger as _core_logger  # noqa: E402  调用时读取模块属性，测试可 monkeypatch LOG_DIR

_LOG_SOURCES = {"error": "error.log", "app": "app.log"}
_LOG_READ_CAP_BYTES = 4 * 1024 * 1024
# 搜索（含“只看此请求”）要能找到几天前的记录；16MB 约 1 秒，仍远小于日志总上限
_LOG_QUERY_READ_CAP_BYTES = 16 * 1024 * 1024
_LOG_BLOCK_BYTES = 256 * 1024
_LOG_LIMIT_DEFAULT = 200
_LOG_LIMIT_MAX = 500
_LOG_QUERY_MAX_CHARS = 200
_LOG_MESSAGE_MAX_CHARS = 8000
_LOG_CONTINUATION_MAX_LINES = 200
_LOG_EXTRA_MAX_CHARS = 4000
_LOG_FILES_LIST_MAX = 60
# 读取取消（见 _claim_log_read）：每个查看器最新一次读取的 seq，最多记住 _LOG_VIEWERS_MAX 个查看器
_LOG_VIEWER_RE = _re.compile(r"[A-Za-z0-9_-]{1,64}")
_LOG_VIEWERS_MAX = 64
_log_reads_lock = threading.Lock()
_log_reads: OrderedDict = OrderedDict()

# 与写入端共用同一个日志头定义：StructuredFormatter 保证续行（traceback、被单行化的消息）不会匹配它
_LOG_HEADER_RE = _core_logger.LOG_HEADER_RE
_LOG_REQUEST_ID_RE = _re.compile(r"\breq_[a-z0-9]{8}\b")


def _log_dir() -> Path:
    return Path(_core_logger.LOG_DIR)


def _log_files_for(source: str, log_dir: Path) -> list[Path]:
    """某个来源的可读文件，最新在前：当前文件 → 轮转副本。只认固定文件名。

    轮转副本 <base>.YYYY-MM-DD 在同一天再次轮转（超过单文件上限、重启时目标已存在）时依次命名为
    .1、.2 …，序号越大越新（core.logger.rotation_target），所以按 (日期, 序号) 倒序，序号按数字比较。"""
    base = _LOG_SOURCES[source]
    dated_re = _re.compile(_re.escape(base) + r"\.(\d{4}-\d{2}-\d{2})(?:\.(\d{1,6}))?")
    try:
        root = log_dir.resolve()
        names = [p.name for p in log_dir.iterdir()]
    except OSError:
        return []
    dated = []
    for name in names:
        match = dated_re.fullmatch(name)
        if match:
            dated.append(((match.group(1), int(match.group(2) or 0)), name))
    dated = [name for _, name in sorted(dated, reverse=True)]
    result = []
    for name in ([base] if base in names else []) + dated:
        path = log_dir / name
        try:
            if path.is_symlink() or not path.is_file() or path.resolve().parent != root:
                continue
        except OSError:
            continue
        result.append(path)
    return result


def _split_trailing_json(text: str):
    """拆出行尾 " {json}" 附加字段；返回 (正文, dict 或 None)。"""
    if not text.endswith("}"):
        return text, None
    idx = len(text)
    for _ in range(8):  # 从右往左找能完整解析为对象的最短后缀
        idx = text.rfind(" {", 0, idx)
        if idx < 0:
            break
        try:
            value = json.loads(text[idx + 1:])
        except ValueError:
            continue
        if isinstance(value, dict):
            return text[:idx], value
    return text, None


def _build_log_entry(time_str: str, level: str, logger_name: str, head: str, cont: list[str]) -> tuple:
    """返回 (entry, haystack)：entry 为接口输出，haystack 为小写化的检索文本。"""
    message, extra = _split_trailing_json(head)
    while cont and not cont[-1].strip():
        cont.pop()
    if extra is None and cont:
        # 异常堆栈时 JSON 附在最后一行之后
        last, last_extra = _split_trailing_json(cont[-1])
        if last_extra is not None and "request_id" in last_extra:
            cont[-1], extra = last, last_extra
    if cont:
        message = message + "\n" + "\n".join(cont)
    if len(message) > _LOG_MESSAGE_MAX_CHARS:
        message = message[:_LOG_MESSAGE_MAX_CHARS] + "…（已截断）"
    extra = dict(extra or {})
    request_id = extra.pop("request_id", None) or ""
    # 合并了多个请求的去重摘要带 request_ids 列表：不属于其中任何一个，不能把第一个 ID 当成它的请求。
    # 列表留在 extra 里（检索文本包含它），“只看此请求”搜索其中任一 ID 都能找到这条摘要。
    if not request_id and "request_ids" not in extra:
        match = _LOG_REQUEST_ID_RE.search(head)
        request_id = match.group(0) if match else ""
    entry = {
        "time": time_str,
        "level": level,
        "logger": logger_name,
        "message": message,
        "request_id": str(request_id),
    }
    extra_text = ""
    if extra:
        extra_text = json.dumps(extra, ensure_ascii=False, default=str)
        if len(extra_text) > _LOG_EXTRA_MAX_CHARS:
            extra = {"raw": extra_text[:_LOG_EXTRA_MAX_CHARS] + "…"}
        entry["extra"] = extra
    haystack = " ".join((time_str, level, logger_name, message, entry["request_id"], extra_text))
    return entry, haystack.casefold()


def _parse_log_lines(lines: list[str]) -> list:
    """把以日志头开始的一段行解析为 (entry, haystack) 列表（时间正序）。续行挂到上一条。"""
    parsed = []
    current = None

    def flush():
        if current is not None:
            parsed.append(_build_log_entry(*current))

    for text in lines:
        m = _LOG_HEADER_RE.match(text)
        if m:
            flush()
            current = (m.group(1), m.group(2), m.group(3), m.group(4) or "", [])
        elif current is not None:
            if len(current[4]) < _LOG_CONTINUATION_MAX_LINES:
                current[4].append(text)
        elif text.strip():
            # 文件开头无日志头的行（格式不符），独立成条，避免丢失
            parsed.append(_build_log_entry("", "", "", text, []))
    flush()
    return parsed


def _decode_line(raw: bytes) -> str:
    return raw.decode("utf-8", "replace").rstrip("\r")


def _carried_lines(tail: list, limit: int | None) -> list[str]:
    """挂起区 tail（文件倒序存放的若干块，每块以 b"\\n" 开头，行不跨块）按文件顺序的前 limit 行
    （None：全部）。只解码用得到的行：_parse_log_lines 每条记录最多保留 _LOG_CONTINUATION_MAX_LINES 行续行。"""
    lines = []
    for chunk in reversed(tail):
        lines.extend(chunk.split(b"\n")[1:])
        if limit is not None and len(lines) >= limit:
            del lines[limit:]
            break
    return [_decode_line(raw) for raw in lines]


def _iter_log_entries_newest_first(path: Path, state: dict):
    """从文件末尾按块倒读，逐条产出 (entry, haystack)，最新在前；受 state["budget"] 字节上限约束。

    一条记录的续行可能跨越很多块（几 MB 的响应体、traceback）。还没找到它的日志头时，已读的续行只按块
    挂起，不再随每个新块重新解码、重新匹配——那样一段 16MB 的无日志头内容要花 3 秒、上百 MB 内存。
    每块只处理新读到的行；找到日志头时只解码挂起区里会被保留的那些行。"""
    try:
        fh = open(path, "rb")
    except OSError:
        return
    with fh:
        fh.seek(0, os.SEEK_END)
        pos = fh.tell()
        # 还没找到日志头的区域（按文件顺序）= head + tail：
        #   head_parts：开头那一行已读到的部分（该行始于更早的块），文件倒序存放的若干块；
        #   tail：其后已检查过、确定不是日志头的行，文件倒序存放，每块以 b"\n" 开头（行不跨块）。
        head_parts, tail = [], []
        while pos > 0:
            if not state["is_current"]():
                state["superseded"] = True  # 同一个查看器已开始更新的读取，本次不再继续扫描
                return
            if state["budget"] <= 0:
                state["truncated"] = True
                return
            step = min(_LOG_BLOCK_BYTES, pos, state["budget"])
            pos -= step
            fh.seek(pos)
            block = fh.read(step)
            state["budget"] -= step
            if pos > 0 and b"\n" not in block:
                head_parts.append(block)  # 整块都在同一行里（一行超过一个块）
                continue
            head_parts.append(block)
            raw_lines = b"".join(reversed(head_parts)).split(b"\n")
            head_parts = []
            first = 1 if pos > 0 else 0  # 未到文件开头时，第一行可能只读到一半
            texts = [_decode_line(raw) for raw in raw_lines[first:]]
            if pos > 0:
                header_at = next((i for i, t in enumerate(texts) if _LOG_HEADER_RE.match(t)), None)
                if header_at is None:
                    # 整块都是某条日志的续行：挂起，继续往前读找它的日志头
                    head_parts = [raw_lines[0]]
                    tail.append(b"\n" + b"\n".join(raw_lines[1:]))
                    continue
                before = raw_lines[1:first + header_at]
                texts = texts[header_at:]
                has_header = True
            else:
                before = []
                has_header = any(_LOG_HEADER_RE.match(t) for t in texts)
            if tail:
                # 挂起的行是最后一条记录的续行（最多保留那么多行）；文件开头一个日志头都没有时，
                # 它们各自独立成条，需要全部解码
                texts += _carried_lines(tail, _LOG_CONTINUATION_MAX_LINES if has_header else None)
            # 本块日志头之前的行（及只读到一半的第一行）属于更早的记录，留给下一块
            head_parts = [raw_lines[0]] if pos > 0 else []
            tail = [b"\n" + b"\n".join(before)] if before else []
            for item in reversed(_parse_log_lines(texts)):
                yield item


def _read_cap_bytes(query: str) -> int:
    return _LOG_QUERY_READ_CAP_BYTES if query else _LOG_READ_CAP_BYTES


def _claim_log_read(viewer: str, seq: int):
    """登记查看器 viewer 的第 seq 次读取，返回 is_current()：同一查看器开始了 seq 更大的读取后返回 False。

    设置页每输入一个字（300ms 防抖）就发起一次读取并中止上一次，但浏览器中止请求并不会让服务器停止扫描：
    慢慢输入一个请求 ID 会叠起好几个同时进行的 16MB 扫描，它们争抢 GIL，最后真正需要的那次要等好几秒，
    阅读器取图、下载等其他请求也跟着变慢。所以同一查看器的旧读取在下一个块边界（约 256KB）处自行停止。
    按 seq 而不是到达顺序比较：先发出的旧请求晚到服务器时，也不会反过来取消新的请求。
    没带 viewer/seq 的调用（脚本、测试）从不被取消；不同查看器（多个标签页）互不影响。"""
    if not viewer or seq <= 0:
        return lambda: True
    with _log_reads_lock:
        latest = _log_reads.get(viewer, 0)
        if seq > latest:
            _log_reads[viewer] = seq
        _log_reads.move_to_end(viewer)
        while len(_log_reads) > _LOG_VIEWERS_MAX:
            _log_reads.popitem(last=False)
    return lambda: _log_reads.get(viewer, seq) <= seq


def _read_log_entries(source: str, query: str, limit: int, log_dir: Path, is_current=None):
    """返回 (entries, truncated, superseded)。superseded=True：同一查看器已开始更新的读取，本次提前结束。"""
    state = {"budget": _read_cap_bytes(query), "truncated": False, "superseded": False,
             "is_current": is_current or (lambda: True)}
    needle = query.casefold()
    entries = []
    for path in _log_files_for(source, log_dir):
        if state["superseded"] or not state["is_current"]():
            state["superseded"] = True
            break
        if state["budget"] <= 0:
            state["truncated"] = True  # 还有更早的文件没读
            break
        items = _iter_log_entries_newest_first(path, state)
        try:
            for entry, haystack in items:
                if needle and needle not in haystack:
                    continue
                entries.append(entry)
                if len(entries) >= limit:
                    return entries, state["truncated"], state["superseded"]
        finally:
            items.close()  # 及时释放文件句柄（Windows 上占用会妨碍日志轮转）
    return entries, state["truncated"], state["superseded"]


def _log_dir_files(log_dir: Path):
    """LOG_DIR 下的文件概况（只 stat，不读内容），按修改时间倒序。"""
    files = []
    total = 0
    try:
        children = list(log_dir.iterdir())
    except OSError:
        return [], 0
    for path in children:
        try:
            if path.is_symlink() or not path.is_file():
                continue
            st = path.stat()
        except OSError:
            continue
        total += st.st_size
        files.append((st.st_mtime, path.name, st.st_size))
    files.sort(reverse=True)
    listed = [
        {
            "name": name,
            "size_kb": round(size / 1024, 1),
            "modified": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S"),
        }
        for mtime, name, size in files[:_LOG_FILES_LIST_MAX]
    ]
    return listed, total


@api_system_bp.get("/api/system/logs")
def read_logs():
    """读取最近日志（最新在前）。source=error（WARNING+，默认）| app（INFO+）；q 子串过滤；limit ≤ 500。
    viewer + seq（设置页每次读取递增）：同一查看器有更新的读取开始后，本次读取提前结束并返回 409。"""
    source = (request.args.get("source") or "error").strip().lower()
    if source not in _LOG_SOURCES:
        return jsonify({"status": "error", "message": "source 仅支持 error 或 app"}), 400
    query = (request.args.get("q") or "").strip()[:_LOG_QUERY_MAX_CHARS]
    try:
        limit = int(request.args.get("limit", _LOG_LIMIT_DEFAULT))
    except (TypeError, ValueError):
        limit = _LOG_LIMIT_DEFAULT
    limit = max(1, min(limit, _LOG_LIMIT_MAX))
    viewer = request.args.get("viewer") or ""
    try:
        seq = int(request.args.get("seq", 0))
    except (TypeError, ValueError):
        seq = 0
    if not _LOG_VIEWER_RE.fullmatch(viewer):
        viewer = ""

    log_dir = _log_dir()
    try:
        entries, truncated, superseded = _read_log_entries(
            source, query, limit, log_dir, _claim_log_read(viewer, seq))
        if superseded:
            return jsonify({"status": "error", "superseded": True,
                            "message": "已有更新的日志读取，本次已取消"}), 409
        files, total_bytes = _log_dir_files(log_dir)
    except Exception as e:
        log.error(f"读取日志失败 error={e}")
        return jsonify({"status": "error", "message": "读取日志失败"}), 500
    return jsonify({
        "status": "ok",
        "source": source,
        "query": query,
        "limit": limit,
        "entries": entries,
        # truncated=True：读满 read_limit_mb 仍有更早的内容没读（搜索结果为空时也不代表“没有”）
        "truncated": truncated,
        "read_limit_mb": round(_read_cap_bytes(query) / 1024 / 1024, 2),
        "files": files,
        "total_size_mb": round(total_bytes / 1024 / 1024, 2),
        "retention_days": _core_logger.MAX_LOG_AGE_DAYS,
        "log_dir": str(log_dir),
    })


@api_system_bp.post("/api/system/logs/flush")
def flush_logs():
    """立即写出所有未输出的去重摘要（“↑ 同类消息 N 次已合并”）。

    launcher 结束服务用的是 taskkill /F，进程退出时的 atexit 刷新不会执行；它在强制结束前先调用这里，
    停止/重启前最后 30–120 秒内被合并的重复次数才不会丢。只写出已有的计数，没有其他副作用：
    还没有重复的消息保持原来的合并窗口，服务继续运行时也不会因为这次调用把它们再完整记录一遍。"""
    flushed = _core_logger.flush_log_summaries(force=True)
    return jsonify({"status": "ok", "flushed": flushed})


@api_system_bp.post("/api/system/logs/open-folder")
def open_log_folder():
    """在资源管理器中打开日志目录（仅 Windows）。"""
    startfile = getattr(os, "startfile", None)
    if startfile is None:
        return jsonify({"status": "error", "message": "当前系统不支持直接打开文件夹，请手动前往日志目录"}), 501
    log_dir = _log_dir()
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        startfile(str(log_dir))
        log.info("API打开日志文件夹")
        return jsonify({"status": "ok", "message": "已打开日志文件夹"})
    except Exception as e:
        log.warning(f"API打开日志文件夹失败 error={e}")
        return jsonify({"status": "error", "message": "打开日志文件夹失败"}), 500
