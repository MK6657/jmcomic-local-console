"""
ZIP / PDF 导出 API

两个端点共用同一套模式：
  校验任务 → 收集文件 → 写入临时文件（避免大文件全量缓冲内存）
  → send_file 返回 → response.call_on_close 延迟清理临时文件
"""
import os
import tempfile
import zipfile
from pathlib import Path

from flask import Blueprint, jsonify, send_file

import core.database as db
from core.logger import log
from core.path_guard import DOWNLOAD_ROOT, is_safe_path
from core.validation import validate_job_id, EXPORT_IMAGE_EXTENSIONS
from core.file_tree import safe_files

api_export_bp = Blueprint("api_export", __name__)


class IncompletePdfError(ValueError):
    def __init__(self, images):
        self.images = [path.name for path in images]
        super().__init__(f"有 {len(self.images)} 张图片损坏或无法转换，已取消 PDF 导出；请修复或重新下载图片后重试")


def _check_job(job_id: str):
    """校验任务是否存在、已完成、并有合法输出路径"""
    if not validate_job_id(job_id):
        return None, (jsonify({"status": "error", "message": "job_id 格式非法"}), 400)
    job = db.get_job(job_id)
    if not job:
        return None, (jsonify({"status": "error", "message": "任务不存在"}), 404)
    if job["status"] != "completed":
        return None, (jsonify({"status": "error", "message": "任务未完成"}), 400)
    output_path = job.get("output_path", "")
    if not output_path:
        return None, (jsonify({"status": "error", "message": "任务无输出路径"}), 404)
    if not is_safe_path(output_path):
        return None, (jsonify({"status": "error", "message": "输出路径越权"}), 403)
    return job, None


def _resolve_output_dir(job):
    """获取任务输出目录（存在且为目录），否则返回 None"""
    output_dir = Path(job["output_path"])
    if not output_dir.exists() or not output_dir.is_dir():
        return None
    return output_dir


def _safe_export_name(output_dir: Path) -> str:
    """生成下载文件名（去除路径分隔符和空格）"""
    return output_dir.name.replace(" ", "_").replace("/", "_").replace("\\", "_")


def _cleanup_tmp(tmp_path: str | None):
    """静默删除临时文件"""
    if tmp_path and os.path.exists(tmp_path):
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


def _send_tmp_file(tmp_path: str, mimetype: str, download_name: str):
    """send_file 返回临时文件，并在响应真正发送完毕后清理临时文件。

    注意：send_file 默认 direct_passthrough=True，此时 Werkzeug 不会把响应
    迭代器包进 ClosingIterator，response.call_on_close 回调永远不会执行
    （导致临时文件泄漏）。显式关闭 direct_passthrough 使回调生效。
    本地应用无需 wsgi.file_wrapper 的 sendfile 优化，性能无感。
    """
    response = send_file(
        tmp_path,
        mimetype=mimetype,
        as_attachment=True,
        download_name=download_name,
        max_age=0,  # 导出内容随任务变化，禁止浏览器缓存
    )
    response.direct_passthrough = False

    @response.call_on_close
    def _cleanup():
        _cleanup_tmp(tmp_path)

    return response


@api_export_bp.post("/api/export/<job_id>/zip")
def export_zip(job_id: str):
    """导出下载目录为 ZIP 文件"""
    job, err = _check_job(job_id)
    if err:
        return err
    output_dir = _resolve_output_dir(job)
    if output_dir is None:
        return jsonify({"status": "error", "message": "下载目录不存在"}), 404
    tmp_path = None
    try:
        safe_name = _safe_export_name(output_dir)
        # 使用临时文件避免大 ZIP 全量缓冲内存
        tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        tmp_path = tmp.name
        with tmp, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in safe_files(output_dir):
                if is_safe_path(f):
                    arcname = str(f.relative_to(output_dir.parent))
                    zf.write(str(f), arcname)
        return _send_tmp_file(tmp_path, "application/zip", f"{safe_name}.zip")
    except Exception as e:
        _cleanup_tmp(tmp_path)
        log.error(f"ZIP 导出失败 job_id={job_id} error={e}")
        return jsonify({"status": "error", "message": "ZIP 导出失败"}), 500


def _collect_images(output_dir: Path) -> list[Path]:
    """递归收集目录下所有图片文件（排序保证页序稳定）"""
    return [
        f for f in safe_files(output_dir)
        if f.is_file() and f.suffix.lower() in EXPORT_IMAGE_EXTENSIONS
    ]


def _needs_pillow_conversion(img_path: Path) -> bool:
    """判断图片是否需要经 Pillow 预处理才能嵌入 PDF。

    img2pdf 拒绝带 alpha 通道的图片（AlphaChannelError），动图只应取首帧。
    Image.open 只读文件头，开销极小。判断失败时返回 True 交给 Pillow 兜底。
    """
    try:
        from PIL import Image
        with Image.open(img_path) as im:
            im.load()  # Decode pixels too: a readable header alone does not prove a complete image.
            if im.mode in ("RGBA", "LA", "PA", "P"):
                # P 模式可能带透明 palette，统一转换最稳妥
                return True
            if getattr(im, "is_animated", False):
                return True
        return False
    except Exception:
        return True


def _image_to_jpeg_bytes(img_path: Path) -> bytes | None:
    """将图片经 Pillow 转为 JPEG 字节（丢弃 alpha / 动图取首帧）。失败返回 None。"""
    import io as _io

    try:
        from PIL import Image
        with Image.open(img_path) as im:
            rgb = im.convert("RGB")
            buf = _io.BytesIO()
            rgb.save(buf, format="JPEG", quality=90)
            return buf.getvalue()
    except Exception as e:
        log.warning(f"PDF 图片转换失败: {img_path.name} error={e}")
        return None


def _build_pdf(images: list[Path], out_stream) -> int:
    """完整写入图片列表并返回页数；任何坏图都抛错，禁止静默缺页。

    优先直接嵌入原图（无损、无重编码开销）；对带 alpha/动图先转 JPEG。
    如整体转换仍失败（个别图片格式异常），回退到全量 Pillow 转换。
    """
    import img2pdf

    pages: list = []
    failed = []
    for img_path in images:
        if _needs_pillow_conversion(img_path):
            b = _image_to_jpeg_bytes(img_path)
            if b:
                pages.append(b)
            else:
                failed.append(img_path)
        else:
            pages.append(str(img_path))

    if failed:
        raise IncompletePdfError(failed)
    if not pages:
        return 0

    try:
        img2pdf.convert(pages, outputstream=out_stream)
        return len(pages)
    except Exception as e:
        log.warning(f"PDF 直接嵌入失败，回退全量转换 error={e}")

    # 兜底：全部经 Pillow 归一化为 JPEG
    out_stream.seek(0)
    out_stream.truncate()
    fallback = []
    failed = []
    for path in images:
        converted = _image_to_jpeg_bytes(path)
        if converted:
            fallback.append(converted)
        else:
            failed.append(path)
    if failed:
        raise IncompletePdfError(failed)
    img2pdf.convert(fallback, outputstream=out_stream)
    return len(fallback)


@api_export_bp.post("/api/export/<job_id>/pdf")
def export_pdf(job_id: str):
    """导出下载目录为 PDF 文件（每张图片一页，按文件名排序）"""
    job, err = _check_job(job_id)
    if err:
        return err
    output_dir = _resolve_output_dir(job)
    if output_dir is None:
        return jsonify({"status": "error", "message": "下载目录不存在"}), 404

    try:
        images = _collect_images(output_dir)
    except (ValueError, OSError):
        return jsonify(status="error", message="无法安全读取下载目录"), 403
    if not images:
        return jsonify({"status": "error", "message": "下载目录中没有图片"}), 404

    tmp_path = None
    try:
        tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        tmp_path = tmp.name
        with tmp:
            page_count = _build_pdf(images, tmp)
        if page_count != len(images):
            _cleanup_tmp(tmp_path)
            return jsonify(status="error", message="PDF 页数不完整，已取消导出，请检查原图"), 422

        safe_name = _safe_export_name(output_dir)
        log.info(f"PDF 导出完成 job_id={job_id} pages={page_count}/{len(images)}")
        return _send_tmp_file(tmp_path, "application/pdf", f"{safe_name}.pdf")
    except IncompletePdfError as e:
        _cleanup_tmp(tmp_path)
        log.warning(f"PDF 完整性检查失败 job_id={job_id} failed={len(e.images)}")
        return jsonify(status="error", message=str(e), failed_images=e.images), 422
    except Exception as e:
        _cleanup_tmp(tmp_path)
        log.error(f"PDF 导出失败 job_id={job_id} error={e}")
        return jsonify({"status": "error", "message": "PDF 导出失败"}), 500
