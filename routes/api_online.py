"""在线阅读 API：页面列表 + 按需取图（解码后缓存），不创建下载任务"""
from flask import Blueprint, jsonify, send_file

from core import online_reader
from core.logger import log
from core.validation import validate_numeric

api_online_bp = Blueprint("api_online", __name__)


@api_online_bp.get("/api/online/<album_id>")
def online_album(album_id: str):
    """整本漫画的在线页列表，结构同 /api/preview/<id>，供连续阅读页的在线模式使用"""
    if not validate_numeric(album_id):
        return jsonify({"status": "error", "message": "album_id 必须是纯数字"}), 400
    try:
        data = online_reader.album_pages(album_id)
    except TimeoutError as e:
        return jsonify({"status": "error", "message": str(e)}), 504
    except Exception as e:
        log.error(f"在线阅读 获取页面列表失败 album_id={album_id} error={type(e).__name__}: {e}")
        return jsonify({"status": "error", "message": "在线加载失败，请稍后重试"}), 502
    if not data["pages"]:
        return jsonify({"status": "error", "message": "没有可在线阅读的页面"}), 404
    return jsonify({
        "status": "ok",
        "album_id": album_id,
        "title": data["title"],
        "total_pages": len(data["pages"]),
        "pages": data["pages"],
        "skipped_chapters": data["skipped_chapters"],
    })


@api_online_bp.get("/api/online-img/<photo_id>/<int:index>")
def online_image(photo_id: str, index: int):
    """单页图片（index 从 0 开始）；首次请求时从上游下载并解码，之后直接读缓存"""
    if not validate_numeric(photo_id):
        return jsonify({"status": "error", "message": "photo_id 必须是纯数字"}), 400
    try:
        path = online_reader.image_file(photo_id, index)
    except IndexError:
        return jsonify({"status": "error", "message": "页码超出范围"}), 404
    except TimeoutError as e:
        return jsonify({"status": "error", "message": str(e)}), 504
    except Exception as e:
        log.error(f"在线阅读 取图失败 photo_id={photo_id} index={index} error={type(e).__name__}: {e}")
        return jsonify({"status": "error", "message": "图片加载失败"}), 502
    # 同一 photo/页码的解码结果不会变化，允许浏览器缓存一天，来回滚动不再请求
    return send_file(path, mimetype=online_reader.image_mime(path), max_age=86400)
