"""
jmcomic 服务封装模块
使用 build_jmcomic_option() 确保设置真实生效。
"""
import os
import shutil
import tempfile
import threading
import time
from collections import OrderedDict
from copy import copy
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

from jmcomic import (
    JmModuleConfig,
    JmMagicConstants,
    JmcomicText,
)

from . import database as db
from .logger import log, bind_request_id
from .progress import progress_manager
from .settings import get_settings, build_jmcomic_option
from .validation import safe_dirname as _safe_dirname
from .path_guard import DOWNLOAD_ROOT, is_safe_path
from .packer import CbzPacker
from .file_tree import is_link
from . import archive_pages
from . import update_store
from . import sdk_adapter
from . import background_calls
from .upstream import classify_failure
from .storage import staged_file
from . import image_download
from .transfer import download_budget, DownloadInterrupted
from .local_availability import forget as forget_local_state, has_local_pages, is_readable, readable_folder
from .task_commands import finish_download


# ── 全局复用客户端（搜索/详情用，避免每次新建 client + 连接池）──
_global_option = None
_global_client = None
_option_lock = threading.RLock()
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


def _get_or_create_option():
    """获取缓存的 JmOption 实例（模块级单例，惰性创建，settings 缓存失效时自动重建）"""
    global _global_option
    if _global_option is None:
        with _option_lock:
            if _global_option is None:
                opt_dict = build_jmcomic_option()
                _global_option = sdk_adapter.build_option(opt_dict)
    return _global_option


def invalidate_option_cache(*, _owned_cleanup=False):
    """Retire the old shared client; close it only after its last request releases it."""
    global _global_option, _global_client, _option_generation
    with _option_lock:
        previous = _global_client
        _option_generation += 1
        _global_option = None
        _global_client = None
        if previous is not None:
            previous._jm_retired = True
            _retired_clients[id(previous)] = previous
        safe = True
        for retired in list(_retired_clients.values()):
            if getattr(retired, "_jm_users", 0) == 0 and not _dispose_client(retired):
                safe = False
    if not safe and not _owned_cleanup:
        background_calls.request_cleanup()
    return safe


def get_client(shared=True):
    """Acquire a client. Every acquisition must be balanced by close_client()."""
    global _global_client, _active_independent_clients
    with _option_lock:
        if shared:
            background_calls.require_accepting()
        option = _get_or_create_option()
        if shared:
            if _global_client is None:
                _global_client = sdk_adapter.new_client(option)
                setattr(_global_client, _JM_SHARED_ATTR, True)
                _global_client._jm_users = 0
                _global_client._jm_retired = False
            _global_client._jm_users += 1
            return _global_client, option
        client = sdk_adapter.new_client(option)
        client._jm_counted = True
        with _clients_lock:
            _active_independent_clients += 1
        return client, option


def option_generation() -> int:
    """当前 jmcomic 设置的版本号；设置保存（invalidate_option_cache）后变化。"""
    return _option_generation


def new_image_client():
    """在线阅读取图用的 client：由 online_reader 的连接池长期持有、逐个请求独占复用，用完 close_client() 关闭。

    与 get_client() 用同一份 option（代理、impersonate、请求头规则都一样），只多一项：curl 句柄不绑定线程。
    curl_cffi 的 Session 默认每个线程一个 curl 句柄，换一个线程用同一个 client 仍是新连接、新 TLS 握手；
    池里的 client 会被不同的请求线程轮流使用，所以改用 client 自己的句柄，连接随 client 复用。
    不计入 get_active_client_count()（池的大小有上限，不是泄漏）。"""
    with _option_lock:
        option = _get_or_create_option()
        overrides = {}
        if option.client.postman.src_dict.get("type") == "curl_cffi_session":
            overrides["use_thread_local_curl"] = False
        return sdk_adapter.new_client(option, **overrides)


def new_check_client(timeout: int):
    """检查新章节（core/update_checker.py）专用的 client：每次检查新建一个，用完 close_client() 关闭。

    与下载同一份 option（代理、impersonate、client 类型都一样），另外三点：
      - 不带缓存（cache=False 且 set_cache_dict(None)）：每次都向上游要最新的章节列表；
      - retry_times = 0：第一次出错就抛出，换哪个域名由检查自己决定（每个域名最多问一次）；
      - timeout 覆盖 curl 的总超时（调用方已限制在 20 秒以内）。
    计入 get_active_client_count()。"""
    global _active_independent_clients
    with _option_lock:
        client = sdk_adapter.new_client(_get_or_create_option(), cache=False, timeout=timeout)
    client.set_cache_dict(None)
    client.retry_times = 0
    client._jm_counted = True
    with _clients_lock:
        _active_independent_clients += 1
    return client


def get_active_client_count() -> int:
    with _clients_lock:
        return _active_independent_clients


def _dispose_client(client):
    """Close the transport once; callers serialize lifetime transitions."""
    if getattr(client, _JM_CLOSED_ATTR, False):
        _retired_clients.pop(id(client), None)
        return True
    try:
        root = client.get_root_postman()
        if hasattr(root, "session") and hasattr(root.session, "close"):
            root.session.close()
        elif hasattr(root, "close"):
            root.close()
        elif hasattr(client, "close"):
            client.close()
    except Exception as exc:
        log.warning(f"关闭 client 异常: {exc}")
        return False
    setattr(client, _JM_CLOSED_ATTR, True)
    _retired_clients.pop(id(client), None)
    return True


def close_client(client) -> bool:
    global _active_independent_clients
    if client is None:
        return True
    with _option_lock:
        if getattr(client, _JM_SHARED_ATTR, False):
            client._jm_users = max(0, getattr(client, "_jm_users", 0) - 1)
            if getattr(client, "_jm_retired", False) and client._jm_users == 0:
                _retired_clients[id(client)] = client
                safe = _dispose_client(client)
                if not safe:
                    background_calls.request_cleanup()
                return safe
            return True
        if getattr(client, _JM_CLOSED_ATTR, False):
            return True
        if not _dispose_client(client):
            return False
        if getattr(client, "_jm_counted", False):
            with _clients_lock:
                _active_independent_clients -= 1
        return True


def _shutdown_client_cache():
    return invalidate_option_cache(_owned_cleanup=True)


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


def _should_stop(job_id: str, album_id: str, tracker, pause_ev, stop_event=None) -> bool:
    """Check explicit cancellation or runtime shutdown without conflating them."""
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
    # 暂停等待 — 保留已下载进度，不推送 0/0
    while job and job["status"] == "paused":
        if stop_event is not None and stop_event.is_set():
            return True
        log.debug(f"下载任务暂停中 job_id={job_id}")
        if pause_ev:
            pause_ev.wait(timeout=3)
        else:
            time.sleep(3)  # pause_ev 未传入时的兜底
        job = db.get_job(job_id)
        if stop_event is not None and stop_event.is_set():
            return True
        if job is None or job["status"] == "canceled":
            if tracker:
                tracker.push("progress", {
                    "job_id": job_id, "status": "canceled",
                    "message": "任务已被删除或取消",
                })
            return True
    return False


def _download_image_single_attempt(
    client, job_id, album_id, album, output_path,
    photo, idx, img, img_url, img_path, scramble_id,
    done_pages, total_pages, failed_pages, pending_images,
    tracker, photo_dir, pass_num, timeout, lock, refetch_gif=False, stop_event=None,
):
    """单张图片单次下载尝试。
    成功 → done_pages +1
    超时且是首趟 → 记录到 pending_images
    失败且是末趟 → 记录到 failed_pages
    使用传入的每任务锁 lock，避免跨任务争用模块级 _download_lock。
    GIF 页（image_is_gif）不切片，保存为保留全部帧的动画 WebP（文件名仍是 NNNNN.webp）。
    """
    if stop_event is not None and stop_event.is_set():
        return
    img_name = f"{idx + 1:05d}.webp"
    img_path = photo_dir / img_name if not str(img_path).startswith(str(photo_dir)) else img_path
    if not is_safe_path(img_path) or not is_safe_path(photo_dir):
        raise ValueError("图片输出路径越权")

    gif = image_is_gif(img)
    skip = get_settings().get("skip_existing", "true") == "true"
    try:
        # Existing files need the same error boundary as downloaded files. An
        # invalid/bomb image must be replaced or recorded, never abort a chapter.
        if skip and pass_num == 1 and not (gif and refetch_gif) and _valid_image(img_path):
            with lock:
                done_pages[0] += 1
            if tracker:
                tracker.push("progress", {
                    "job_id": job_id, "status": "running",
                    "done_pages": done_pages[0], "total_pages": total_pages or 0,
                    "progress": round(done_pages[0] / (total_pages or 1) * 100, 1),
                })
            return
        # SDK/Pillow need the real suffix. Staging stays on this disk but outside
        # every reader/packer scan, including when the process is interrupted.
        with staged_file(img_path) as staged:
            options = dict(timeout=_operation_timeout(timeout),
                           attempts=1 + int(get_settings().get("retry_times", 2)),
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
            if stop_event is not None and stop_event.is_set():
                return
            staged.publish(_valid_image)
        # 确认下载成功后计数
        with lock:
            done_pages[0] += 1
    except DownloadInterrupted:
        return
    except Exception as e:
        if stop_event is not None and stop_event.is_set():
            return  # Runtime owns recovery; a stop is neither a failed page nor a retry.
        if pass_num == 1 and pending_images is not None:
            # 首趟：只存位置信息，不存 client（线程退出后 client 会关闭）
            with lock:
                pending_images.append({
                    "photo": photo, "idx": idx,
                    "img": img, "img_url": img_url, "img_path": img_path,
                    "scramble_id": scramble_id, "photo_dir": photo_dir,
                })
            log.warning(
                f"首趟下载失败，已跳过待重试 job_id={job_id} "
                f"pass={pass_num} img={idx + 1} error={e}"
            )
        else:
            with lock:
                err_msg = f"第 {idx+1} 页下载失败: {e}"
                log.warning(f"图片下载最终失败 job_id={job_id} error={err_msg}")
                failed_pages.append(err_msg)
                db.update_job(job_id, error_message=err_msg[:500])
    with lock:
        progress_pct = round(done_pages[0] / total_pages * 100, 1) if total_pages > 0 else 0
        db.update_job(job_id, done_pages=done_pages[0])
        if tracker:
            tracker.push("progress", {
                "job_id": job_id, "status": "running",
                "done_pages": done_pages[0], "total_pages": total_pages,
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
        return True
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        return False


def _submit_client_call(pool, client, fn, *args, **kwargs):
    """Keep a shared transport leased until the future actually finishes/cancels.
    The worker thread inherits the caller's request_id, so jmcomic's retry/failure records join its trace."""
    shared = client is not None and getattr(client, _JM_SHARED_ATTR, False)
    def acquire():
        with _option_lock:
            client._jm_users += 1
    return background_calls.submit(pool, bind_request_id(fn), *args,
        acquire=acquire if shared else None,
        release=(lambda: close_client(client)) if shared else None, **kwargs)


def _call_with_timeout(label: str, timeout: float, timeout_msg: str, fn, *args, **kwargs):
    """在独立线程中执行 jmcomic 远程调用，带超时保护。

    统一取代散落各处的 `ThreadPoolExecutor(1) + fut.result(timeout=...)` 样板：
    只有真正的等待/传输超时归为 timeout，其余失败保留结构化种类。
    请求返回后，仍在执行的调用继续持有客户端租约，直到实际结束。
    """
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        fut = _submit_client_call(pool, getattr(fn, "__self__", None), fn, *args, **kwargs)
        result = fut.result(timeout=timeout)
    except Exception as e:
        pool.shutdown(wait=False)  # 不阻塞请求线程；挂起的工作线程由其自身超时终结
        failure = classify_failure(e, timeout_message=timeout_msg)
        log.warning(f"{label} 失败 kind={failure.kind} error={type(e).__name__}: {e}")
        if failure is e:
            raise
        raise failure from e
    pool.shutdown(wait=True)
    return result


def search_albums(keyword: str, page: int = 1, page_size: int = 20, sort: str = "latest"):
    """搜索漫画，page_size 真实限制结果数量，带 25 秒超时"""
    log.info(f"搜索 keyword={keyword} page={page} page_size={page_size} sort={sort}")
    client, _ = get_client()

    order_map = {
        "latest": JmMagicConstants.ORDER_BY_LATEST,
        "views": JmMagicConstants.ORDER_BY_VIEW,
        "likes": JmMagicConstants.ORDER_BY_LIKE,
        "pictures": JmMagicConstants.ORDER_BY_PICTURE,
    }

    try:
        search_page = _call_with_timeout(
            f"搜索 keyword={keyword}", 25, "搜索超时，请稍后重试",
            client.search,
            search_query=keyword,
            page=page,
            main_tag=0,
            order_by=order_map.get(sort, JmMagicConstants.ORDER_BY_LATEST),
            time=JmMagicConstants.TIME_ALL,
            category=JmMagicConstants.CATEGORY_ALL,
            sub_category=None,
        )
        # search_page.content 是已物化的普通列表（见 docs/reports/连接池并发分析报告.md）
        all_items = list(search_page.content)
    finally:
        close_client(client)

    # 真实限制page_size
    sliced = all_items[:page_size]

    items = []
    for album_id, info in sliced:
        items.append({
            "album_id": str(album_id),
            "title": _safe_str(info.get("name", "")),
            "author": _safe_str(info.get("author", "")),
            "tags": info.get("tags", []),
            "cover_url": JmcomicText.get_album_cover_url(album_id),
        })

    total = getattr(search_page, "total", None)
    # 不提供虚假 total，让前端自己处理分页

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def check_album_photos(client, album, timeout: float = 15, failed: list | None = None) -> list:
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
    check_pool = ThreadPoolExecutor(max_workers=min(len(pending), 10))
    fut_map, completed = {}, set()
    try:
        for index, photo in pending:
            working = copy(photo)
            if isinstance(getattr(working, "page_arr", None), list):
                working.page_arr = list(working.page_arr)
            future = _submit_client_call(check_pool, client, client.check_photo, working)
            fut_map[future] = (index, working)
        for fut in as_completed(fut_map, timeout=timeout):
            index, working = fut_map[fut]
            try:
                fut.result()
            except Exception as e:
                log.warning(f"获取 photo 详情失败 photo_id={working.photo_id} error={type(e).__name__}: {e}")
            else:
                if getattr(working, "page_arr", None) is not None:
                    photo_list[index] = working
                    completed.add(index)
                else:
                    log.warning(f"获取 photo 详情未返回图片列表 photo_id={working.photo_id}")
    except FuturesTimeout:
        unfinished = sum(1 for fut in fut_map if not fut.done())
        log.warning(f"check_photo 超时（{timeout}s），album_id={getattr(album, 'album_id', '?')}，"
                    f"已跳过 {unfinished} 个未完成章节")
    finally:
        check_pool.shutdown(wait=False, cancel_futures=True)
    if failed is not None:
        # Use the outcome observed before the deadline, not a second Future.done
        # check that could race a late completion and mislabel an empty result.
        failed.extend(photo_list[index] for index, _ in pending if index not in completed)
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
    client, _ = get_client()
    try:
        # 包装远程调用，设 20 秒超时防止永久挂起占用 Waitress 线程
        album = _call_with_timeout(
            f"获取专辑详情 album_id={album_id}", 20, "获取专辑详情超时，请稍后重试",
            client.get_album_detail, album_id,
        )

        photos, failed = [], []
        checked = check_album_photos(client, album, failed=failed)
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
    from copy import deepcopy
    import json
    key = (str(db.DB_PATH), album_id)
    ttl = max(0, ttl)
    with _detail_fetch_locks[hash(key) % len(_detail_fetch_locks)]:
        now = time.time()
        with _detail_cache_lock:
            generation = _detail_cache_generation
            cached = _detail_cache.get(key)
            if (cached and _detail_is_complete(cached["data"]) and ttl > 0
                    and now - cached["ts"] < min(300, ttl)):
                _detail_cache.move_to_end(key)
                return deepcopy(cached["data"])
        stored = db.get_cached_album_detail(album_id, ttl=ttl) if ttl > 0 else None
        if stored and not _detail_is_complete(stored["detail"]):
            with _detail_cache_lock:
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
        with _detail_cache_lock:
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


def organize_download(output_path: str, mode: str, album) -> str | None:
    """整理下载目录结构。

    Args:
        output_path: 当前下载输出路径（漫画目录）
        mode: 整理模式 ("none", "by_author", "flat")
        album: jmcomic album 对象（含 name, author 等属性）

    Returns:
        新路径，如果未整理则返回 None
    """
    if mode == "none" or not mode:
        return None

    output = Path(output_path)
    if not output.exists() or not output.is_dir():
        log.warning(f"整理目录不存在，跳过整理: {output_path}")
        return None

    dl_root = output.parent  # downloads/ 目录

    try:
        if mode == "by_author":
            author = _safe_dirname(_safe_str(getattr(album, "author", "")) or "unknown_author")
            new_root = dl_root / author
            new_output = new_root / output.name
            # A move into an existing directory nests chapters or overwrites files.
            # Keep the original intact when a destination already exists.
            if new_output.exists() or new_output.is_relative_to(output):
                log.warning(f"整理目标已存在或位于源目录内部，保留源目录: {new_output}")
                return None
            if not is_safe_path(new_output):
                raise ValueError("整理目标路径越权")
            new_root.mkdir(parents=True, exist_ok=True)
            shutil.move(str(output), str(new_output))
            log.info(f"按作者整理完成: {output_path} -> {new_output}")
            return str(new_output)

        elif mode == "flat":
            # 扁平化：将各章节子目录中的图片移到漫画目录下
            chapter_dirs = sorted([
                d for d in output.iterdir()
                if d.is_dir()
            ])
            counter = 0
            for ch_dir in chapter_dirs:
                # 取章节名的最后一部分（如 "Ch-c1"）
                ch_prefix = ch_dir.name.replace(" ", "_")
                images = sorted(ch_dir.glob("*.*"))
                for img_path in images:
                    if img_path.suffix.lower() not in (".webp", ".jpg", ".jpeg", ".png", ".gif", ".bmp"):
                        continue
                    counter += 1
                    new_name = f"{ch_prefix}_{counter:05d}{img_path.suffix}"
                    dest = output / new_name
                    if dest.exists():
                        log.warning(f"扁平化目标已存在，保留源图片: {dest}")
                        continue
                    shutil.move(str(img_path), str(dest))
                # 删除空章节目录
                _remove_empty_dir(ch_dir)
            log.info(f"扁平化整理完成: {output_path}, 共整理 {counter} 张图片")
            return output_path  # 路径不变

    except Exception as e:
        log.warning(f"整理目录失败 mode={mode} path={output_path} error={e}")
        return None

    return None


def _remove_empty_dir(path: Path):
    """安全删除空目录"""
    try:
        if path.exists() and path.is_dir():
            path.rmdir()
    except OSError:
        pass  # 目录非空或无权删除，忽略


# 章节目录标记 .jm-chapter.json：{"photo_id": ..., "format": N}。format 2 起 GIF 页不切片、保留全部帧；
# 没有 format（旧版本创建）的目录里 GIF 页可能被切成错位横条或只剩第一帧，重新下载时这些页不能按
# “已存在”跳过（skip_existing）。整本下载成功后由 _mark_chapter_current 升级标记，之后不再重取。
_CHAPTER_MARKER = ".jm-chapter.json"
_CHAPTER_FORMAT = 2


def _chapter_format(directory) -> int:
    """章节目录的格式版本；读不到或旧标记（只有 photo_id）视为 1。"""
    import json
    try:
        value = json.loads((Path(directory) / _CHAPTER_MARKER).read_text(encoding="utf-8")).get("format", 1)
    except (OSError, ValueError, AttributeError):
        return 1
    return value if isinstance(value, int) and not isinstance(value, bool) else 1


def _mark_chapter_current(album_dir, photo) -> None:
    """本章节的每一页都已按当前格式写好：把标记升级到 _CHAPTER_FORMAT。先写临时文件再替换——
    写坏的标记会让 _chapter_output_dir 不再认领这个目录。失败只记录，下次重新下载会再重取 GIF 页。"""
    import json
    tmp = None
    try:
        directory = _chapter_output_dir(album_dir, photo)
        if _chapter_format(directory) >= _CHAPTER_FORMAT:
            return
        fd, name = tempfile.mkstemp(prefix=_CHAPTER_MARKER + ".", suffix=".tmp", dir=directory)
        tmp = Path(name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"photo_id": str(photo.photo_id), "format": _CHAPTER_FORMAT}, handle)
        os.replace(tmp, directory / _CHAPTER_MARKER)
        tmp = None
    except (OSError, ValueError) as e:
        log.warning(f"更新章节目录标记失败 photo_id={getattr(photo, 'photo_id', '')} error={e}")
    finally:
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


def _chapter_output_dir(album_dir, photo):
    """Identify folders by chapter ID; never adopt or overwrite an unmarked old folder."""
    import json
    from .validation import validate_numeric
    photo_id = str(photo.photo_id)
    if not validate_numeric(photo_id):
        raise ValueError("章节 ID 无效")
    base_name = f"{_safe_dirname(photo.name or photo_id, max_len=100)}__{photo_id}"
    suffix = 1
    while True:
        name = base_name if suffix == 1 else f"{base_name}_{suffix}"
        directory = Path(album_dir) / name
        marker = directory / _CHAPTER_MARKER
        if not is_safe_path(directory) or not is_safe_path(marker):
            raise ValueError("章节输出路径越权")
        if directory.exists():
            try:
                record = json.loads(marker.read_text(encoding="utf-8"))
                if record.get("photo_id") == photo_id:
                    return directory
            except (OSError, ValueError, AttributeError):
                pass
            suffix += 1
            continue
        try:
            directory.mkdir()
        except FileExistsError:
            continue
        marker.write_text(json.dumps({"photo_id": photo_id, "format": _CHAPTER_FORMAT}), encoding="utf-8")
        return directory


def _download_chapter(
    job_id, album_id, album, album_dir, photo, total_pages,
    done_pages, pending_images, failed_pages, tracker, pause_ev, lock, pass_num, timeout,
    stop_event=None,
):
    """下载单个章节的所有图片（支持章节内多图并行，每图独立 client）"""
    if _should_stop(job_id, album_id, tracker, pause_ev, stop_event):
        return
    control = {"stop_event": stop_event} if stop_event is not None else {}
    image_threads = int(get_settings().get("image_threads", "3"))
    photo_dir = _chapter_output_dir(album_dir, photo)
    # 旧版本下载的章节：GIF 页可能已被切片/只剩第一帧，首趟不按“已存在”跳过它们
    refetch_gif = _chapter_format(photo_dir) < _CHAPTER_FORMAT

    photo_images = list(enumerate(photo))
    if len(photo_images) <= 1 or image_threads <= 1:
        # 单张图或禁止并行 → 串行
        for i, img in photo_images:
            _download_chapter_image(
                None, job_id, album_id, album, photo, photo_dir,
                i, img, total_pages, done_pages, pending_images,
                failed_pages, tracker, pause_ev, lock, pass_num, timeout, refetch_gif,
                **control,
            )
    else:
        # 多图并行：每个线程独立 client，避免 HTTP 非线程安全问题
        with ThreadPoolExecutor(max_workers=image_threads) as img_pool:
            img_futs = []
            for i, img in photo_images:
                fut = img_pool.submit(
                    bind_request_id(_download_chapter_image),
                    None, job_id, album_id, album, photo, photo_dir,
                    i, img, total_pages, done_pages, pending_images,
                    failed_pages, tracker, pause_ev, lock, pass_num, timeout, refetch_gif,
                    **control,
                )
                img_futs.append(fut)
            for fut in as_completed(img_futs):
                # 提前检查取消状态，减少取消延迟
                if _should_stop(job_id, album_id, tracker, pause_ev, stop_event):
                    for pending in img_futs:
                        pending.cancel()
                    break
                try:
                    fut.result()  # Already completed; network limits belong to the transport.
                except Exception as e:
                    log.warning(f"图片下载线程异常 job_id={job_id} error={e}")

    if stop_event is None or not stop_event.is_set():
        db.update_job(job_id, current_photo=str(photo.photo_id))


def _download_chapter_image(
    _, job_id, album_id, album, photo, photo_dir,
    i, img, total_pages, done_pages, pending_images,
    failed_pages, tracker, pause_ev, lock, pass_num, timeout, refetch_gif=False,
    stop_event=None,
):
    """下载单张图片（每个线程独立 client，避免 HTTP 非线程安全问题）"""
    # _should_stop 内部已持锁（db 查询），无需额外保护
    if _should_stop(job_id, album_id, tracker, pause_ev, stop_event):
        return
    img_client, _ = get_client(shared=False)
    try:
        img_url = img.download_url
        img_name = f"{i + 1:05d}.webp"
        img_path = photo_dir / img_name
        # GIF 不加扰：不传 scramble_id（不切片）；_download_image_single_attempt 原样取回后存成动画 WebP
        scramble_id = int(img.scramble_id) if img.scramble_id and not image_is_gif(img) else None
        _download_image_single_attempt(
            img_client, job_id, album_id, album, "",
            photo, i, img, img_url, img_path, scramble_id,
            done_pages, total_pages, failed_pages,
            pending_images if pass_num == 1 else None,
            tracker, photo_dir, pass_num=pass_num, timeout=timeout, lock=lock, refetch_gif=refetch_gif,
            **({"stop_event": stop_event} if stop_event is not None else {}),
        )
    finally:
        close_client(img_client)


def download_album_job(job_id: str, album_id: str, photo_ids: list[str], *, stop_event=None):
    """
    在后台线程中执行下载任务 — 两趟式：首趟快跳，末趟重试。

    Runtime shutdown cooperatively stops between requests and before publishing
    new content. In-flight SDK calls keep their transport until they actually end;
    the manager reports an undrained shutdown rather than closing it early.
    """
    if stop_event is not None and stop_event.is_set():
        return
    tracker = progress_manager.get_tracker(job_id)
    if not tracker:
        log.error(f"下载任务无法获取 tracker，标记为失败 job_id={job_id}")
        finish_download(job_id, "failed", error_message="tracker 不可用，任务初始化失败")
        return

    failed_pages = []
    pending_images = []
    job_lock = threading.Lock()  # 每个任务独立锁，消除跨任务锁争用
    log.info(f"开始下载任务 job_id={job_id} album_id={album_id} photo_count={len(photo_ids)}")

    client = None  # 在 finally 中按需关闭
    pause_ev = None
    written_dirs = []  # 这次写过的目录：结束时（无论成败）让“本地可读”重新判断

    try:
        if _should_stop(job_id, album_id, tracker, pause_ev, stop_event):
            return
        db.update_wishlist_download_status(album_id, "downloading")
        tracker.push("progress", {
            "job_id": job_id, "status": "running",
            "done_pages": 0, "total_pages": 0, "progress": 0,
            "message": "正在获取漫画信息...",
        })

        client, _ = get_client(shared=False)
        with download_budget(client, _operation_timeout(), stop_event=stop_event,
                             should_cancel=lambda: _download_canceled(job_id)):
            album = client.get_album_detail(album_id)
        if _should_stop(job_id, album_id, tracker, pause_ev, stop_event):
            return

        total_pages = 0
        photos_to_download = []
        seen_photo_ids = set()
        for photo in album:
            if _should_stop(job_id, album_id, tracker, pause_ev, stop_event):
                return
            if str(photo.photo_id) in photo_ids or not photo_ids:
                if str(photo.photo_id) in seen_photo_ids:
                    continue
                seen_photo_ids.add(str(photo.photo_id))
                with download_budget(client, _operation_timeout(), stop_event=stop_event,
                                     should_cancel=lambda: _download_canceled(job_id)):
                    client.check_photo(photo)
                if _should_stop(job_id, album_id, tracker, pause_ev, stop_event):
                    return
                pages = len(photo) if hasattr(photo, "__len__") and photo.page_arr else 0
                total_pages += pages
                photos_to_download.append(photo)

        if not photos_to_download or total_pages <= 0:
            raise ValueError("选中的章节不存在或没有可下载图片")

        # 检查新章节的基线（core/update_store.py）：用这次已经取到的完整章节列表，不多发请求。
        # 本地原来有没有能读的内容，必须在这个任务写任何文件之前判断。记录失败只写日志，绝不影响下载
        try:
            update_store.note_download_started(album_id, update_store.episodes_of(album), is_readable(album_id))
        except Exception as e:
            log.warning(f"记录检查新章节的基线失败（不影响下载）album_id={album_id} error={e}")

        dl_root = str(DOWNLOAD_ROOT)
        os.makedirs(dl_root, exist_ok=True)

        album_dir = Path(dl_root) / f"{_safe_dirname(album.name)}_{album_id}"
        if photo_ids:
            # 列出了章节的任务（新章节、详情页选中的或“下载全部”发来的全部章节 id）：写进阅读器现在打开的目录，
            # 不按上游现在的名字另建。上游改了标题、按作者整理过、旧版本的目录名时，另建的目录里只有这次的章节，
            # 而阅读器只打开最新的目录，更早的章节就看不到了。photo_ids 为 [] 的整部下载照旧按上游现在的名字建目录
            existing = _reusable_album_dir(album_id)
            if existing is not None:
                if not _same_dir(existing, album_dir):
                    log.info(f"部分章节写进已有的下载目录 job_id={job_id} album_id={album_id} dir={existing}")
                album_dir = existing
        if not is_safe_path(album_dir):
            raise ValueError("专辑输出路径越权")
        # 目录不在（被删除了）或只剩没有图片的空壳（例如“打开文件夹”替排队中的重试建出的）：更早完成、写到这里的
        # 任务，它们的文件已经不在了。这次下载会把目录重新建出来——标记那些任务被取代，这次若失败或被取消，
        # 残缺的几页不能让它们重新显示成完整、可离线阅读（core.local_availability）。
        # 只剩能读的压缩包（自动打包后删了原图）不算空壳：那次下载的内容还在
        recreated = not has_local_pages(album_dir)
        album_dir.mkdir(parents=True, exist_ok=True)
        forget_local_state(album_dir)  # 马上要往里写：各页面下次重新判断，不等有效期
        written_dirs.append(album_dir)
        output_path = str(album_dir)

        db.update_job(job_id, total_pages=total_pages, output_path=output_path)
        if recreated:
            superseded = db.mark_completed_jobs_superseded(album_id, output_path, job_id)
            if superseded:
                log.info(f"下载目录已重新建出，更早完成的任务不再算本地可读 job_id={job_id} album_id={album_id} "
                         f"count={superseded}")
        tracker.push("progress", {
            "job_id": job_id, "status": "running",
            "done_pages": 0, "total_pages": total_pages, "progress": 0,
            "message": f"开始下载，共 {total_pages} 页",
        })

        # 用列表包裹 done_pages 以便在函数中修改
        done_pages = [0]

        # 检查暂停 — 使用 Event 等待（不轮询 DB，减少压力）
        from .job_manager import job_manager as _jm
        pause_ev = _jm.get_pause_event(job_id)
        # 预存暂停时的进度，避免推送 0/0 给前端
        _paused_done = 0
        _paused_total = 0
        _lock = job_lock  # 每个任务独立锁，传递到下载函数

        # ── 首趟：章节级并行，每章独立线程 ──
        photo_threads = int(get_settings().get("photo_threads", "3"))
        log.info(f"章节并行下载 threads={photo_threads} chapters={len(photos_to_download)} job_id={job_id}")

        with ThreadPoolExecutor(max_workers=photo_threads) as pool:
            futs = []
            for photo in photos_to_download:
                fut = pool.submit(
                    bind_request_id(_download_chapter),
                    job_id, album_id, album, album_dir, photo, total_pages,
                    done_pages, pending_images, failed_pages,
                    tracker, pause_ev, _lock, 1, _FIRST_PASS_TIMEOUT,
                    **({"stop_event": stop_event} if stop_event is not None else {}),
                )
                futs.append(fut)
            # 等待所有章节完成，不阻塞 cancel（每个 chapter 线程内检查 _should_stop）
            for fut in as_completed(futs):
                if _should_stop(job_id, album_id, tracker, pause_ev, stop_event):
                    for pending in futs:
                        pending.cancel()
                    break
                try:
                    fut.result()
                except Exception as e:
                    log.warning(f"章节下载线程异常 job_id={job_id} error={e}")

        # ── 第二趟：串行重试 pending 图片（量少，无需并行）──
        if _should_stop(job_id, album_id, tracker, pause_ev, stop_event):
            return
        if pending_images:
            log.info(f"开始重试 {len(pending_images)} 张未下载的图片 job_id={job_id}")
            tracker.push("progress", {
                "job_id": job_id, "status": "running",
                "done_pages": done_pages[0], "total_pages": total_pages,
                "progress": round(done_pages[0] / total_pages * 100, 1) if total_pages > 0 else 0,
                "message": f"正在重试 {len(pending_images)} 张未下载的图片...",
            })
            # 第二趟用独立 client（章节线程的 client 已关闭）
            retry_client, _ = get_client(shared=False)
            should_exit = False
            try:
                for pinfo in pending_images:
                    if _should_stop(job_id, album_id, tracker, pause_ev, stop_event):
                        should_exit = True
                        break
                    _download_image_single_attempt(
                        retry_client, job_id, album_id, album, output_path,
                        pinfo["photo"], pinfo["idx"], pinfo["img"],
                        pinfo["img_url"], pinfo["img_path"], pinfo["scramble_id"],
                        done_pages, total_pages, failed_pages, None,
                        tracker, pinfo["photo_dir"], pass_num=2, timeout=_RETRY_TIMEOUT,
                        lock=job_lock,
                        **({"stop_event": stop_event} if stop_event is not None else {}),
                    )
            finally:
                close_client(retry_client)
                if should_exit:
                    # 注意：不在这里 close_client(client)，外层 finally 会统一关闭
                    tracker.close()
                    return

        now = datetime.now().isoformat()

        # Futures can fail before recording a page error. Do not pack partial data.
        if _should_stop(job_id, album_id, tracker, pause_ev, stop_event):
            return
        if done_pages[0] != total_pages:
            failed_pages.append(f"下载不完整：{done_pages[0]}/{total_pages} 页")
        if not failed_pages:
            # 每一页都已按当前格式写好（旧目录里的 GIF 页已重新取回）：升级章节标记，以后不再重取
            for photo in photos_to_download:
                _mark_chapter_current(album_dir, photo)

        # -- 以下为 CBZ 打包、元数据写入、完成标记等（不变）--
        organize_mode = get_settings().get("organize_mode", "none")
        if (not failed_pages and organize_mode == "by_author"
                and not _same_dir(Path(output_path).parent, dl_root)):
            # 写进的是已经按作者整理过的目录（不在下载目录第一层）：不再移动，否则会套进 <作者>/<作者>/ 里。
            # 扁平化只在目录里面挪文件，照常做
            log.info(f"下载目录已按作者整理过，不再移动 job_id={job_id} path={output_path}")
        elif not failed_pages and organize_mode and organize_mode != "none":
            new_path = organize_download(output_path, organize_mode, album)
            if new_path:
                output_path = new_path
                db.update_job(job_id, output_path=output_path)
                log.info(f"整理后 output_path 已更新: {output_path}")

        settings = get_settings()
        if not failed_pages and settings.get("auto_pack") == "true":
            tracker.push("archiving", {
                "job_id": job_id, "status": "archiving",
                "message": "正在打包 CBZ...",
            })
            packer = CbzPacker()
            output_dir = Path(output_path)
            suffix = ".zip" if settings.get("pack_format") == "zip" else packer.extension()
            # Preserve a valid output directory for the library and open-folder.
            cbz_path = output_dir / f"{output_dir.name}{suffix}"
            try:
                # 只删真正打进包里的原图（空的、过大的图片不打包，也不删）
                originals = {
                    path: (path.stat().st_size, path.stat().st_mtime_ns)
                    for path in CbzPacker.packable_images(output_dir)
                }
                written_dirs.append(output_dir)
                # 目录里已有的压缩包（同名的会被新包覆盖；另一种格式的、别的 .cbz 不动，但新包优先，它们会被“挡住”）：
                # 它们里面散图没有的页都放进新包，一页不丢。有一个暂时打不开、或有带不过来的页，
                # 或者要被覆盖的那个打不开：不打包（原图也不删），那些页可能只存在于旧包里
                bases = []
                for path in archive_pages.candidates(output_dir):
                    index = archive_pages.read_index(path)
                    replaced = path.name.lower() == cbz_path.name.lower()
                    if index.transient or index.skipped or (replaced and index.status == "corrupt"):
                        raise ValueError(f"旧压缩包 {path.name} 暂时打不开或有读不了的页（{index.status}，"
                                         f"跳过 {index.skipped} 页），没有打包，原图保留")
                    if index.status in ("ok", "empty"):
                        bases.append(index)
                packer.pack(output_dir, cbz_path, base=bases)
                log.info(f"CBZ 打包完成: {cbz_path}")
                # 删原图前核对新包：能打开、没有跳过的页、页数对得上；不对就保留原图
                check = archive_pages.read_index(cbz_path)
                if check.status not in ("ok", "empty") or check.skipped or len(check.pages) != packer.packed_count:
                    raise ValueError(f"新压缩包 {cbz_path.name} 核对不通过（{check.status}，{len(check.pages)}/"
                                     f"{packer.packed_count} 页），原图保留")
                if settings.get("delete_originals") == "true":
                    if not is_safe_path(output_dir):
                        log.error(f"delete_originals 安全校验失败，拒绝删除: {output_dir}")
                    else:
                        if _should_stop(job_id, album_id, tracker, pause_ev, stop_event):
                            return
                        # Delete only packed unchanged images, never the directory.
                        for path, signature in originals.items():
                            if stop_event is not None and stop_event.is_set():
                                return
                            if (is_safe_path(path) and path.is_file()
                                    and (path.stat().st_size, path.stat().st_mtime_ns) == signature):
                                path.unlink()
                        log.info(f"已清理打包原图，保留归档及其他文件: {output_dir}")
            except Exception as e:
                log.warning(f"CBZ 打包失败（不影响下载完成状态）job_id={job_id} error={e}")
            finally:
                forget_local_state(output_dir)

        try:
            db.upsert_album_meta(album_id, title=album.name, author=album.author,
                                cover_url=JmcomicText.get_album_cover_url(album_id))
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

        if _should_stop(job_id, album_id, tracker, pause_ev, stop_event):
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
                err_all += f" ...（共 {len(failed_pages)} 页失败）"
            log.warning(f"下载任务部分失败 job_id={job_id} failed_pages={len(failed_pages)} total_pages={total_pages}")
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
                                 photo_ids=[str(p.photo_id) for p in photos_to_download])
            if not ok:
                log.warning(f"transition_job_status 失败，状态可能已被取消 job_id={job_id}")
            if ok:
                tracker.push("completed", {
                    "job_id": job_id, "status": "completed",
                    "output_path": output_path,
                    "done_pages": done_val, "total_pages": total_pages,
                })

        tracker.close()

    except DownloadInterrupted:
        return
    except Exception as e:
        if stop_event is not None and stop_event.is_set():
            log.info(f"停机期间上游调用结束，保留任务供恢复 job_id={job_id} error={type(e).__name__}")
            return
        err_msg = str(e)[:1000]
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
