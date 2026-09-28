"""
jmcomic 服务封装模块
使用 build_jmcomic_option() 确保设置真实生效。
"""
import hashlib
import os
import re
import tempfile
import threading
import time
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
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
from .validation import is_allowed_image, safe_dirname as _safe_dirname
from .path_guard import DOWNLOAD_ROOT, is_safe_path
from .packer import CbzPacker
from .file_tree import is_link
from . import archive_pages
from . import update_store
from .local_availability import forget as forget_local_state, has_local_pages, is_readable, readable_folder


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


def _get_or_create_option():
    """获取缓存的 JmOption 实例（模块级单例，惰性创建，settings 缓存失效时自动重建）"""
    global _global_option
    if _global_option is None:
        with _option_lock:
            if _global_option is None:
                opt_dict = build_jmcomic_option()
                _global_option = JmModuleConfig.option_class().construct(opt_dict)
    return _global_option


def invalidate_option_cache():
    """Retire the old shared client; close it only after its last request releases it."""
    global _global_option, _global_client, _option_generation
    with _option_lock:
        previous = _global_client
        _option_generation += 1
        _global_option = None
        _global_client = None
        if previous is not None:
            previous._jm_retired = True
            if getattr(previous, "_jm_users", 0) == 0:
                _dispose_client(previous)


def get_client(shared=True):
    """Acquire a client. Every acquisition must be balanced by close_client()."""
    global _global_client, _active_independent_clients
    with _option_lock:
        option = _get_or_create_option()
        if shared:
            if _global_client is None:
                _global_client = option.new_jm_client()
                setattr(_global_client, _JM_SHARED_ATTR, True)
                _global_client._jm_users = 0
                _global_client._jm_retired = False
            _global_client._jm_users += 1
            return _global_client, option
        client = option.new_jm_client()
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
        return option.new_jm_client(**overrides)


def new_check_client(timeout: int):
    """检查新章节（core/update_checker.py）专用的 client：每次检查新建一个，用完 close_client() 关闭。

    与下载同一份 option（代理、impersonate、client 类型都一样），另外三点：
      - 不带缓存（cache=False 且 set_cache_dict(None)）：每次都向上游要最新的章节列表；
      - retry_times = 0：第一次出错就抛出，换哪个域名由检查自己决定（每个域名最多问一次）；
      - timeout 覆盖 curl 的总超时（调用方已限制在 20 秒以内）。
    计入 get_active_client_count()。"""
    global _active_independent_clients
    with _option_lock:
        client = _get_or_create_option().new_jm_client(cache=False, timeout=timeout)
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
        return
    setattr(client, _JM_CLOSED_ATTR, True)
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


def close_client(client) -> None:
    global _active_independent_clients
    if client is None:
        return
    with _option_lock:
        if getattr(client, _JM_SHARED_ATTR, False):
            client._jm_users = max(0, getattr(client, "_jm_users", 0) - 1)
            if getattr(client, "_jm_retired", False) and client._jm_users == 0:
                _dispose_client(client)
            return
        if getattr(client, _JM_CLOSED_ATTR, False):
            return
        _dispose_client(client)
        if getattr(client, "_jm_counted", False):
            with _clients_lock:
                _active_independent_clients -= 1



_FIRST_PASS_TIMEOUT = 120
_RETRY_TIMEOUT = 600  # 末趟重试 10 分钟
# 并行下载共享状态锁（保护 done_pages / failed_pages / pending_images）
# 注意：_download_lock 已弃用，改用每个任务独立锁 job_lock 以消除跨任务争用。
# 保留模块级锁仅用于模块级共享资源（如 client 创建），目前无需模块级锁。
_download_lock = threading.Lock()


def _should_stop(job_id: str, album_id: str, tracker, pause_ev) -> bool:
    """检查下载是否应停止（取消/删除），返回 True 表示应停止。"""
    job = db.get_job(job_id)
    if job is None:
        log.warning(f"下载中的任务已被删除 job_id={job_id}")
        db.update_wishlist_download_status(album_id, "none")
        if tracker:
            tracker.push("progress", {
                "job_id": job_id, "status": "canceled",
                "message": "任务已被删除",
            })
        return True
    if job["status"] == "canceled":
        log.info(f"下载任务已取消 job_id={job_id}")
        db.update_wishlist_download_status(album_id, "none")
        if tracker:
            tracker.push("progress", {
                "job_id": job_id, "status": "canceled",
                "message": "任务已被取消",
            })
        return True
    # 暂停等待 — 保留已下载进度，不推送 0/0
    while job and job["status"] == "paused":
        log.debug(f"下载任务暂停中 job_id={job_id}")
        if pause_ev:
            pause_ev.wait(timeout=3)
        else:
            time.sleep(3)  # pause_ev 未传入时的兜底
        job = db.get_job(job_id)
        if job is None or job["status"] == "canceled":
            db.update_wishlist_download_status(album_id, "none")
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
    tracker, photo_dir, pass_num, timeout, lock, refetch_gif=False, flat_pages=frozenset(),
):
    """单张图片单次下载尝试。
    成功 → done_pages +1
    超时且是首趟 → 记录到 pending_images
    失败且是末趟 → 记录到 failed_pages
    使用传入的每任务锁 lock，避免跨任务争用模块级 _download_lock。
    GIF 页（image_is_gif）不切片，保存为保留全部帧的动画 WebP（文件名仍是 NNNNN.webp）。
    """
    img_name = f"{idx + 1:05d}.webp"
    img_path = photo_dir / img_name if not str(img_path).startswith(str(photo_dir)) else img_path
    if not is_safe_path(img_path) or not is_safe_path(photo_dir):
        raise ValueError("图片输出路径越权")

    gif = image_is_gif(img)
    skip = get_settings().get("skip_existing", "true") == "true"
    # refetch_gif：章节目录是旧版本下载的（GIF 页被切片/只剩第一帧），GIF 页不能按“已存在”跳过。
    # 已存在：章节目录里的这一页是有效图片，或者章节目录里没有这一页、而它已按本版本起的页名扁平化在漫画目录里
    # （flat_pages：_FlatPages.numbers，散图或压缩包里）——否则重新下载会把扁平化过的页再下一遍，整理后同一页出现两次。
    # 章节目录里这一页坏了（打不开）时重新下载：新页整理时替换扁平页，坏页永远不会替换好页
    if (skip and pass_num == 1 and not (gif and refetch_gif)
            and (_valid_image(img_path)
                 or (img_path.stem in flat_pages and not os.path.lexists(img_path)))):
        with lock:
            done_pages[0] += 1
        if tracker:
            tracker.push("progress", {
                "job_id": job_id, "status": "running",
                "done_pages": done_pages[0] if done_pages else 0,
                "total_pages": total_pages or 0,
                "progress": round((done_pages[0] if done_pages else 0) / (total_pages or 1) * 100, 1),
            })
        return

    tmp_path = raw_gif = None
    try:
        # Never overwrite a previous valid image before a replacement is verified.
        fd, name = tempfile.mkstemp(prefix=img_path.stem + ".", suffix=img_path.suffix, dir=photo_dir)
        os.close(fd)
        tmp_path = Path(name)
        if gif:
            # GIF 页：先按 .gif 原样保存（不解码、不切片；扩展名相同 jmcomic 就不经 PIL 转换），
            # 再由 _save_gif_as_webp 把全部帧写成动画 WebP。直接让 jmcomic 存成 .webp 只会留下第一帧。
            fd, name = tempfile.mkstemp(prefix=img_path.stem + ".", suffix=".gif", dir=photo_dir)
            os.close(fd)
            raw_gif = Path(name)
            client.download_image(
                img_url=img_url,
                img_save_path=str(raw_gif),
                scramble_id=None,
                decode_image=False,
            )
            _save_gif_as_webp(raw_gif, tmp_path)
        else:
            client.download_image(
                img_url=img_url,
                img_save_path=str(tmp_path),
                scramble_id=scramble_id,
                decode_image=True,
            )
        # 下载成功但文件可能为空（jmcomic 静默失败），校验后纠正
        if not _valid_image(tmp_path):
            raise IOError("下载结果不是完整有效的图片")
        os.replace(tmp_path, img_path)
        # 确认下载成功后计数
        with lock:
            done_pages[0] += 1
    except Exception as e:
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        if pass_num == 1 and pending_images is not None:
            # 首趟：只存位置信息，不存 client（线程退出后 client 会关闭）
            with lock:
                pending_images.append({
                    "photo": photo, "idx": idx,
                    "img": img, "img_url": img_url, "img_path": img_path,
                    "scramble_id": scramble_id, "photo_dir": photo_dir,
                })
            log.warning(
                f"首趟下载超时，已跳过待重试 job_id={job_id} "
                f"pass={pass_num} img={idx + 1} error={e}"
            )
        else:
            with lock:
                err_msg = f"第 {idx+1} 页下载失败: {e}"
                log.warning(f"图片下载最终失败 job_id={job_id} error={err_msg}")
                failed_pages.append(err_msg)
                db.update_job(job_id, error_message=err_msg[:500])
    finally:
        if raw_gif is not None:
            try:
                raw_gif.unlink(missing_ok=True)
            except OSError:
                pass

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
    try:
        from PIL import Image
        with Image.open(path) as image:
            image.verify()
        return True
    except (OSError, ValueError, SyntaxError):
        return False


def _submit_client_call(pool, client, fn, *args, **kwargs):
    """Keep a shared transport leased until the future actually finishes/cancels.
    The worker thread inherits the caller's request_id, so jmcomic's retry/failure records join its trace."""
    shared = client is not None and getattr(client, _JM_SHARED_ATTR, False)
    if shared:
        with _option_lock:
            client._jm_users += 1
    try:
        future = pool.submit(bind_request_id(fn), *args, **kwargs)
    except Exception:
        if shared:
            close_client(client)
        raise
    if shared:
        future.add_done_callback(lambda _: close_client(client))
    return future


def _call_with_timeout(label: str, timeout: float, timeout_msg: str, fn, *args, **kwargs):
    """在独立线程中执行 jmcomic 远程调用，带超时保护。

    统一取代散落各处的 `ThreadPoolExecutor(1) + fut.result(timeout=...)` 样板：
    超时或调用异常时记录日志并抛 TimeoutError(timeout_msg)，
    确保 Waitress 请求线程不会被网络调用永久挂起。
    """
    pool = ThreadPoolExecutor(max_workers=1)
    fut = _submit_client_call(pool, getattr(fn, "__self__", None), fn, *args, **kwargs)
    try:
        result = fut.result(timeout=timeout)
    except Exception as e:
        # 超时的 TimeoutError() 没有文字，带上类型才看得出是超时还是上游报错
        log.warning(f"{label} 超时或失败 error={type(e).__name__}: {e}")
        pool.shutdown(wait=False)  # 不阻塞请求线程；挂起的工作线程由其自身超时终结
        raise TimeoutError(timeout_msg)
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
            f"搜索 keyword={keyword}", 25, "搜索超时，请检查关键词或稍后重试",
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

    check_photo 原地更新 photo；失败或超时的章节保留原样（page_arr 为空，页数按 0 计）。
    传入 failed 列表时，把这些没取到的章节追加进去（调用方据此区分“网络失败”和“确实没有页面”）。
    详情页与在线阅读共用。
    """
    photo_list = list(album)
    # 预先设置 from_album，避免 check_photo 内部重复调用 get_album_detail
    for photo in photo_list:
        if getattr(photo, 'from_album', None) is None:
            photo.from_album = album
    # 已有 page_arr 的章节直接跳过 check
    pending = [photo for photo in photo_list if not getattr(photo, 'page_arr', None)]
    if not pending:
        return photo_list
    # 独立线程池 + 手动 shutdown(wait=False)：
    # 不能用 with 语法 —— __exit__ 会 shutdown(wait=True)，
    # 阻塞等待挂起的 check_photo，让超时保护形同虚设。
    check_pool = ThreadPoolExecutor(max_workers=min(len(pending), 10))
    fut_map = {_submit_client_call(check_pool, client, client.check_photo, photo): photo for photo in pending}
    try:
        for fut in as_completed(fut_map, timeout=timeout):
            try:
                fut.result()
            except Exception as e:
                log.warning(f"获取 photo 详情失败 photo_id={fut_map[fut].photo_id} error={type(e).__name__}: {e}")
    except TimeoutError:
        unfinished = sum(1 for fut in fut_map if not fut.done())
        log.warning(f"check_photo 超时（{timeout}s），album_id={getattr(album, 'album_id', '?')}，"
                    f"已跳过 {unfinished} 个未完成章节")
    finally:
        check_pool.shutdown(wait=False, cancel_futures=True)
    if failed is not None:
        failed.extend(photo for fut, photo in fut_map.items()
                      if not fut.done() or fut.cancelled() or fut.exception() is not None)
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

        photos = []
        for p in check_album_photos(client, album):
            page_count = len(p) if hasattr(p, '__len__') and hasattr(p, 'page_arr') and p.page_arr else 0
            photos.append({
                "photo_id": str(p.photo_id),
                "title": _safe_str(p.name) if hasattr(p, "name") and p.name else "-",
                "page_count": page_count,
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
            if cached and ttl > 0 and now - cached["ts"] < min(300, ttl):
                _detail_cache.move_to_end(key)
                return deepcopy(cached["data"])
        stored = db.get_cached_album_detail(album_id, ttl=ttl) if ttl > 0 else None
        if stored:
            data = stored["detail"]
            if stored["cover_cdn_url"]:
                data["cover"] = stored["cover_cdn_url"]
            timestamp = stored["cached_at"]
        else:
            data = get_album_detail(album_id)
            timestamp = time.time()
        with _detail_cache_lock:
            if generation == _detail_cache_generation:
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


# Windows 上文件 / 目录（或目录里的文件）正被别的程序打开且没允许删除共享时（杀毒软件扫描刚写好的页、资源管理器的
# 缩略图 / 预览、索引、看图软件、阅读器正在发送的页），改名会失败（PermissionError，WinError 5 / 32）。
# 这些程序通常很快放手：短暂重试几次，总共最多等约 2.6 秒
_LOCK_RETRY_DELAYS = (0.1, 0.25, 0.5, 0.75, 1.0)


def _looks_locked(error: OSError) -> bool:
    return isinstance(error, PermissionError) or getattr(error, "winerror", None) in (5, 32)


def _retry_on_lock(operation, *args) -> None:
    """operation(*args)；看起来是被占用（_looks_locked）就按 _LOCK_RETRY_DELAYS 短暂重试。
    最后仍失败时抛出最后的 OSError（跨磁盘 EXDEV 之类不是占用的错误不重试）"""
    for delay in (*_LOCK_RETRY_DELAYS, None):
        try:
            operation(*args)
            return
        except OSError as e:
            if delay is None or not _looks_locked(e):
                raise
            time.sleep(delay)


def _rename_with_retry(src, dst, replace: bool = False) -> None:
    """os.rename（目标已存在时失败）或 replace=True 时 os.replace（原子替换已有文件）；被占用时短暂重试。
    从不复制再删除"""
    _retry_on_lock(os.replace if replace else os.rename, src, dst)


# 扁平化整理挪到漫画目录下的图片后缀
_FLAT_IMAGE_SUFFIXES = (".webp", ".jpg", ".jpeg", ".png", ".gif", ".bmp")
# 章节目录里一页的文件名（不含后缀）就是它的页号 NNNNN（_download_image_single_attempt 写的 00001.webp …）
_PAGE_NUMBER = re.compile(r"[0-9]{5}")
# 扁平页名去掉“<前缀>_”之后（不含后缀）：本版本起写的 p<页号>；旧版本写的是 5 位的累加序号
_FLAT_NEW_REST = re.compile(r"p([0-9]{5})")
_FLAT_LEGACY_REST = re.compile(r"[0-9]{5}")


def _flat_prefix(chapter_dir_name: str) -> str:
    """扁平化后页名的章节前缀：章节目录名，空格换成 _。整理（organize_download）和 skip_existing 共用这一条规则"""
    return chapter_dir_name.replace(" ", "_")


def _flat_page_name(chapter_dir_name: str, page_name: str) -> str:
    """章节目录里的一张图扁平化后的名字。页（NNNNN.<后缀>，NNNNN 是页号）→ <前缀>_pNNNNN.<后缀>，
    如 第1话__71_p00001.webp：同一章的同一页永远是同一个名字。别的图片 → <前缀>_x<原文件名>。
    旧版本的扁平页名是 <前缀>_NNNNN.<后缀>，NNNNN 是那次整理全程累加的序号、不是页号（第2话__72_00004 可能是
    第 2 话的第 1 页）：这里起的名字（p / x 开头）永远不会和它们重名，旧文件永远不会被当成别的页、被覆盖"""
    stem, suffix = os.path.splitext(page_name)
    tag = "p" if _PAGE_NUMBER.fullmatch(stem) else "x"
    return f"{_flat_prefix(chapter_dir_name)}_{tag}{stem}{suffix}"


class _AlbumRoot:
    """漫画目录第一层，列一次目录、首趟各章共用（_FlatPages）：不再每一章把整个目录和它的压缩包重新列一遍——
    扁平化过的长篇漫画第一层有上万个文件，每章都列一遍、逐个算 page_key 要好几分钟。首趟期间第一层不会变
    （页写进章节目录，扁平化和打包都在首趟之后），所以一个任务用同一份：
      entries：章节前缀 → 第一层的条目（os.DirEntry），按名字去掉最后一个“_”及其后的部分分组：扁平页名
               <前缀>_pNNNNN.<后缀> 和旧页名 <前缀>_NNNNN.<后缀> 这样分组正好落在 <前缀> 下
      keys：第一层每个条目的 page_key（不管是不是有效图片）：压缩包里的同一页被它挡住
      archives：第一层可用的压缩包（archive_pages.candidates，按优先顺序）
    压缩包仍是每章按 archive_pages.select 的规则选一次（archive_pages.select_from：没变的压缩包直接用记住的页目录，
    暂时打不开的后面的章节再试）；选中的压缩包第一层的页按同样的前缀分组，同一个压缩包（签名不变）只分一次"""

    def __init__(self, album_dir):
        self.album_dir = Path(album_dir)
        self.entries: dict[str, list] = {}
        self.keys: set[str] = set()
        try:
            with os.scandir(self.album_dir) as found:
                for entry in found:
                    self.keys.add(archive_pages.page_key(entry.name))
                    self.entries.setdefault(entry.name.rsplit("_", 1)[0], []).append(entry)
        except OSError:
            pass  # 列不出目录：什么都不算已存在（照常下载，不会少页）
        try:
            self.archives = archive_pages.candidates(self.album_dir)
        except Exception as e:
            log.warning(f"列出漫画目录里的压缩包失败（按没有压缩包处理）dir={self.album_dir} error={e}")
            self.archives = []
        self._grouped: dict = {}
        self._lock = threading.Lock()

    def archive(self):
        """该用的压缩包（archive_pages.select 的规则，用列好的候选）"""
        return archive_pages.select_from(self.archives)

    def archived(self, index, prefix: str):
        """压缩包 index 第一层里、按 entries 的规则分组落在 prefix 下的阅读器页"""
        key = (index.path, index.signature)
        with self._lock:
            groups = self._grouped.get(key)
            if groups is None:
                groups = {}
                for page in index.reader_pages:
                    if not page.chapter:  # 按章节目录存的页不算（不整理时的布局，不认）
                        groups.setdefault(page.name.rsplit("_", 1)[0], []).append(page)
                self._grouped[key] = groups
        return groups.get(prefix, ())


class _FlatPages:
    """一章已经扁平化在漫画目录里的页（skip_existing 用；不论现在选的是哪种整理方式——用户可能换过）。
    散图只算漫画目录第一层的普通文件（不是链接、在下载目录里、有效图片）；压缩包是阅读器用的那个
    （archive_pages.select 的规则，能读、不是暂时打不开），只算它第一层的页，而且只算漫画目录里没有同一页
    （page_key 相同）散图的——有这样的散图（哪怕是坏的、空的）时阅读器和自动打包都用散图，这一页只看散图：
    散图坏了就重新下载、替换它，否则自动打包会拿坏散图覆盖压缩包里的好页（章节目录里的页也是同一规则）。
    root：首趟各章共用的 _AlbumRoot（不传时自己列一次）；只看这一章前缀下的条目。
      numbers：本版本起的页名 <前缀>_pNNNNN 认出的页号 NNNNN（散图须是 .webp、有效图片，与章节目录里的页同一规则）
      new_any：有没有任何一张本版本起的扁平页（压缩包里的，或散图——坏的也算：这一章就不按旧页名整章认，
               坏散图这一页重新下载、替换它，不会因为整章算已存在而永远留着）
      legacy：旧版本页名 <前缀>_NNNNN（累加序号）的页，按 page_key 去重——散图和压缩包里的同一页只算一次，
              与阅读器（archive_pages.merge_pages）一致"""

    def __init__(self, photo_dir, root: _AlbumRoot | None = None):
        self.numbers: set[str] = set()
        self.new_any = False
        self.legacy: set[str] = set()
        photo_dir = Path(photo_dir)
        if root is None:
            root = _AlbumRoot(photo_dir.parent)
        prefix = _flat_prefix(photo_dir.name)
        head = prefix + "_"
        for entry in root.entries.get(prefix, ()):
            kind, value = self._classify(head, entry.name)
            if kind is None:
                continue
            path = Path(entry.path)
            try:
                if not entry.is_file(follow_symlinks=False) or is_link(path) or not is_safe_path(path):
                    continue
                if kind == "new":
                    self.new_any = True
                if not _valid_image(path):
                    continue
            except OSError:
                continue
            if kind == "new":
                if entry.name.lower().endswith(".webp"):
                    self.numbers.add(value)
            else:
                self.legacy.add(value)
        try:
            index = root.archive()
        except Exception as e:
            log.warning(f"读取漫画目录里的压缩包失败（按没有压缩包处理）dir={root.album_dir} error={e}")
            index = None
        if index is not None and index.status == "ok" and not index.transient:
            for page in root.archived(index, prefix):
                kind, value = self._classify(head, page.name)
                if kind == "new":
                    self.new_any = True
                    if archive_pages.page_key(page.name) not in root.keys:
                        self.numbers.add(value)
                elif kind == "legacy" and value not in root.keys:
                    self.legacy.add(value)

    @staticmethod
    def _classify(head: str, name: str):
        """('new', 页号) / ('legacy', page_key) / (None, None)"""
        if not name.startswith(head):
            return None, None
        rest, suffix = os.path.splitext(name[len(head):])
        if suffix.lower() not in _FLAT_IMAGE_SUFFIXES:
            return None, None
        match = _FLAT_NEW_REST.fullmatch(rest)
        if match:
            return "new", match.group(1)
        if _FLAT_LEGACY_REST.fullmatch(rest):
            return "legacy", archive_pages.page_key(name)
        return None, None


def _has_own_page(photo_dir) -> bool:
    """章节目录里有没有这一章自己的页：一张有效图片，或者一个页文件 NNNNN.webp（下载写的名字）——哪怕它坏了、
    是空的：坏页要逐页重新下载、替换掉，不能因为整章按旧页名算已存在而永远留着（扁平化不挪坏图）。
    别的坏图片（如中断留下的临时文件 00001.xxxx.webp）不算：重新下载也替换不了它"""
    try:
        for path in Path(photo_dir).iterdir():
            suffix = path.suffix.lower()
            if suffix not in _FLAT_IMAGE_SUFFIXES or not path.is_file():
                continue
            if (suffix == ".webp" and _PAGE_NUMBER.fullmatch(path.stem)) or _valid_image(path):
                return True
    except OSError:
        return True  # 看不清：按有处理（不整章跳过，逐页照常判断）
    return False


def _legacy_flattened_chapter(photo_dir, pages: int, flat: _FlatPages) -> bool:
    """这一章是否已经由旧版本整章扁平化在漫画目录里（旧页名是那次整理全程累加的序号，不是本章的页号，按页认不出来，
    也永远不按页用）：没有本版本起的扁平页，章节目录里没有这一章自己的页（_has_own_page），而旧页名的页
    （散图 ∪ 压缩包第一层，同一页只算一次）正好 pages 张。多一张少一张都不算——逐页照常判断，缺的页重新下载，
    和旧页逐字节相同的新页整理时丢掉（_LegacyFlatCopies），其余的成为新页名，旧文件原样保留"""
    if pages <= 0 or flat.new_any or len(flat.legacy) != pages:
        return False
    return not _has_own_page(photo_dir)


def _sha256_of_file(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class _LegacyFlatCopies:
    """扁平化去重用：一章旧版本扁平页（<前缀>_NNNNN，累加序号）内容的 sha256 → 张数（Counter）。每张旧页只顶
    一张新页（_take_legacy_copy 用掉一次）：同一章里两页内容相同（如两张空白页）、旧页只剩其中一张时，另一页不会被丢掉。
    散图：漫画目录第一层的普通文件（不是链接、在下载目录里、阅读器显示的后缀），读文件；
    压缩包：阅读器用的那个（archive_pages.select，能读、不是暂时打不开）第一层的页，用 archive_pages 的安全读取
    （校验 CRC 与大小，从不解压到磁盘），漫画目录里有同一页（page_key 相同）散图的不算——阅读器和自动打包都用
    散图、不用它。第一次问到某一章时才列目录、读压缩包；这一章没有旧页名的页就什么都不读。
    读不出来的页（被占用、坏了）、列不出的目录、打不开的压缩包都不算：新页照常扁平化，可能出现两次，一页不丢"""

    def __init__(self, album_dir):
        self.album_dir = Path(album_dir)
        self._root = None           # [(名字, 路径)]；None = 还没列
        self._index = None
        self._index_read = False
        self._digests: dict[str, Counter] = {}

    def digests(self, chapter_dir_name: str) -> Counter:
        """这一章还没被新页顶掉的旧页：sha256 → 张数（同一个 Counter，_take_legacy_copy 就地减）"""
        head = _flat_prefix(chapter_dir_name) + "_"
        if head not in self._digests:
            self._digests[head] = self._read(head)
        return self._digests[head]

    def _root_entries(self):
        if self._root is None:
            try:
                with os.scandir(self.album_dir) as entries:
                    self._root = [(entry.name, Path(entry.path)) for entry in entries]
            except OSError as e:
                log.warning(f"扁平化去重：列不出漫画目录（不去重）dir={self.album_dir} error={e}")
                self._root = False
        return self._root

    def _archive(self):
        if not self._index_read:
            self._index_read = True
            try:
                index = archive_pages.select(self.album_dir)
            except Exception as e:
                log.warning(f"扁平化去重：读取漫画目录里的压缩包失败（不用它去重）dir={self.album_dir} error={e}")
                index = None
            if index is not None and index.status == "ok" and not index.transient:
                self._index = index
        return self._index

    def _read(self, head: str) -> Counter:
        root = self._root_entries()
        if root is False:
            return Counter()
        loose = [(name, path) for name, path in root if _FlatPages._classify(head, name)[0] == "legacy"]
        index = self._archive()
        packed = []
        if index is not None:
            shadowed = {archive_pages.page_key(name) for name, _ in root}
            packed = [page for page in index.reader_pages
                      if not page.chapter and _FlatPages._classify(head, page.name)[0] == "legacy"
                      and archive_pages.page_key(page.name) not in shadowed]
        found: Counter = Counter()
        for name, path in loose:
            try:
                if (not is_allowed_image(os.path.splitext(name)[1]) or not path.is_file() or is_link(path)
                        or not is_safe_path(path)):
                    continue
                found[_sha256_of_file(path)] += 1
            except OSError as e:
                log.warning(f"扁平化去重：读不出旧扁平页（不用它去重）{path} error={e}")
        if packed:
            try:
                with archive_pages.open_pages(index) as read:
                    for page in packed:
                        try:
                            found[hashlib.sha256(read(page)).hexdigest()] += 1
                        except archive_pages.ArchiveError as e:
                            log.warning(f"扁平化去重：读不出压缩包里的旧扁平页（不用它去重）"
                                        f"{index.path.name}:{page.name} error={e}")
            except Exception as e:
                log.warning(f"扁平化去重：打不开压缩包（不用它去重）{index.path} error={e}")
        return found


def _take_legacy_copy(path, remaining: Counter) -> bool:
    """path 的内容和 remaining 里一张还没被顶掉的旧扁平页逐字节相同：用掉那一张（就地减一）并返回 True。
    读不出来 → 不算"""
    if not remaining:
        return False
    try:
        digest = _sha256_of_file(path)
    except OSError:
        return False
    if not remaining.get(digest):
        return False
    remaining[digest] -= 1
    if not remaining[digest]:
        del remaining[digest]
    return True


def organize_download(output_path: str, mode: str, album, report: dict | None = None) -> str | None:
    """整理下载目录结构。

    Args:
        output_path: 当前下载输出路径（漫画目录）
        mode: 整理模式 ("none", "by_author", "flat")
        album: jmcomic album 对象（含 name, author 等属性）
        report: 传入 dict 时，扁平化做完后写入 report["left"]：移不动（或该删的重复页删不掉）、留在章节目录里的有效图片张数
                （没做完——目录不在、出错——就不写）

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
            try:
                new_root.mkdir(parents=True)
                created_root = True
            except FileExistsError:
                created_root = False
            # 整个目录一次改名：要么整体搬过去，要么原样留在原处。从不“复制一份再删原目录”（shutil.move 改名失败时
            # 就这么做）：目录里有文件被别的程序打开时（杀毒软件扫描刚写好的页、资源管理器的缩略图 / 预览、索引、
            # 看图软件）Windows 拒绝改名，删原目录会停在那个文件上，留下删了一半、任务记录还指着的原目录，
            # 和一份没有任务指向的完整副本。改名失败：目录完整留在原处，任务的 output_path 照旧有效，
            # 这部漫画下一次下载（写进同一个目录，_reusable_album_dir）完成后再整理一次
            try:
                _rename_with_retry(output, new_output)
            except OSError as e:
                log.warning(f"按作者整理失败（目录可能正被别的程序占用），保留在原位置，下次下载这部漫画后再整理: "
                            f"{output_path} -> {new_output} error={e}")
                if created_root:
                    _remove_empty_dir(new_root)  # 只删这次建的、仍是空的作者目录
                return None
            log.info(f"按作者整理完成: {output_path} -> {new_output}")
            return str(new_output)

        elif mode == "flat":
            # 扁平化：将各章节子目录中的有效图片（普通文件，不是链接）移到漫画目录下，改名为 _flat_page_name
            # （<前缀>_pNNNNN.webp）。同一章的同一页每次都得到同一个名字，而且永远不会和旧版本的累加序号页名重名：
            # 目标已存在就一定是同一章的同一页（真的重新下载了），原子替换它；skip_existing 也按这个名字认出已经
            # 扁平化的页。坏的 / 空的图片不挪（更不会替换一张好的扁平页）。只改名（被占用时短暂重试），
            # 从不复制再删除：移不动的页原样留在章节目录里，一页也不会丢，下次下载这部漫画后再整理；
            # 这期间它排在这一章扁平页的前面，漫画目录里已经有它的一份（替换不了的扁平页，或它重复的旧页）时出现两次。
            # 旧版本的扁平页（<前缀>_NNNNN）按页认不出来：这一章又下载了的页和其中某一张旧扁平页（散图，或压缩包
            # 第一层）逐字节相同时，它就是漫画目录里已有的那一页——删掉刚下载的这一张、不挪（旧的那张刚读过、留着），
            # 否则同一页出现两次。旧版本的扁平页和其他文件从不改名、覆盖或删除
            chapter_dirs = sorted([
                d for d in output.iterdir()
                if d.is_dir()
            ])
            legacy_copies = _LegacyFlatCopies(output)
            moved = left = dropped = 0
            for ch_dir in chapter_dirs:
                for img_path in sorted(ch_dir.iterdir()):
                    if img_path.suffix.lower() not in _FLAT_IMAGE_SUFFIXES:
                        continue
                    try:
                        if not img_path.is_file() or is_link(img_path):
                            continue
                    except OSError:
                        continue
                    dest = output / _flat_page_name(ch_dir.name, img_path.name)
                    if not _valid_image(img_path):
                        log.warning(f"扁平化跳过坏的或空的图片，留在章节目录: {img_path}"
                                    + ("（不替换已有的扁平页）" if os.path.lexists(dest) else ""))
                        continue
                    # 它自己的新页名散图已经在、却坏了：不按旧页丢掉，下面替换那张坏散图（否则坏图永远留着、每次重新下载）
                    if (not (os.path.lexists(dest) and not _valid_image(dest))
                            and _take_legacy_copy(img_path, legacy_copies.digests(ch_dir.name))):
                        try:
                            _retry_on_lock(os.remove, img_path)
                            dropped += 1
                            log.info(f"扁平化：和漫画目录里一张旧版本扁平页逐字节相同，删掉刚下载的这一张: {img_path}")
                        except OSError as e:
                            left += 1
                            log.warning(f"扁平化：和旧版本扁平页相同的新页删不掉（可能正被别的程序占用），保留在章节目录，"
                                        f"下次下载这部漫画后再整理: {img_path} error={e}")
                        continue
                    replace = os.path.lexists(dest)
                    try:
                        _rename_with_retry(img_path, dest, replace=replace)
                        moved += 1
                    except OSError as e:
                        left += 1
                        log.warning(f"扁平化移动图片失败（可能正被别的程序占用），保留在章节目录，下次下载这部漫画后再整理: "
                                    f"{img_path} -> {dest} replace={replace} error={e}")
                # 删除空章节目录（有章节标记的目录不空，照旧保留）
                _remove_empty_dir(ch_dir)
            log.info(f"扁平化整理完成: {output_path}, 共整理 {moved} 张图片"
                     + (f"，{dropped} 张和旧扁平页相同、已删掉" if dropped else "")
                     + (f"，{left} 张移动失败、留在章节目录" if left else ""))
            if report is not None:
                report["left"] = left
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
    done_pages, pending_images, failed_pages, tracker, pause_ev, lock, pass_num, timeout, album_root=None,
):
    """下载单个章节的所有图片（支持章节内多图并行，每图独立 client）。
    album_root：首趟各章共用的漫画目录第一层（_AlbumRoot）；不传时这一章自己列一次"""
    image_threads = int(get_settings().get("image_threads", "3"))
    photo_dir = _chapter_output_dir(album_dir, photo)
    # 旧版本下载的章节：GIF 页可能已被切片/只剩第一帧，首趟不按“已存在”跳过它们
    refetch_gif = _chapter_format(photo_dir) < _CHAPTER_FORMAT

    photo_images = list(enumerate(photo))
    skip = get_settings().get("skip_existing", "true") == "true"
    # 已经扁平化在漫画目录里的页（散图和压缩包；只有首趟、开启了 skip_existing 时才看）
    flat = _FlatPages(photo_dir, album_root) if skip and pass_num == 1 else None
    flat_pages = frozenset(flat.numbers) if flat is not None else frozenset()
    # 旧格式章节只有 GIF 页需要重取（其余页照样按“已存在”跳过）：没有 GIF 页就不算需要重取
    needs_refetch = refetch_gif and any(image_is_gif(img) for _, img in photo_images)
    if (flat is not None and not needs_refetch
            and _legacy_flattened_chapter(photo_dir, len(photo_images), flat)):
        # 旧版本扁平化过的整章（页名是累加序号，逐页认不出来）：整章算已存在，一页都不下载，
        # 否则每一页都会再下一遍、整理后出现两次
        log.info(f"章节已扁平化整理在漫画目录里，整章跳过 job_id={job_id} photo_id={photo.photo_id} "
                 f"pages={len(photo_images)}")
        with lock:
            done_pages[0] += len(photo_images)
        if tracker:
            tracker.push("progress", {
                "job_id": job_id, "status": "running",
                "done_pages": done_pages[0],
                "total_pages": total_pages or 0,
                "progress": round(done_pages[0] / (total_pages or 1) * 100, 1),
            })
    elif len(photo_images) <= 1 or image_threads <= 1:
        # 单张图或禁止并行 → 串行
        for i, img in photo_images:
            _download_chapter_image(
                None, job_id, album_id, album, photo, photo_dir,
                i, img, total_pages, done_pages, pending_images,
                failed_pages, tracker, pause_ev, lock, pass_num, timeout, refetch_gif, flat_pages,
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
                    failed_pages, tracker, pause_ev, lock, pass_num, timeout, refetch_gif, flat_pages,
                )
                img_futs.append(fut)
            for fut in as_completed(img_futs):
                # 提前检查取消状态，减少取消延迟
                if _should_stop(job_id, album_id, tracker, pause_ev):
                    break
                try:
                    fut.result(timeout=timeout)  # 加超时避免永久挂起
                except Exception as e:
                    log.warning(f"图片下载线程异常 job_id={job_id} error={e}")

    db.update_job(job_id, current_photo=str(photo.photo_id))


def _download_chapter_image(
    _, job_id, album_id, album, photo, photo_dir,
    i, img, total_pages, done_pages, pending_images,
    failed_pages, tracker, pause_ev, lock, pass_num, timeout, refetch_gif=False, flat_pages=frozenset(),
):
    """下载单张图片（每个线程独立 client，避免 HTTP 非线程安全问题）"""
    # _should_stop 内部已持锁（db 查询），无需额外保护
    if _should_stop(job_id, album_id, tracker, pause_ev):
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
            flat_pages=flat_pages,
        )
    finally:
        close_client(img_client)


def download_album_job(job_id: str, album_id: str, photo_ids: list[str]):
    """
    在后台线程中执行下载任务 — 两趟式：首趟快跳，末趟重试。

    首趟每张图 120s 超时，超时则跳过（不阻塞后续）；
    末趟回扫所有 pending 图片，给足 600s 重试。
    """
    tracker = progress_manager.get_tracker(job_id)
    if not tracker:
        log.error(f"下载任务无法获取 tracker，标记为失败 job_id={job_id}")
        db.update_job(job_id, status="failed", error_message="tracker 不可用，任务初始化失败")
        return

    failed_pages = []
    pending_images = []
    job_lock = threading.Lock()  # 每个任务独立锁，消除跨任务锁争用
    log.info(f"开始下载任务 job_id={job_id} album_id={album_id} photo_count={len(photo_ids)}")

    client = None  # 在 finally 中按需关闭
    pause_ev = None
    written_dirs = []  # 这次写过的目录：结束时（无论成败）让“本地可读”重新判断

    try:
        if _should_stop(job_id, album_id, tracker, pause_ev):
            return
        db.update_wishlist_download_status(album_id, "downloading")
        tracker.push("progress", {
            "job_id": job_id, "status": "running",
            "done_pages": 0, "total_pages": 0, "progress": 0,
            "message": "正在获取漫画信息...",
        })

        client, _ = get_client(shared=False)
        album = client.get_album_detail(album_id)

        total_pages = 0
        photos_to_download = []
        seen_photo_ids = set()
        for photo in album:
            if str(photo.photo_id) in photo_ids or not photo_ids:
                if str(photo.photo_id) in seen_photo_ids:
                    continue
                seen_photo_ids.add(str(photo.photo_id))
                client.check_photo(photo)
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
        # 各章认已扁平化的页（skip_existing）共用的漫画目录第一层：整个首趟只列一次，不是每章列一遍
        album_root = _AlbumRoot(album_dir) if get_settings().get("skip_existing", "true") == "true" else None

        with ThreadPoolExecutor(max_workers=photo_threads) as pool:
            futs = []
            for photo in photos_to_download:
                fut = pool.submit(
                    bind_request_id(_download_chapter),
                    job_id, album_id, album, album_dir, photo, total_pages,
                    done_pages, pending_images, failed_pages,
                    tracker, pause_ev, _lock, 1, _FIRST_PASS_TIMEOUT, album_root,
                )
                futs.append(fut)
            # 等待所有章节完成，不阻塞 cancel（每个 chapter 线程内检查 _should_stop）
            for fut in futs:
                try:
                    fut.result(timeout=_FIRST_PASS_TIMEOUT)  # 加超时避免永久挂起
                except Exception as e:
                    log.warning(f"章节下载线程异常 job_id={job_id} error={e}")

        # ── 第二趟：串行重试 pending 图片（量少，无需并行）──
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
                    if _should_stop(job_id, album_id, tracker, pause_ev):
                        should_exit = True
                        break
                    _download_image_single_attempt(
                        retry_client, job_id, album_id, album, output_path,
                        pinfo["photo"], pinfo["idx"], pinfo["img"],
                        pinfo["img_url"], pinfo["img_path"], pinfo["scramble_id"],
                        done_pages, total_pages, failed_pages, None,
                        tracker, pinfo["photo_dir"], pass_num=2, timeout=_RETRY_TIMEOUT,
                        lock=job_lock,
                    )
            finally:
                close_client(retry_client)
                if should_exit:
                    # 注意：不在这里 close_client(client)，外层 finally 会统一关闭
                    tracker.close()
                    return

        now = datetime.now().isoformat()

        # Futures can fail before recording a page error. Do not pack partial data.
        if _should_stop(job_id, album_id, tracker, pause_ev):
            return
        if done_pages[0] != total_pages:
            failed_pages.append(f"下载不完整：{done_pages[0]}/{total_pages} 页")
        if not failed_pages:
            # 每一页都已按当前格式写好（旧目录里的 GIF 页已重新取回）：升级章节标记，以后不再重取
            for photo in photos_to_download:
                _mark_chapter_current(album_dir, photo)

        # -- 以下为 CBZ 打包、元数据写入、完成标记等（不变）--
        organize_mode = get_settings().get("organize_mode", "none")
        flat_report = {}
        if (not failed_pages and organize_mode == "by_author"
                and not _same_dir(Path(output_path).parent, dl_root)):
            # 写进的是已经按作者整理过的目录（不在下载目录第一层）：不再移动，否则会套进 <作者>/<作者>/ 里。
            # 扁平化只在目录里面挪文件，照常做
            log.info(f"下载目录已按作者整理过，不再移动 job_id={job_id} path={output_path}")
        elif not failed_pages and organize_mode and organize_mode != "none":
            new_path = organize_download(output_path, organize_mode, album, flat_report)
            if new_path:
                output_path = new_path
                db.update_job(job_id, output_path=output_path)
                log.info(f"整理后 output_path 已更新: {output_path}")

        settings = get_settings()
        # 扁平化没做完（有页移不动、留在了章节目录里，或整理出错）：这次不自动打包。否则那一页会按章节路径打进包、
        # 开了“删除原图”还会被删掉，下次下载时再也整理不到它，重新下载的同一页换了名字，包里永远有两份。
        # 散图原样留着，下次下载这部漫画、整理完后再打包
        flat_unfinished = (not failed_pages and organize_mode == "flat" and flat_report.get("left") != 0)
        if flat_unfinished and settings.get("auto_pack") == "true":
            log.warning(f"扁平化整理没做完（{flat_report.get('left', '出错')} 张图片留在章节目录），这次不自动打包，"
                        f"下次下载这部漫画后再打包 job_id={job_id} path={output_path}")
        if not failed_pages and settings.get("auto_pack") == "true" and not flat_unfinished:
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
                        if _should_stop(job_id, album_id, tracker, pause_ev):
                            return
                        # Delete only packed unchanged images, never the directory.
                        for path, signature in originals.items():
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
            ok = db.transition_job_status(job_id, ["running", "paused"], "failed",
                                     done_pages=done_val,
                                     error_message=err_all, completed_at=now)
            if not ok:
                log.warning(f"transition_job_status 失败，状态可能已被更改 job_id={job_id}")
            if ok:
                db.update_wishlist_download_status(album_id, "failed")
                tracker.push("failed", {
                    "job_id": job_id, "status": "failed",
                    "error_message": err_all,
                    "done_pages": done_val, "total_pages": total_pages,
                })
        else:
            log.info(f"下载任务完成 job_id={job_id} total_pages={total_pages} output_path={output_path}")
            ok = db.transition_job_status(job_id, ["running", "paused"], "completed",
                                     done_pages=done_val, completed_at=now)
            if ok:
                db.update_wishlist_download_status(album_id, "completed")
                # 这次真正下载完成的章节并入检查新章节的基线，不再算新章节（失败 / 取消的任务什么都不并入）。
                # 记录失败只写日志，绝不影响下载
                try:
                    update_store.absorb_downloaded(album_id, [str(p.photo_id) for p in photos_to_download])
                except Exception as e:
                    log.warning(f"更新检查新章节的基线失败（不影响下载）album_id={album_id} error={e}")
            else:
                log.warning(f"transition_job_status 失败，状态可能已被取消 job_id={job_id}")
            if ok:
                tracker.push("completed", {
                    "job_id": job_id, "status": "completed",
                    "output_path": output_path,
                    "done_pages": done_val, "total_pages": total_pages,
                })

        tracker.close()

    except Exception as e:
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
            changed = db.transition_job_status(job_id, ["running", "paused"], "failed", error_message=err_msg[:1000])
        except Exception as db_err:
            log.error(f"更新失败状态到数据库出错: {db_err}")
            changed = False
        if not changed:
            return
        try:
            db.update_wishlist_download_status(album_id, "failed")
        except Exception as wl_err:
            log.error(f"更新 wishlist 状态出错: {wl_err}")
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
        close_client(client)
        for folder in written_dirs:
            forget_local_state(folder)


def _safe_str(val: Any) -> str:
    """安全转换为字符串，None 时返回空字符串"""
    return str(val) if val is not None else ""
