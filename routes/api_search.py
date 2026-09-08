"""
搜索 API & 搜索历史
"""

from flask import Blueprint, jsonify, request

from core.database import add_search_history, get_search_history, delete_search_history, clear_search_history
from core.jm_service import search_albums
from core.logger import log
from core.validation import validate_numeric  # P1-8 统一 album_id 校验

api_search_bp = Blueprint("api_search", __name__, url_prefix="/api")

_VALID_SORTS = {"latest", "views", "likes", "pictures"}


@api_search_bp.get("/search")
def search():
    """搜索漫画（GET /api/search?q=&page=&page_size=&sort=）"""
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"status": "error", "message": "缺少搜索关键词"}), 400
    if len(q) > 200:
        return jsonify({"status": "error", "message": "搜索关键词过长，最大 200 字符"}), 400
    page = request.args.get("page", 1, type=int)
    if not page or page < 1:
        page = 1
    page = min(page, 500)
    sort = request.args.get("sort", "latest")
    if sort not in _VALID_SORTS:
        sort = "latest"
    page_size = request.args.get("page_size", 20, type=int)
    if not page_size or page_size < 1:
        page_size = 20
    page_size = min(page_size, 100)
    log.info(f"API搜索 q={q} page={page} page_size={page_size} sort={sort}")

    try:
        result = search_albums(keyword=q, page=page, page_size=page_size, sort=sort)
        add_search_history(q)
        raw_items = result.get("items", [])
        items = raw_items[:page_size]
        return jsonify({
            "status": "ok",
            "items": items,
            "total": result["total"],
            "page": result["page"],
            "page_size": page_size,
        })
    except Exception as e:
        log.error(f"API搜索失败 q={q} error={e}")
        err_msg = str(e)
        if "ConnectionError" in err_msg or "connection" in err_msg.lower():
            return jsonify({"status": "error", "message": "无法连接到 18comic 服务器，请检查网络连接"}), 502
        if "超时" in err_msg:
            return jsonify({"status": "error", "message": "搜索超时，请检查关键词或稍后重试"}), 504
        return jsonify({"status": "error", "message": "搜索失败，请稍后重试"}), 500


@api_search_bp.get("/search-history")
def search_history():
    """返回最近的搜索记录(已去重)"""
    try:
        items = get_search_history(limit=20)
        return jsonify({"status": "ok", "items": items})
    except Exception as e:
        log.error(f"获取搜索历史失败 error={e}")
        return jsonify({"status": "error", "message": "获取搜索历史失败"}), 500


@api_search_bp.delete("/search-history/<string:keyword>")
def delete_search_history_route(keyword: str):
    """删除指定的搜索历史"""
    keyword = keyword.strip()
    if not keyword:
        return jsonify({"status": "error", "message": "缺少 keyword"}), 400
    try:
        deleted = delete_search_history(keyword)
        return jsonify({"status": "ok", "deleted": deleted})
    except Exception as e:
        log.error(f"删除搜索历史失败 keyword={keyword} error={e}")
        return jsonify({"status": "error", "message": "删除失败"}), 500


@api_search_bp.post("/search-history/clear")
def clear_search_history_route():
    """清空所有搜索历史"""
    try:
        total = clear_search_history()
        return jsonify({"status": "ok", "deleted": total})
    except Exception as e:
        log.error(f"清空搜索历史失败 error={e}")
        return jsonify({"status": "error", "message": "清空失败"}), 500
