"""图片预览/本地阅读器 API v2.0.0"""
from pathlib import Path
from flask import Blueprint, jsonify, send_file

from core.path_guard import is_safe_path, DOWNLOAD_ROOT
from core.database import get_all_jobs, get_completed_job_by_album_id
from core.logger import log
from core.validation import validate_numeric  # P1-8 补充 album_id 校验

api_preview_bp = Blueprint("api_preview", __name__)

# 允许的图片扩展名
ALLOWED_EXTENSIONS = {".webp", ".jpg", ".jpeg", ".png", ".gif"}


def _find_album_dir(album_id: str) -> tuple[Path | None, str | None]:
    """查找 album_id 对应的本地目录

    优先从 jobs 表获取 output_path；
    若没有匹配任务，返回 None（用户应通过下载管理页进入预览）。
    """
    # 策略：从 jobs 表查找
    job = get_completed_job_by_album_id(album_id)
    if job and job.get("output_path"):
        p = Path(job["output_path"])
        if p.exists():
            return p, job.get("title") or p.name

    # 没有匹配的已完成任务
    return None, None


def _scan_pages(album_dir: Path) -> list[dict]:
    """扫描专辑目录下的所有章节子目录，返回扁平化的 pages 列表"""
    pages = []
    page_number = 0

    chapter_dirs = sorted([d for d in album_dir.iterdir() if d.is_dir()])

    if not chapter_dirs:
        # 没有子目录，直接在专辑目录下找图片（单章扁平结构）
        image_files = sorted(
            f
            for f in album_dir.iterdir()
            if f.is_file() and f.suffix.lower() in ALLOWED_EXTENSIONS
        )
        for img in image_files:
            page_number += 1
            rel_path = str(img.relative_to(DOWNLOAD_ROOT)).replace("\\", "/")
            pages.append({
                "page": page_number,
                "url": f"/api/preview-img/{rel_path}",
                "chapter": album_dir.name,
            })
    else:
        for ch_dir in chapter_dirs:
            image_files = sorted(
                f
                for f in ch_dir.iterdir()
                if f.is_file() and f.suffix.lower() in ALLOWED_EXTENSIONS
            )
            for img in image_files:
                page_number += 1
                rel_path = str(img.relative_to(DOWNLOAD_ROOT)).replace("\\", "/")
                pages.append({
                    "page": page_number,
                    "url": f"/api/preview-img/{rel_path}",
                    "chapter": ch_dir.name,
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

        if not album_dir.exists():
            log.warning(f"预览本地目录不存在 album_dir={album_dir}")
            return jsonify({"status": "error", "message": "本地文件不存在"}), 404

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
    # 构建完整路径
    full_path = DOWNLOAD_ROOT / img_path

    # 路径安全检查 —— 拒绝路径穿越
    if not is_safe_path(full_path):
        log.warning(f"图片访问路径越权 img_path={img_path}")
        return jsonify({"status": "error", "message": "没有权限访问该文件"}), 403

    # 检查文件是否存在
    if not full_path.exists() or not full_path.is_file():
        log.warning(f"图片文件不存在 img_path={img_path}")
        return jsonify({"status": "error", "message": "文件不存在"}), 404

    # 只允许访问 webp/jpg/jpeg/png/gif
    suffix = full_path.suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        log.warning(f"不支持的图片类型 img_path={img_path} suffix={suffix}")
        return jsonify({"status": "error", "message": "不支持的图片类型"}), 403

    # 正确的 Content-Type
    mime_map = {
        ".webp": "image/webp",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
    }
    mimetype = mime_map.get(suffix, "application/octet-stream")

    # max_age=0：预览图内容可能因重试/重新下载而变化（URL 不变），
    # 不能吃 SEND_FILE_MAX_AGE_DEFAULT 的一年强缓存；保留 ETag 走 304 协商缓存
    return send_file(str(full_path), mimetype=mimetype, max_age=0)
