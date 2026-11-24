"""
jmcomic 服务封装模块
使用 build_jmcomic_option() 确保设置真实生效。
"""
import os
import threading
import time
from collections import OrderedDict
from copy import copy
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout, as_completed, wait, FIRST_COMPLETED
from datetime import datetime
from pathlib import Path
from typing import Any

from jmcomic import (
    JmModuleConfig,
    JmMagicConstants,
    JmcomicText,
)

from . import database as db
from .download_checkpoint import DownloadCheckpoint
from .publication import Workspace, private_path
from .logger import log, bind_request_id, sanitize
from .progress import progress_manager
from .settings import get_settings, build_jmcomic_option
from .validation import safe_dirname as _safe_dirname
from .path_guard import DOWNLOAD_ROOT, is_safe_path
from . import download_capacity
from .download_finalize import finalize_files, organize_download
from .download_layout import (CHAPTER_FORMAT as _CHAPTER_FORMAT, chapter_format as _chapter_format,
                              chapter_output_dir as _chapter_output_dir, mark_chapter_current as _mark_chapter_current)
from .file_tree import is_link
from . import update_store
from . import sdk_adapter
from . import background_calls
from .background_calls import BackgroundCallsBusy, BackgroundCallsStopped
from . import metadata_budget
from .upstream import UpstreamFailure, classify_failure
from .storage import staged_file
from . import image_download
from .transfer import download_budget, DownloadInterrupted
from .local_availability import forget as forget_local_state, has_local_pages, is_readable, readable_folder
from .task_commands import finish_download


# ── 全局复用客户端（搜索/详情用，避免每次新建 client + 连接池）──
_global_option = None
_global_client = None
_option_lock = threading.RLock()
_factory_drained = threading.Condition(_option_lock)
_shared_builders = 0
_independent_builders = 0
# 独立 client 活跃计数（用于 session 泄漏监控）
_active_independent_clients = 0
_clients_lock = threading.Lock()

# 直接在 client 对象上打标记，替代旧的 id() 集合方案：
#   - _JM_SHARED_ATTR: 是否为全局复用客户端（close_client 跳过）
#   - _JM_CLOSED_ATTR: 是否已被关闭（防止重复关闭导致计数变负）
# 标记随对象存活，invalidate_option_cache() 并发清缓存也不会误判。
_JM_SHARED_ATTR = "_jm_downloader_shared"
_JM_CLOSED_ATTR = "_jm_downloader_closed"
# invalidate_option_cache() 每调用一次加 1：长期持有 client 的一方（在线阅读的取图连接池）据此丢弃按旧设置建的 client
_option_generation = 0
_retired_clients = {}  # Strong ownership until a retired transport really closes.


def _get_or_create_option(generation):
    """Capture settings and run SDK initialization outside the generation lock."""
    global _global_option
    candidate = sdk_adapter.build_option(build_jmcomic_option())
    with _option_lock:
        if generation != _option_generation:
            return None
        if _global_option is None:
            _global_option = candidate
        return _global_option


def invalidate_option_cache(*, _owned_cleanup=False):
    """Retire the old shared client; close it only after its last request releases it."""
    global _global_option, _global_client, _option_generation
    with _option_lock:
        previous = _global_client
        _option_generation += 1
        _global_option = None
        _global_client = None
        sdk_adapter.publish_diagnostic_domain()
        if previous is not None:
            previous._jm_retired = True
            _retired_clients[id(previous)] = previous
        idle = [client for client in _retired_clients.values() if not getattr(client, "_jm_users", 0)]
    safe = True
    for retired in idle:
        if not _dispose_client(retired):
            safe = False
    if not safe and not _owned_cleanup:
        background_calls.request_cleanup()
    return safe


def _acquire_client(kind, timeout=None, stop=None):
    """Construct optimistically, publish only into the generation we sampled."""
    global _global_client, _active_independent_clients
    shared = kind == "shared"
    deadline = metadata_budget.deadline_after(20 if shared else 120)
    while True:
        with _option_lock:
            if shared and stop.is_set():
                raise BackgroundCallsStopped("后台调用正在停止，请稍后重试")
            metadata_budget.remaining(deadline)
            if shared and _global_client is not None:
                _global_client._jm_users += 1
                return _global_client, _global_option
            option, generation = _global_option, _option_generation
        if option is None:
            option = _get_or_create_option(generation)
            if option is None:
                continue
        deadline = min(deadline, sdk_adapter.discovery_deadline(option, timeout))
        metadata_budget.remaining(deadline, stop)
        overrides = {}
        if kind == "image":
            overrides["image_only"] = True
            if option.client.postman.src_dict.get("type") == "curl_cffi_session":
                overrides["use_thread_local_curl"] = False
        elif kind == "check":
            overrides.update(cache=False, timeout=timeout)
        try:
            candidate = sdk_adapter.new_client(option, deadline=deadline, **overrides)
        except Exception:
            if generation != option_generation():
                continue
            raise
        try:
            if shared:
                metadata_budget.install(candidate)
            elif kind == "check":
                candidate.set_cache_dict(None)
                candidate.retry_times = 0
            with _option_lock:
                if shared and stop.is_set():
                    raise BackgroundCallsStopped("后台调用正在停止，请稍后重试")
                metadata_budget.remaining(deadline)
                if generation != _option_generation or (shared and _global_client is not None):
                    continue
                sdk_adapter.publish_diagnostic_domain(option)
                if shared:
                    setattr(candidate, _JM_SHARED_ATTR, True)
                    candidate._jm_users = 1
                    candidate._jm_retired = False
                    _global_client = candidate
                elif kind != "image":
                    candidate._jm_counted = True
                    with _clients_lock:
                        _active_independent_clients += 1
                acquired, candidate = candidate, None
                return acquired, option
        finally:
            if candidate is not None and not _dispose_client(candidate):
                with _option_lock:
                    _retired_clients[id(candidate)] = candidate
                background_calls.request_cleanup()


def get_client(shared=True):
    """Acquire a client. Every acquisition must be balanced by close_client()."""
    global _shared_builders
    if not shared:
        return _acquire_independent("independent")
    with _factory_drained:
        background_calls.require_accepting()
        if _global_client is not None:
            _global_client._jm_users += 1
            return _global_client, _global_option
        stop = background_calls.stop_event()
        _shared_builders += 1
    try:
        return _acquire_client("shared", stop=stop)
    finally:
        with _factory_drained:
            _shared_builders -= 1
            _factory_drained.notify_all()


def option_generation() -> int:
    """当前 jmcomic 设置的版本号；设置保存（invalidate_option_cache）后变化。"""
    return _option_generation


def _acquire_independent(kind, timeout=None):
    global _independent_builders
    with _option_lock:
        _independent_builders += 1
    try:
        return _acquire_client(kind, timeout)
    finally:
        with _option_lock:
            _independent_builders -= 1


def new_image_client():
    """在线阅读取图用的 client：由 online_reader 的连接池长期持有、逐个请求独占复用，用完 close_client() 关闭。

    与 get_client() 用同一份 option（代理、impersonate、请求头规则都一样），只多一项：curl 句柄不绑定线程。
    curl_cffi 的 Session 默认每个线程一个 curl 句柄，换一个线程用同一个 client 仍是新连接、新 TLS 握手；
    池里的 client 会被不同的请求线程轮流使用，所以改用 client 自己的句柄，连接随 client 复用。
    不计入 get_active_client_count()（池的大小有上限，不是泄漏）。"""
    return _acquire_independent("image")[0]


def new_check_client(timeout: int):
    """检查新章节（core/update_checker.py）专用的 client：每次检查新建一个，用完 close_client() 关闭。

    与下载同一份 option（代理、impersonate、client 类型都一样），另外三点：
      - 不带缓存（cache=False 且 set_cache_dict(None)）：每次都向上游要最新的章节列表；
      - retry_times = 0：第一次出错就抛出，换哪个域名由检查自己决定（每个域名最多问一次）；
      - timeout 覆盖 curl 的总超时（调用方已限制在 20 秒以内）。
    计入 get_active_client_count()。"""
    return _acquire_independent("check", timeout)[0]


def get_active_client_count() -> int:
    with _clients_lock:
        return _active_independent_clients


def get_in_flight_client_count() -> int:
    """Count work that would be interrupted by a source-launcher restart."""
    with _option_lock:
        shared = getattr(_global_client, "_jm_users", 0)
        retired = sum(getattr(client, "_jm_users", 0) for client in _retired_clients.values())
        return _shared_builders + _independent_builders + shared + retired + get_active_client_count()


def _dispose_client(client):
    """Close once without holding a service lock across native session cleanup."""
    global _active_independent_clients
    with _option_lock:
        lock = getattr(client, "_jm_close_lock", None)
        if lock is None:
            lock = client._jm_close_lock = threading.Lock()
    with lock:
        if getattr(client, _JM_CLOSED_ATTR, False):
            return True
        try:
            sdk_adapter.close_transport(client)
        except Exception as exc:
            log.warning(f"关闭 client 异常: {type(exc).__name__}")
            return False
        with _option_lock:
            setattr(client, _JM_CLOSED_ATTR, True)
            _retired_clients.pop(id(client), None)
            if getattr(client, "_jm_counted", False):
                with _clients_lock:
                    _active_independent_clients -= 1
        return True


def close_client(client) -> bool:
    if client is None:
        return True
    with _option_lock:
        if getattr(client, _JM_CLOSED_ATTR, False):
            return True
        shared = getattr(client, _JM_SHARED_ATTR, False)
        if shared:
            client._jm_users = max(0, getattr(client, "_jm_users", 0) - 1)
            if not getattr(client, "_jm_retired", False) or client._jm_users:
                return True
            _retired_clients[id(client)] = client
    safe = _dispose_client(client)
    if shared and not safe:
        background_calls.request_cleanup()
    return safe


def _shutdown_client_cache():
    safe = invalidate_option_cache(_owned_cleanup=True)
    if background_calls.stop_event().is_set():
        with _factory_drained:
            _factory_drained.wait_for(lambda: not _shared_builders)
    return safe


background_calls.set_shutdown_cleanup(_shutdown_client_cache)



_FIRST_PASS_TIMEOUT = 120  # Legacy caps; each operation also obeys the saved network timeout.
_RETRY_TIMEOUT = 600
# 并行下载共享状态锁（保护 done_pages / failed_pages / pending_images）
# 注意：_download_lock 已弃用，改用每个任务独立锁 job_lock 以消除跨任务争用。
# 保留模块级锁仅用于模块级共享资源（如 client 创建），目前无需模块级锁。
_download_lock = threading.Lock()


def _operation_timeout(cap=120) -> float:
    """Apply saved settings to real transport calls, not only Future.result()."""
    return max(0.001, min(float(get_settings().get("timeout", 30)), float(cap), 120.0))


def _download_canceled(job_id):
    job = db.get_job(job_id)
    return job is None or job["status"] == "canceled"


def _should_stop(job_id: str, tracker, stop_event=None) -> bool:
    """Only a running row admits more work. A quick resume is queued until this writer exits."""
    if stop_event is not None and stop_event.is_set():
        return True
    job = db.get_job(job_id)
    if job is None:
        log.warning(f"下载中的任务已被删除 job_id={job_id}")
        if tracker:
            tracker.push("progress", {
                "job_id": job_id, "status": "canceled",
                "message": "任务已被删除",
            })
        return True
    if job["status"] == "canceled":
        log.info(f"下载任务已取消 job_id={job_id}")
        if tracker:
            tracker.push("progress", {
                "job_id": job_id, "status": "canceled",
                "message": "任务已被取消",
            })
        return True
    return job["status"] != "running" or (stop_event is not None and stop_event.is_set())


_IMAGE_FORMAT_EXTENSIONS = {"webp": ".webp", "png": ".png", "jpg": ".jpg"}


def _page_extension(settings: dict | None = None, *, gif: bool = False) -> str:
    """Resolve output page extension from settings; animated GIFs stay animated WebP."""
    if gif:
        return ".webp"
    fmt = str((settings or {}).get("image_format", "webp")).strip().lower()
    return _IMAGE_FORMAT_EXTENSIONS.get(fmt, ".webp")


def _download_image_single_attempt(
    client, job_id, album_id, album, output_path,
    photo, idx, img, img_url, img_path, scramble_id,
    done_pages, total_pages, failed_pages, pending_images,
    tracker, photo_dir, pass_num, timeout, lock, refetch_gif=False, stop_event=None, node_attempts=None,
    checkpoint=None, settings=None,
):
    """下载单页的一趟：在同一超时预算内最多尝试 node_attempts 个节点。
    成功 → done_pages +1
    首趟失败且有待重试列表 → 记录到 pending_images；否则即为最终失败，记录到 failed_pages。
    使用传入的每任务锁 lock，避免跨任务争用模块级 _download_lock。
    GIF 页（image_is_gif）不切片，保存为保留全部帧的动画 WebP（文件名仍是 NNNNN.webp）。
    """
    if stop_event is not None and stop_event.is_set():
        return
    gif = image_is_gif(img)
    settings = get_settings() if settings is None else settings
    if img_path.parent != photo_dir or not is_safe_path(img_path) or not is_safe_path(photo_dir):
        raise ValueError("图片输出路径越权")

    skip = settings.get("skip_existing", "true") == "true"

    def check_publish():
        if stop_event is not None and stop_event.is_set():
            raise DownloadInterrupted("stopped")
        if _download_canceled(job_id):
            raise DownloadInterrupted("canceled")
        if stop_event is not None and stop_event.is_set():
            raise DownloadInterrupted("stopped")

    try:
        # Existing files need the same error boundary as downloaded files. An
        # invalid/bomb image must be replaced or recorded, never abort a chapter.
        reusable = pass_num == 1 and ((not getattr(checkpoint, 'isolated', False) and skip and not (gif and refetch_gif))
                                      or (checkpoint is not None and checkpoint.contains(img_path)))
        verified_digest = None
        if reusable and _valid_image(img_path):
            if getattr(checkpoint, 'isolated', False):
                verified_digest = checkpoint.receipt_digest(img_path)
        else:
            with staged_file(img_path) as staged:
                if node_attempts is None:
                    node_attempts = 1 + int(settings.get("retry_times", 2))
                options = dict(timeout=_operation_timeout(timeout), attempts=node_attempts,
                               stop_event=stop_event, should_cancel=lambda: _download_canceled(job_id))
                if gif:
                    with staged_file(img_path, suffix=".gif") as raw:
                        image_download.download_image(client,
                            img_url=img_url, img_save_path=str(raw.path),
                            scramble_id=None, decode_image=False, **options,
                        )
                        if stop_event is not None and stop_event.is_set():
                            return
                        _save_gif_as_webp(raw.path, staged.path)
                else:
                    image_download.download_image(client,
                        img_url=img_url, img_save_path=str(staged.path),
                        scramble_id=scramble_id, decode_image=True, **options,
                    )
                def verify_page(path):
                    nonlocal verified_digest
                    if not _valid_image(path):
                        return False
                    if getattr(checkpoint, 'isolated', False):
                        verified_digest = checkpoint.digest(path)
                        checkpoint.remember_digest(img_path, verified_digest)
                    return True
                staged.publish(verify_page, before_replace=check_publish)
        # 确认下载成功后计数
        with lock:
            if checkpoint is not None:
                checkpoint.confirm(img_path, verified_digest)
            done_pages[0] += 1
    except DownloadInterrupted:
        return
    except Exception as e:
        if (stop_event is not None and stop_event.is_set()) or (checkpoint and _should_stop(job_id, None, stop_event)):
            return  # Runtime owns recovery; a stop is neither a failed page nor a retry.
        if pass_num == 1 and pending_images is not None and not image_download.non_retryable_error(e):
            # 首趟：只存位置信息，不存 client（线程退出后 client 会关闭）
            with lock:
                pending_images.append({
                    "photo": photo, "idx": idx,
                    "img": img, "photo_dir": photo_dir,
                })
            log.warning(
                f"首趟下载失败，已跳过待重试 job_id={job_id} "
                f"pass={pass_num} img={idx + 1} error={e}"
            )
        else:
            with lock:
                err_msg = f"第 {idx+1} 页下载失败: {image_download.failure_detail(e)}"
                log.warning(f"图片下载最终失败 job_id={job_id} error={err_msg}")
                failed_pages.append(err_msg)
                db.update_job(job_id, error_message=err_msg[:500])
    with lock:
        reported = max(done_pages[0], min(checkpoint.previous_done, total_pages)) if checkpoint else done_pages[0]
        progress_pct = round(reported / total_pages * 100, 1) if total_pages > 0 else 0
        db.update_job(job_id, done_pages=reported)
        if tracker and not _should_stop(job_id, None, stop_event):
            tracker.push("progress", {
                "job_id": job_id, "status": "running",
                "done_pages": reported, "total_pages": total_pages,
                "progress": progress_pct,
                "message": f"正在下载 {idx + 1}/{len(photo)} 页",
            })


def _save_gif_as_webp(gif_path, webp_path) -> None:
    """把原样取回的 GIF 页写成 WebP：多帧的写成动画 WebP（逐帧时长与循环次数照搬），单帧的写成普通 WebP。

    jmcomic 按扩展名转换时只调用 Image.save(path)（没有 save_all），GIF 只会剩下第一帧；本地阅读器、
    CBZ/ZIP 打包都按 .webp 处理这些页面，动画 WebP 在浏览器里照常播放，PDF 导出只取首帧。"""
    from PIL import Image, ImageSequence
    with Image.open(gif_path) as image:
        if getattr(image, "n_frames", 1) <= 1:
            image.save(webp_path, "WEBP")
            return
        # GIF 没有循环扩展时只播放一次（WebP loop=1）；loop=0 两者都表示无限循环
        loop = image.info.get("loop", 1)
        # 浏览器把 ≤10ms（含 0）的 GIF 帧按 100ms 播放；WebP 不做这个修正，照搬会快得看不清
        durations = [frame.info.get("duration") or 0 for frame in ImageSequence.Iterator(image)]
        durations = [duration if duration > 10 else 100 for duration in durations]
        image.seek(0)
        image.save(webp_path, "WEBP", save_all=True, duration=durations, loop=loop)


def _valid_image(path):
    from PIL import Image
    try:
        with Image.open(path) as image:
            image.verify()
        # verify() need not decode pixels (including WebP/JPEG). A page whose
        # header survived a truncated write is not a valid resume point.
        with Image.open(path) as image:
            for frame in range(getattr(image, 'n_frames', 1)):
                image.seek(frame)
                image.load()
        return True
    except (OSError, EOFError, ValueError, SyntaxError, Image.DecompressionBombError):
        return False


def _submit_client_call(pool, client, fn, *args, _call_deadline=None, _call_stop=None, **kwargs):
    """Keep a shared transport leased until the future actually finishes/cancels.
    The worker thread inherits the caller's request_id, so jmcomic's retry/failure records join its trace."""
    shared = client is not None and getattr(client, _JM_SHARED_ATTR, False)
    def acquire():
        with _option_lock:
            client._jm_users += 1
    deadline = (_call_deadline if _call_deadline is not None else
                metadata_budget.deadline_after(20))
    stopped = background_calls.stop_event()
    def invoke():
        with metadata_budget.call_scope(deadline, stopped, _call_stop):
            return fn(*args, **kwargs)
    return background_calls.submit(pool, bind_request_id(invoke),
        acquire=acquire if shared else None,
        release=(lambda: close_client(client)) if shared else None)


def _call_with_timeout(label: str, timeout: float, timeout_msg: str, fn, *args,
                       _call_deadline=None, _call_stop=None, _call_client=None, **kwargs):
    """在独立线程中执行 jmcomic 远程调用，带超时保护。

    统一取代散落各处的 `ThreadPoolExecutor(1) + fut.result(timeout=...)` 样板：
    只有真正的等待/传输超时归为 timeout，其余失败保留结构化种类。
    请求返回后，仍在执行的调用继续持有客户端租约，直到实际结束。
    """
    deadline = metadata_budget.deadline_after(min(timeout, 120))
    if _call_deadline is not None:
        deadline = min(deadline, _call_deadline)
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        owner = _call_client if _call_client is not None else getattr(fn, "__self__", None)
        fut = _submit_client_call(pool, owner, fn, *args,
                                  _call_deadline=deadline, _call_stop=_call_stop, **kwargs)
        result = fut.result(timeout=metadata_budget.remaining(deadline, _call_stop))
    except Exception as e:
        pool.shutdown(wait=False)  # 不阻塞请求线程；挂起的工作线程由其自身超时终结
        failure = classify_failure(e, timeout_message=timeout_msg)
        log.warning(f"{label} 失败 kind={failure.kind} error={type(e).__name__}: {e}")
        if failure is e:
            raise
        raise failure from e
    pool.shutdown(wait=True)
    return result


MAX_SEARCH_PAGE = 500


def _search_window(client, keyword, page, page_size, order_by):
    """取 UI 页对应的 [start, end)；上游未给总数时多看一条以确认是否有下一页。"""
    native_size = JmModuleConfig.PAGE_SIZE_SEARCH
    start = (page - 1) * page_size
    end = start + page_size
    native_start = start // native_size * native_size
    can_advance = page < MAX_SEARCH_PAGE
    read_until = end + (1 if can_advance else 0)
    rows = []
    total = None
    while native_start < read_until:
        metadata_budget.check_current()
        result = client.search(
            search_query=keyword, page=native_start // native_size + 1, main_tag=0,
            order_by=order_by, time=JmMagicConstants.TIME_ALL,
            category=JmMagicConstants.CATEGORY_ALL, sub_category=None,
        )
        content = result.content
        if getattr(result, "is_single_album", False) is True:
            return (content[:1] if page == 1 else []), 1, False
        reported_total = getattr(result, "total", None)
        if reported_total == 0 and native_start == 0 and not content:
            return [], 0, False
        if reported_total is not None and reported_total > 0:
            total = reported_total
            read_until = min(end, total)
        rows.extend(content[max(0, start - native_start):max(0, read_until - native_start)])
        if total is None and len(content) < native_size:
            break
        native_start += native_size

    has_more = end < total if total is not None else len(rows) > page_size
    return rows[:page_size], total, can_advance and has_more


def search_albums(keyword: str, page: int = 1, page_size: int = 20, sort: str = "latest"):
    """搜索一个 UI 页，所有原生页共用一次搜索的超时与客户端租约。"""
    log.info(f"搜索 keyword={keyword} page={page} page_size={page_size} sort={sort}")
    client, _ = get_client()

    order_map = {
        "latest": JmMagicConstants.ORDER_BY_LATEST,
        "views": JmMagicConstants.ORDER_BY_VIEW,
        "likes": JmMagicConstants.ORDER_BY_LIKE,
        "pictures": JmMagicConstants.ORDER_BY_PICTURE,
    }

    try:
        rows, total, has_more = _call_with_timeout(
            f"搜索 keyword={keyword}", _operation_timeout(25), "搜索超时，请稍后重试",
            _search_window, client, keyword, page, page_size,
            order_map.get(sort, JmMagicConstants.ORDER_BY_LATEST), _call_client=client,
        )
    finally:
        close_client(client)

    items = []
    for album_id, info in rows:
        items.append({
            "album_id": str(album_id),
            "title": _safe_str(info.get("name", "")),
            "author": _safe_str(info.get("author", "")),
            "tags": info.get("tags", []),
            "cover_url": JmcomicText.get_album_cover_url(album_id),
        })

    return {
        "items": items,
        "total": total,
        "has_more": has_more,
        "page": page,
        "page_size": page_size,
    }


def check_album_photos(client, album, timeout: float = 15, failed: list | None = None,
                       *, deadline=None, stop_event=None, failures: list | None = None) -> list:
    """并行补全各章节的图片列表（page_arr），避免逐章串行 HTTP 请求。

    SDK只更新工作副本；超时返回的对象不会被晚到的线程修改。
    传入 failed 列表时，把这些没取到的章节追加进去（调用方据此区分“网络失败”和“确实没有页面”）。
    详情页与在线阅读共用。
    """
    photo_list = list(album)
    # 预先设置 from_album，避免 check_photo 内部重复调用 get_album_detail
    for photo in photo_list:
        if getattr(photo, 'from_album', None) is None:
            photo.from_album = album
    # 已有 page_arr 的章节直接跳过 check
    pending = [(index, photo) for index, photo in enumerate(photo_list)
               if not getattr(photo, 'page_arr', None)]
    if not pending:
        return photo_list
    # 独立线程池 + 手动 shutdown(wait=False)：
    # 不能用 with 语法 —— __exit__ 会 shutdown(wait=True)，
    # 阻塞等待挂起的 check_photo，让超时保护形同虚设。
    deadline = min(deadline if deadline is not None else float("inf"),
                   metadata_budget._clock() + min(timeout, _operation_timeout(timeout)))
    stopped = background_calls.stop_event()
    check_pool = ThreadPoolExecutor(max_workers=min(len(pending), 4))
    fut_map, completed, next_index = {}, set(), 0
    errors = {}
    interrupted = None
    try:
        while next_index < len(pending) or fut_map:
            left = metadata_budget.remaining(deadline, stopped, stop_event)
            while next_index < len(pending) and len(fut_map) < 4:
                index, photo = pending[next_index]
                working = copy(photo)
                if isinstance(getattr(working, "page_arr", None), list):
                    working.page_arr = list(working.page_arr)
                try:
                    future = _submit_client_call(check_pool, client, client.check_photo, working,
                                                  _call_deadline=deadline, _call_stop=stop_event)
                except BackgroundCallsBusy:
                    break
                fut_map[future] = (index, working)
                next_index += 1
            if not fut_map:
                background_calls.wait_for_capacity(min(left, 0.05))
                continue
            ready, _ = wait(fut_map, timeout=min(left, 0.05), return_when=FIRST_COMPLETED)
            metadata_budget.remaining(deadline, stopped, stop_event)
            for fut in ready:
                index, working = fut_map.pop(fut)
                try:
                    fut.result()
                except Exception as e:
                    errors[index] = classify_failure(e).detached()
                    log.warning(f"获取 photo 详情失败 photo_id={working.photo_id} error={type(e).__name__}: {e}")
                else:
                    if getattr(working, "page_arr", None) is not None:
                        photo_list[index] = working
                        completed.add(index)
                    else:
                        errors[index] = UpstreamFailure("invalid_response", "上游未返回章节图片列表，请稍后重试")
                        log.warning(f"获取 photo 详情未返回图片列表 photo_id={working.photo_id}")
    except (FuturesTimeout, BackgroundCallsStopped, metadata_budget.MetadataStopped) as error:
        interrupted = classify_failure(error).detached()
        unfinished = len(pending) - len(completed)
        log.warning(f"check_photo 中断 kind={interrupted.kind}，album_id={getattr(album, 'album_id', '?')}，"
                    f"已跳过 {unfinished} 个未完成章节")
    finally:
        check_pool.shutdown(wait=False, cancel_futures=True)
    if failed is not None:
        # Use the outcome observed before the deadline, not a second Future.done
        # check that could race a late completion and mislabel an empty result.
        failed.extend(photo_list[index] for index, _ in pending if index not in completed)
    if failures is not None:
        failures.extend(errors.get(index, interrupted) for index, _ in pending if index not in completed)
    return photo_list


def image_is_gif(image) -> bool:
    """GIF 页没有加扰：jmcomic 自己的下载器（JmOption.decide_download_image_decode）从不解码 GIF。
    按加扰切片处理会把它切成错位的横条；按扩展名直接转存成 .webp 也只剩第一帧。所以在线阅读原样缓存 GIF，
    下载任务原样取回后由 _save_gif_as_webp 写成动画 WebP。"""
    flag = getattr(image, "is_gif", None)
    if isinstance(flag, bool):
        return flag
    url = str(getattr(image, "download_url", "") or "")
    return url.split("?", 1)[0].lower().endswith(".gif")


def get_album_detail(album_id: str) -> dict:
    """获取漫画详情，带 20 秒超时防止阻塞 Waitress 线程"""
    log.info(f"获取专辑详情 album_id={album_id}")
    deadline = metadata_budget.deadline_after(_operation_timeout(20))
    client, _ = get_client()
    try:
        # 包装远程调用，设 20 秒超时防止永久挂起占用 Waitress 线程
        album = _call_with_timeout(
            f"获取专辑详情 album_id={album_id}", 20, "获取专辑详情超时，请稍后重试",
            client.get_album_detail, album_id, _call_deadline=deadline, _call_client=client,
        )

        photos, failed = [], []
        checked = check_album_photos(client, album, failed=failed, deadline=deadline)
        unknown = {id(photo) for photo in failed}
        for p in checked:
            known = id(p) not in unknown
            page_count = len(p.page_arr or ()) if known else None
            photos.append({
                "photo_id": str(p.photo_id),
                "title": _safe_str(p.name) if hasattr(p, "name") and p.name else "-",
                "page_count": page_count,
                "page_count_known": known,
            })

        return {
            "album_id": str(album.album_id),
            "title": _safe_str(album.name),
            "author": _safe_str(album.author),
            "tags": getattr(album, 'tags', None) or [],
            "actors": getattr(album, 'actors', None) or [],
            "works": getattr(album, 'works', None) or [],
            "views": album.views if hasattr(album, "views") else 0,
            "likes": album.likes if hasattr(album, "likes") else 0,
            "description": _safe_str(album.description)[:500] if hasattr(album, "description") and album.description else "",
            "cover": JmcomicText.get_album_cover_url(album_id),
            "photos": photos,
            "chapter_count": len(album),
            "details_complete": not failed,
        }
    finally:
        close_client(client)


_detail_cache = OrderedDict()
_detail_cache_lock = threading.Lock()
# Bounded locks: same-album cold requests share a fetch, without unbounded lock storage.
_detail_fetch_locks = [threading.Lock() for _ in range(64)]
_detail_cache_generation = 0


def clear_album_detail_cache():
    """Clear both cache tiers; an older in-flight fetch must not repopulate them."""
    global _detail_cache_generation
    with _detail_cache_lock:
        _detail_cache_generation += 1
        _detail_cache.clear()
        db.clear_cached_album_details()


def forget_album_detail(album_id: str) -> None:
    """只丢掉这一部漫画的详情缓存（内存 + 数据库）：检查新章节确认了新章节后调用，
    详情页下次打开时重新取章节列表，新章节才会出现在章节表里。别的漫画的缓存不动；
    正在进行的旧请求也不会把旧的章节列表写回来。"""
    global _detail_cache_generation
    album_id = str(album_id)
    with _detail_cache_lock:
        _detail_cache_generation += 1
        _detail_cache.pop((str(db.DB_PATH), album_id), None)
        db.delete_cached_album_detail(album_id)


def _detail_is_complete(data) -> bool:
    """Do not reuse unknown counts, including zeros cached by older releases."""
    if not isinstance(data, dict) or data.get("details_complete") is False:
        return False
    photos = data.get("photos", [])
    if not isinstance(photos, list):
        return False
    for photo in photos:
        if not isinstance(photo, dict):
            return False
        count = photo.get("page_count")
        if (isinstance(count, bool) or not isinstance(count, int) or count < 0
                or (count == 0 and photo.get("page_count_known") is not True)):
            return False
    return True


def get_album_detail_cached(album_id: str, ttl: int = 3600) -> dict:
    """Bounded two-tier cache with request coalescing and isolated return values."""
    deadline = metadata_budget.deadline_after(_operation_timeout(20))
    stopped = background_calls.stop_event()
    key = (str(db.DB_PATH), album_id)
    lock = _detail_fetch_locks[hash(key) % len(_detail_fetch_locks)]
    while not lock.acquire(timeout=min(0.05, metadata_budget.remaining(deadline, stopped))):
        pass
    try:
        with metadata_budget.call_scope(deadline, stopped):
            return _get_album_detail_cached_locked(album_id, ttl)
    finally:
        lock.release()


def _get_album_detail_cached_locked(album_id: str, ttl: int) -> dict:
    from copy import deepcopy
    import json
    key = (str(db.DB_PATH), album_id)
    ttl = max(0, ttl)
    now = time.time()
    with metadata_budget.lock_scope(_detail_cache_lock):
        generation = _detail_cache_generation
        cached = _detail_cache.get(key)
        if (cached and _detail_is_complete(cached["data"]) and ttl > 0
                and now - cached["ts"] < min(300, ttl)):
            _detail_cache.move_to_end(key)
            return deepcopy(cached["data"])
    stored = db.get_cached_album_detail(album_id, ttl=ttl) if ttl > 0 else None
    if stored and not _detail_is_complete(stored["detail"]):
        with metadata_budget.lock_scope(_detail_cache_lock):
            _detail_cache.pop(key, None)
            db.delete_cached_album_detail(album_id)
        stored = None
    if stored:
        data = stored["detail"]
        if stored["cover_cdn_url"]:
            data["cover"] = stored["cover_cdn_url"]
        timestamp = stored["cached_at"]
    else:
        data = get_album_detail(album_id)
        timestamp = time.time()
    with metadata_budget.lock_scope(_detail_cache_lock):
        # This check is the publication permit. A stop observed while waiting
        # for the lock must discard the result before either cache is changed.
        # Once admitted, the existing DB transaction is allowed to finish.
        metadata_budget.check_current()
        if generation == _detail_cache_generation and _detail_is_complete(data):
            if stored is None:
                db.set_cached_album_detail(album_id, json.dumps(data, ensure_ascii=False), data.get("cover", ""))
            _detail_cache[key] = {"data": deepcopy(data), "ts": timestamp}
            _detail_cache.move_to_end(key)
            while len(_detail_cache) > 500:
                _detail_cache.popitem(last=False)
    return deepcopy(data)


def _same_dir(a, b) -> bool:
    return os.path.normcase(os.path.realpath(str(a))) == os.path.normcase(os.path.realpath(str(b)))


def _reusable_album_dir(album_id: str) -> Path | None:
    """这部漫画阅读器现在打开的目录（最近一次完成、本地可读的下载，core.local_availability 同一规则）：
    在下载目录里面（不是下载目录本身）、不是链接的目录时返回；否则 None（照常按上游现在的名字建目录）"""
    folder = readable_folder(album_id)
    if not folder:
        return None
    path = Path(folder)
    try:
        if _same_dir(path, DOWNLOAD_ROOT) or not is_safe_path(path) or is_link(path) or not path.is_dir():
            return None
    except OSError:
        return None
    return path


def _download_chapter(
    job_id, album_id, album, album_dir, photo, total_pages,
    done_pages, pending_images, failed_pages, tracker, checkpoint, lock, pass_num, timeout,
    stop_event=None, node_attempts=None, settings=None,
):
    """下载单个章节的所有图片（支持章节内多图并行，每图独立 client）"""
    if _should_stop(job_id, tracker, stop_event):
        return
    photo_dir = _chapter_output_dir(album_dir, photo)
    # 旧版本下载的章节：GIF 页可能已被切片/只剩第一帧，首趟不按“已存在”跳过它们
    refetch_gif = _chapter_format(photo_dir) < _CHAPTER_FORMAT

    def fetch(item):
        index, image = item
        try:
            _download_chapter_image(
                None, job_id, album_id, album, photo, photo_dir,
                index, image, total_pages, done_pages, pending_images,
                failed_pages, tracker, checkpoint, lock, pass_num, timeout, refetch_gif,
                stop_event, node_attempts, settings,
            )
        except Exception as error:
            log.warning(f"图片下载线程异常 job_id={job_id} page={index + 1} error={error}")

    download_capacity.run_images(enumerate(photo), bind_request_id(fetch),
                                 workers=max(1, min(len(photo), download_capacity.image_limit())),
                                 should_stop=lambda: _should_stop(job_id, None, stop_event))

    if not _should_stop(job_id, tracker, stop_event):
        db.update_job(job_id, current_photo=str(photo.photo_id))


def _download_chapter_image(
    _, job_id, album_id, album, photo, photo_dir,
    i, img, total_pages, done_pages, pending_images,
    failed_pages, tracker, checkpoint, lock, pass_num, timeout, refetch_gif=False,
    stop_event=None, node_attempts=None, settings=None,
):
    """下载单张图片（每个线程独立 client，避免 HTTP 非线程安全问题）"""
    # _should_stop 内部已持锁（db 查询），无需额外保护
    if _should_stop(job_id, tracker, stop_event):
        return
    img_client, _ = get_client(shared=False)
    try:
        img_url = img.download_url
        gif = image_is_gif(img)
        settings = get_settings() if settings is None else settings
        img_name = f"{i + 1:05d}{_page_extension(settings, gif=gif)}"
        img_path = photo_dir / img_name
        # GIF 不加扰：不传 scramble_id（不切片）；_download_image_single_attempt 原样取回后存成动画 WebP
        scramble_id = int(img.scramble_id) if img.scramble_id and not gif else None
        _download_image_single_attempt(
            img_client, job_id, album_id, album, "",
            photo, i, img, img_url, img_path, scramble_id,
            done_pages, total_pages, failed_pages,
            pending_images if pass_num == 1 else None,
            tracker, photo_dir, pass_num=pass_num, timeout=timeout, lock=lock, refetch_gif=refetch_gif,
            stop_event=stop_event, node_attempts=node_attempts, checkpoint=checkpoint, settings=settings,
        )
    finally:
        close_client(img_client)


def download_album_job(job_id: str, album_id: str, photo_ids: list[str], *, stop_event=None,
                       begin_file_finalization=None):
    """
    在后台线程中执行下载任务；允许重试时，首趟失败的图片再串行补一趟。

    Runtime shutdown cooperatively stops between requests and before publishing
    new content. In-flight SDK calls keep their transport until they actually end;
    the manager reports an undrained shutdown rather than closing it early.
    """
    if stop_event is not None and stop_event.is_set():
        return
    checkpoint = DownloadCheckpoint(job_id, db.get_job(job_id) or {})
    _run_album_download(job_id, album_id, photo_ids, checkpoint, stop_event, begin_file_finalization)
    return checkpoint


def _run_album_download(job_id, album_id, photo_ids, checkpoint, stop_event, begin_file_finalization):
    tracker = progress_manager.get_tracker(job_id)
    if not tracker:
        log.error(f"下载任务无法获取 tracker，标记为失败 job_id={job_id}")
        finish_download(job_id, "failed", error_message="tracker 不可用，任务初始化失败")
        return

    failed_pages = []
    job_lock = threading.Lock()  # 每个任务独立锁，消除跨任务锁争用
    log.info(f"开始下载任务 job_id={job_id} album_id={album_id} photo_count={len(photo_ids)}")

    client = None  # 在 finally 中按需关闭
    written_dirs = []  # 这次写过的目录：结束时（无论成败）让“本地可读”重新判断

    try:
        if _should_stop(job_id, tracker, stop_event):
            return
        tracker.push("progress", {
            "job_id": job_id, "status": "running",
            "done_pages": checkpoint.previous_done, "total_pages": checkpoint.previous_total,
            "progress": round(checkpoint.previous_done * 100 / (checkpoint.previous_total or 1), 1),
            "message": "正在获取漫画信息...",
        })

        client, _ = get_client(shared=False)
        with download_budget(client, _operation_timeout(), stop_event=stop_event,
                             should_cancel=lambda: _should_stop(job_id, None, stop_event)):
            album = client.get_album_detail(album_id)
        if _should_stop(job_id, tracker, stop_event):
            return

        total_pages = 0
        photos_to_download = []
        seen_photo_ids = set()
        selected = set(map(str, photo_ids))
        empty = []
        for photo in album:
            if _should_stop(job_id, tracker, stop_event):
                return
            if not selected or str(photo.photo_id) in selected:
                if str(photo.photo_id) in seen_photo_ids:
                    continue
                seen_photo_ids.add(str(photo.photo_id))
                with download_budget(client, _operation_timeout(), stop_event=stop_event,
                                     should_cancel=lambda: _should_stop(job_id, None, stop_event)):
                    client.check_photo(photo)
                if _should_stop(job_id, tracker, stop_event):
                    return
                pages = len(photo) if hasattr(photo, "__len__") and photo.page_arr else 0
                if not pages:
                    empty.append(str(photo.photo_id))
                    continue
                total_pages += pages
                photos_to_download.append(photo)

        missing = selected - seen_photo_ids
        if selected and (missing or empty):
            problems = []
            if missing:
                problems.append("缺失：" + "、".join(sorted(missing)[:10]))
            if empty:
                problems.append("无图片：" + "、".join(empty[:10]))
            raise ValueError(f"选中的章节不完整（{'；'.join(problems)}），请刷新章节列表后重试")
        if not photos_to_download or total_pages <= 0:
            raise ValueError("选中的章节不存在或没有可下载图片")

        # 检查新章节的基线（core/update_store.py）：用这次已经取到的完整章节列表，不多发请求。
        # 本地原来有没有能读的内容，必须在这个任务写任何文件之前判断。记录失败只写日志，绝不影响下载
        try:
            update_store.note_download_started(album_id, update_store.episodes_of(album), is_readable(album_id))
        except Exception as e:
            log.warning(f"记录检查新章节的基线失败（不影响下载）album_id={album_id} error={e}")

        dl_root = str(DOWNLOAD_ROOT)
        album_dir = None
        settings = get_settings()
        organize_mode = settings.get("organize_mode", "none")
        existing = _reusable_album_dir(album_id)
        if existing is not None and (photo_ids or organize_mode == "by_author"):
            album_dir = existing
        if checkpoint.directory is not None and not private_path(checkpoint.directory, dl_root):
            album_dir = checkpoint.directory
        from .download_layout import album_output_dir
        album_dir = album_output_dir(dl_root, job_id, album.name, album_id, photos_to_download,
                                     preferred=album_dir, pack=settings.get('auto_pack') == 'true')
        os.makedirs(dl_root, exist_ok=True)
        if not is_safe_path(album_dir):
            raise ValueError("专辑输出路径越权")
        # 目录不在（被删除了）或只剩没有图片的空壳（例如“打开文件夹”替排队中的重试建出的）：更早完成、写到这里的
        # 任务，它们的文件已经不在了。这次下载会把目录重新建出来——标记那些任务被取代，这次若失败或被取消，
        # 残缺的几页不能让它们重新显示成完整、可离线阅读（core.local_availability）。
        # 只剩能读的压缩包（自动打包后删了原图）不算空壳：那次下载的内容还在
        if not has_local_pages(album_dir):
            db.mark_completed_jobs_superseded(album_id, str(album_dir), job_id)
        workspace = Workspace(dl_root, job_id, album_id, album_dir, checkpoint)
        seeds = workspace.seed(photos_to_download, existing or album_dir,
                               settings.get('skip_existing') == 'true', _valid_image, image_is_gif,
                               page_extension=lambda gif: _page_extension(settings, gif=gif))
        album_dir = workspace.work
        checkpoint.own(album_dir)
        for seed in seeds:
            checkpoint.remember_digest(seed, checkpoint.digest(seed))
        written_dirs.append(album_dir)
        checkpoint.directory = album_dir
        output_path = str(album_dir)

        db.update_job(job_id, total_pages=total_pages, done_pages=min(checkpoint.previous_done, total_pages),
                      output_path=output_path)
        tracker.push("progress", {
            "job_id": job_id, "status": "running",
            "done_pages": min(checkpoint.previous_done, total_pages), "total_pages": total_pages,
            "progress": round(min(checkpoint.previous_done, total_pages) * 100 / total_pages, 1),
            "message": f"开始下载，共 {total_pages} 页",
        })

        # 用列表包裹 done_pages 以便在函数中修改
        done_pages = [0]

        _lock = job_lock  # 每个任务独立锁，传递到下载函数

        # ── 首趟：章节级并行，每章独立线程 ──
        node_attempts = 1 + int(settings.get("retry_times", 2))
        pending_images = [] if node_attempts > 1 else None
        photo_threads = int(settings.get("photo_threads", "3"))
        log.info(f"章节并行下载 threads={photo_threads} chapters={len(photos_to_download)} job_id={job_id}")

        with ThreadPoolExecutor(max_workers=photo_threads) as pool:
            futs = []
            for photo in photos_to_download:
                fut = pool.submit(
                    bind_request_id(_download_chapter),
                    job_id, album_id, album, album_dir, photo, total_pages,
                    done_pages, pending_images, failed_pages,
                    tracker, checkpoint, _lock, 1, _FIRST_PASS_TIMEOUT, stop_event, node_attempts, settings,
                )
                futs.append(fut)
            # 等待所有章节完成，不阻塞 cancel（每个 chapter 线程内检查 _should_stop）
            for fut in as_completed(futs):
                if _should_stop(job_id, tracker, stop_event):
                    for pending in futs:
                        pending.cancel()
                    break
                try:
                    fut.result()
                except Exception as e:
                    log.warning(f"章节下载线程异常 job_id={job_id} error={e}")

        # ── 第二趟：串行重试 pending 图片（量少，无需并行）──
        if _should_stop(job_id, tracker, stop_event):
            return
        if pending_images:
            log.info(f"开始重试 {len(pending_images)} 张未下载的图片 job_id={job_id}")
            tracker.push("progress", {
                "job_id": job_id, "status": "running",
                "done_pages": done_pages[0], "total_pages": total_pages,
                "progress": round(done_pages[0] / total_pages * 100, 1) if total_pages > 0 else 0,
                "message": f"正在重试 {len(pending_images)} 张未下载的图片...",
            })
            def retry_page(page):
                _download_chapter_image(None, job_id, album_id, album, page["photo"], page["photo_dir"],
                                        page["idx"], page["img"], total_pages, done_pages, None, failed_pages,
                                        tracker, checkpoint, job_lock, 2, _RETRY_TIMEOUT,
                                        stop_event=stop_event, node_attempts=node_attempts, settings=settings)

            download_capacity.run_images(pending_images, retry_page, workers=1,
                                         should_stop=lambda: _should_stop(job_id, None, stop_event))

        now = datetime.now().isoformat()

        # Futures can fail before recording a page error. Do not pack partial data.
        if _should_stop(job_id, tracker, stop_event):
            return
        missing_pages = max(0, total_pages - done_pages[0])
        failed_count = max(len(failed_pages), missing_pages)
        if done_pages[0] != total_pages and len(failed_pages) < missing_pages:
            failed_pages.append(f"下载不完整：{done_pages[0]}/{total_pages} 页")
        elif done_pages[0] != total_pages and not failed_pages:
            failed_pages.append(f"下载不完整：{done_pages[0]}/{total_pages} 页")
            failed_count = max(failed_count, 1)
        if not failed_pages:
            # 每一页都已按当前格式写好（旧目录里的 GIF 页已重新取回）：升级章节标记，以后不再重取
            for photo in photos_to_download:
                _mark_chapter_current(album_dir, photo)

            def check_file_control():
                if _should_stop(job_id, tracker, stop_event):
                    raise DownloadInterrupted("stopped" if stop_event is not None and stop_event.is_set() else "canceled")

            if begin_file_finalization is not None and not begin_file_finalization(job_id):
                return
            manifest = workspace.seal(photos_to_download, check_file_control)
            view, publication = workspace.project(manifest, album, settings, check_file_control)
            publication[2]['keep_location'] = existing is not None and workspace.target == existing
            output_path = str(view)
            written_dirs.append(view)

        if _should_stop(job_id, tracker, stop_event):
            return
        try:
            db.upsert_album_meta(album_id, title=album.name, author=album.author,
                                cover_url=JmcomicText.get_album_cover_url(album_id))
            resolved_title = _safe_str(album.name).strip()[:500]
            if resolved_title and resolved_title != album_id:
                current_job = db.get_job(job_id)
                stored_title = (current_job.get("title") or "").strip() if current_job else ""
                if not stored_title or stored_title == album_id:
                    db.update_job(job_id, title=resolved_title)
        except Exception as e:
            log.warning(f"写入 album_meta 失败 album_id={album_id} error={e}")

        try:
            tags = getattr(album, 'tags', None)
            if tags:
                tag_names = []
                for t in tags:
                    if isinstance(t, str):
                        tag_names.append(t)
                    elif isinstance(t, dict):
                        tag_names.append(t.get("name", "") or "")
                synced = db.batch_sync_auto_tags(album_id, tag_names)
                if synced > 0:
                    log.info(f"下载完成同步标签 album_id={album_id} count={synced}")
        except Exception as e:
            log.warning(f"同步自动标签失败 album_id={album_id} error={e}")

        done_val = done_pages[0]

        if _should_stop(job_id, tracker, stop_event):
            return

        # 最终写入前检查是否已被取消，防止覆盖 canceled 状态
        final_job = db.get_job(job_id)
        if final_job and final_job["status"] == "canceled":
            log.info(f"任务已被取消，跳过最终状态写入 job_id={job_id}")
            tracker.close()
            return

        if failed_pages:
            err_all = "; ".join(failed_pages[:5])
            if len(failed_pages) > 5:
                err_all += f" ...（共 {failed_count} 页失败）"
            log.warning(f"下载任务部分失败 job_id={job_id} failed_pages={failed_count} total_pages={total_pages}")
            ok = finish_download(job_id, "failed", done_pages=done_val,
                                 error_message=err_all, completed_at=now)
            if not ok:
                log.warning(f"transition_job_status 失败，状态可能已被更改 job_id={job_id}")
            if ok:
                tracker.push("failed", {
                    "job_id": job_id, "status": "failed",
                    "error_message": err_all,
                    "done_pages": done_val, "total_pages": total_pages,
                })
        else:
            log.info(f"下载任务完成 job_id={job_id} total_pages={total_pages} output_path={output_path}")
            ok = finish_download(job_id, "completed", done_pages=done_val, completed_at=now,
                                 photo_ids=[str(p.photo_id) for p in photos_to_download],
                                 publication=publication, output_path=output_path)
            if not ok:
                log.warning(f"transition_job_status 失败，状态可能已被取消 job_id={job_id}")
            if ok:
                tracker.push("completed", {
                    "job_id": job_id, "status": "completed",
                    "output_path": output_path,
                    "done_pages": done_val, "total_pages": total_pages,
                })
                try:
                    workspace.release_pages(manifest)
                except (OSError, ValueError) as error:
                    log.warning(f"任务完成，保留未清理的工作副本 job_id={job_id} error={type(error).__name__}")

        tracker.close()

    except DownloadInterrupted:
        return
    except Exception as e:
        if stop_event is not None and stop_event.is_set():
            log.info(f"停机期间上游调用结束，保留任务供恢复 job_id={job_id} error={type(e).__name__}")
            return
        err_msg = sanitize(str(e))[:1000]
        log.error(f"下载任务异常 job_id={job_id} error={err_msg}")
        # 异常处理前先检查是否已被取消，防止覆盖 canceled 状态
        try:
            cur_job = db.get_job(job_id)
        except Exception as e:
            log.error(f"获取 job 状态失败 job_id={job_id} error={e}")
            cur_job = None
        if cur_job and cur_job["status"] == "canceled":
            log.info(f"任务已被取消，异常处理跳过状态写入 job_id={job_id}")
            tracker.close()
            return
        if failed_pages:
            err_msg = "; ".join(failed_pages[:3]) + " | " + err_msg
        try:
            changed = finish_download(job_id, "failed", error_message=err_msg[:1000])
        except Exception as db_err:
            log.error(f"更新失败状态到数据库出错: {db_err}")
            changed = False
        if not changed:
            return
        try:
            tracker_ref = progress_manager.get_tracker(job_id)
            if tracker_ref:
                tracker_ref.push("failed", {
                    "job_id": job_id, "status": "failed",
                    "error_message": err_msg[:500],
                })
                tracker_ref.close()
        except Exception as tr_err:
            log.error(f"推送失败事件到 tracker 出错: {tr_err}")
    finally:
        try:
            close_client(client)
        finally:
            for folder in written_dirs:
                forget_local_state(folder)
            # If even failure persistence was unavailable, end the stream
            # without inventing a terminal DB state. Recovery owns that row.
            tracker.close()


def _safe_str(val: Any) -> str:
    """安全转换为字符串，None 时返回空字符串"""
    return str(val) if val is not None else ""
