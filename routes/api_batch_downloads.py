"""批量下载 API（core/batch_downloads.py）

POST /api/batch-downloads/preview  列出这次会下载什么（只读：不建任务、不写文件、不联网）
POST /api/batch-downloads/confirm  按确认时重新算出的清单一次性加入下载队列；清单变了（token 对不上）就什么都不写

加入队列之后只调一次调度：任务照常受定时下载时间段和同时下载数的限制，这里从不绕过它们直接开始下载。
"""
import re

from flask import Blueprint, jsonify, request

import core.database as db
from core import batch_downloads
from core.job_manager import job_manager
from core.logger import log
from core.scheduler import window_state
from core.settings import get_settings
from core.validation import validate_numeric

api_batch_downloads_bp = Blueprint("api_batch_downloads", __name__, url_prefix="/api/batch-downloads")

_TOKEN = re.compile(r"[0-9a-f]{32}")
_MAX_IDS = batch_downloads.MAX_ALBUMS


def _error(status, message, **extra):
    return jsonify({"status": "error", "message": message, **extra}), status


def _numeric_ids(value, limit):
    """[数字字符串 / 非 bool 整数]，最多 limit 个 → 去重后的字符串列表；不合规时 None"""
    if not isinstance(value, list) or len(value) > limit:
        return None
    ids = []
    for item in value:
        text = str(item) if isinstance(item, int) and not isinstance(item, bool) else item
        if not validate_numeric(text):
            return None
        ids.append(text)
    return list(dict.fromkeys(ids))


def _parse(body):
    """(kind, album_ids, 错误响应)"""
    if not isinstance(body, dict):
        return None, None, _error(400, "请求体需为 JSON 对象")
    kind = body.get("kind")
    if kind not in batch_downloads.KINDS:
        return None, None, _error(400, "kind 不对")
    raw = body.get("album_ids")
    if kind != "selected_favourites":
        if raw is not None:
            return None, None, _error(400, "album_ids 只用于下载选中的收藏")
        return kind, None, None
    album_ids = _numeric_ids(raw, _MAX_IDS)
    if not album_ids:
        return None, None, _error(400, f"请选择 1-{_MAX_IDS} 个收藏")
    return kind, album_ids, None


def _context() -> dict:
    """确认窗口的说明：定时下载时间段、队列、跳过已存在的文件"""
    try:
        max_running = int(db.get_setting("max_running_jobs", "1") or 1)
    except (TypeError, ValueError):
        max_running = 1
    return {
        "window": window_state(),
        "queue": {"ahead": db.queue_ahead(), "max_running": max_running},
        "settings": {"skip_existing": get_settings().get("skip_existing", "true") == "true"},
    }


def _preview_payload(kind, album_ids) -> dict:
    return {**batch_downloads.preview(kind, album_ids), **_context()}


@api_batch_downloads_bp.post("/preview")
def preview():
    """POST /api/batch-downloads/preview {kind, album_ids?}"""
    kind, album_ids, error = _parse(request.get_json(silent=True))
    if error:
        return error
    try:
        return jsonify(_preview_payload(kind, album_ids))
    except Exception as e:
        log.error(f"批量下载预览失败 kind={kind} error={e}")
        return _error(500, "没能列出要下载的内容")


@api_batch_downloads_bp.post("/confirm")
def confirm():
    """POST /api/batch-downloads/confirm {kind, token, exclude?, album_ids?}"""
    body = request.get_json(silent=True)
    kind, album_ids, error = _parse(body)
    if error:
        return error
    token = body.get("token")
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        return _error(400, "缺少确认信息，请重新打开清单")
    exclude = body.get("exclude")
    exclude = [] if exclude is None else _numeric_ids(exclude, _MAX_IDS)
    if exclude is None:
        return _error(400, f"exclude 只能是最多 {_MAX_IDS} 个车号")
    try:
        result = batch_downloads.confirm(kind, token, exclude, album_ids)
    except batch_downloads.Busy:
        return _error(409, "上一次确认还在处理，请稍后再试", reason="busy")
    except batch_downloads.NothingSelected:
        return _error(400, "没有勾选要下载的漫画", reason="nothing_selected")
    except batch_downloads.Stale:
        try:
            fresh = _preview_payload(kind, album_ids)
        except Exception as e:
            log.error(f"批量下载预览失败 kind={kind} error={e}")
            return _error(500, "没能列出要下载的内容")
        return _error(409, "清单有变化，已按最新情况重新列出，请再看一下后确认", reason="stale", preview=fresh)
    except Exception as e:
        log.error(f"批量下载确认失败 kind={kind} error={e}")
        return _error(500, "没能加入下载队列，没有创建任何任务，请重新打开清单")
    if result["created"]:
        try:
            job_manager.schedule_next()   # 任务已经写进队列；调度照常看定时下载时间段
        except Exception as e:
            log.error(f"批量下载后调度失败（任务仍在队列中）kind={kind} error={e}")
    context = _context()
    return jsonify({"status": "ok", **result, "window": context["window"], "queue": context["queue"]}), 201
