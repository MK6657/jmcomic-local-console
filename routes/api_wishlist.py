"""收藏清单 API"""
import json
import re
import threading

from flask import Blueprint, jsonify, request, Response

from core import database as db
from core.jm_service import get_album_detail, get_album_detail_cached
from core.job_manager import job_manager
from core.logger import log
from core.validation import validate_numeric  # 统一 album_id 纯数字校验

api_wishlist_bp = Blueprint("api_wishlist", __name__)

# 批量操作统一上限
_MAX_BATCH_CHECK = 500
_MAX_BATCH_DOWNLOAD = 50
_MAX_BATCH_IMPORT_FILE = 200
_MAX_BATCH_IMPORT_RAW_TOKENS = 200
_MAX_BATCH_IMPORT_RAW_ACTUAL = 50


def _sync_tags_for_wishlist(album_id: str):
    """收藏时后台同步标签和元数据，确保 /library 立刻显示标签"""
    try:
        data = get_album_detail_cached(album_id)
        tag_names = []
        raw_tags = data.get("tags", []) or []
        for t in raw_tags:
            if isinstance(t, str):
                tag_names.append(t)
            elif isinstance(t, dict):
                tag_names.append(t.get("name", ""))
            else:
                tag_names.append(str(t))
        synced = db.batch_sync_auto_tags(album_id, tag_names)
        db.upsert_album_meta(
            album_id,
            title=data.get("title", ""),
            author=data.get("author", ""),
            cover_url=data.get("cover", ""),
        )
        if synced > 0:
            log.info(f"收藏同步标签 album_id={album_id} synced={synced}")
    except Exception as e:
        log.warning(f"收藏同步标签异常 album_id={album_id} error={e}")


# ──────────────────────────────────────────────


@api_wishlist_bp.post("/api/wishlist")
def add_wishlist():
    """添加收藏"""
    body = request.get_json(force=True)
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

    # 如果没有提供标题等信息，尝试从 jmcomic 获取
    if not title or not author:
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

    added = db.add_wishlist(album_id, title, author, cover_url)
    if added:
        log.info(f"添加收藏 album_id={album_id} title={title}")
        # 真正的后台异步同步标签（不阻塞 HTTP 响应）
        try:
            t = threading.Thread(
                target=_sync_tags_for_wishlist,
                args=(album_id,),
                daemon=True,
            )
            t.start()
        except Exception as e:
            log.warning(f"启动标签同步线程失败 album_id={album_id} error={e}")
        return jsonify({
            "status": "ok",
            "message": "已添加到收藏",
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
    """获取收藏列表，支持搜索(q)和排序(sort)"""
    page = request.args.get("page", 1, type=int)
    page_size = request.args.get("page_size", 50, type=int)
    q = request.args.get("q", "", type=str).strip()
    sort = request.args.get("sort", "added_at", type=str).strip()
    # 排序白名单（对应 db.get_all_wishlist 的 sort_map）
    _VALID_SORTS_WISHLIST = {"title", "author", "added_at"}
    if sort not in _VALID_SORTS_WISHLIST:
        sort = "added_at"
    log.debug(f"API获取收藏列表 page={page} q={q}")
    page = max(1, min(page, 500))
    page_size = max(1, min(200, page_size))

    result = db.get_all_wishlist(page=page, page_size=page_size, keyword=q, sort=sort)
    return jsonify({"status": "ok", **result})


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
    body = request.get_json(force=True)
    album_ids = body.get("album_ids", [])

    if not album_ids or not isinstance(album_ids, list):
        return jsonify({"status": "error", "message": "缺少 album_ids 列表"}), 400

    # 限制列表长度防 DoS
    if len(album_ids) > _MAX_BATCH_CHECK:
        return jsonify({"status": "error", "message": f"album_ids 数量过多，最大 {_MAX_BATCH_CHECK}"}), 400

    # 校验所有 album_id 为纯数字
    for aid in album_ids:
        if not validate_numeric(str(aid).strip()):
            return jsonify({
                "status": "error",
                "message": f"album_id '{aid}' 不是合法的纯数字格式",
            }), 400

    str_ids = [str(aid).strip() for aid in album_ids]
    result = db.batch_check_wishlist(str_ids)
    return jsonify({"status": "ok", "result": result})


@api_wishlist_bp.post("/api/wishlist/download")
def batch_download_wishlist():
    """批量下载收藏"""
    body = request.get_json(force=True)
    ids = body.get("ids", [])

    if not ids or not isinstance(ids, list):
        return jsonify({"status": "error", "message": "缺少 ids 列表"}), 400

    # 限制列表长度防 DoS
    if len(ids) > _MAX_BATCH_DOWNLOAD:
        return jsonify({"status": "error", "message": f"批量下载数量过多，最大 {_MAX_BATCH_DOWNLOAD}"}), 400

    # 校验所有 album_id 为纯数字
    for aid in ids:
        if not validate_numeric(str(aid).strip()):
            return jsonify({
                "status": "error",
                "message": f"album_id '{aid}' 不是合法的纯数字格式",
            }), 400

    job_ids = []
    for album_id in ids:
        album_id = str(album_id).strip()
        # 获取标题
        item = db.get_wishlist(album_id)
        title = item.get("title", album_id) if item else album_id

        # 创建下载任务（下载全部章节）
        job_id = job_manager.create_job(album_id, title, [])
        job_ids.append({"album_id": album_id, "job_id": job_id})

        # 同步更新 wishlist 状态
        db.update_wishlist_download_status(album_id, "queued")
        log.info(f"收藏批量下载 创建任务 album_id={album_id} job_id={job_id}")

    # 立即触发调度，不需要等 2 秒循环
    job_manager.schedule_next()

    return jsonify({"status": "ok", "job_ids": job_ids}), 201


@api_wishlist_bp.post("/api/wishlist/import")
def batch_import_wishlist():
    """批量导入收藏"""
    body = request.get_json(force=True)
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
        if validate_numeric(token):
            numeric_ids.append(token)
        else:
            failed_validation.append(token)

    # 最多接受 _MAX_BATCH_IMPORT_RAW_ACTUAL 个
    numeric_ids = numeric_ids[:_MAX_BATCH_IMPORT_RAW_ACTUAL]
    added = 0
    skipped_existing = 0
    errors = []

    for album_id in numeric_ids:
        try:
            result = db.add_wishlist(album_id, "", "", "")
            if result:
                added += 1
            else:
                skipped_existing += 1
        except Exception as e:
            errors.append({"album_id": album_id, "message": "导入失败"})
            log.error(f"导入失败 album_id={album_id} error={e}")

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


@api_wishlist_bp.post("/api/wishlist/import-file")
def import_wishlist_file():
    """通过上传 JSON 文件导入收藏"""
    if "file" not in request.files:
        return jsonify({"status": "error", "message": "缺少上传文件"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"status": "error", "message": "文件名为空"}), 400
    # 文件名安全校验（防路径穿越）
    from werkzeug.utils import secure_filename
    if secure_filename(file.filename) != file.filename:
        log.warning(f"上传文件名不合法 filename={file.filename}")
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
        data = json.loads(raw.decode("utf-8"))
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

    for idx, entry in enumerate(data):
        if not isinstance(entry, dict):
            errors.append({"index": idx, "message": "条目不是对象"})
            continue

        album_id = str(entry.get("album_id", "")).strip()
        if not album_id or not validate_numeric(album_id):
            errors.append({"index": idx, "album_id": album_id, "message": "album_id 缺失或不是纯数字"})
            continue

        title = str(entry.get("title", "") or "")
        author = str(entry.get("author", "") or "")
        cover_url = str(entry.get("cover_url", "") or "")

        # 截断过长的字符串
        if len(str(title)) > 500:
            title = str(title)[:500]
        if len(str(author)) > 200:
            author = str(author)[:200]
        if len(str(cover_url)) > 2000:
            cover_url = str(cover_url)[:2000]

        try:
            result = db.add_wishlist(album_id, title, author, cover_url)
            if result:
                added += 1
            else:
                skipped_existing += 1
        except Exception as e:
            errors.append({"index": idx, "album_id": album_id, "message": "导入失败"})
            log.error(f"文件导入失败 album_id={album_id} error={e}")

    log.info(
        f"文件导入完成: added={added}, skipped_existing={skipped_existing}, errors={len(errors)}"
    )
    return jsonify({
        "status": "ok",
        "added": added,
        "skipped_existing": skipped_existing,
        "errors": errors,
    })
