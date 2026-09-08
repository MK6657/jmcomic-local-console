"""
资源库 + 标签管理 API
"""
import re
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

from flask import Blueprint, jsonify, request

import core.database as db
from core.jm_service import get_album_detail, get_album_detail_cached
from core.logger import log
from core.validation import validate_numeric  # 统一 album_id 纯数字校验

api_library_bp = Blueprint("api_library", __name__, url_prefix="/api/library")


def _sanitize_tag(tag: str) -> str:
    """清洗标签：去空格、小写、截断"""
    return tag.strip().lower()[:50]


# 排序白名单
_VALID_SORTS_LIBRARY = {"updated_at", "title", "album_id", "added_at"}
# 状态白名单
_VALID_STATUSES = {"downloaded", "queued", "none", "undownloaded"}


# ─── 资源库列表 ───


@api_library_bp.get("/", strict_slashes=False)
def library_list():
    """资源库列表 GET /api/library?page=&page_size=&tag=&q=&status=&sort="""
    try:
        page = request.args.get("page", 1, type=int)
        page_size = request.args.get("page_size", 50, type=int)
        page = max(1, min(page, 500))
        page_size = max(1, min(page_size, 200))
        tag = request.args.get("tag", None, type=str)
        if tag:
            tag = tag.strip()[:100] or None
        keyword = request.args.get("q", "", type=str).strip()[:200]
        status = request.args.get("status", None, type=str)
        if status:
            status = status.strip()[:20] or None
            if status and status not in _VALID_STATUSES:
                status = None
            # 前端 "undownloaded" 对应数据库中的 "none"
            if status == "undownloaded":
                status = "none"
        sort = request.args.get("sort", "updated_at", type=str).strip()[:50]
        if sort not in _VALID_SORTS_LIBRARY:
            sort = "updated_at"

        result = db.get_library(
            page=page,
            page_size=page_size,
            tag=tag,
            keyword=keyword,
            status=status,
            sort=sort,
        )
        log.info(
            f"API资源库列表 结果数={result['total']} page={page} tag={tag} "
            f"q={keyword} status={status} sort={sort}"
        )
        return jsonify({"status": "ok", **result})
    except Exception as e:
        log.error(f"API资源库列表失败 error={e}")
        return jsonify({"status": "error", "message": "获取资源库列表失败"}), 500


# ─── 标签云 ───


@api_library_bp.get("/tags")
def tag_cloud():
    """标签云 GET /api/library/tags?min_count="""
    try:
        min_count = request.args.get("min_count", 1, type=int)
        tags = db.get_all_tags(min_count=min_count)
        return jsonify({"status": "ok", "tags": tags})
    except Exception as e:
        log.error(f"API标签云失败 error={e}")
        return jsonify({"status": "error", "message": "获取标签云失败"}), 500


# ─── 统计信息 ───


@api_library_bp.get("/stats")
def library_stats():
    """统计信息 GET /api/library/stats"""
    try:
        stats = db.get_library_stat()
        return jsonify({"status": "ok", **stats})
    except Exception as e:
        log.error(f"API资源库统计失败 error={e}")
        return jsonify({"status": "error", "message": "获取统计信息失败"}), 500


# ─── 单个条目详情 ───


@api_library_bp.get("/<album_id>")
def library_item(album_id: str):
    """单个条目详情 GET /api/library/<album_id>"""
    if not validate_numeric(album_id):
        return jsonify({"status": "error", "message": "album_id 必须是纯数字"}), 400
    try:
        # album_id 精确匹配（SQL 层 WHERE ai.album_id = ?），
        # 不走 keyword LIKE 模糊通道，避免命中标题/作者含相同数字串的其他条目
        result = db.get_library(page=1, page_size=1, album_id=album_id)
        item = result["items"][0] if result["items"] else None
        if not item:
            return jsonify({"status": "error", "message": "未找到该条目"}), 404
        return jsonify({"status": "ok", "item": item})
    except Exception as e:
        log.error(f"API资源库详情失败 album_id={album_id} error={e}")
        return jsonify({"status": "error", "message": "获取条目详情失败"}), 500


# ─── 某漫画的标签列表 ───


@api_library_bp.get("/<album_id>/tags")
def album_tags_list(album_id: str):
    """获取某漫画的标签 GET /api/library/<album_id>/tags"""
    if not validate_numeric(album_id):
        return jsonify({"status": "error", "message": "album_id 必须是纯数字"}), 400
    try:
        tags = db.get_album_tags(album_id)
        return jsonify({"status": "ok", "tags": tags})
    except Exception as e:
        log.error(f"API获取标签失败 album_id={album_id} error={e}")
        return jsonify({"status": "error", "message": "获取标签失败"}), 500


# ─── 添加用户自定义标签 ───


@api_library_bp.post("/<album_id>/tags")
def album_tags_add(album_id: str):
    """添加自定义标签 POST /api/library/<album_id>/tags {tags: [...]}"""
    if not validate_numeric(album_id):
        return jsonify({"status": "error", "message": "album_id 必须是纯数字"}), 400
    try:
        body = request.get_json(force=True)
        raw_tags = body.get("tags", [])
        if not raw_tags or not isinstance(raw_tags, list):
            return jsonify({"status": "error", "message": "缺少 tags 列表"}), 400
        if len(raw_tags) > 200 or any(not isinstance(tag, str) for tag in raw_tags):
            return jsonify(status="error", message="tags 必须是最多 200 个字符串"), 400

        added = 0
        skipped = 0
        for t in raw_tags:
            cleaned = _sanitize_tag(str(t))
            if not cleaned:
                skipped += 1
                continue
            if db.add_album_tag(album_id, cleaned, source="user"):
                added += 1
            else:
                skipped += 1

        log.info(f"API添加标签 album_id={album_id} added={added} skipped={skipped}")
        return jsonify({"status": "ok", "added": added, "skipped": skipped})
    except Exception as e:
        log.error(f"API添加标签失败 album_id={album_id} error={e}")
        return jsonify({"status": "error", "message": "添加标签失败"}), 500


# ─── 删除标签 ───


@api_library_bp.delete("/<album_id>/tags")
def album_tags_delete(album_id: str):
    """删除标签 DELETE /api/library/<album_id>/tags
    Request Body (可选):
      {"tags": ["tag1"]}      — 删除指定标签(不限 source)
      {"source": "auto"}      — 清空所有自动同步标签
      {}                       — 删除所有标签
    """
    if not validate_numeric(album_id):
        return jsonify({"status": "error", "message": "album_id 必须是纯数字"}), 400
    try:
        body = request.get_json(force=True) or {}
        total_deleted = 0
        if (not isinstance(body, dict)
                or ("tags" in body and (not isinstance(body["tags"], list)
                    or any(not isinstance(tag, str) for tag in body["tags"])))
                or ("source" in body and body["source"] not in ("auto", "user"))
                or any(key not in {"tags", "source"} for key in body)):
            return jsonify(status="error", message="无效的标签删除条件"), 400

        # 模式1：删除指定标签列表
        if "tags" in body and isinstance(body["tags"], list):
            for t in body["tags"]:
                cleaned = _sanitize_tag(str(t))
                if cleaned and db.remove_album_tag(album_id, cleaned):
                    total_deleted += 1
        # 模式2：按 source 清空
        elif "source" in body and isinstance(body["source"], str):
            src = body["source"].strip()
            if src in ("auto", "user"):
                total_deleted = db.clear_album_tags(album_id, source=src)
        # 模式3：删除全部
        else:
            total_deleted = db.clear_album_tags(album_id)

        log.info(f"API删除标签 album_id={album_id} deleted={total_deleted}")
        return jsonify({"status": "ok", "deleted": total_deleted})
    except Exception as e:
        log.error(f"API删除标签失败 album_id={album_id} error={e}")
        return jsonify({"status": "error", "message": "删除标签失败"}), 500


# ─── 同步标签（从 18comic） ───


@api_library_bp.post("/<album_id>/tags/sync")
def album_tags_sync(album_id: str):
    """触发从 18comic 同步标签 POST /api/library/<album_id>/tags/sync
    使用 ThreadPoolExecutor + 20s 超时
    """
    if not validate_numeric(album_id):
        return jsonify({"status": "error", "message": "album_id 必须是纯数字"}), 400
    try:
        pool = ThreadPoolExecutor(max_workers=1)
        fut = pool.submit(get_album_detail_cached, album_id)
        try:
            data = fut.result(timeout=20)
        except FuturesTimeout:
            log.warning(f"API同步标签超时 album_id={album_id}")
            pool.shutdown(wait=False)  # 不阻塞 Waitress 线程
            return jsonify({"status": "error", "message": "同步标签超时"}), 504

        pool.shutdown(wait=True)

        raw_tags = data.get("tags", []) or []
        tag_names = [t if isinstance(t, str) else (t.get("name") or str(t)) for t in raw_tags]
        synced = db.batch_sync_auto_tags(album_id, tag_names)

        # 同时缓存元数据
        db.upsert_album_meta(
            album_id,
            title=data.get("title", ""),
            author=data.get("author", ""),
            cover_url=data.get("cover", ""),
        )

        # 获取同步后的总标签数
        all_tags = db.get_album_tags(album_id)
        log.info(
            f"API同步标签完成 album_id={album_id} synced={synced} "
            f"total_tags={len(all_tags)}"
        )
        return jsonify({
            "status": "ok",
            "synced": synced,
            "total_tags": len(all_tags),
        })
    except Exception as e:
        log.error(f"API同步标签失败 album_id={album_id} error={e}")
        return jsonify({"status": "error", "message": "同步标签失败"}), 500


# ─── 标签搜索自动补全 ───


@api_library_bp.get("/search-tags")
def search_tags_autocomplete():
    """标签自动补全 GET /api/library/search-tags?q="""
    try:
        q = request.args.get("q", "", type=str).strip().lower()[:200]
        if not q:
            return jsonify({"status": "ok", "tags": []})

        # 从 get_all_tags 结果中模糊匹配
        all_tags = db.get_all_tags(min_count=1)
        matched = [
            {"tag": t["tag"], "count": t["count"]}
            for t in all_tags
            if q in t["tag"]
        ]
        # 按 count DESC 排序并限制前 20 条
        matched.sort(key=lambda x: x["count"], reverse=True)
        matched = matched[:20]
        return jsonify({"status": "ok", "tags": matched})
    except Exception as e:
        log.error(f"API标签搜索失败 q={q} error={e}")
        return jsonify({"status": "error", "message": "搜索标签失败"}), 500


# ─── 批量同步所有标签 ───


@api_library_bp.post("/tags/sync-all")
def sync_all_tags():
    """批量重新同步所有漫画标签 POST /api/library/tags/sync-all
    从 wishlist + completed jobs 获取所有 album_id，逐个同步标签（单次最多 50 个）。
    后台异步执行，立即返回接受状态。
    """
    def _sync_worker():
        try:
            all_lib = db.get_library(page=1, page_size=200)
            all_ids = [item["album_id"] for item in all_lib.get("items", [])]
            # 限前 50 个，避免超时
            target_ids = all_ids[:50]
            processed = 0
            errors = 0
            # 同步每个漫画的标签
            for aid in target_ids:
                try:
                    data = get_album_detail_cached(aid)
                    raw_tags = data.get("tags", []) or []
                    tag_names = []
                    for t in raw_tags:
                        if isinstance(t, str):
                            tag_names.append(t)
                        elif isinstance(t, dict):
                            tag_names.append(t.get("name", ""))
                    synced = db.batch_sync_auto_tags(aid, tag_names)
                    db.upsert_album_meta(aid,
                        title=data.get("title", ""),
                        author=data.get("author", ""),
                        cover_url=data.get("cover", ""),
                    )
                    if synced > 0:
                        processed += 1
                except Exception as e:
                    errors += 1
                    log.warning(f"同步标签异常 album_id={aid} error={e}")
            log.info(f"批量同步标签完成: processed={processed}, errors={errors}")
        except Exception as e:
            log.error(f"后台批量同步标签失败 error={e}")

    t = threading.Thread(target=_sync_worker, daemon=True)
    t.start()
    log.info("API批量同步标签已接受，后台执行中")
    return jsonify({"status": "accepted", "message": "批量同步已开始，后台执行中"}), 202
