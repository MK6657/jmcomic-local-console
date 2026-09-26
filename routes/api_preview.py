"""图片预览/本地阅读器 API v2.0.0"""
from pathlib import Path
from urllib.parse import quote

from flask import Blueprint, jsonify, send_file

from core.path_guard import is_safe_path, DOWNLOAD_ROOT
from core.database import get_completed_job_by_album_id
from core.file_tree import safe_files
from core.logger import log
# P1-8 补充 album_id 校验；图片类型与 MIME 统一取自 core.validation
from core.validation import validate_numeric, is_allowed_image, get_image_mime

api_preview_bp = Blueprint("api_preview", __name__)


def _find_album_dir(album_id: str) -> tuple[Path | None, str | None]:
    """查找 album_id 对应的本地目录

    优先从 jobs 表获取 output_path；
    若没有匹配任务或目录已被删除，返回 None（用户应通过下载管理页进入预览）。
    """
    job = get_completed_job_by_album_id(album_id)
    if job and job.get("output_path"):
        p = Path(job["output_path"])
        if p.is_dir() and is_safe_path(p):
            return p, job.get("title") or p.name
    return None, None


def _scan_pages(album_dir: Path) -> list[dict]:
    """Include root images and nested chapters, with natural order and URL escaping."""
    pages = []
    for img in safe_files(album_dir):
        if not is_allowed_image(img.suffix) or not is_safe_path(img):
            continue
        rel_path = img.relative_to(DOWNLOAD_ROOT).as_posix()
        pages.append({
            "page": len(pages) + 1,
            "url": "/api/preview-img/" + quote(rel_path, safe="/"),
            "chapter": img.parent.name,
        })
    return pages


@api_preview_bp.get("/api/preview/<album_id>")
def preview_album(album_id: str):
    """获取某个漫画的本地章节和图片列表

    扫描 DOWNLOAD_ROOT/<漫画名>/ 目录下的所有图片，
    按章节子目录分组，返回扁平化的 pages 列表。
    """
    if not validate_numeric(album_id):
        return jsonify({"status": "error", "message": "album_id 必须是纯数字"}), 400
    log.info(f"API预览 album_id={album_id}")
    try:
        album_dir, title = _find_album_dir(album_id)
        if not album_dir:
            log.warning(f"预览未找到本地目录 album_id={album_id}")
            return jsonify({"status": "error", "message": "未找到已下载的漫画"}), 404

        pages = _scan_pages(album_dir)

        if not pages:
            log.warning(f"预览目录无图片 album_dir={album_dir}")
            return jsonify({"status": "error", "message": "本地没有找到图片"}), 404

        log.info(f"API预览成功 album_id={album_id} total_pages={len(pages)}")
        return jsonify({
            "status": "ok",
            "album_id": album_id,
            "title": title or album_dir.name,
            "total_pages": len(pages),
            "pages": pages,
        })

    except Exception as e:
        log.error(f"API预览失败 album_id={album_id} error={e}")
        return jsonify({"status": "error", "message": "预览失败，请检查本地文件是否存在"}), 500


@api_preview_bp.get("/api/preview-img/<path:img_path>")
def preview_image(img_path: str):
    """返回本地图片文件（经 path_guard 校验）"""
    full_path = DOWNLOAD_ROOT / img_path

    # 路径安全检查 —— 拒绝路径穿越
    if not is_safe_path(full_path):
        log.warning(f"图片访问路径越权 img_path={img_path}")
        return jsonify({"status": "error", "message": "没有权限访问该文件"}), 403

    # 检查文件是否存在
    if not full_path.is_file():
        log.warning(f"图片文件不存在 img_path={img_path}")
        return jsonify({"status": "error", "message": "文件不存在"}), 404

    # 只允许访问 webp/jpg/jpeg/png/gif
    if not is_allowed_image(full_path.suffix):
        log.warning(f"不支持的图片类型 img_path={img_path} suffix={full_path.suffix}")
        return jsonify({"status": "error", "message": "不支持的图片类型"}), 403

    # max_age=0：预览图内容可能因重试/重新下载而变化（URL 不变），
    # 不能吃 SEND_FILE_MAX_AGE_DEFAULT 的一年强缓存；保留 ETag 走 304 协商缓存
    return send_file(str(full_path), mimetype=get_image_mime(full_path.suffix), max_age=0)
