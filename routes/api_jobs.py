"""下载任务 API"""
from collections import OrderedDict
import json
import os
import re
import stat
import time
import threading
from pathlib import Path

from flask import Blueprint, request, jsonify, Response, stream_with_context

from core.database import get_all_jobs, get_job
import core.database as db
from core.job_manager import job_manager
from core.task_commands import TaskCommandError
from core.logger import log
from core.progress import progress_manager, StreamCapacityError
from core.path_guard import is_safe_path, DOWNLOAD_ROOT
from core.validation import validate_numeric, validate_job_id, require_job_id
from core.packer import CbzPacker
from core import album_maintenance, archive_pages, download_folios
from core import desktop_platform
# ── 模块级压缩包检查缓存（避免 N+1 文件系统扫描）：值是压缩包格式 'cbz' / 'zip'，没有为 None ──
_cbz_cache: dict[str, tuple[str | None, float]] = {}
_CBZ_CACHE_TTL = 60  # 缓存有效期（秒）
_CBZ_CACHE_MAX = 500  # 最大缓存条目数
_cbz_cache_lock = threading.Lock()


def _scan_cbz_path(output_path: str) -> str | None:
    """实际扫描文件系统：输出目录里打包出的、能读的压缩包格式 'cbz' / 'zip'（与阅读器同一规则：
    core.archive_pages.select_status，含 pack_format=zip 写的 <目录名>.zip；打不开 / 没有图片的不算），
    第二层还有 .cbz 也算 'cbz'（旧版按章节打包）；没有为 None"""
    if not output_path:
        return None
    try:
        p = Path(output_path)
        if not p.exists() or not p.is_dir():
            return None
        if not is_safe_path(p):
            return None
        found = archive_pages.select_status(p)
        if found is not None and found[1] == "ok":
            return found[2]
        # 使用 glob 高效扫描（限制递归深度为 2 层）
        return "cbz" if any(p.glob("*/*.cbz")) else None
    except Exception:
        pass
    return None


def _dir_mtime_ns(output_path: str) -> int | None:
    if not output_path:
        return None
    try:
        st = os.stat(output_path)
        return st.st_mtime_ns if stat.S_ISDIR(st.st_mode) else None
    except OSError:
        return None


def _check_cbz_path(output_path: str) -> str | None:
    """检查单个路径的压缩包格式（带 TTL + 目录 mtime 失效缓存）"""
    now = time.time()
    mtime_ns = _dir_mtime_ns(output_path)
    with _cbz_cache_lock:
        cached = _cbz_cache.get(output_path)
        if (
            cached is not None
            and now - cached[1] < _CBZ_CACHE_TTL
            and (len(cached) < 3 or cached[2] == mtime_ns)
        ):
            return cached[0]
    exists = _scan_cbz_path(output_path)
    with _cbz_cache_lock:
        _cbz_cache.pop(output_path, None)
        _cbz_cache[output_path] = (exists, now, mtime_ns)
        # Eviction is atomic with insertion and O(1) on both dict and OrderedDict.
        while len(_cbz_cache) > _CBZ_CACHE_MAX:
            del _cbz_cache[next(iter(_cbz_cache))]
    return exists


def check_cbz_exists(job) -> bool:
    """检查任务的输出路径中是否有打包出的压缩包（兼容旧调用方）"""
    return bool(_check_cbz_path(job.get("output_path") or ""))


# 仍会往输出目录写入的任务状态（打开文件夹时目录可以先建出来）
_ACTIVE_JOB_STATUSES = ("queued", "running", "paused")


def _check_job_id(job_id: str):
    """校验 job_id 并返回统一错误响应（供路由直接调用）"""
    if not validate_job_id(job_id):
        return jsonify({"status": "error", "message": "job_id 格式非法"}), 400
    return None


api_jobs_bp = Blueprint("api_jobs", __name__)


@api_jobs_bp.post("/api/jobs")
def create_job():
    """创建新的下载任务(不阻塞: 不调 jmcomic API, 标题从请求体取或默认用 album_id)"""
    body = request.get_json(force=True)
    if not isinstance(body, dict):
        return jsonify(status="error", message="请求体需为 JSON 对象"), 400
    album_id = str(body.get("album_id", "") or "").strip()
    photo_ids = body.get("photo_ids", [])
    title = str(body.get("title", "") or "").strip() or album_id

    if not album_id:
        return jsonify({"status": "error", "message": "缺少 album_id"}), 400

    # 校验 album_id 为纯数字（SSRF 保护）
    if not validate_numeric(album_id):
        return jsonify({"status": "error", "message": "album_id 必须是纯数字"}), 400

    # 校验 photo_ids 为列表
    if not isinstance(photo_ids, list):
        return jsonify({"status": "error", "message": "photo_ids 必须是数组"}), 400
    if len(photo_ids) > 1000 or any(not validate_numeric(str(pid)) for pid in photo_ids):
        return jsonify(status="error", message="photo_ids 必须是最多 1000 个数字章节 ID"), 400
    photo_ids = list(dict.fromkeys(str(pid) for pid in photo_ids))

    # 限制 title 长度
    if len(title) > 500:
        title = title[:500]

    try:
        result = job_manager.create_job_result(album_id, title, photo_ids)
    except TaskCommandError as error:
        return jsonify(status="error", message=str(error), reason=error.reason), error.http_status
    if result.created:
        # The command is already committed. A scheduling error must not tell the
        # user that enqueueing failed and encourage another download request.
        try:
            job_manager.schedule_next()
        except Exception as error:
            log.error(f"创建任务后调度失败（任务记录已保存）job_id={result.job_id} error={error}")
    log.info(f"API创建任务 job_id={result.job_id} album_id={album_id} created={result.created}")
    return jsonify({"status": "ok", "job_id": result.job_id, "created": result.created,
                    "reason": "created" if result.created else "already_active"}), 201 if result.created else 200


@api_jobs_bp.get("/api/jobs")
def list_jobs():
    """Bounded task history; enrich only this page with local archive facts."""
    log.info("API获取任务列表")
    numbers = {}
    for key, default in (("page", 1), ("page_size", 50)):
        raw = request.args.get(key, str(default))
        if not re.fullmatch(r"[0-9]{1,10}", raw) or int(raw) < 1:
            return jsonify(status="error", message=f"{key} 必须是正整数"), 400
        numbers[key] = int(raw)
    status = request.args.get("status") or "all"
    view = request.args.get('view', 'jobs')
    album_id = request.args.get('album_id')
    if (view not in ('jobs', 'folios', 'album') or (view == 'album' and not validate_numeric(album_id or ''))
            or (view != 'jobs' and status != 'all')):
        return jsonify(status='error', message='任务视图或车号无效'), 400
    try:
        result = (db.get_jobs_page(status=status, **numbers) if view == 'jobs' else
                  download_folios.transfer_page(**numbers, album_id=album_id if view == 'album' else None))
    except ValueError as error:
        return jsonify(status="error", message=str(error)), 400
    jobs = result["jobs"]
    maintenance = album_maintenance.statuses(job['album_id'] for job in jobs)
    for job in jobs:
        job['maintenance'] = maintenance.get(job['album_id'])
        if isinstance(job.get("selected_photo_ids"), str):
            try:
                job["selected_photo_ids"] = json.loads(job["selected_photo_ids"])
            except (json.JSONDecodeError, TypeError):
                job["selected_photo_ids"] = []
    if 'view' in request.args:
        result['view'] = view
        result['books'] = download_folios.book_metadata(jobs)
        download_folios.resolve_job_titles(jobs, result['books'])
    else:
        download_folios.resolve_job_titles(jobs)
    # 批量检查 CBZ 存在性（避免 N+1 文件系统扫描，利用 TTL 缓存）
    output_paths = list({j.get("output_path", "") for j in jobs if j.get("output_path")})
    cbz_map = {p: _check_cbz_path(p) for p in output_paths}
    for job in jobs:
        op = job.get("output_path", "")
        fmt = cbz_map.get(op) if op else None
        job["has_cbz"] = bool(fmt)
        job["archive_format"] = fmt if fmt in ("cbz", "zip") else ("cbz" if fmt else None)
    return jsonify({"status": "ok", **result, 'maintenance_failures': album_maintenance.failed_without_history()})


@api_jobs_bp.get("/api/jobs/<job_id>")
def get_single_job(job_id: str):
    """获取单个下载任务详情"""
    log.info(f"API获取任务详情 job_id={job_id}")
    err = _check_job_id(job_id)
    if err:
        return err
    job = get_job(job_id)
    if not job:
        return jsonify({"status": "error", "message": "任务不存在"}), 404
    download_folios.resolve_job_titles([job])
    if isinstance(job.get("selected_photo_ids"), str):
        try:
            job["selected_photo_ids"] = json.loads(job["selected_photo_ids"])
        except (json.JSONDecodeError, TypeError):
            job["selected_photo_ids"] = []
    fmt = _check_cbz_path(job.get("output_path") or "")
    job["has_cbz"] = bool(fmt)
    job["archive_format"] = fmt if fmt in ("cbz", "zip") else ("cbz" if fmt else None)
    job['maintenance'] = album_maintenance.statuses([job['album_id']]).get(job['album_id'])
    return jsonify({"status": "ok", "job": job})


@api_jobs_bp.get('/api/albums/<album_id>/maintenance')
def maintenance_status(album_id):
    if not validate_numeric(album_id):
        return jsonify(status='error', message='album_id 必须是纯数字'), 400
    return jsonify(status='ok', maintenance=album_maintenance.statuses([album_id]).get(album_id))


@api_jobs_bp.post('/api/albums/<album_id>/maintenance/retry')
def retry_maintenance(album_id):
    if not validate_numeric(album_id):
        return jsonify(status='error', message='album_id 必须是纯数字'), 400
    try:
        job_manager.retry_maintenance(album_id)
    except TaskCommandError as error:
        return jsonify(status='error', message=str(error), reason=error.reason), error.http_status
    return jsonify(status='ok', message='已重新排队整理整册'), 202


@api_jobs_bp.get("/api/jobs/<job_id>/events")
def job_events(job_id: str):
    """SSE 事件流 - 推送任务进度更新"""
    log.info(f"API SSE连接 job_id={job_id}")
    err = _check_job_id(job_id)
    if err:
        return err
    tracker = progress_manager.get_tracker(job_id)
    if not tracker:
        job = get_job(job_id)
        if job and job["status"] in ("completed", "failed", "canceled"):
            def _done():
                yield f"event: {job['status']}\ndata: {json.dumps({'job_id': job_id, 'status': job['status']}, ensure_ascii=False)}\n\n"
            return Response(_done(), mimetype="text/event-stream")
        return jsonify({"status": "error", "message": "任务不存在或已完成"}), 404

    try:
        lease = progress_manager.acquire_stream(tracker)
    except StreamCapacityError:
        response = jsonify(status="error", reason="sse_limit", message="进度连接过多，请关闭多余页面后重试")
        response.status_code = 429
        response.headers["Retry-After"] = "5"
        return response

    def generate():
        # 每个 SSE 客户端订阅独立队列，互不干扰。
        # 注意：Flask 的 request 没有 is_disconnected()（旧代码调用它导致
        # 每条 SSE 流在第一个事件处 AttributeError 崩溃、前端反复重连）。
        # 客户端断开由 yield 抛出的 GeneratorExit 感知，无需主动探测。
        try:
            yield ": connected\n\n"
            yield from tracker.iter_events(lease.queue, max_duration=300)
        except Exception:
            # 在这里记录：仍处于 stream_with_context 的请求上下文中，记录带本请求的 request_id，
            # “只看此请求”能看到流为什么中断。异常一旦离开生成器，请求上下文先被弹出，waitress 随后写的
            # "Exception while serving" 就不带 request_id 了。记录后正常结束本条流（前端 EventSource 会按
            # onerror 的规则重连），不再抛给 waitress，同一个异常不会记两遍。
            log.exception(f"SSE 推送异常，已结束本条事件流 job_id={job_id}")
        finally:
            lease.close()

    try:
        response = Response(stream_with_context(generate()), mimetype="text/event-stream")
        response.headers["Cache-Control"] = "no-store"
        response.call_on_close(lease.close)
        return response
    except BaseException:
        lease.close()
        raise


@api_jobs_bp.post("/api/jobs/<job_id>/cancel")
def cancel_job(job_id: str):
    """取消任务(queued/running/paused 状态的任务可取消)"""
    err = _check_job_id(job_id)
    if err:
        return err
    ok, msg = job_manager.cancel_job(job_id)
    if not ok:
        log.warning(f"API取消任务失败 job_id={job_id} reason={msg}")
        return jsonify({"status": "error", "message": msg}), 400
    log.info(f"API取消任务 job_id={job_id}")
    return jsonify({"status": "ok", "message": msg})


@api_jobs_bp.post("/api/jobs/<job_id>/pause")
def pause_job(job_id: str):
    """暂停任务(仅 running 状态的任务可暂停)"""
    err = _check_job_id(job_id)
    if err:
        return err
    ok, msg = job_manager.pause_job(job_id)
    if not ok:
        log.warning(f"API暂停任务失败 job_id={job_id} reason={msg}")
        return jsonify({"status": "error", "message": msg}), 400
    log.info(f"API暂停任务 job_id={job_id}")
    return jsonify({"status": "ok", "message": msg})


@api_jobs_bp.post("/api/jobs/<job_id>/resume")
def resume_job(job_id: str):
    """恢复暂停的任务(仅 paused 状态的任务可恢复)"""
    err = _check_job_id(job_id)
    if err:
        return err
    ok, msg = job_manager.resume_job(job_id)
    if not ok:
        log.warning(f"API恢复任务失败 job_id={job_id} reason={msg}")
        return jsonify({"status": "error", "message": msg}), 400
    log.info(f"API恢复任务 job_id={job_id}")
    return jsonify({"status": "ok", "message": msg})


@api_jobs_bp.post("/api/jobs/<job_id>/retry")
def retry_job(job_id: str):
    """重试 / 重新下载任务(失败、已取消、已完成的任务都可以,会创建新任务;原记录保留)"""
    err = _check_job_id(job_id)
    if err:
        return err
    try:
        result = job_manager.retry_job_result(job_id)
    except TaskCommandError as error:
        log.warning(f"API重试任务失败 job_id={job_id} reason={error.reason}")
        return jsonify(status="error", message=str(error), reason=error.reason), error.http_status
    log.info(f"API重试任务 old_job_id={job_id} new_job_id={result.job_id} created={result.created}")
    return jsonify({"status": "ok", "job_id": result.job_id, "created": result.created,
                    "reason": "created" if result.created else "already_active"}), 201 if result.created else 200


@api_jobs_bp.delete("/api/jobs/<job_id>")
def remove_job(job_id: str):
    """删除任务记录(running 任务需先取消)"""
    err = _check_job_id(job_id)
    if err:
        return err
    try:
        job_manager.remove_job(job_id)
    except TaskCommandError as error:
        return jsonify({"status": "error", "message": str(error)}), error.http_status
    log.info(f"API删除任务 job_id={job_id}")
    return jsonify({"status": "ok", "message": "已删除"})


@api_jobs_bp.post("/api/jobs/<job_id>/open-folder")
def open_folder(job_id: str):
    """打开任务的下载目录。已结束任务的目录不在了 → 404（不重建空目录）；未结束任务的目录先建出来再打开。"""
    err = _check_job_id(job_id)
    if err:
        return err
    job = get_job(job_id)
    if not job:
        return jsonify({"status": "error", "message": "任务不存在"}), 404

    output_path = job.get("output_path")
    if not output_path:
        return jsonify({"status": "error", "message": "该任务尚无输出路径"}), 400

    from core.publication import WORK
    if any(part.casefold() == WORK for part in Path(output_path).parts):
        if job.get("status") in _ACTIVE_JOB_STATUSES:
            return jsonify({"status": "error", "message": "任务正在隔离工作区下载中，完成后即可打开书架目录"}), 400
        return jsonify({"status": "error", "message": "该任务未生成公开书架目录，可以重新下载"}), 404

    if not is_safe_path(output_path):
        return jsonify({"status": "error", "message": "路径安全校验失败"}), 403

    if not desktop_platform.supports_open_folder():
        return jsonify({"status": "error", "message": "当前系统不支持直接打开文件夹，请手动前往下载目录"}), 501

    if not os.path.isdir(output_path):
        # 还没结束的任务（排队中 / 下载中 / 已暂停，含“重试”沿用旧路径的新任务）：目录马上就会写入，先建出来再打开。
        # 已结束的任务（已完成 / 失败 / 已取消）不再写入这个目录：文件被删了就如实报告，
        # 不能重建一个空目录——原来重建出的空目录会让各页面把这部漫画显示成“可离线阅读”，点“阅读”却什么都没有。
        if job.get("status") not in _ACTIVE_JOB_STATUSES:
            log.warning(f"API打开目录失败 目录已不存在 job_id={job_id} status={job.get('status')} path={output_path}")
            return jsonify({
                "status": "error",
                "message": "下载文件夹已不存在（可能已被删除或移动），可以重新下载",
            }), 404
        try:
            os.makedirs(output_path, exist_ok=True)
        except OSError as e:
            log.warning(f"API打开目录失败 无法创建目录 job_id={job_id} path={output_path} error={e}")
            return jsonify({"status": "error", "message": "打开文件夹失败"}), 500

    try:
        desktop_platform.open_folder(output_path)
        log.info(f"API打开目录 job_id={job_id} path={output_path}")
        return jsonify({"status": "ok", "message": "已打开文件夹"})
    except desktop_platform.UnsupportedDesktopOperation:
        return jsonify({"status": "error", "message": "当前系统不支持直接打开文件夹，请手动前往下载目录"}), 501
    except Exception as e:
        log.warning(f"API打开目录失败 job_id={job_id} path={output_path} error={e}")
        return jsonify({"status": "error", "message": "打开文件夹失败"}), 500




# ─── 批量清理 ───


@api_jobs_bp.post("/api/jobs/clear/completed")
def clear_completed():
    """清空所有 completed 状态的任务记录(只删记录,不删文件)"""
    deleted = job_manager.clear_jobs("completed")
    log.info(f"API清空已完成任务 deleted={deleted}")
    return jsonify({"status": "ok", "deleted": deleted})


@api_jobs_bp.post("/api/jobs/clear/failed")
def clear_failed():
    """清空所有 failed 状态的任务记录"""
    deleted = job_manager.clear_jobs("failed")
    log.info(f"API清空失败任务 deleted={deleted}")
    return jsonify({"status": "ok", "deleted": deleted})


@api_jobs_bp.post("/api/jobs/clear/canceled")
def clear_canceled():
    """清空所有 canceled 状态的任务记录"""
    deleted = job_manager.clear_jobs("canceled")
    log.info(f"API清空已取消任务 deleted={deleted}")
    return jsonify({"status": "ok", "deleted": deleted})


@api_jobs_bp.post("/api/jobs/clear/finished")
def clear_finished():
    """清空所有已完成或已结束的记录(completed/failed/canceled), 保留 queued/running"""
    deleted = job_manager.clear_jobs(["completed", "failed", "canceled"])
    log.info(f"API清空已结束任务 deleted={deleted}")
    return jsonify({"status": "ok", "deleted": deleted})
