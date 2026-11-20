"""
ZIP / CBZ / PDF 导出 API

三个端点共用同一套模式：
  校验任务 → 收集文件 → 写入临时文件（避免大文件全量缓冲内存）
  → send_file 返回 → response.call_on_close 延迟清理临时文件

CBZ 导出采用 ZIP_STORED（零重压缩直存）+ 仅含图片条目（images_only=True）+ 去除外层冗余套娃目录，
极大降低导出 CPU 开销并让主流 CBZ 漫画阅读器实现零解压延迟翻页。

页从哪里来与阅读器相同（core.archive_pages.merge_pages）：散图优先，目录里的压缩包（CBZ / 本程序打包的 ZIP）
补齐散图没有的页，同一页不会出现两次；只剩压缩包时全从压缩包来。页只在内存 / 系统临时目录里解出，
从不写进下载目录。只剩压缩包而它损坏或没有图片时给出明确的错误（422）；一页都没有 → 404。
ZIP / CBZ 导出永远不把 .cbz / .zip 压缩包本身再装进去。
"""
import hashlib
import io
import os
import shutil
import tempfile
import threading
import zipfile
from contextlib import closing, contextmanager, nullcontext
from dataclasses import dataclass
from functools import wraps
from pathlib import Path

from flask import Blueprint, Response, g, jsonify, request, send_file

import core.database as db
from core import album_maintenance, archive_pages, desktop_platform, publication
from core.logger import log
from core.path_guard import DOWNLOAD_ROOT, is_safe_path
from core.path_utils import get_export_dir
from core.publication_access import published_path, snapshot_page_digests
from core.validation import validate_job_id, validate_numeric, is_allowed_image, EXPORT_IMAGE_EXTENSIONS
from core.file_tree import is_link, _entry_is_link, natural_key
from core.download_layout import CHAPTER_MARKER
from core.storage import is_temporary_path
from core.export_budget import ExportBudget, ExportLimit, ExportTimeout, BudgetedWriter
from core.zip_preflight import BoundedZipFile, ZipMetadataLimit

api_export_bp = Blueprint("api_export", __name__)

# A nonblocking process-wide lease covers collection/generation AND the response
# lifetime. Thus an abandoned/unclosed download cannot accumulate temp artifacts.
# There is no executor queue and no HTTP thread waiting for another export.
_EXPORT_SLOT = threading.BoundedSemaphore(1)
_RETRY_AFTER = "5"
_COPY_CHUNK = 1024 * 1024


def _export_capacity(view):
    @wraps(view)
    def guarded(**source):
        if not _EXPORT_SLOT.acquire(blocking=False):
            return (jsonify(status="error", reason="export_busy", message="已有导出正在生成或下载，请稍后重试"),
                    503, {"Retry-After": _RETRY_AFTER})
        response = None
        transferred = False
        slot = _EXPORT_SLOT
        release_lock = threading.Lock()
        released = False

        def release():
            nonlocal released
            with release_lock:
                if not released:
                    released = True
                    slot.release()

        try:
            budget = ExportBudget()
            response = view(budget=budget, **source)
            budget.check()
            if isinstance(response, _ExportResponse) and response.status_code == 200:
                response.call_on_close(release)
                g._export_response = response
                transferred = True
            return response
        except ExportLimit as e:
            return jsonify(status="error", reason="export_limit", message=str(e)), 413
        except ExportTimeout as e:
            return (jsonify(status="error", reason="export_timeout", message=str(e)),
                    503, {"Retry-After": _RETRY_AFTER})
        except archive_pages.ArchiveBusy as e:
            log.warning(f"导出时压缩包暂时读不了 source={source} error={e}")
            return jsonify(status="error", reason="archive_busy", message=_ARCHIVE_BUSY), 503
        except archive_pages.ArchiveError as e:
            log.warning(f"导出时压缩包页读取失败 source={source} error={e}")
            return jsonify(status="error", reason="archive_corrupt", message=_PAGE_READ_FAILED), 422
        except ExportSourceUnreadable as e:
            log.warning(f"导出时无法完整读取下载目录 source={source} error={e.__cause__ or e}")
            return (jsonify(status="error", reason="export_source_unreadable",
                            message="部分下载内容无法读取，已取消导出。请检查目录是否存在、是否有读取权限，再重试。"),
                    503, {"Retry-After": _RETRY_AFTER})
        except OSError as e:
            log.error(f"导出临时文件读写失败 source={source} error={e}")
            return jsonify(status="error", message="临时空间不足或无法读写临时文件，已取消导出"), 500
        except Exception as e:
            log.error(f"导出失败 source={source} error={e}")
            return jsonify(status="error", message="导出失败，已取消导出"), 500
        finally:
            if not transferred:
                try:
                    if isinstance(response, Response):
                        response.close()
                finally:
                    release()
    return guarded


@api_export_bp.teardown_request
def _close_failed_export(error):
    # Flask may fail in an after_request handler before handing the generated
    # response to WSGI. Such a response has no consumer to call close().
    if error is not None:
        response = getattr(g, "_export_response", None)
        if response is not None:
            response.close()


class _ExportResponse(Response):
    def close(self):
        # Werkzeug normally runs callbacks only after the iterator closes. Even
        # an I/O failure on close must release the lease and attempt temp cleanup.
        try:
            if hasattr(self.response, "close"):
                self.response.close()
        finally:
            for callback in self._on_close:
                callback()

    def __call__(self, environ, start_response):
        try:
            return super().__call__(environ, start_response)
        except BaseException:
            self.close()  # e.g. start_response fails before returning the iterator
            raise


class IncompletePdfError(ValueError):
    def __init__(self, images):
        self.images = [path.name for path in images]
        super().__init__(f"有 {len(self.images)} 张图片损坏或无法转换，已取消 PDF 导出；请修复或重新下载图片后重试")


class ExportSourceUnreadable(RuntimeError):
    """A complete page inventory cannot be established; never send a partial attachment."""


def _check_complete_source(source, size, budget):
    if size < budget.sources[source][0]:
        raise ExportSourceUnreadable("源文件在导出期间变短，已取消导出")


@dataclass(frozen=True)
class _ExportSource:
    directory: Path
    album_name: str
    expected_pages: int | None
    snapshot_job: str | None


def _export_directory(job_id=None, album_id=None):
    """Only completed tasks may export snapshots. Album reads always follow the public catalog."""
    snapshot = None
    if album_id is not None:
        if not validate_numeric(album_id):
            return None, (jsonify(status="error", message="album_id 必须是纯数字"), 400)
        record = db.get_completed_job_by_album_id(album_id)
        if not record or record.get("superseded_at"):
            return None, (jsonify(status="error", message="未找到可导出的本地资源"), 404)
    else:
        if not validate_job_id(job_id):
            return None, (jsonify(status="error", message="job_id 格式非法"), 400)
        record = db.get_job(job_id)
        if not record:
            return None, (jsonify(status="error", message="任务不存在"), 404)
        if record["status"] != "completed":
            return None, (jsonify(status="error", message="任务未完成"), 400)
        try:
            snapshot = publication.job_snapshot(job_id, DOWNLOAD_ROOT)
        except (OSError, ValueError) as error:
            raise ExportSourceUnreadable() from error
    if snapshot is None and record.get("superseded_at"):
        # 目录被删除后又被后来的下载重新建出（core.local_availability）：里面已不是这次下载的完整内容
        return None, (jsonify({"status": "error", "message": "这次下载的文件已不在（目录已被重新下载），请从最新的下载任务导出"}), 404)
    output_path = record.get("output_path")
    if snapshot is None and not output_path:
        return None, (jsonify({"status": "error", "message": "任务无输出路径"}), 404)
    output_dir = Path(output_path) if snapshot is None else snapshot
    if snapshot is None:
        allowed = published_path(output_dir, DOWNLOAD_ROOT, directory=True) is not None
        if album_id is not None and (not allowed or not output_dir.is_dir()):
            album_maintenance.wait_for_active_move()
            record = db.get_completed_job_by_album_id(album_id)
            if not record or record.get("superseded_at"):
                return None, (jsonify(status="error", message="未找到可导出的本地资源"), 404)
            output_path = record.get("output_path")
            if not output_path:
                return None, (jsonify({"status": "error", "message": "任务无输出路径"}), 404)
            output_dir = Path(output_path)
            allowed = published_path(output_dir, DOWNLOAD_ROOT, directory=True) is not None
    else:
        allowed = is_safe_path(output_dir) and output_dir.resolve() != DOWNLOAD_ROOT.resolve()
    if not allowed:
        return None, (jsonify({"status": "error", "message": "输出路径越权"}), 403)
    if not output_dir.is_dir():
        if snapshot is not None:
            raise ExportSourceUnreadable("任务成品目录已不在原位置")
        return None, (jsonify(status="error", message="下载目录不存在"), 404)
    if is_link(output_dir):
        return None, (jsonify(status="error", message="输出目录不能是链接"), 403)
    name = Path(output_path).name if output_path else ""
    if name in ("", ".."):
        name = album_id or job_id
    return _ExportSource(output_dir, name, record.get("total_pages"), job_id if snapshot is not None else None), None


# 压缩包打不开 / 没有图片时导出的说明
_ARCHIVE_EXPORT_PROBLEMS = {
    "corrupt": "本地压缩包已损坏，无法导出",
    "empty": "本地压缩包里没有可导出的图片",
}


_PAGE_READ_FAILED = "压缩包里有页读不出来，压缩包可能已损坏，已取消导出"
_ARCHIVE_BUSY = "本地压缩包暂时打不开（可能被别的程序占用），请稍后再导出"


def _check_export_archive_metadata(handle, budget):
    """Apply the export budget before constructing the reader index."""
    try:
        with BoundedZipFile(handle, max_entries=budget.limits.archive_entries,
                            max_directory_bytes=budget.limits.archive_metadata_bytes, check=budget.check):
            budget.check()
    except ZipMetadataLimit:
        raise ExportLimit("压缩包目录条目或元数据超过导出限制") from None


def _archive_for_export(output_dir: Path, has_loose_images: bool, budget):
    """导出用的压缩包：(页目录或 None, 错误响应或 None)。有散图时压缩包只用来补页，不可用就不用；
    没有散图时压缩包是唯一来源，损坏 / 没有图片 → 422 说明原因。"""
    # Keep select()'s precedence and transient-error semantics, but check the ZIP
    # directory size before ZipFile/SDK index construction allocates metadata.
    first = busy = index = None
    # Reuse the bounded scan, rather than calling candidates() and doing another
    # unchecked traversal of a directory that may still be growing.
    found = {path.name.lower(): path for path in budget.archive_paths}
    own = [found[name.lower()] for name in (output_dir.name + ".cbz", output_dir.name + ".zip")
           if name.lower() in found]
    own.sort(key=lambda path: (archive_pages._signature(path) or (0, 0))[1], reverse=True)
    own_names = {path.name.lower() for path in own}
    others = sorted((path for name, path in found.items() if name.endswith(".cbz") and name not in own_names),
                    key=lambda path: natural_key(path.name))
    for path in own + others:
        budget.check()
        try:
            budget.check_size(path.stat().st_size)
            with path.open("rb") as handle:
                _check_export_archive_metadata(handle, budget)
        except (ExportLimit, ExportTimeout):
            raise
        except Exception:
            # read_index classifies corrupt versus transient, including locks.
            pass
        candidate = archive_pages.read_index(path)
        budget.check()
        if candidate.status == "ok":
            index = candidate
            break
        first = first or candidate
        busy = busy or (candidate if candidate.transient else None)
    index = index or busy or first
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


def _collect_files(output_dir, budget, images_only=False, *, snapshot=False):
    """Bound the scan itself, including trees containing only empty directories.

    safe_files sorts a fully materialized tree; even iter_safe_files may traverse
    arbitrarily many ignored entries before yielding. Check every directory
    entry here, then sort only the bounded selection with the same natural key.
    """
    files, pending = [], [output_dir]
    while pending:
        budget.check()
        folder = pending.pop()
        if is_temporary_path(folder):
            continue
        try:
            if is_link(folder):
                if folder == output_dir:
                    raise ValueError("源目录不能是符号链接或目录联接")
                continue
            with os.scandir(folder) as listing:
                for entry in listing:
                    budget.visit()
                    if is_temporary_path(entry.name) or _entry_is_link(entry):
                        continue
                    if folder == output_dir and (entry.name == '.jm-view.json' or
                                                 (snapshot and entry.name == '.jm-publication.json')):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    path = Path(entry.path)
                    size = entry.stat(follow_symlinks=False).st_size
                    if path.suffix.lower() in archive_pages.ARCHIVE_SUFFIXES:
                        if folder == output_dir:
                            budget.archive_paths.append(path)
                        continue
                    if size <= 0:
                        continue
                    is_page = path.suffix.lower() in EXPORT_IMAGE_EXTENSIONS
                    if (images_only and not is_page) or not is_safe_path(path):
                        continue
                    budget.add_source(path, size, is_page)
                    files.append(path)
        except OSError as e:
            raise ExportSourceUnreadable() from e
    budget.check()
    files.sort(key=lambda path: natural_key(path.relative_to(output_dir)))
    budget.check()
    return files


def _merge_export_pages(output_dir, files, index, budget):
    """Same merge keys/order as the reader; budget archive additions as we go."""
    entries = [(path.relative_to(output_dir).as_posix(), path) for path in files]
    have = {archive_pages.page_key(name) for name, _ in entries}
    extra = False
    if index is not None and index.status == "ok":
        for page in index.pages:
            budget.check()
            if archive_pages.page_key(page.name) in have:
                continue
            budget.add_source(page, page.size, True)
            entries.append((page.name, page))
            extra = True
    if extra:
        entries.sort(key=lambda item: natural_key(item[0].replace("\\", "/")))
    budget.check()
    return entries


def _export_entries(source, budget, *, images_only, library):
    """One bounded inventory for both entry points. Do not scan again for library availability."""
    output_dir = source.directory
    files = _collect_files(output_dir, budget, images_only=images_only, snapshot=source.snapshot_job is not None)
    if source.snapshot_job is not None:
        # A seal names loose pages. Archives and unrelated sidecars cannot supply missing content.
        try:
            digests = snapshot_page_digests(source.snapshot_job, output_dir, budget)
        except OSError as error:
            raise ExportSourceUnreadable() from error
        markers = {f"{name.rsplit('/', 1)[0]}/{CHAPTER_MARKER}" for name in digests}
        entries = []
        for path in files:
            name = path.relative_to(output_dir).as_posix()
            if path.suffix.lower() in EXPORT_IMAGE_EXTENSIONS:
                if name not in digests:
                    raise ExportSourceUnreadable("任务成品含有清单外的页")
                budget.page_digests[path] = digests[name]
            elif name not in markers:
                continue
            entries.append((name, path))
        if (not digests or len(digests) != budget.pages
                or (source.expected_pages and source.expected_pages != budget.pages)):
            raise ExportSourceUnreadable("任务成品页数与发布记录不符")
        return entries, None, None
    has_images = any(path.suffix.lower() in EXPORT_IMAGE_EXTENSIONS for path in files)
    index, err = _archive_for_export(output_dir, has_images, budget)
    if err:
        return None, None, err
    entries = _merge_export_pages(output_dir, files, index, budget)
    if not index and not has_images:
        return None, None, (jsonify(status="error", message="下载目录中没有图片"), 404)
    if library and index is None and not any(is_allowed_image(path.suffix) for path in files):
        return None, None, (jsonify(status="error", message="资源中没有可离线阅读的图片"), 404)
    return entries, index, None


def _read_archive_page(read, source, budget):
    budget.check()
    data = read(source)  # shared reader pins entries and checks signature/CRC/size
    budget.observe_source(source, len(data))
    if len(data) != source.size:
        raise archive_pages.ArchiveError(f"页大小不符: {source.name}")
    return data


def _safe_export_name(name: str) -> str:
    """生成下载文件名（去除路径分隔符和空格）"""
    return name.replace(" ", "_").replace("/", "_").replace("\\", "_")


def _zip_file_chunks(source, budget):
    expected = budget.page_digests.get(source)
    digest = hashlib.sha256() if expected is not None else None
    total = 0
    try:
        if is_link(source) or not is_safe_path(source):
            raise ValueError("源文件路径在导出时发生变化")
        with source.open("rb") as handle:
            while True:
                budget.check()
                chunk = handle.read(_COPY_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                budget.observe_source(source, total)
                if digest is not None:
                    digest.update(chunk)
                yield chunk
    except (OSError, ValueError) as error:
        if expected is not None:
            raise ExportSourceUnreadable() from error
        raise
    _check_complete_source(source, total, budget)
    if digest is not None and digest.hexdigest() != expected:
        raise ExportSourceUnreadable("任务成品页内容与发布记录不符")


def _cleanup_tmp(tmp_path: str | None):
    """静默删除临时文件"""
    if tmp_path and os.path.exists(tmp_path):
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


def _wants_local_save() -> bool:
    return (
        request.headers.get("X-Export-Target", "").strip().lower() == "local"
        or request.args.get("save", "").strip().lower() == "local"
    )


def _prepare_local_export_dir() -> Path:
    export_dir = get_export_dir()
    if export_dir.exists() and (is_link(export_dir) or not export_dir.is_dir()):
        raise OSError("导出目录不可用或为符号链接")
    export_dir.mkdir(parents=True, exist_ok=True)
    if is_link(export_dir) or not export_dir.is_dir():
        raise OSError("导出目录不可用或为符号链接")
    return export_dir


def _finalize_local_export(tmp_path: str, export_dir: Path, filename: str) -> Path:
    root = export_dir.resolve()
    clean = Path(filename).name
    if not clean or clean in (".", "..") or clean != filename:
        raise ValueError("导出文件名无效")
    stem, suffix = Path(clean).stem, Path(clean).suffix
    candidates = [export_dir / f"{stem}{suffix}"] + [
        export_dir / f"{stem} ({index}){suffix}" for index in range(2, 100)
    ]
    last_error = None
    for candidate in candidates:
        if candidate.parent.resolve() != root:
            raise ValueError("导出目标路径越权")
        if os.path.lexists(candidate) and (is_link(candidate) or candidate.is_dir()):
            continue
        try:
            os.replace(tmp_path, candidate)
            return candidate
        except PermissionError as exc:
            last_error = exc
            continue
    raise last_error or OSError("无法写入导出文件（同名文件可能正被占用）")


def _send_tmp_file(tmp_path: str, mimetype: str, download_name: str):
    """send_file 返回临时文件，并在响应真正发送完毕后清理临时文件。

    注意：send_file 默认 direct_passthrough=True，此时 Werkzeug 不会把响应
    迭代器包进 ClosingIterator，response.call_on_close 回调永远不会执行
    （导致临时文件泄漏）。显式关闭 direct_passthrough 使回调生效。
    本地应用无需 wsgi.file_wrapper 的 sendfile 优化，性能无感。
    """
    response = None
    try:
        response = _ExportResponse.force_type(send_file(
            tmp_path,
            mimetype=mimetype,
            as_attachment=True,
            download_name=download_name,
            max_age=0,  # 导出内容随任务变化，禁止浏览器缓存
        ))
        response.direct_passthrough = False
        response.call_on_close(lambda: _cleanup_tmp(tmp_path))
        return response
    except BaseException:
        try:
            if response is not None:
                response.close()
        finally:
            _cleanup_tmp(tmp_path)
        raise


def _export_archive(budget, *, job_id=None, album_id=None, as_cbz: bool = False):
    """导出下载目录为 ZIP 或 CBZ 文件。

    CBZ 模式采用 ZIP_STORED 零重压缩直存 + 仅含图片页 + 省略外层书名套娃目录，
    既节省服务端 CPU，又让外部 CBZ 阅读器获得最佳随机读取性能。
    """
    source_dir, err = _export_directory(job_id, album_id)
    if err:
        return err
    tmp_path = None
    sent = False
    try:
        # 打包出的 .cbz / .zip 不再装进导出的归档；ZIP 保留章节标记，CBZ 仅写入可阅读图片页
        entries, index, err = _export_entries(source_dir, budget, images_only=as_cbz, library=album_id is not None)
        if err:
            return err
        safe_name = _safe_export_name(source_dir.album_name)
        suffix = ".cbz" if as_cbz else ".zip"
        compression = zipfile.ZIP_STORED if as_cbz else zipfile.ZIP_DEFLATED
        mimetype = "application/vnd.comicbook+zip" if as_cbz else "application/zip"
        local_save = _wants_local_save()
        export_dir = _prepare_local_export_dir() if local_save else None
        # 使用临时文件避免大压缩包全量缓冲内存；本地保存时直接在导出目录同盘暂存以便原子重命名
        tmp = tempfile.NamedTemporaryFile(
            dir=export_dir if local_save else None,
            prefix=".jm-export-" if local_save else "tmp",
            suffix=".tmp" if local_save else suffix,
            delete=False,
        )
        tmp_path = tmp.name
        with tmp, zipfile.ZipFile(BudgetedWriter(tmp, budget), "w", compression) as zf, _reader_for(index, entries) as read:
            for name, source in entries:
                budget.check()
                arcname = name if as_cbz else f"{source_dir.album_name}/{name}"
                with zf.open(arcname, "w", force_zip64=True) as target:
                    if isinstance(source, archive_pages.ArchivePage):
                        data = _read_archive_page(read, source, budget)
                        for offset in range(0, len(data), _COPY_CHUNK):
                            budget.check()
                            target.write(memoryview(data)[offset:offset + _COPY_CHUNK])
                            budget.check()
                        del data
                    else:
                        with closing(_zip_file_chunks(source, budget)) as chunks:
                            for chunk in chunks:
                                target.write(chunk)
        budget.check()
        if local_save:
            final_path = _finalize_local_export(tmp_path, export_dir, f"{safe_name}{suffix}")
            sent = True
            size = final_path.stat().st_size
            log.info(f"归档本地导出完成 album_id={album_id} job_id={job_id} path={final_path} size={size}")
            return jsonify(
                status="ok",
                saved=True,
                filename=final_path.name,
                folder=str(final_path.parent),
                path=str(final_path),
                size=size,
            )
        response = _send_tmp_file(tmp_path, mimetype, f"{safe_name}{suffix}")
        sent = True
        return response
    finally:
        if not sent:
            _cleanup_tmp(tmp_path)


@api_export_bp.post("/api/export/<job_id>/zip")
@api_export_bp.post("/api/library/<album_id>/export/zip")
@_export_capacity
def export_zip(budget, job_id=None, album_id=None):
    """导出下载目录为 ZIP 文件"""
    return _export_archive(budget, job_id=job_id, album_id=album_id, as_cbz=False)


@api_export_bp.post("/api/export/<job_id>/cbz")
@api_export_bp.post("/api/library/<album_id>/export/cbz")
@_export_capacity
def export_cbz(budget, job_id=None, album_id=None):
    """导出下载目录为 CBZ 漫画压缩包（ZIP_STORED 零重压缩直存）"""
    return _export_archive(budget, job_id=job_id, album_id=album_id, as_cbz=True)


def _check_pixels(image, budget):
    if image.width * image.height > budget.limits.page_pixels:
        raise ExportLimit("单页图片像素数超过导出限制")


def _needs_pillow_conversion(image_source, budget=None) -> bool:
    """判断图片是否需要经 Pillow 预处理才能嵌入 PDF。

    img2pdf 拒绝带 alpha 通道的图片（AlphaChannelError），动图只应取首帧。
    同时完整解码当前页验证图片；判断失败时返回 True 交给 Pillow 兜底。
    """
    budget = budget or ExportBudget()
    try:
        from PIL import Image
        with Image.open(image_source) as im:
            _check_pixels(im, budget)
            im.load()  # Decode pixels too: a readable header alone does not prove a complete image.
            budget.check()
            if im.mode in ("RGBA", "LA", "PA", "P"):
                # P 模式可能带透明 palette，统一转换最稳妥
                return True
            if getattr(im, "n_frames", 1) > 1:
                return True
        return False
    except (ExportLimit, ExportTimeout):
        raise
    except Exception:
        return True


def _image_to_jpeg_file(img_path: Path, folder: Path, budget, *, stream=None) -> Path | None:
    """Normalize one complete image to a budgeted JPEG file, never a book of bytes."""
    target = None
    try:
        from PIL import Image
        with Image.open(img_path if stream is None else stream) as im:
            budget.check()
            _check_pixels(im, budget)
            im.load()
            budget.check()
            with im.convert("RGB") as rgb:
                with tempfile.NamedTemporaryFile(dir=folder, suffix=".jpg", delete=False) as tmp:
                    target = Path(tmp.name)
                    writer = BudgetedWriter(tmp, budget, str(target))
                    rgb.save(writer, format="JPEG", quality=90)
                    writer.flush()
                    budget.resize_staging(str(target), target.stat().st_size)
            budget.input_sources[str(target)] = None
            budget.check()
            return target
    except (ExportLimit, ExportTimeout, OSError) as e:
        # PIL also uses OSError for undecodable pixels. Only OS errors with an
        # errno are storage failures; those must not become a "bad image" 422.
        if not isinstance(e, OSError) or e.errno is not None:
            raise
        log.warning(f"PDF 图片转换失败: {img_path.name} error={e}")
        return None
    except Exception as e:
        log.warning(f"PDF 图片转换失败: {img_path.name} error={e}")
        return None
    finally:
        # Partial files are cleaned with their enclosing conversion directory.
        # Keep them in the live staging budget until then.
        budget.check()


class _PdfInput:
    """img2pdf reads a single bounded page at a time, including late file growth."""
    def __init__(self, path, budget):
        self.path, self.budget = Path(path), budget

    def read_bytes(self):
        budget = self.budget
        budget.check()
        size = self.path.stat().st_size
        budget.check_size(size, True)
        if is_link(self.path):
            raise ValueError("源文件在 PDF 导出时变成了链接")
        with self.path.open("rb") as handle:
            # read(max_page_bytes) can itself reserve 128 MiB for a tiny image.
            # Bound allocation by this file's observed size and reject late
            # growth, rather than silently embedding only its original prefix.
            data = handle.read(size + 1)
        if len(data) > size:
            raise ExportLimit("源图片在导出期间变大，已取消，请重试")
        budget.check_size(len(data), True)
        key = budget.input_sources.get(str(self.path), self.path)
        if key is not None:
            _check_complete_source(key, len(data), budget)
            budget.observe_source(key, len(data))
        return data


class _PdfPages(list):
    def __init__(self, paths, budget):
        super().__init__(str(path) for path in paths)
        self.budget = budget

    def __iter__(self):
        for path in super().__iter__():
            self.budget.check()
            yield _PdfInput(path, self.budget)
            self.budget.check()


def _clear_conversions(folder, budget):
    """Free old paths/accounting BEFORE allocating the fallback JPEGs."""
    for path in folder.iterdir():
        path.unlink()
        budget.forget_staging(str(path))
        budget.input_sources.pop(str(path), None)


@contextmanager
def _pdf_page_input(path, folder, budget, force_jpeg):
    expected = budget.page_digests.get(path)
    if expected is None:
        yield _PdfPages([path], budget)
        return
    # Decode, embed or transcode these same authenticated bytes. Keep at most one
    # source page in memory; a fallback that rereads a snapshot authenticates it again.
    try:
        data = _PdfInput(path, budget).read_bytes()
    except (OSError, ValueError) as error:
        raise ExportSourceUnreadable() from error
    if hashlib.sha256(data).hexdigest() != expected:
        raise ExportSourceUnreadable("任务成品页内容与发布记录不符")
    with io.BytesIO(data) as stream:
        del data
        normalize = force_jpeg or _needs_pillow_conversion(stream, budget)
        stream.seek(0)
        if normalize:
            converted = _image_to_jpeg_file(path, folder, budget, stream=stream)
            if converted is None:
                raise IncompletePdfError([path])
            yield _PdfPages([converted], budget)
        else:
            yield stream


def _write_pdf_pages(pages, writer, folder, budget, *, force_jpeg=False):
    """Spool one image PDF at a time, then assemble with disk-backed streams.

    img2pdf.convert(all_pages, outputstream=...) retains the whole book before
    writing. Pdf.pages.extend also copies all foreign streams into memory.
    QPDF's job assembly keeps source streams on disk until the guarded save.
    """
    import img2pdf
    import pikepdf

    sources = []
    for path in pages:
        budget.check()
        with tempfile.NamedTemporaryFile(dir=folder, suffix=".pdf", delete=False) as tmp:
            target = Path(tmp.name)
            with _pdf_page_input(path, folder, budget, force_jpeg) as page_input:
                img2pdf.convert(page_input, outputstream=BudgetedWriter(tmp, budget, str(target)), first_frame_only=True)
            budget.check()
        sources.append({"file": str(target), "range": "1-z"})
    job = document = None
    try:
        budget.check()
        # outputFile is required by the job configuration, but create_pdf only
        # builds metadata; all output goes through BudgetedWriter below.
        job = pikepdf.Job({"empty": "", "outputFile": str(folder / "unused-output.pdf"), "pages": sources})
        document = job.create_pdf()
        budget.check()
        if len(document.pages) != len(pages):
            raise ValueError("单页 PDF 组合后页数不完整")
        try:
            document.save(writer, progress=lambda _percent: budget.check())
        except BaseException as error:
            # pikepdf's Python save frame retains its native document on a
            # failed write. Drop that frame before closing Windows source files.
            raise error.with_traceback(None)
        budget.check()
    finally:
        try:
            if document is not None:
                document.close()
        finally:
            # The job owns lazy source handles. Clear references even on errors
            # so a retained traceback cannot keep Windows temp files open.
            document = job = None


def _build_pdf(images: list[Path], out_stream, budget=None) -> int:
    """完整写入图片列表并返回页数；任何坏图都抛错，禁止静默缺页。

    优先直接嵌入原图（无损、无重编码开销）；对带 alpha/动图先转 JPEG。
    如整体转换仍失败（个别图片格式异常），回退到全量 Pillow 转换。
    """
    if budget is None:
        budget = ExportBudget()
        # Preserve the two-argument helper contract with the same safety limits.
        for path in images:
            budget.add_source(path, path.stat().st_size, True)
    writer = out_stream if isinstance(out_stream, BudgetedWriter) else BudgetedWriter(out_stream, budget)
    with tempfile.TemporaryDirectory(prefix="jm-pdf-jpeg-") as tmp_dir:
        folder = Path(tmp_dir)
        try:
            pages, failed = [], []
            for path in images:
                budget.check()
                if path in budget.page_digests:
                    pages.append(path)  # Read and prepare a sealed page once, at its single-page PDF write.
                    continue
                key = budget.input_sources.get(str(path), path)
                if key is not None:
                    size = path.stat().st_size
                    _check_complete_source(key, size, budget)
                    budget.observe_source(key, size)
                if _needs_pillow_conversion(path, budget):
                    converted = _image_to_jpeg_file(path, folder, budget)
                    if converted is None:
                        failed.append(path)
                    else:
                        pages.append(converted)
                else:
                    pages.append(path)
                budget.check()
            if failed:
                raise IncompletePdfError(failed)
            if not pages:
                return 0

            try:
                _write_pdf_pages(pages, writer, folder, budget)
                budget.check()
                return len(pages)
            except (ExportLimit, ExportTimeout, ExportSourceUnreadable, IncompletePdfError, OSError):
                raise
            except Exception as e:
                log.warning(f"PDF 直接嵌入失败，回退全量转换 error={e}")

            # Do not retain first-attempt paths, bytes, or files during fallback.
            pages.clear()
            converted = None
            _clear_conversions(folder, budget)
            writer.seek(0)
            writer.truncate()
            budget.check()
            failed = []
            for path in images:
                budget.check()
                if path in budget.page_digests:
                    pages.append(path)
                    continue
                converted = _image_to_jpeg_file(path, folder, budget)
                if converted is None:
                    failed.append(path)
                else:
                    pages.append(converted)
            if failed:
                raise IncompletePdfError(failed)
            _write_pdf_pages(pages, writer, folder, budget, force_jpeg=True)
            budget.check()
            return len(pages)
        finally:
            _clear_conversions(folder, budget)


@api_export_bp.post("/api/export/<job_id>/pdf")
@api_export_bp.post("/api/library/<album_id>/export/pdf")
@_export_capacity
def export_pdf(budget, job_id=None, album_id=None):
    """导出下载目录为 PDF 文件（每张图片一页，按文件名排序）"""
    source_dir, err = _export_directory(job_id, album_id)
    if err:
        return err

    names = {}          # 从压缩包解出的临时文件名 → 压缩包里的页名（报错时用）
    extract_dir = None  # 压缩包里的页按顺序解到系统临时目录（不是下载目录），沿用按文件生成 PDF 的流程
    tmp_path = None
    sent = False
    try:
        try:
            entries, index, err = _export_entries(source_dir, budget, images_only=True, library=album_id is not None)
        except (ValueError, OSError):
            return jsonify(status="error", message="无法安全读取下载目录"), 403
        if err:
            return err
        pages = []
        if any(isinstance(source, archive_pages.ArchivePage) for _, source in entries):
            extract_dir = tempfile.mkdtemp(prefix="jm-pdf-pages-")
            with archive_pages.open_pages(index) as read:
                for number, (name, source) in enumerate(entries, start=1):
                    budget.check()
                    if not isinstance(source, archive_pages.ArchivePage):
                        pages.append(source)
                        continue
                    target = Path(extract_dir) / f"archive-{number:05d}{source.suffix}"
                    budget.resize_staging(str(target), source.size)
                    data = _read_archive_page(read, source, budget)
                    written = target.write_bytes(data)
                    budget.resize_staging(str(target), target.stat().st_size)
                    if written != len(data) or written != target.stat().st_size:
                        raise OSError("临时文件写入不完整")
                    del data
                    budget.input_sources[str(target)] = source
                    names[target.name] = name
                    pages.append(target)
        else:
            pages = [source for _, source in entries]

        local_save = _wants_local_save()
        export_dir = _prepare_local_export_dir() if local_save else None
        tmp = tempfile.NamedTemporaryFile(
            dir=export_dir if local_save else None,
            prefix=".jm-export-" if local_save else "tmp",
            suffix=".tmp" if local_save else ".pdf",
            delete=False,
        )
        tmp_path = tmp.name
        with tmp:
            page_count = _build_pdf(pages, BudgetedWriter(tmp, budget), budget)
        budget.check()
        if page_count != len(pages):
            return jsonify(status="error", message="PDF 页数不完整，已取消导出，请检查原图"), 422

        safe_name = _safe_export_name(source_dir.album_name)
        if local_save:
            final_path = _finalize_local_export(tmp_path, export_dir, f"{safe_name}.pdf")
            sent = True
            size = final_path.stat().st_size
            log.info(f"PDF 本地导出完成 album_id={album_id} job_id={job_id} pages={page_count}/{len(pages)} path={final_path}")
            return jsonify(
                status="ok",
                saved=True,
                filename=final_path.name,
                folder=str(final_path.parent),
                path=str(final_path),
                size=size,
            )
        log.info(f"PDF 导出完成 album_id={album_id} job_id={job_id} pages={page_count}/{len(pages)}")
        response = _send_tmp_file(tmp_path, "application/pdf", f"{safe_name}.pdf")
        sent = True
        return response
    except IncompletePdfError as e:
        log.warning(f"PDF 完整性检查失败 album_id={album_id} job_id={job_id} failed={len(e.images)}")
        return jsonify(status="error", message=str(e), failed_images=[names.get(n, n) for n in e.images]), 422
    finally:
        if not sent:
            _cleanup_tmp(tmp_path)
        if extract_dir:
            shutil.rmtree(extract_dir, ignore_errors=True)
            for key in tuple(budget.staging):
                if Path(key).parent == Path(extract_dir):
                    budget.forget_staging(key)


@api_export_bp.post("/api/export/open-folder")
def open_export_folder():
    """在系统文件管理器中前置打开统一「导出」目录（可选高亮指定刚导出的文件）。"""
    if not desktop_platform.supports_open_folder():
        return jsonify(status="error", message="当前系统不支持直接打开文件夹，请手动前往导出目录"), 501
    try:
        export_dir = _prepare_local_export_dir()
    except OSError as e:
        log.warning(f"API打开导出目录失败 error={e}")
        return jsonify(status="error", message="导出目录不可用"), 403

    body = request.get_json(silent=True)
    selected = None
    if isinstance(body, dict):
        filename = body.get("filename")
        if isinstance(filename, str) and filename and Path(filename).name == filename and filename not in (".", ".."):
            candidate = export_dir / filename
            try:
                if (candidate.parent.resolve() == export_dir.resolve()
                        and not is_link(candidate) and candidate.is_file()):
                    selected = candidate
            except OSError:
                selected = None

    try:
        if selected is not None:
            desktop_platform.open_folder(export_dir, select=selected)
        else:
            desktop_platform.open_folder(export_dir)
        log.info(f"API打开导出目录 path={export_dir} select={selected.name if selected else ''}")
        return jsonify(status="ok", message="已打开导出文件夹", folder=str(export_dir))
    except desktop_platform.UnsupportedDesktopOperation:
        return jsonify(status="error", message="当前系统不支持直接打开文件夹，请手动前往导出目录"), 501
    except Exception as e:
        log.warning(f"API打开导出目录失败 path={export_dir} error={e}")
        return jsonify(status="error", message="打开导出文件夹失败"), 500
