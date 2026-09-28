"""
检查新章节 API（core/update_checker.py + core/update_store.py）

只有 POST /api/updates/<album_id>/check 会联网（一次只取一次章节列表，从不下载）；
其余接口只读数据库、本地文件（core.local_availability）和内存里的运行状态。
本地没有能读的已下载内容的漫画（只收藏的、文件删了的）什么都不返回：旧的检查记录不会露出来。
"""
from datetime import timedelta

from flask import Blueprint, jsonify, request

import core.database as db
from core import local_availability, update_checker, update_store
from core.local_availability import MAX_IDS
from core.logger import log
from core.settings import get_settings
from core.validation import validate_numeric
from routes.api_library import readable_among

api_updates_bp = Blueprint("api_updates", __name__, url_prefix="/api/updates")

_BAD_ID = {"status": "error", "message": "album_id 必须是纯数字"}


def _auto_enabled() -> bool:
    return get_settings().get("auto_update_check", "true") == "true"


def _state_payload(album_id: str) -> dict:
    """GET /api/updates/<id> 的内容；本地不可读时只有 eligible: false。"""
    if not local_availability.is_readable(album_id):
        return {"status": "ok", "album_id": album_id, "eligible": False}
    auto = _auto_enabled()
    checking = update_checker.is_checking(album_id)
    row = update_store.get(album_id)
    paused = update_checker.paused_until() if auto else None
    effective = None
    if auto:
        candidates = [value for value in ((row or {}).get("next_check_at"), paused) if value]
        effective = max(candidates) if candidates else None
    return {
        "status": "ok", "album_id": album_id, "eligible": True,
        "auto_enabled": auto, "checking": checking,
        "update": update_store.describe(row, auto, checking, effective, paused),
    }


@api_updates_bp.get("/<album_id>")
def update_state(album_id: str):
    """一部漫画的检查状态 GET /api/updates/<album_id>（只读）"""
    if not validate_numeric(album_id):
        return jsonify(_BAD_ID), 400
    try:
        return jsonify(_state_payload(album_id))
    except Exception as e:
        log.error(f"读取检查新章节状态失败 album_id={album_id} error={e}")
        return jsonify({"status": "error", "message": "读取检查状态失败"}), 500


@api_updates_bp.post("/<album_id>/check")
def check_now(album_id: str):
    """立即检查 POST /api/updates/<album_id>/check（请求体忽略；自动检查关着时也能用）。
    在本请求线程里同步执行：最多等 20 秒拿锁，再加一次检查自己的时限。只取章节列表，从不下载。"""
    if not validate_numeric(album_id):
        return jsonify(_BAD_ID), 400
    try:
        result = update_checker.check_album(album_id, "manual", lock_wait=update_checker.MANUAL_LOCK_WAIT)
    except update_checker.NotTarget:
        return jsonify({"status": "error", "reason": "not_target",
                        "message": "本地还没有已下载的内容，不检查新章节"}), 409
    except update_checker.Busy:
        return jsonify({"status": "error", "reason": "busy", "message": "正在检查别的漫画，请稍后再试"}), 409
    except Exception as e:
        log.error(f"检查新章节失败 album_id={album_id} error={e}")
        return jsonify({"status": "error", "message": "检查新章节失败"}), 500
    try:
        payload = _state_payload(album_id)
    except Exception as e:
        log.error(f"读取检查新章节状态失败 album_id={album_id} error={e}")
        return jsonify({"status": "error", "message": "检查新章节失败"}), 500
    payload["outcome"] = result["outcome"]
    return jsonify(payload)


@api_updates_bp.post("/states")
def update_states():
    """列表用 POST /api/updates/states {album_ids: [...]}（最多 200 个）：
    只返回本地可读、已确认有新章节的 → {updates: {id: {state, new_count, removed_count, confirmed_at, titles}}}"""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"status": "error", "message": "请求体需为 JSON 对象"}), 400
    album_ids = body.get("album_ids")
    if not isinstance(album_ids, list):
        return jsonify({"status": "error", "message": "缺少 album_ids 列表"}), 400
    if len(album_ids) > MAX_IDS:
        return jsonify({"status": "error", "message": f"album_ids 数量过多，最大 {MAX_IDS}"}), 400
    ids = []
    for aid in album_ids:
        # bool 是 int 的子类，必须单独排除；负数/小数转成字符串后不是纯数字，会被拒绝
        text = str(aid) if isinstance(aid, int) and not isinstance(aid, bool) else aid
        if not validate_numeric(text):
            return jsonify({"status": "error", "message": "album_ids 只能包含纯数字"}), 400
        ids.append(text)
    try:
        readable = local_availability.readable_album_ids(ids)
        updates = update_store.summaries([aid for aid in dict.fromkeys(ids) if aid in readable])
    except Exception as e:
        log.error(f"读取检查新章节状态失败 count={len(ids)} error={e}")
        return jsonify({"status": "error", "message": "读取检查状态失败"}), 500
    return jsonify({"status": "ok", "updates": updates})


@api_updates_bp.get("/summary")
def update_summary():
    """设置页的状态面板 GET /api/updates/summary（只读）"""
    try:
        enabled = _auto_enabled()
        runtime = update_checker.runtime_status()
        if not enabled:
            runtime["phase"] = "off"
        elif not runtime["armed"] or not runtime["running"]:
            runtime["phase"] = "not_running"
        elif runtime["phase"] == "off":
            runtime["phase"] = "idle"   # 刚打开开关：后台下一轮（1 分钟内）就会继续
        if runtime["phase"] not in ("starting", "paused"):
            runtime["resume_at"] = None
        targets = readable_among(db.get_completed_album_ids())
        return jsonify({
            "status": "ok",
            "enabled": enabled,
            "runtime": runtime,
            "counts": update_store.overview(targets),
            "checks_24h": update_store.checks_since(update_checker._now() - timedelta(hours=24)),
            "last": update_store.last_check(targets),
        })
    except Exception as e:
        log.error(f"读取检查新章节概况失败 error={e}")
        return jsonify({"status": "error", "message": "读取检查状态失败"}), 500


@api_updates_bp.get("/pending")
def pending_updates():
    """PR-B 的输入 GET /api/updates/pending（只读）：已确认、可以下载的新章节（不含“章节有变动”的），
    只含本地可读的漫画。"""
    try:
        items = update_store.pending_new_chapters()
        readable = readable_among(item["album_id"] for item in items)
        return jsonify({"status": "ok", "items": [item for item in items if item["album_id"] in readable]})
    except Exception as e:
        log.error(f"读取待下载的新章节失败 error={e}")
        return jsonify({"status": "error", "message": "读取待下载的新章节失败"}), 500
