"""
ZIP / PDF 导出 API

两个端点共用同一套模式：
  校验任务 → 收集文件 → 写入临时文件（避免大文件全量缓冲内存）
  → send_file 返回 → response.call_on_close 延迟清理临时文件

页从哪里来与阅读器相同（core.archive_pages.merge_pages）：散图优先，目录里的压缩包（CBZ / 本程序打包的 ZIP）
补齐散图没有的页，同一页不会出现两次；只剩压缩包时全从压缩包来。页只在内存 / 系统临时目录里解出，
从不写进下载目录。只剩压缩包而它损坏或没有图片时给出明确的错误（422）；一页都没有 → 404。
ZIP 导出永远不把 .cbz / .zip 压缩包本身再装进去。
"""
import os
import shutil
import tempfile
import zipfile
from contextlib import nullcontext
from pathlib import Path

from flask import Blueprint, jsonify, send_file

import core.database as db
from core import archive_pages
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
    if job.get("superseded_at"):
        # 目录被删除后又被后来的下载重新建出（core.local_availability）：里面已不是这次下载的完整内容
        return None, (jsonify({"status": "error", "message": "这次下载的文件已不在（目录已被重新下载），请从最新的下载任务导出"}), 404)
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


# 压缩包打不开 / 没有图片时导出的说明
_ARCHIVE_EXPORT_PROBLEMS = {
    "corrupt": "本地压缩包已损坏，无法导出",
    "empty": "本地压缩包里没有可导出的图片",
}


_PAGE_READ_FAILED = "压缩包里有页读不出来，压缩包可能已损坏，已取消导出"
_ARCHIVE_BUSY = "本地压缩包暂时打不开（可能被别的程序占用），请稍后再导出"


def _archive_for_export(output_dir: Path, has_loose_images: bool):
    """导出用的压缩包：(页目录或 None, 错误响应或 None)。有散图时压缩包只用来补页，不可用就不用；
    没有散图时压缩包是唯一来源，损坏 / 没有图片 → 422 说明原因。"""
    index = archive_pages.select(output_dir)
    if index is not None and index.transient:
        # 暂时打不开（被别的程序占用等）：不是损坏；导出不完整的内容也不行，请稍后再导出
        log.warning(f"导出压缩包暂时打不开 archive={index.path.name} detail={index.reason}")
        return None, (jsonify(status="error", reason="archive_busy", message=_ARCHIVE_BUSY), 503)
    if index is None or index.status == "ok":
        return index, None
    if has_loose_images:
        return None, None
    text = _ARCHIVE_EXPORT_PROBLEMS.get(index.status, _ARCHIVE_EXPORT_PROBLEMS["empty"])
    log.warning(f"导出压缩包不可用 archive={index.path.name} status={index.status} detail={index.reason}")
    reason = "archive_corrupt" if index.status == "corrupt" else "archive_empty"
    return None, (jsonify(status="error", reason=reason, message=f"{text}（{index.path.name}）"), 422)


def _reader_for(index, entries):
    """要从压缩包读页时打开它一次（archive_pages.open_pages），否则什么也不打开"""
    if any(isinstance(source, archive_pages.ArchivePage) for _, source in entries):
        return archive_pages.open_pages(index)
    return nullcontext()


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
        # 打包出的 .cbz / .zip 不再装进导出的 ZIP；章节标记等其他文件照旧放进去；空文件（中断的下载留下的）不算页
        files = [f for f in safe_files(output_dir, nonempty=True)
                 if is_safe_path(f) and f.suffix.lower() not in archive_pages.ARCHIVE_SUFFIXES]
        has_images = any(f.suffix.lower() in EXPORT_IMAGE_EXTENSIONS for f in files)
        index, err = _archive_for_export(output_dir, has_images)
        if err:
            return err
        # 散图没有的页从压缩包补（保留章节路径和页序，同一页用散图）
        entries = archive_pages.merge_pages(
            [(f.relative_to(output_dir).as_posix(), f) for f in files], index, reader_only=False)
        if not has_images and not any(isinstance(source, archive_pages.ArchivePage) for _, source in entries):
            return jsonify({"status": "error", "message": "下载目录中没有图片"}), 404
        safe_name = _safe_export_name(output_dir)
        # 使用临时文件避免大 ZIP 全量缓冲内存
        tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        tmp_path = tmp.name
        with tmp, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf, _reader_for(index, entries) as read:
            for name, source in entries:
                arcname = f"{output_dir.name}/{name}"
                if isinstance(source, archive_pages.ArchivePage):
                    zf.writestr(arcname, read(source))
                else:
                    zf.write(str(source), arcname)
        return _send_tmp_file(tmp_path, "application/zip", f"{safe_name}.zip")
    except archive_pages.ArchiveBusy as e:
        _cleanup_tmp(tmp_path)
        log.warning(f"ZIP 导出时压缩包暂时读不了 job_id={job_id} error={e}")
        return jsonify(status="error", reason="archive_busy", message=_ARCHIVE_BUSY), 503
    except archive_pages.ArchiveError as e:
        _cleanup_tmp(tmp_path)
        log.warning(f"ZIP 导出时压缩包页读取失败 job_id={job_id} error={e}")
        return jsonify(status="error", reason="archive_corrupt", message=_PAGE_READ_FAILED), 422
    except Exception as e:
        _cleanup_tmp(tmp_path)
        log.error(f"ZIP 导出失败 job_id={job_id} error={e}")
        return jsonify({"status": "error", "message": "ZIP 导出失败"}), 500


def _collect_images(output_dir: Path) -> list[Path]:
    """递归收集目录下所有图片文件（排序保证页序稳定）"""
    return [
        f for f in safe_files(output_dir, nonempty=True)  # 空文件（中断的下载留下的）不算页
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
    index, err = _archive_for_export(output_dir, bool(images))
    if err:
        return err
    # 散图没有的页从压缩包补（同一页用散图）；只剩压缩包时全从压缩包来
    entries = archive_pages.merge_pages(
        [(img.relative_to(output_dir).as_posix(), img) for img in images], index, reader_only=False)
    if not entries:
        return jsonify({"status": "error", "message": "下载目录中没有图片"}), 404

    names = {}          # 从压缩包解出的临时文件名 → 压缩包里的页名（报错时用）
    extract_dir = None  # 压缩包里的页按顺序解到系统临时目录（不是下载目录），沿用按文件生成 PDF 的流程
    tmp_path = None
    try:
        pages = []
        if any(isinstance(source, archive_pages.ArchivePage) for _, source in entries):
            extract_dir = tempfile.mkdtemp(prefix="jm-pdf-pages-")
            try:
                with archive_pages.open_pages(index) as read:
                    for number, (name, source) in enumerate(entries, start=1):
                        if not isinstance(source, archive_pages.ArchivePage):
                            pages.append(source)
                            continue
                        data = read(source)
                        target = Path(extract_dir) / f"archive-{number:05d}{source.suffix}"
                        target.write_bytes(data)
                        names[target.name] = name
                        pages.append(target)
            except archive_pages.ArchiveBusy as e:
                log.warning(f"PDF 导出时压缩包暂时读不了 job_id={job_id} error={e}")
                return jsonify(status="error", reason="archive_busy", message=_ARCHIVE_BUSY), 503
            except archive_pages.ArchiveError as e:
                log.warning(f"PDF 导出时压缩包页读取失败 job_id={job_id} error={e}")
                return jsonify(status="error", reason="archive_corrupt", message=_PAGE_READ_FAILED), 422
            except OSError as e:
                # 写临时文件失败（多半是系统盘空间不足），不是压缩包的问题
                log.error(f"PDF 导出写临时文件失败 job_id={job_id} dir={extract_dir} error={e}")
                return jsonify(status="error", message="临时空间不足或无法写入临时文件，已取消导出"), 500
        else:
            pages = [source for _, source in entries]

        tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        tmp_path = tmp.name
        with tmp:
            page_count = _build_pdf(pages, tmp)
        if page_count != len(pages):
            _cleanup_tmp(tmp_path)
            return jsonify(status="error", message="PDF 页数不完整，已取消导出，请检查原图"), 422

        safe_name = _safe_export_name(output_dir)
        log.info(f"PDF 导出完成 job_id={job_id} pages={page_count}/{len(pages)}")
        return _send_tmp_file(tmp_path, "application/pdf", f"{safe_name}.pdf")
    except IncompletePdfError as e:
        _cleanup_tmp(tmp_path)
        log.warning(f"PDF 完整性检查失败 job_id={job_id} failed={len(e.images)}")
        return jsonify(status="error", message=str(e), failed_images=[names.get(n, n) for n in e.images]), 422
    except Exception as e:
        _cleanup_tmp(tmp_path)
        log.error(f"PDF 导出失败 job_id={job_id} error={e}")
        return jsonify({"status": "error", "message": "PDF 导出失败"}), 500
    finally:
        if extract_dir:
            shutil.rmtree(extract_dir, ignore_errors=True)
