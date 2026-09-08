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
from .logger import log
from .progress import progress_manager
from .settings import get_settings, build_jmcomic_option
from .validation import safe_dirname as _safe_dirname
from .path_guard import DOWNLOAD_ROOT, is_safe_path
from .packer import CbzPacker
from .file_tree import safe_files
from .validation import EXPORT_IMAGE_EXTENSIONS


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
    global _global_option, _global_client
    with _option_lock:
        previous = _global_client
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
    tracker, photo_dir, pass_num, timeout, lock,
):
    """单张图片单次下载尝试。
    成功 → done_pages +1
    超时且是首趟 → 记录到 pending_images
    失败且是末趟 → 记录到 failed_pages
    使用传入的每任务锁 lock，避免跨任务争用模块级 _download_lock。
    """
    img_name = f"{idx + 1:05d}.webp"
    img_path = photo_dir / img_name if not str(img_path).startswith(str(photo_dir)) else img_path
    if not is_safe_path(img_path) or not is_safe_path(photo_dir):
        raise ValueError("图片输出路径越权")

    skip = get_settings().get("skip_existing", "true") == "true"
    if skip and pass_num == 1 and _valid_image(img_path):
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

    tmp_path = None
    try:
        # Never overwrite a previous valid image before a replacement is verified.
        fd, name = tempfile.mkstemp(prefix=img_path.stem + ".", suffix=img_path.suffix, dir=photo_dir)
        os.close(fd)
        tmp_path = Path(name)
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


def _valid_image(path):
    try:
        from PIL import Image
        with Image.open(path) as image:
            image.verify()
        return True
    except (OSError, ValueError, SyntaxError):
        return False


def _submit_client_call(pool, client, fn, *args, **kwargs):
    """Keep a shared transport leased until the future actually finishes/cancels."""
    shared = client is not None and getattr(client, _JM_SHARED_ATTR, False)
    if shared:
        with _option_lock:
            client._jm_users += 1
    try:
        future = pool.submit(fn, *args, **kwargs)
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
        log.warning(f"{label} 超时或失败 error={e}")
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
        # 并行检查所有 photo，避免逐章串行 HTTP 请求
        photo_list = list(album)
        checked = {}
        # 预先设置 from_album，避免 check_photo 内部重复调用 get_album_detail
        for photo in photo_list:
            if not hasattr(photo, 'from_album') or photo.from_album is None:
                photo.from_album = album
        # 已有 page_arr 的章节直接跳过 check
        pending_photos = []
        for photo in photo_list:
            if hasattr(photo, 'page_arr') and photo.page_arr:
                checked[id(photo)] = photo
            else:
                pending_photos.append(photo)
        # 独立线程池 + 手动 shutdown(wait=False)：
        # 不能用 with 语法 —— __exit__ 会 shutdown(wait=True)，
        # 阻塞等待挂起的 check_photo，让 15s 超时保护形同虚设。
        check_pool = None
        fut_map = {}
        try:
            if pending_photos:
                check_pool = ThreadPoolExecutor(max_workers=min(len(pending_photos), 10))
                for photo in pending_photos:
                    fut = _submit_client_call(check_pool, client, client.check_photo, photo)
                    fut_map[fut] = photo
                for fut in as_completed(fut_map, timeout=15):
                    photo = fut_map[fut]
                    try:
                        fut.result()
                        checked[id(photo)] = photo
                    except Exception:
                        checked[id(photo)] = photo  # 失败也保留，page_count 为 0
                        log.warning(f"获取 photo 详情失败 photo_id={photo.photo_id}")
        except TimeoutError:
            log.warning(f"check_photo 超时（15s），album_id={album_id}，已跳过 {len(fut_map)} 个未完成章节")
            for photo in photo_list:
                if id(photo) not in checked:
                    checked[id(photo)] = photo
        finally:
            if check_pool is not None:
                check_pool.shutdown(wait=False, cancel_futures=True)

        for photo in photo_list:
            p = checked.get(id(photo), photo)
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
        marker = directory / ".jm-chapter.json"
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
        marker.write_text(json.dumps({"photo_id": photo_id}), encoding="utf-8")
        return directory


def _download_chapter(
    job_id, album_id, album, album_dir, photo, total_pages,
    done_pages, pending_images, failed_pages, tracker, pause_ev, lock, pass_num, timeout,
):
    """下载单个章节的所有图片（支持章节内多图并行，每图独立 client）"""
    image_threads = int(get_settings().get("image_threads", "3"))
    photo_dir = _chapter_output_dir(album_dir, photo)

    photo_images = list(enumerate(photo))
    if len(photo_images) <= 1 or image_threads <= 1:
        # 单张图或禁止并行 → 串行
        for i, img in photo_images:
            _download_chapter_image(
                None, job_id, album_id, album, photo, photo_dir,
                i, img, total_pages, done_pages, pending_images,
                failed_pages, tracker, pause_ev, lock, pass_num, timeout,
            )
    else:
        # 多图并行：每个线程独立 client，避免 HTTP 非线程安全问题
        with ThreadPoolExecutor(max_workers=image_threads) as img_pool:
            img_futs = []
            for i, img in photo_images:
                fut = img_pool.submit(
                    _download_chapter_image,
                    None, job_id, album_id, album, photo, photo_dir,
                    i, img, total_pages, done_pages, pending_images,
                    failed_pages, tracker, pause_ev, lock, pass_num, timeout,
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
    failed_pages, tracker, pause_ev, lock, pass_num, timeout,
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
        scramble_id = int(img.scramble_id) if img.scramble_id else None
        _download_image_single_attempt(
            img_client, job_id, album_id, album, "",
            photo, i, img, img_url, img_path, scramble_id,
            done_pages, total_pages, failed_pages,
            pending_images if pass_num == 1 else None,
            tracker, photo_dir, pass_num=pass_num, timeout=timeout, lock=lock,
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

        dl_root = str(DOWNLOAD_ROOT)
        os.makedirs(dl_root, exist_ok=True)

        album_dir = Path(dl_root) / f"{_safe_dirname(album.name)}_{album_id}"
        if not is_safe_path(album_dir):
            raise ValueError("专辑输出路径越权")
        album_dir.mkdir(parents=True, exist_ok=True)
        output_path = str(album_dir)

        db.update_job(job_id, total_pages=total_pages, output_path=output_path)
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
                    _download_chapter,
                    job_id, album_id, album, album_dir, photo, total_pages,
                    done_pages, pending_images, failed_pages,
                    tracker, pause_ev, _lock, 1, _FIRST_PASS_TIMEOUT,
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

        # -- 以下为 CBZ 打包、元数据写入、完成标记等（不变）--
        organize_mode = get_settings().get("organize_mode", "none")
        if not failed_pages and organize_mode and organize_mode != "none":
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
                originals = {
                    path: (path.stat().st_size, path.stat().st_mtime_ns)
                    for path in safe_files(output_dir)
                    if path.suffix.lower() in EXPORT_IMAGE_EXTENSIONS
                }
                packer.pack(output_dir, cbz_path)
                log.info(f"CBZ 打包完成: {cbz_path}")
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


def _safe_str(val: Any) -> str:
    """安全转换为字符串，None 时返回空字符串"""
    return str(val) if val is not None else ""
