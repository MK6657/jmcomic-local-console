"""收藏清单 API"""
from datetime import datetime, timedelta
import json
import re

from flask import Blueprint, jsonify, request, Response

from core import batch_downloads, database as db, tag_sync
from core.jm_service import get_album_detail_cached
from core.job_manager import job_manager
from core.logger import log
from core.local_availability import states_among
from core.tag_jobs import tag_jobs
from core.background_calls import BackgroundCallsStopped
from core.upstream import known_failure
from core.scheduler import window_state
from core.validation import (
    extract_album_id,
    is_safe_upload_filename as _is_safe_upload_filename,
    validate_numeric,
)
from routes.api_library import mark_local_details

api_wishlist_bp = Blueprint("api_wishlist", __name__)

# 批量操作统一上限
_MAX_BATCH_CHECK = 500
_MAX_BATCH_DOWNLOAD = 50
_MAX_BATCH_IMPORT_FILE = 200
_MAX_BATCH_IMPORT_RAW_TOKENS = 200
_MAX_BATCH_IMPORT_RAW_ACTUAL = 50
_ADDED_AT_ISO = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}"
    r"(?:[T ][0-9]{2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]{1,6})?)?(?:Z|[+-][0-9]{2}:[0-5][0-9])?)?"
)


# ──────────────────────────────────────────────


@api_wishlist_bp.post("/api/wishlist")
def add_wishlist():
    """添加收藏"""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"status": "error", "message": "请求体需为 JSON 对象"}), 400
    album_id = str(body.get("album_id", "")).strip()

    if not album_id:
        return jsonify({"status": "error", "message": "缺少 album_id"}), 400
    if not validate_numeric(album_id):
        return jsonify({"status": "error", "message": "album_id 必须是纯数字"}), 400

    title = str(body.get("title", "") or "")
    author = str(body.get("author", "") or "")
    cover_url = str(body.get("cover_url", "") or "")

    # 限制字符串长度防 DoS
    if len(str(title)) > 500:
        title = str(title)[:500]
    if len(str(author)) > 200:
        author = str(author)[:200]
    if len(str(cover_url)) > 2000:
        cover_url = str(cover_url)[:2000]

    # 如果已在收藏或本地缓存已有元信息，优先复用，避免为已知标题同步阻塞等待上游 20s
    detail = None
    try:
        tag_jobs.require_accepting()
    except BackgroundCallsStopped as exc:
        return jsonify(known_failure(exc).payload()), 503
    existing = db.get_wishlist(album_id)
    if existing is not None:
        return jsonify({
            "status": "error",
            "message": "该漫画已在收藏中",
            "data": {
                "album_id": album_id,
                "title": title or existing.get("title", ""),
                "author": author or existing.get("author", ""),
            },
        }), 409
    if not title or not author or not cover_url:
        local_meta = db.get_album_meta(album_id) if hasattr(db, "get_album_meta") else None
        if local_meta:
            title = title or local_meta.get("title", "")
            author = author or local_meta.get("author", "")
            cover_url = cover_url or local_meta.get("cover_url", "")
    if not title:
        try:
            detail = get_album_detail_cached(album_id)
            if not title:
                title = detail.get("title", "")
            if not author:
                author = detail.get("author", "")
            if not cover_url:
                cover_url = detail.get("cover", "")
        except Exception as e:
            log.warning(f"获取专辑详情失败 album_id={album_id} error={e}")

    try:
        tag_jobs.require_accepting()
    except BackgroundCallsStopped as exc:
        return jsonify(known_failure(exc).payload()), 503
    added = db.add_wishlist(album_id, title, author, cover_url)
    if added:
        log.info(f"添加收藏 album_id={album_id} title={title}")
        # 真正的后台异步同步标签（不阻塞 HTTP 响应）
        try:
            tag_jobs.submit(tag_sync.sync_one, album_id, detail)
        except Exception as e:
            log.warning(f"启动标签同步线程失败 album_id={album_id} error={e}")
            warning = "收藏已保存，标签未同步，可稍后手动同步"
        else:
            warning = None
        return jsonify({
            "status": "ok",
            "message": "已添加到收藏",
            "warnings": [warning] if warning else [],
            "data": {"album_id": album_id, "title": title, "author": author},
        }), 201
    else:
        return jsonify({
            "status": "error",
            "message": "该漫画已在收藏中",
            "data": {"album_id": album_id, "title": title, "author": author},
        }), 409


@api_wishlist_bp.delete("/api/wishlist/<album_id>")
def remove_wishlist(album_id: str):
    """移除收藏"""
    if not validate_numeric(album_id):
        return jsonify({"status": "error", "message": "album_id 必须是纯数字"}), 400

    removed = db.remove_wishlist(album_id)
    if removed:
        log.info(f"移除收藏 album_id={album_id}")
        return jsonify({"status": "ok", "message": "已移除收藏"})
    else:
        return jsonify({"status": "error", "message": "该条目不在收藏中"}), 404


@api_wishlist_bp.get("/api/wishlist")
def list_wishlist():
    """获取收藏列表 GET /api/wishlist?page=&page_size=&q=&status=&sort=

    status：readable 已下载（可阅读）/ active 排队中·下载中 / failed 失败 / missing 下载过·本地文件不可用 /
    none 未下载（从未下载或已取消），其他值 = 全部；
    sort：added_at 最新添加 / added_asc 最早添加 / title / author / status（白名单外 = added_at）。
    筛选、排序、分页都在 SQL 里做，total 是筛选后的总数。
    """
    page = request.args.get("page", 1, type=int)
    page_size = request.args.get("page_size", 50, type=int)
    q = request.args.get("q", "", type=str).strip()[:200]
    sort = request.args.get("sort", "added_at", type=str).strip()
    if sort not in db.WISHLIST_SORTS:  # 白名单（键即 SQL 排序子句的索引，值永远来自服务端）
        sort = "added_at"
    status = request.args.get("status", "", type=str).strip()
    if status not in db.WISHLIST_STATUS_GROUPS:
        status = ""
    log.debug(f"API获取收藏列表 page={page} q={q} status={status} sort={sort}")
    page = max(1, min(page, 500))
    page_size = max(1, min(200, page_size))

    # “已下载（可阅读）”与资源库、详情、搜索用同一条规则；只有完成过下载的收藏才可能可读
    with db.library_snapshot() as conn:
        candidates = db.get_completed_album_ids(wishlist_only=True, keyword=q, conn=conn)
        states = states_among(candidates, conn)
        result = db.get_all_wishlist(
            page=page, page_size=page_size, keyword=q, sort=sort, conn=conn,
            status=status or None, readable_ids=(aid for aid, state in states.items() if state.readable),
        )
    mark_local_details(result["items"], states)
    return jsonify({"status": "ok", **result, "applied": {"status": status, "sort": sort}})


@api_wishlist_bp.get("/api/wishlist/<album_id>")
def get_single_wishlist(album_id: str):
    """获取单个收藏条目"""
    log.debug(f"API获取单个收藏 album_id={album_id}")
    if not validate_numeric(album_id):
        return jsonify({"status": "error", "message": "album_id 必须是纯数字"}), 400
    item = db.get_wishlist(album_id)
    return jsonify({"status": "ok", "item": item})


@api_wishlist_bp.post("/api/wishlist/check")
def batch_check_wishlist():
    """批量查询收藏状态"""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"status": "error", "message": "请求体需为 JSON 对象"}), 400
    album_ids = body.get("album_ids", [])

    if not album_ids or not isinstance(album_ids, list):
        return jsonify({"status": "error", "message": "缺少 album_ids 列表"}), 400

    # 限制列表长度防 DoS
    if len(album_ids) > _MAX_BATCH_CHECK:
        return jsonify({"status": "error", "message": f"album_ids 数量过多，最大 {_MAX_BATCH_CHECK}"}), 400

    # 校验所有 album_id 为纯数字
    for aid in album_ids:
        if isinstance(aid, bool) or not validate_numeric(str(aid).strip()):
            return jsonify({
                "status": "error",
                "message": f"album_id '{str(aid)[:32]}' 不是合法的纯数字格式",
            }), 400

    str_ids = [str(aid).strip() for aid in album_ids]
    result = db.batch_check_wishlist(str_ids)
    return jsonify({"status": "ok", "result": result})


@api_wishlist_bp.post("/api/wishlist/download")
def batch_download_wishlist():
    """收藏单行的「下载」：整部下载（core.batch_downloads.enqueue_single，一个事务）。
    已有排队 / 下载中 / 已暂停任务的漫画不重复建任务，列在 skipped 里。有新任务时 201，否则 200；
    window 是定时下载时间段现在的状态（任务照常只在时间段内开始）。"""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"status": "error", "message": "请求体需为 JSON 对象"}), 400
    ids = body.get("ids", [])

    if not ids or not isinstance(ids, list):
        return jsonify({"status": "error", "message": "缺少 ids 列表"}), 400

    # 限制列表长度防 DoS
    if len(ids) > _MAX_BATCH_DOWNLOAD:
        return jsonify({"status": "error", "message": f"批量下载数量过多，最大 {_MAX_BATCH_DOWNLOAD}"}), 400

    # 校验所有 album_id 为纯数字
    for aid in ids:
        if isinstance(aid, bool) or not validate_numeric(str(aid).strip()):
            return jsonify({
                "status": "error",
                "message": f"album_id '{str(aid)[:32]}' 不是合法的纯数字格式",
            }), 400

    try:
        result = batch_downloads.enqueue_single(str(aid).strip() for aid in ids)
    except batch_downloads.Busy:
        return jsonify({"status": "error", "reason": "busy", "message": "上一次确认还在处理，请稍后再试"}), 409
    except Exception as e:
        log.error(f"收藏下载 加入下载队列失败 count={len(ids)} error={e}")
        return jsonify({"status": "error", "message": "没能加入下载队列，没有创建任何任务"}), 500

    if result["job_ids"]:
        # 立即触发调度，不需要等 2 秒循环（照常受定时下载时间段限制）
        try:
            job_manager.schedule_next()
        except Exception as e:
            log.error(f"收藏下载后调度失败（任务仍在队列中）error={e}")

    return jsonify({"status": "ok", **result, "window": window_state()}), 201 if result["job_ids"] else 200


@api_wishlist_bp.post("/api/wishlist/import")
def batch_import_wishlist():
    """批量导入收藏"""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"status": "error", "message": "请求体需为 JSON 对象"}), 400
    raw = body.get("raw", "")
    if not isinstance(raw, str):
        return jsonify(status="error", message="raw 必须是字符串"), 400
    raw = raw.strip()

    if not raw:
        return jsonify({"status": "error", "message": "缺少 raw 内容"}), 400

    # 限制 raw 长度防 DoS
    if len(raw) > 50000:
        return jsonify({"status": "error", "message": "导入内容过长，最大 50000 字符"}), 400

    # 解析纯数字（支持逗号、空格、换行分隔）
    tokens = re.split(r"[,，\s\n\r]+", raw)
    # 限制 token 数量上限，防止 CPU DoS
    MAX_TOKENS = _MAX_BATCH_IMPORT_RAW_TOKENS
    if len(tokens) > MAX_TOKENS:
        tokens = tokens[:MAX_TOKENS]
    numeric_ids = []
    failed_validation = []
    for token in tokens:
        token = token.strip()
        if not token:
            continue
        parsed_id = extract_album_id(token)
        if parsed_id:
            numeric_ids.append(parsed_id)
        else:
            failed_validation.append(token)

    # 最多接受 _MAX_BATCH_IMPORT_RAW_ACTUAL 个（单个事务批量写入）
    numeric_ids = numeric_ids[:_MAX_BATCH_IMPORT_RAW_ACTUAL]
    added = 0
    skipped_existing = 0
    errors = []

    try:
        with db.transaction() as conn:
            for album_id in numeric_ids:
                result = db.add_wishlist(album_id, "", "", "", conn=conn)
                if result:
                    added += 1
                else:
                    skipped_existing += 1
    except Exception as e:
        log.error(f"批量导入事务失败 error={e}")
        return jsonify({"status": "error", "message": "批量导入失败，请稍后重试"}), 500

    log.info(
        f"批量导入完成: added={added}, skipped_existing={skipped_existing}, "
        f"failed_validation={len(failed_validation)}, errors={len(errors)}"
    )
    return jsonify({
        "status": "ok",
        "added": added,
        "skipped_existing": skipped_existing,
        "failed_validation": failed_validation,
        "errors": errors,
    })


@api_wishlist_bp.get("/api/wishlist/export")
def export_wishlist():
    """导出收藏清单为 JSON 文件下载"""
    log.info("API导出收藏")
    items = db.export_all_wishlist()
    json_str = json.dumps(items, ensure_ascii=False, indent=2)
    return Response(
        json_str,
        mimetype="application/json; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="wishlist.json"',
            "Cache-Control": "no-store",  # 导出内容实时生成，禁止缓存
        },
    )


def _imported_added_at(value, fallback):
    if value is None:
        return None
    if isinstance(value, str) and _ADDED_AT_ISO.fullmatch(value):
        try:
            datetime.fromisoformat(value)
            return value
        except ValueError:
            pass
    return fallback.isoformat()


@api_wishlist_bp.post("/api/wishlist/import-file")
def import_wishlist_file():
    """通过上传 JSON 文件导入收藏"""
    if "file" not in request.files:
        return jsonify({"status": "error", "message": "缺少上传文件"}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"status": "error", "message": "文件名为空"}), 400
    # 文件名安全校验（防路径穿越与控制字符，兼容中文文件名与浏览器重命名后缀）
    if not _is_safe_upload_filename(file.filename):
        log.warning(f"上传文件名不合法 filename={file.filename[:120]}")
        return jsonify({"status": "error", "message": "文件名不合法"}), 400

    # 限制上传文件大小为 5MB
    MAX_SIZE = 5 * 1024 * 1024
    if request.content_length and request.content_length > MAX_SIZE:
        return jsonify({"status": "error", "message": "上传文件过大，最大允许 5MB"}), 400

    try:
        # 安全读取：最多读取 MAX_SIZE 字节
        raw = file.read(MAX_SIZE + 1)
        if len(raw) > MAX_SIZE:
            return jsonify({"status": "error", "message": "上传文件过大，最大允许 5MB"}), 400
        data = json.loads(raw.decode("utf-8-sig"))
    except Exception as e:
        log.warning(f"收藏文件导入JSON解析失败: {e}")
        return jsonify({"status": "error", "message": "JSON 解析失败，请检查文件格式"}), 400

    if not isinstance(data, list):
        return jsonify({"status": "error", "message": "JSON 必须是一个数组"}), 400

    if len(data) > _MAX_BATCH_IMPORT_FILE:
        return jsonify({"status": "error", "message": f"最多导入 {_MAX_BATCH_IMPORT_FILE} 条记录"}), 400

    added = 0
    skipped_existing = 0
    errors = []

    imported_at = datetime.now()
    valid_entries = []
    for idx, entry in enumerate(data):
        if not isinstance(entry, dict):
            errors.append({"index": idx, "message": "条目不是对象"})
            continue

        album_id = extract_album_id(str(entry.get("album_id", "")).strip()) or ""
        if not album_id or not validate_numeric(album_id):
            errors.append({"index": idx, "album_id": str(entry.get("album_id", "")).strip(), "message": "album_id 缺失或不是纯数字"})
            continue

        title = str(entry.get("title", "") or "")[:500]
        author = str(entry.get("author", "") or "")[:200]
        cover_url = str(entry.get("cover_url", "") or "")[:2000]
        added_at = _imported_added_at(entry.get("added_at", ""), imported_at - timedelta(microseconds=idx))
        valid_entries.append((idx, album_id, title, author, cover_url, added_at))

    try:
        with db.transaction() as conn:
            for idx, album_id, title, author, cover_url, added_at in valid_entries:
                result = db.add_wishlist(album_id, title, author, cover_url, added_at=added_at, conn=conn)
                if result:
                    added += 1
                else:
                    skipped_existing += 1
    except Exception as e:
        log.error(f"文件导入事务失败 error={e}")
        return jsonify({"status": "error", "message": "文件导入失败，请稍后重试"}), 500

    log.info(
        f"文件导入完成: added={added}, skipped_existing={skipped_existing}, errors={len(errors)}"
    )
    return jsonify({
        "status": "ok",
        "added": added,
        "skipped_existing": skipped_existing,
        "errors": errors,
    })
