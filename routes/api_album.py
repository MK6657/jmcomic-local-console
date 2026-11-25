"""
专辑详情 API & 封面重定向
"""
from collections import OrderedDict
import hashlib
import io
import os
import threading
from pathlib import Path

from flask import Blueprint, jsonify, make_response, redirect, request, send_file

from core import archive_pages, thumb_cache
from core.database import get_completed_job_by_album_id
from core.file_tree import is_link
from core.logger import log
from core.path_guard import is_safe_path
from core.path_utils import get_cache_dir
from core.validation import validate_numeric, get_image_mime
from core.jm_service import get_album_detail_cached
from core.upstream import known_failure
from core.local_availability import scan_page_images
from jmcomic import JmcomicText

api_album_bp = Blueprint("api_album", __name__)

_COVER_KEY_CACHE_MAX = 512
_cover_key_lock = threading.Lock()
# album_id -> (album_dir_str, dir_mtime_ns, verify_path_str, verify_mtime_ns, verify_size, cache_key)
_cover_key_cache: OrderedDict[str, tuple[str, int, str, int, int, str]] = OrderedDict()


def _remember_cover_key(album_id: str, entry: tuple[str, int, str, int, int, str]) -> None:
    with _cover_key_lock:
        _cover_key_cache[album_id] = entry
        _cover_key_cache.move_to_end(album_id)
        while len(_cover_key_cache) > _COVER_KEY_CACHE_MAX:
            _cover_key_cache.popitem(last=False)


def _lookup_fast_cover_thumb(album_id: str, album_dir: Path, width: int) -> Path | None:
    try:
        dir_mtime_ns = album_dir.stat().st_mtime_ns
    except OSError:
        return None
    with _cover_key_lock:
        entry = _cover_key_cache.get(album_id)
        if entry is not None:
            _cover_key_cache.move_to_end(album_id)
    if entry is None:
        return None
    dir_str, cached_dir_mtime, verify_str, verify_mtime, verify_size, cache_key = entry
    if dir_str != str(album_dir) or cached_dir_mtime != dir_mtime_ns:
        return None
    try:
        st = os.stat(verify_str)
    except OSError:
        return None
    if st.st_mtime_ns != verify_mtime or st.st_size != verify_size:
        return None
    return thumb_cache.get_cached_thumbnail(cache_key=cache_key, width=width)


def _send_bytes_thumb(data: bytes):
    etag = hashlib.sha1(data).hexdigest()[:20]
    return send_file(io.BytesIO(data), mimetype="image/webp", max_age=0, etag=etag)


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
@api_album_bp.get("/api/cover/<album_id>/portrait")
def cover_redirect(album_id: str):
    """返回本地封面图片，若无本地文件则 302 重定向到 CDN"""
    if not validate_numeric(album_id):
        log.warning(f"API封面重定向 album_id 格式非法 album_id={album_id}")
        return jsonify({"status": "error", "message": "album_id 必须是纯数字"}), 400
    log.debug(f"API封面重定向 album_id={album_id}")
    try:
        width = thumb_cache.parse_width(request.args.get("w"))
        if width is None and request.path.endswith("/portrait") and request.args.get("original") != "1":
            width = thumb_cache.DEFAULT_PORTRAIT_WIDTH

        from core import album_maintenance, path_guard
        from core.publication_access import published_path

        album_dir = None
        for attempt in range(2):
            job = get_completed_job_by_album_id(album_id)
            if job and job.get("output_path") and not job.get("superseded_at"):
                resolved = published_path(Path(job["output_path"]), path_guard.DOWNLOAD_ROOT, directory=True)
                if resolved is not None and resolved.is_dir() and not is_link(resolved):
                    album_dir = resolved
                    break
                if attempt == 0:
                    album_maintenance.wait_for_active_move()
                    continue
            break

        if album_dir is not None:
            if width is not None:
                fast_thumb = _lookup_fast_cover_thumb(album_id, album_dir, width)
                if fast_thumb is not None:
                    return send_file(str(fast_thumb), mimetype="image/webp", max_age=0)
            dir_mtime_ns = album_dir.stat().st_mtime_ns
            loose = scan_page_images(album_dir, check_paths=False)
            index = archive_pages.select(album_dir)
            pages = archive_pages.merge_pages(loose, index)
            if pages and isinstance(pages[0][1], archive_pages.ArchivePage):
                try:
                    first_page = pages[0][1]
                    version = archive_pages.version(index)
                    arc_key = f"arc:{album_id}:{version}:{first_page.name}"
                    arc_path = getattr(index, "path", None)
                    if width is not None:
                        cached_thumb = thumb_cache.get_cached_thumbnail(cache_key=arc_key, width=width)
                        if cached_thumb is not None:
                            if arc_path is not None:
                                arc_st = os.stat(arc_path)
                                _remember_cover_key(
                                    album_id,
                                    (str(album_dir), dir_mtime_ns, str(arc_path), arc_st.st_mtime_ns, arc_st.st_size, arc_key),
                                )
                            return send_file(str(cached_thumb), mimetype="image/webp", max_age=0)
                    raw = archive_pages.read_page(index, first_page)
                    if width is not None:
                        resized = thumb_cache.get_or_build_thumbnail(
                            cache_key=arc_key,
                            width=width,
                            source_bytes=raw,
                        )
                        if isinstance(resized, Path):
                            if arc_path is not None:
                                arc_st = os.stat(arc_path)
                                _remember_cover_key(
                                    album_id,
                                    (str(album_dir), dir_mtime_ns, str(arc_path), arc_st.st_mtime_ns, arc_st.st_size, arc_key),
                                )
                            return send_file(str(resized), mimetype="image/webp", max_age=0)
                        if isinstance(resized, bytes):
                            return _send_bytes_thumb(resized)
                    mime = get_image_mime(first_page.suffix)
                    log.debug(f"API封面返回本地归档首页 album_id={album_id}")
                    return send_file(io.BytesIO(raw), mimetype=mime, max_age=0, etag=f"{version}-cover")
                except archive_pages.ArchiveError:
                    pass
            if loose and is_safe_path(loose[0][1]):
                first_img = loose[0][1]
                if width is not None:
                    st = first_img.stat()
                    file_key = f"file:{first_img}:{st.st_mtime_ns}:{st.st_size}"
                    resized = thumb_cache.get_or_build_thumbnail(
                        cache_key=file_key,
                        width=width,
                        source_path=first_img,
                    )
                    if isinstance(resized, Path):
                        _remember_cover_key(
                            album_id,
                            (str(album_dir), dir_mtime_ns, str(first_img), st.st_mtime_ns, st.st_size, file_key),
                        )
                        return send_file(str(resized), mimetype="image/webp", max_age=0)
                    if isinstance(resized, bytes):
                        return _send_bytes_thumb(resized)
                mime = get_image_mime(first_img.suffix.lower())
                log.debug(f"API封面返回本地文件 album_id={album_id}")
                return send_file(str(first_img), mimetype=mime, max_age=0)

        covers_dir = get_cache_dir() / "covers"
        if covers_dir.is_dir() and not is_link(covers_dir):
            for ext in (".webp", ".png", ".jpg"):
                cached_cover = covers_dir / f"{album_id}{ext}"
                if cached_cover.is_file() and not is_link(cached_cover):
                    if width is not None:
                        st = cached_cover.stat()
                        resized = thumb_cache.get_or_build_thumbnail(
                            cache_key=f"cover:{cached_cover}:{st.st_mtime_ns}:{st.st_size}",
                            width=width,
                            source_path=cached_cover,
                        )
                        if isinstance(resized, Path):
                            return send_file(str(resized), mimetype="image/webp", max_age=0)
                        if isinstance(resized, bytes):
                            return _send_bytes_thumb(resized)
                    mime = get_image_mime(ext)
                    log.debug(f"API封面返回本地缓存封面 album_id={album_id}")
                    return send_file(str(cached_cover), mimetype=mime, max_age=0)

        # -- CDN 回退（本地没有封面）--
        size = "" if request.args.get("fallback") == "square" else "_3x4"
        url = JmcomicText.get_album_cover_url(album_id, size=size)
        if not url.startswith(("http://", "https://")):
            log.error(f"封面 URL scheme 异常 album_id={album_id} url={url}")
            return jsonify({"status": "error", "message": "获取封面失败"}), 500
        resp = make_response(redirect(url, code=302))
        # A successful download changes the preferred cover immediately.
        resp.headers['Cache-Control'] = 'private, no-store'
        resp.headers['Vary'] = 'Cookie'
        return resp
    except Exception as e:
        log.error(f"API封面重定向失败 album_id={album_id} error={e}")
        return jsonify({"status": "error", "message": "获取封面失败"}), 500
