"""
专辑详情 API & 封面重定向
"""
import re
from pathlib import Path

from flask import Blueprint, jsonify, make_response, redirect, send_file

from core.database import get_db, get_all_jobs, get_job, get_completed_job_by_album_id
from core.logger import log
from core.path_guard import is_safe_path
from core.validation import validate_numeric, require_numeric, ALLOWED_EXTENSIONS, is_allowed_image, get_image_mime
from core.jm_service import get_album_detail_cached
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
        err_msg = str(e)
        if "ConnectionError" in err_msg or "Timeout" in err_msg or "connection" in err_msg.lower() or "timeout" in err_msg.lower():
            return jsonify({"status": "error", "message": "无法连接到 18comic 服务器，请检查网络连接"}), 502
        return jsonify({"status": "error", "message": "获取专辑详情失败，请检查 album_id 是否正确或稍后重试"}), 500


@api_album_bp.get("/api/cover/<album_id>")
def cover_redirect(album_id: str):
    """返回本地封面图片，若无本地文件则 302 重定向到 CDN"""
    if not validate_numeric(album_id):
        log.warning(f"API封面重定向 album_id 格式非法 album_id={album_id}")
        return jsonify({"status": "error", "message": "album_id 必须是纯数字"}), 400
    log.info(f"API封面重定向 album_id={album_id}")
    try:
        # -- 本地封面查找 --
        job = get_completed_job_by_album_id(album_id)
        if job and job.get("output_path"):
            album_dir = Path(job["output_path"])
            if album_dir.exists():
                first_img = None
                for ch_dir in sorted(album_dir.iterdir()):
                    if ch_dir.is_dir():
                        for f in sorted(ch_dir.iterdir()):
                            if f.is_file() and f.suffix.lower() in ALLOWED_EXTENSIONS:
                                first_img = f
                                break
                        if first_img:
                            break
                if not first_img:
                    for f in sorted(album_dir.iterdir()):
                        if f.is_file() and f.suffix.lower() in ALLOWED_EXTENSIONS:
                            first_img = f
                            break
                if first_img:
                    if not is_safe_path(first_img):
                        log.warning(f"封面路径越权 album_id={album_id} path={first_img}")
                    else:
                        mime = get_image_mime(first_img.suffix.lower())
                        log.info(f"API封面返回本地文件 album_id={album_id}")
                        # max_age=0：本地封面可能随重新下载变化，走 ETag 协商缓存
                        return send_file(str(first_img), mimetype=mime, max_age=0)

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
