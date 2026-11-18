"""
专辑详情 API & 封面重定向
"""
import io
import re
from pathlib import Path

from flask import Blueprint, jsonify, make_response, redirect, send_file

from core import archive_pages
from core.database import get_db, get_all_jobs, get_job, get_completed_job_by_album_id
from core.file_tree import is_link
from core.logger import log
from core.path_guard import is_safe_path
from core.path_utils import get_cache_dir
from core.validation import validate_numeric, require_numeric, ALLOWED_EXTENSIONS, is_allowed_image, get_image_mime
from core.jm_service import get_album_detail_cached
from core.upstream import known_failure
from core.local_availability import first_page_image
from jmcomic import JmcomicText

api_album_bp = Blueprint("api_album", __name__)


@api_album_bp.get("/api/album/<album_id>")
def album_detail(album_id: str):
    """获取漫画专辑详情（GET /api/album/<album_id>）"""
    if not validate_numeric(album_id):
        log.warning(f"API专辑详情 album_id 格式非法 album_id={album_id}")
        return jsonify({"status": "error", "message": "album_id 必须是纯数字"}), 400
    log.info(f"API专辑详情 album_id={album_id}")
    try:
        data = get_album_detail_cached(album_id)
        return jsonify({"status": "ok", "data": data})
    except Exception as e:
        log.error(f"API专辑详情失败 album_id={album_id} error={e}")
        failure = known_failure(e)
        if failure is not None:
            return jsonify(failure.payload()), failure.http_status
        return jsonify({"status": "error", "message": "获取专辑详情失败，请检查 album_id 是否正确或稍后重试"}), 500


@api_album_bp.get("/api/cover/<album_id>")
def cover_redirect(album_id: str):
    """返回本地封面图片，若无本地文件则 302 重定向到 CDN"""
    if not validate_numeric(album_id):
        log.warning(f"API封面重定向 album_id 格式非法 album_id={album_id}")
        return jsonify({"status": "error", "message": "album_id 必须是纯数字"}), 400
    log.info(f"API封面重定向 album_id={album_id}")
    try:
        # -- 本地封面查找（散图优先 → CBZ/ZIP 首张可读页 → 本地封面缓存）--
        job = get_completed_job_by_album_id(album_id)
        if job and job.get("output_path") and not job.get("superseded_at"):
            album_dir = Path(job["output_path"])
            if album_dir.exists() and is_safe_path(album_dir):
                first_img = first_page_image(album_dir)
                if first_img:
                    if not is_safe_path(first_img):
                        log.warning(f"封面路径越权 album_id={album_id} path={first_img}")
                    else:
                        mime = get_image_mime(first_img.suffix.lower())
                        log.info(f"API封面返回本地文件 album_id={album_id}")
                        # max_age=0：本地封面可能随重新下载变化，走 ETag 协商缓存
                        return send_file(str(first_img), mimetype=mime, max_age=0)
                index = archive_pages.select(album_dir)
                if index is not None and index.status == "ok" and index.reader_pages:
                    try:
                        first_page = index.reader_pages[0]
                        raw = archive_pages.read_page(index, first_page)
                        mime = get_image_mime(first_page.suffix)
                        log.info(f"API封面返回本地归档首页 album_id={album_id}")
                        return send_file(io.BytesIO(raw), mimetype=mime, max_age=0)
                    except archive_pages.ArchiveError:
                        pass

        covers_dir = get_cache_dir() / "covers"
        if covers_dir.is_dir() and not is_link(covers_dir):
            for ext in (".webp", ".png", ".jpg"):
                cached_cover = covers_dir / f"{album_id}{ext}"
                if cached_cover.is_file() and not is_link(cached_cover):
                    mime = get_image_mime(ext)
                    log.info(f"API封面返回本地缓存封面 album_id={album_id}")
                    return send_file(str(cached_cover), mimetype=mime, max_age=0)

        # -- CDN 回退（本地没有封面）--
        url = JmcomicText.get_album_cover_url(album_id)
        if not url.startswith(("http://", "https://")):
            log.error(f"封面 URL scheme 异常 album_id={album_id} url={url}")
            return jsonify({"status": "error", "message": "获取封面失败"}), 500
        resp = make_response(redirect(url, code=302))
        resp.headers['Cache-Control'] = 'private, max-age=86400'
        resp.headers['Vary'] = 'Cookie'
        return resp
    except Exception as e:
        log.error(f"API封面重定向失败 album_id={album_id} error={e}")
        return jsonify({"status": "error", "message": "获取封面失败"}), 500
