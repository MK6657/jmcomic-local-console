"""在线阅读：按需从上游取单页图片并缓存，供连续阅读页（/online/<album_id>）展示。

- 不创建下载任务，不写入 downloads/ 或资源库；本地阅读（/read、/preview）仍然只读本地文件。
- 页面缓存在 runtime/cache/online/<photo_id>/<nnnnn>.webp（名字固定，真实格式由 image_mime 按文件头判断），
  按总大小淘汰最久未读的页面。
- 每页只取一次上游字节。不需要解扰的页（num == 0、没有 scramble_id、GIF）原样保存；需要解扰的按 jmcomic
  的方式切片还原后编码为 JPEG q90（比 WebP method 0 快一个数量级、画质相当，只多占本地缓存）。下载任务不走这里。
- 章节图片列表：album_pages() 记下的列表一直有效（图片地址不带时效参数，只按 LRU 淘汰）。不在内存里时（服务重启、
  清除缓存后阅读页还开着）由读者的请求去查：同一章节同时只查一次，其余请求等这一次的结果；失败后
  _PHOTO_RETRY_AFTER 秒内不再查。预取从不查章节，章节不在内存里就不预取。
- 每页一个总时限（_page_budget()：设置页的“超时”，至少 _PAGE_BUDGET 秒），从请求到达算起：等同一页的预取、
  等取图名额、查章节列表、逐个域名尝试都算在内。
- 取图不用 jmcomic 的重试：它按 API 域名重试，而图片 URL 是绝对地址，每次重试都打在同一个坏掉的图片域名上。
  这里先试本页自己的图片域名（很快就失败时最多再试一次），再按 JmModuleConfig.DOMAIN_IMAGE_LIST 换域名（路径和参数
  不变）；60 秒内失败过的域名排到最后，最近成功的域名优先。单次请求的连接超时是 _CONNECT_TIMEOUT，连上后卡住
  （_STALL_TIME 秒几乎收不到数据）就换下一个域名；还在收数据的慢速传输可以用完剩下的时间，不会被掐断后在别的域名从头再取。
- 只有连接层面的失败（连不上、重置、卡住、响应不是图片）记为域名故障，被每页总时限截断的不算。回了 HTTP 状态码说明
  域名是通的：4xx 不记，5xx 只在别的域名取到了这一页时才记；两个域名对同一页回同一个状态码（真实 CDN 对不存在的页
  回 502）就是这一页的问题，不再换域名，也不影响预取。
- 连接复用：取图用一个小的 client 池，每个进行中的取图独占一个 client，连接和 TLS 会话随 client 复用；
  设置保存后（jm_service.invalidate_option_cache）按新设置重建，clear_cache() 时关闭。
- 预取：读到某页时，后台取同一章节接下来的 _PREFETCH_AHEAD 页（同时最多 _PREFETCH_WORKERS 页，与前台名额分开），
  与前台请求共用每页一把的锁，同一页不会取两次；clear_cache() 清空队列，进行中的预取结果作废。
"""
import io
import os
import re
import shutil
import tempfile
import threading
import time
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from . import jm_service
from .logger import log
from .path_utils import get_app_root
from .settings import get_settings

CACHE_ROOT = get_app_root() / "runtime" / "cache" / "online"
CACHE_LIMIT_BYTES = 512 * 1024 * 1024
_TRIM_INTERVAL = 30          # 秒；写入新图片后最多每 30 秒检查一次缓存大小
_PHOTO_CACHE_SIZE = 512      # 内存里最多记这么多个章节的图片列表，最久没用的先忘
_PHOTO_RETRY_AFTER = 2.0     # 章节列表查询失败后，这段时间内同一章节的请求直接失败、不再查（阅读页 3 秒后才自动重试）
_LOOKUP_MAX_AGE = 20.0       # 章节列表查询超过这么久仍未结束（上游连上不回）就不再等它，下一个请求重新查（与获取专辑的时限一致）
_MAX_PARALLEL_FETCHES = 4    # 前台（阅读页正在等的图）同时向上游取图的页数
_PREFETCH_WORKERS = 2        # 后台预取同时进行的页数；不占前台名额，前台请求不会排在预取后面
_PREFETCH_AHEAD = 4          # 每读到一页，向后预取的页数（同一章节内）
_PREFETCH_QUEUE = 8          # 排队的预取最多保留这么多页；读者跳到别处后，旧位置的预取先被丢弃
_PREFETCH_IDLE = 30          # 预取线程空闲这么久就退出，有新任务时再启动
_POOL_SIZE = _MAX_PARALLEL_FETCHES + _PREFETCH_WORKERS  # 空闲 client 最多保留这么多个
_PAGE_BUDGET = 15.0          # 每页总时限的下限；实际用设置页的“超时”（默认 30 秒），见 _page_budget()
_CONNECT_TIMEOUT = 4.0       # 单次请求的连接超时（TCP + TLS 握手）
_STALL_SPEED = 1024          # 字节/秒：连上后连续 _STALL_TIME 秒低于这个速度（不回数据、几乎不动）就放弃这次请求
_STALL_TIME = 8              # 秒
_MIN_ATTEMPT = 1.0           # 剩余时间不足这么多就不再发起新请求
_QUICK_FAILURE = 2.0         # 这么快就失败（连接被重置、5xx）的请求在同一域名上立即再试一次，每页最多一次
_CUT_SLACK = 0.25            # 请求失败时离每页时限不到这么多秒：算被时限截断，不记为域名故障
_HOST_COOLDOWN = 60.0        # 失败过的图片域名在这段时间内排到最后（其他域名都失败时才再试）
_JPEG_QUALITY = 90
_STALE_TEMP_SECONDS = 600    # 超过 10 分钟仍未改名的临时文件视为半成品
_FAILED = "在线加载失败或超时，请稍后重试"
# 缓存里只有这种文件名是已完成的页面；写入时的临时文件形如 00001.tmp-xxxxxxxx.webp（旧版为 *.part）
_PAGE_NAME = re.compile(r"\d{5,}\.webp")
_TEMP_MARK = ".tmp-"
_clock = time.monotonic      # 测试替换为假时钟

_photos = OrderedDict()      # photo_id -> JmPhotoDetail，最近用过的在后
_photos_lock = threading.Lock()
_photo_lookups = {}          # photo_id -> _Lookup：正在向上游查的章节
_photo_failed_at = {}        # photo_id -> 最近一次查询失败的时间（_clock）
_fetch_slots = threading.BoundedSemaphore(_MAX_PARALLEL_FETCHES)
_page_locks = {}             # (photo_id, index) -> [Lock, 使用者数]；没人用时删除
_page_locks_guard = threading.Lock()
_trim_lock = threading.Lock()
_last_trim = 0.0
# clear_cache() 每次加 1：之前排队或进行中的预取不再写入缓存
_cache_epoch = 0
_store_lock = threading.Lock()

_MAGIC = ((b"RIFF", "image/webp"), (b"\xff\xd8", "image/jpeg"), (b"\x89PNG", "image/png"), (b"GIF8", "image/gif"))


def _remember_locked(photo_id: str, photo) -> None:
    _photos[photo_id] = photo
    _photos.move_to_end(photo_id)
    while len(_photos) > _PHOTO_CACHE_SIZE:
        _photos.popitem(last=False)


def _remember_photos(photos) -> None:
    with _photos_lock:
        for photo in photos:
            _remember_locked(str(photo.photo_id), photo)


def _cached_photo(photo_id: str):
    """内存里的章节图片列表；没有就是 None（不查上游）。"""
    with _photos_lock:
        photo = _photos.get(photo_id)
        if photo is not None:
            _photos.move_to_end(photo_id)
        return photo


class _Lookup:
    """一次进行中的章节列表查询，同一章节的请求都等它。"""

    def __init__(self):
        self.started = _clock()
        self.done = threading.Event()
        self.photo = None
        self.error = None
        self.late_logged = False


def _get_photo(photo_id: str, deadline: float):
    """章节图片列表：优先用内存里的；没有时向上游查，同一章节同时只查一次。最多等到 deadline，等不到或查询失败抛
    TimeoutError；等不到时查询在后台继续，之后的请求接着等它，不会再发起一次。"""
    with _photos_lock:
        photo = _photos.get(photo_id)
        if photo is not None:
            _photos.move_to_end(photo_id)
            return photo
        lookup = _photo_lookups.get(photo_id)
        stale = lookup is not None and _clock() - lookup.started > _LOOKUP_MAX_AGE
        if stale:
            # 卡住的查询不再等：重新查一次。它迟到的结果不会覆盖新查询（_finish_lookup 只删除当前那个）
            lookup = None
        start = lookup is None
        if start:
            failed_at = _photo_failed_at.get(photo_id)
            if failed_at is not None and _clock() - failed_at < _PHOTO_RETRY_AFTER:
                raise TimeoutError(_FAILED)  # 刚失败过，原因已记录：不马上再查
            lookup = _photo_lookups[photo_id] = _Lookup()
    if stale:
        log.warning(f"在线阅读 获取章节 photo_id={photo_id} 超过 {int(_LOOKUP_MAX_AGE)} 秒仍未完成，重新获取")
    if start:
        _start_lookup(photo_id, lookup)
    if not lookup.done.wait(max(0.0, deadline - _clock())):
        with _photos_lock:
            first, lookup.late_logged = not lookup.late_logged, True
        if first:
            log.warning(f"在线阅读 获取章节 photo_id={photo_id} 超时，仍在后台获取")
        raise TimeoutError(_FAILED)
    if lookup.error is not None:
        raise TimeoutError(_FAILED)
    return lookup.photo


def _start_lookup(photo_id: str, lookup: _Lookup) -> None:
    """在单独的线程里查章节，和 jm_service._call_with_timeout 一样把共享 client 借到查询真正结束为止；
    结果由 _finish_lookup 记下并通知所有等待的请求。"""
    log.info(f"在线阅读 获取章节 photo_id={photo_id}")
    try:
        client, _ = jm_service.get_client()
        try:
            pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="online-photo")
            try:
                future = jm_service._submit_client_call(pool, client, client.get_photo_detail, photo_id, False)
            finally:
                pool.shutdown(wait=False)
        finally:
            jm_service.close_client(client)  # 查询期间由 _submit_client_call 另外借着
    except Exception as e:
        _finish_lookup(photo_id, lookup, None, e)
        return
    future.add_done_callback(lambda done: _finish_lookup(photo_id, lookup, *_outcome(done)))


def _outcome(future):
    try:
        return future.result(), None
    except BaseException as e:  # 包括被取消
        return None, e


def _finish_lookup(photo_id: str, lookup: _Lookup, photo, error) -> None:
    if error is None and photo is None:
        error = ValueError("上游没有返回章节")
    if error is not None:
        # 先记日志再通知等待的请求：请求返回 504 时原因已经在日志里；一次查询只记这一条
        log.warning(f"在线阅读 获取章节 photo_id={photo_id} 超时或失败 error={type(error).__name__}: {error}")
    with _photos_lock:
        if error is None:
            _photo_failed_at.pop(photo_id, None)
            if getattr(photo, "page_arr", None):
                _remember_locked(photo_id, photo)  # 没有页的章节不记，下次再查
        else:
            _photo_failed_at[photo_id] = _clock()
        if _photo_lookups.get(photo_id) is lookup:
            del _photo_lookups[photo_id]
        lookup.photo, lookup.error = photo, error
    lookup.done.set()


def album_pages(album_id: str) -> dict:
    """整本漫画的在线页列表，结构与本地 /api/preview/<id> 一致，另附 photo_id 便于按章节定位。"""
    log.info(f"在线阅读 获取页面列表 album_id={album_id}")
    client, _ = jm_service.get_client()
    try:
        album = jm_service._call_with_timeout(
            f"在线阅读 获取专辑 album_id={album_id}", 20, _FAILED,
            client.get_album_detail, album_id,
        )
        failed = []
        photos = jm_service.check_album_photos(client, album, failed=failed)
    finally:
        jm_service.close_client(client)

    pages, skipped = [], 0
    for photo in photos:
        count = len(photo.page_arr) if getattr(photo, "page_arr", None) else 0
        if not count:
            skipped += 1
            continue
        chapter = str(getattr(photo, "name", "") or "")
        for index in range(count):
            pages.append({
                "page": len(pages) + 1,
                "url": f"/api/online-img/{photo.photo_id}/{index}",
                "chapter": chapter,
                "photo_id": str(photo.photo_id),
            })
    _remember_photos([photo for photo in photos if getattr(photo, "page_arr", None)])
    if not pages and failed:
        # 一页都没有是因为章节列表没取到（网络失败/超时，单章节漫画最常见），不是“没有可在线阅读的页面”：
        # 按可重试的加载失败处理（504），原因见上面 check_album_photos 的警告
        log.warning(f"在线阅读 所有章节的页面列表都获取失败 album_id={album_id} chapters={len(failed)}")
        raise TimeoutError(_FAILED)
    return {"title": str(getattr(album, "name", "") or ""), "pages": pages, "skipped_chapters": skipped}


def _page_path(photo_id: str, index: int) -> Path:
    return CACHE_ROOT / photo_id / f"{index + 1:05d}.webp"


@contextmanager
def _page_lock(key, wait: float = 0.0):
    """每页一把锁：同一页的前台请求与预取只取一次。最多等 wait 秒（<= 0 时拿不到就算了，预取直接跳过），
    拿不到锁时得到 False。"""
    with _page_locks_guard:
        entry = _page_locks.setdefault(key, [threading.Lock(), 0])
        entry[1] += 1
    acquired = False
    try:
        acquired = entry[0].acquire(True, wait) if wait > 0 else entry[0].acquire(False)
        yield acquired
    finally:
        if acquired:
            entry[0].release()
        with _page_locks_guard:
            entry[1] -= 1
            if entry[1] == 0:
                del _page_locks[key]


def _page_budget() -> float:
    """每页的总时限：设置页的“超时”（下载时单次请求用的也是它），至少 _PAGE_BUDGET 秒。"""
    try:
        setting = float(get_settings().get("timeout") or 0)
    except Exception:  # 设置读不出来就用下限
        setting = 0.0
    return max(_PAGE_BUDGET, setting)


def image_file(photo_id: str, index: int) -> Path:
    """返回单页图片的缓存路径；未缓存时从上游取。索引越界抛 IndexError，上游失败或到了每页时限抛 TimeoutError。
    时限从这里算起：等同一页的预取、等取图名额、查章节列表都算在内，不会等完再重新计时。
    无论是否命中缓存，都在后台预取本章节接下来的几页。"""
    target = _page_path(photo_id, index)
    if target.is_file():
        _touch(target)
        _schedule_prefetch(photo_id, index)
        return target
    deadline = _clock() + _page_budget()
    with _page_lock((photo_id, index), deadline - _clock()) as acquired:
        if not target.is_file():  # 同一页的另一个请求或预取刚取完就直接用
            if not acquired:
                log.warning(f"在线阅读 取图失败 photo_id={photo_id} page={index + 1} error=等待同一页的另一次获取超时")
                raise TimeoutError(_FAILED)
            photo = _get_photo(photo_id, deadline)
            if index >= len(photo.page_arr or ()):
                raise IndexError(f"photo {photo_id} has no page {index + 1}")
            _fetch_page(photo, index, target, deadline)
    _schedule_prefetch(photo_id, index)
    _trim_cache()
    return target


def _fetch_page(photo, index: int, target: Path, deadline: float, epoch=None, background=False) -> bool:
    """取一页、按需解扰、写入缓存，到 deadline 为止。上游失败或结果不是有效图片抛 TimeoutError。
    epoch 不为 None 时（预取），clear_cache() 之后不再写入，返回 False。"""
    photo_id = str(photo.photo_id)
    image = photo[index]
    url = image.download_url
    if background:
        data = _download(url, photo_id, index + 1, deadline, background=True)
    else:
        if not _fetch_slots.acquire(timeout=max(0.0, deadline - _clock())):
            log.warning(f"在线阅读 取图失败 photo_id={photo_id} page={index + 1} error=等待取图名额超时")
            raise TimeoutError(_FAILED)
        try:
            data = _download(url, photo_id, index + 1, deadline)
        finally:
            _fetch_slots.release()
    try:
        content = _page_content(image, url, data)
    except Exception as e:
        level = log.info if background else log.warning
        level(f"在线阅读{' 预取' if background else ''} 取图结果不是有效图片 photo_id={photo_id} page={index + 1} "
              f"size={len(data)} error={type(e).__name__}: {e}")
        raise TimeoutError(_FAILED)
    return _store(target, content, epoch)


# ── 解扰 ──────────────────────────────────────────────

def _scramble_num(image, url: str) -> int:
    """切片数，与 jmcomic 相同（JmImageResp.transfer_to 去掉 URL 参数后调用 get_num_by_url）；0 表示不需要解扰。
    GIF 页没有加扰（jmcomic 自己的下载器也不解码 GIF），没有 scramble_id 时 jmcomic 同样原样保存。"""
    if jm_service.image_is_gif(image) or not image.scramble_id:
        return 0
    from jmcomic import JmImageTool
    return JmImageTool.get_num_by_url(int(image.scramble_id), url.split("?", 1)[0])


def _unscramble(source, num: int):
    """把加扰的页面还原，逐步照搬 jmcomic 的 JmImageTool.decode_and_save（测试逐像素比对），只是不在这里保存。"""
    from PIL import Image
    width, height = source.size
    restored = Image.new("RGB", (width, height))
    over = height % num
    for i in range(num):
        move = height // num
        y_src = height - move * (i + 1) - over
        y_dst = move * i
        if i == 0:
            move += over
        else:
            y_dst += over
        restored.paste(source.crop((0, y_src, width, y_src + move)), (0, y_dst, width, y_dst + move))
    return restored


def _page_content(image, url: str, data: bytes) -> bytes:
    """要写入缓存的内容：不需要解扰的页面就是上游字节本身（先确认是完整的图片）；需要解扰的切片还原后编码为 JPEG。"""
    num = _scramble_num(image, url)
    if num == 0:
        _require_image(data)
        return data
    from PIL import Image
    with Image.open(io.BytesIO(data)) as source:
        restored = _unscramble(source, num)
    buffer = io.BytesIO()
    # JPEG q90：5.7 MP 的页面约 20 ms，WebP method 0 约 350 ms（jmcomic 默认 WebP 约 1 s），
    # 相对还原后像素的 PSNR 与 WebP q80 相当（41–43 dB）；体积大一些，但只经过本机回环、只占本地缓存。
    restored.save(buffer, "JPEG", quality=_JPEG_QUALITY)
    return buffer.getvalue()


def _sniff(head: bytes):
    for magic, mime in _MAGIC:
        if head.startswith(magic):
            return mime
    return None


def _require_image(data: bytes) -> None:
    """原样保存前确认上游字节是完整的图片。WebP 打开时就会校验容器结构、PNG 的 verify 校验 CRC，
    都能发现截断；JPEG / GIF 的 verify 查不出截断，解码一遍（JPEG 几十毫秒，GIF 只解第一帧）。"""
    if _sniff(data[:12]) is None:
        raise ValueError("不是图片")
    from PIL import Image
    with Image.open(io.BytesIO(data)) as image:
        fmt = image.format
        image.verify()
    if fmt in ("JPEG", "GIF"):
        with Image.open(io.BytesIO(data)) as image:
            image.load()


# ── 取图：连接池 + 换域名 ────────────────────────────────

_pool_lock = threading.Lock()
_idle_clients = []           # [(设置版本, client)]，后进先出：最近用过的连接最热
_pool_epoch = 0              # clear_cache() 加 1：之前借出的 client 归还时直接关闭


@contextmanager
def _pooled_client():
    """借一个取图 client，独占到用完为止；设置变化后旧 client 不再复用。"""
    global _idle_clients
    generation = jm_service.option_generation()
    with _pool_lock:
        epoch = _pool_epoch
        stale = [client for gen, client in _idle_clients if gen != generation]
        _idle_clients = [(gen, client) for gen, client in _idle_clients if gen == generation]
        client = _idle_clients.pop()[1] if _idle_clients else None
    for old in stale:
        jm_service.close_client(old)
    if client is None:
        client = jm_service.new_image_client()
        _limit_stalls(client)
    try:
        yield client
    finally:
        with _pool_lock:
            keep = (epoch == _pool_epoch and generation == jm_service.option_generation()
                    and len(_idle_clients) < _POOL_SIZE)
            if keep:
                _idle_clients.append((generation, client))
        if not keep:
            jm_service.close_client(client)


def _limit_stalls(client) -> None:
    """让取图 client 放弃卡住的请求：libcurl 的 LOW_SPEED_LIMIT / LOW_SPEED_TIME，连上后连续 _STALL_TIME 秒
    平均不足 _STALL_SPEED 字节/秒就结束这次请求。不回数据的域名因此很快换掉，还在收数据的慢速传输不受影响。
    设在这个 client 自己的 curl_cffi session 上（每次请求都会带上）；没有这样的 session（测试替身）就跳过。"""
    try:
        session = client.get_root_postman().session
        from curl_cffi import CurlOpt
    except Exception:
        return
    if not hasattr(session, "curl_options"):
        return
    options = dict(session.curl_options or {})
    options[CurlOpt.LOW_SPEED_LIMIT] = _STALL_SPEED
    options[CurlOpt.LOW_SPEED_TIME] = _STALL_TIME
    session.curl_options = options


_hosts_lock = threading.Lock()
_host_failed_at = {}         # 图片域名 -> 最近一次失败的时间（_clock）
_last_good_host = None


class _FetchError(Exception):
    """一次图片请求失败。status 是回来的 HTTP 状态码（说明域名是通的）；None 表示响应本身不对（空的、不是图片）。"""

    def __init__(self, message: str, status=None):
        super().__init__(message)
        self.status = status


def _image_hosts():
    from jmcomic import JmModuleConfig
    return list(JmModuleConfig.DOMAIN_IMAGE_LIST)


def _host_order(own: str) -> list:
    """本页自己的域名在前，其次是最近成功过的域名，再是其余图片域名；最近失败过的排到最后。"""
    hosts = [own] + [host for host in _image_hosts() if host != own]
    now = _clock()
    with _hosts_lock:
        good = _last_good_host
        down = {host for host, at in _host_failed_at.items() if now - at < _HOST_COOLDOWN}
    if good in hosts[1:]:
        hosts.remove(good)
        hosts.insert(1, good)
    return [host for host in hosts if host not in down] + [host for host in hosts if host in down]


def _all_hosts_down(own: str) -> bool:
    now = _clock()
    with _hosts_lock:
        return all(now - _host_failed_at.get(host, -_HOST_COOLDOWN) < _HOST_COOLDOWN
                   for host in {own, *_image_hosts()})


def _host_result(host: str, ok: bool) -> None:
    global _last_good_host
    with _hosts_lock:
        if ok:
            _host_failed_at.pop(host, None)
            _last_good_host = host
        else:
            _host_failed_at[host] = _clock()
            if _last_good_host == host:
                _last_good_host = None


def _request(client, url: str, timeout) -> bytes:
    """一次图片请求。请求头与 jmcomic 取图时相同（按 client 类型：API 端是 APP 图片请求头，网页端是网页请求头，
    见 AbstractJmClient.get_jm_image / update_request_with_specify_domain）；代理、impersonate、cookies 来自
    client 的 session，与应用的设置一致。直接走 postman，不经过 jmcomic 的重试。"""
    from jmcomic import JmModuleConfig
    kwargs = {"headers": JmModuleConfig.new_html_headers()}
    client.update_request_with_specify_domain(kwargs, None, True)
    resp = client.postman.get(url, timeout=timeout, **kwargs)
    status = resp.status_code
    if status != 200:
        raise _FetchError(f"HTTP {status}", status=status)
    data = resp.content
    if not data:
        raise _FetchError("响应为空")
    if _sniff(data[:12]) is None:
        raise _FetchError(f"响应不是图片 size={len(data)}")
    return data


def _download(url: str, photo_id: str, page: int, deadline: float, background: bool = False) -> bytes:
    """取一页的原始字节：按 _host_order 逐个域名尝试（路径和参数不变），到 deadline 为止。
    每次请求可以用完剩下的全部时间（卡住的请求由 _limit_stalls 提前结束），慢但还在收数据的传输不会被掐断。
    域名故障的记法见模块说明：连接层面的失败才记；HTTP 5xx 等别的域名取到这一页才记；两个域名对这一页回同一个
    状态码就不再换域名。最终失败记一条 WARNING（预取记 INFO，读者真正打开这一页时会再取），换域名才成功记一条 INFO。"""
    parts = urlsplit(url)
    own = parts.netloc
    tried, last_error = [], None
    statuses = set()          # 这一页已经收到的 HTTP 错误状态码（每个域名算一次）
    server_errors = []        # 对这一页回了 5xx 的域名
    quick_retry = True
    label = "在线阅读 预取" if background else "在线阅读"
    with _pooled_client() as client:
        for host in _host_order(own):
            host_url = url if host == own else urlunsplit(parts._replace(netloc=host))
            error = None
            while deadline - _clock() >= _MIN_ATTEMPT:
                total = deadline - _clock()
                connect = min(_CONNECT_TIMEOUT, total)
                started = _clock()
                try:
                    data = _request(client, host_url, (connect, total - connect))
                except Exception as e:
                    error = e
                    status = getattr(e, "status", None)
                    quick = _clock() - started < _QUICK_FAILURE
                    if quick_retry and quick and (status is None or status >= 500):
                        quick_retry = False
                        continue
                    break
                _host_result(host, True)
                for failed in server_errors:  # 这一页是存在的，它们回的 5xx 是它们自己的问题
                    _host_result(failed, False)
                if tried:
                    log.info(f"{label} 换用图片域名后取图成功 photo_id={photo_id} page={page} host={host} "
                             f"failed={','.join(tried)} error={_describe(last_error)}")
                return data
            if error is None:  # 时间用完了，这个域名一次都没试
                break
            last_error = error
            tried.append(host)
            status = getattr(error, "status", None)
            if status is None:
                if deadline - _clock() > _CUT_SLACK:  # 被每页时限截断的请求不怪这个域名
                    _host_result(host, False)
                continue
            if status >= 500:
                server_errors.append(host)
            if status in statuses:
                break  # 两个域名对这一页回同一个状态码：是这一页的问题，其余域名也一样
            statuses.add(status)
    level = log.info if background else log.warning
    level(f"{label} 取图失败 photo_id={photo_id} page={page} hosts={','.join(tried) or own} "
          f"error={_describe(last_error) if last_error else '超时'}")
    raise TimeoutError(_FAILED)


def _describe(error) -> str:
    text = " ".join(str(error).split()).split(" See https://curl.se/")[0]  # 去掉 curl 固定附带的文档链接
    return f"{type(error).__name__}: {text[:300]}"


# ── 预取 ──────────────────────────────────────────────

_prefetch_cond = threading.Condition()
_prefetch_queue = deque()    # ((photo_id, index), epoch)，队首先取
_prefetch_active = set()
_prefetch_threads = 0


def _schedule_prefetch(photo_id: str, index: int) -> None:
    """把本章节接下来 _PREFETCH_AHEAD 页中未缓存的放到预取队首（离当前页近的先取）。
    章节列表不在内存里（服务重启、清除缓存）就不预取：后台不查章节，读者的请求查到之后再预取。"""
    photo = _cached_photo(photo_id)
    if photo is None:
        return
    end = min(index + 1 + _PREFETCH_AHEAD, len(photo.page_arr or ()))
    keys = [(photo_id, i) for i in range(index + 1, end)]
    keys = [key for key in keys if not _page_path(*key).is_file()]
    if not keys:
        return
    global _prefetch_threads
    with _prefetch_cond:
        epoch = _cache_epoch
        keys = [key for key in keys if key not in _prefetch_active]
        if not keys:
            return
        wanted = set(keys)
        rest = [item for item in _prefetch_queue if item[0] not in wanted]
        _prefetch_queue.clear()
        _prefetch_queue.extend([(key, epoch) for key in keys] + rest)
        while len(_prefetch_queue) > _PREFETCH_QUEUE:
            _prefetch_queue.pop()
        while _prefetch_threads < _PREFETCH_WORKERS:
            _prefetch_threads += 1
            threading.Thread(target=_prefetch_worker, name="online-prefetch", daemon=True).start()
        _prefetch_cond.notify(len(keys))


def _prefetch_worker() -> None:
    global _prefetch_threads
    while True:
        with _prefetch_cond:
            idle_until = time.monotonic() + _PREFETCH_IDLE
            while not _prefetch_queue:
                remaining = idle_until - time.monotonic()
                if remaining <= 0:
                    _prefetch_threads -= 1
                    return
                _prefetch_cond.wait(remaining)
            key, epoch = _prefetch_queue.popleft()
            _prefetch_active.add(key)
        try:
            _prefetch(key, epoch)
        except TimeoutError:
            pass  # _download 已记录原因
        except Exception as e:
            log.info(f"在线阅读 预取失败 photo_id={key[0]} page={key[1] + 1} error={type(e).__name__}: {e}")
        finally:
            with _prefetch_cond:
                _prefetch_active.discard(key)


def _prefetch(key, epoch: int) -> None:
    photo_id, index = key
    target = _page_path(photo_id, index)
    if epoch != _cache_epoch or target.is_file():
        return
    photo = _cached_photo(photo_id)
    if photo is None or index >= len(photo.page_arr or ()):
        return  # 章节列表已不在内存里（清除缓存等）：后台不查章节，等读者打开这一页时再查
    with _page_lock(key) as acquired:
        # 拿不到锁：前台正在取这一页，不重复取
        if not acquired or target.is_file() or epoch != _cache_epoch:
            return
        if _all_hosts_down(urlsplit(photo[index].download_url).netloc):
            return  # 所有图片域名刚失败过：不在后台反复撞，等读者翻到这一页时再取
        stored = _fetch_page(photo, index, target, _clock() + _page_budget(), epoch=epoch, background=True)
    if stored:
        _trim_cache()


# ── 缓存 ──────────────────────────────────────────────

def _store(target: Path, content: bytes, epoch=None) -> bool:
    """先写临时文件再改名，缓存里不会出现半张图；epoch 已过期（clear_cache 之后）则不写。"""
    with _store_lock:
        if epoch is not None and epoch != _cache_epoch:
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=target.stem + _TEMP_MARK, suffix=target.suffix, dir=target.parent)
        tmp = Path(name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
            os.replace(tmp, target)
        except BaseException:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass  # Windows 上被占用的临时文件由 _trim_cache 过期清理
            raise
    return True


def image_mime(path: Path) -> str:
    """缓存名固定是 .webp：原样保存的页面保留上游格式，解扰后的是 JPEG，按文件头给出真实类型。"""
    with open(path, "rb") as handle:
        head = handle.read(12)
    return _sniff(head) or "application/octet-stream"


def clear_cache() -> None:
    """删除缓存的页面，并停止预取、关闭取图连接、忘掉域名失败记录（设置页“清除缓存”）。"""
    global _cache_epoch, _pool_epoch, _idle_clients, _last_good_host
    with _prefetch_cond:
        _prefetch_queue.clear()
        _prefetch_cond.notify_all()
    with _store_lock:
        _cache_epoch += 1
        shutil.rmtree(CACHE_ROOT, ignore_errors=True)
    with _photos_lock:
        _photos.clear()
        _photo_failed_at.clear()
    with _pool_lock:
        _pool_epoch += 1
        idle, _idle_clients = [client for _, client in _idle_clients], []
    for client in idle:
        jm_service.close_client(client)
    with _hosts_lock:
        _host_failed_at.clear()
        _last_good_host = None


def _touch(path: Path) -> None:
    """读取时刷新修改时间，淘汰时按最久未读优先。"""
    try:
        os.utime(path)
    except OSError:
        pass


def _is_cached_page(path: Path) -> bool:
    """已完成的缓存页（00001.webp）；临时文件 00001.tmp-xxxx.webp 和旧版 *.part 都不算。"""
    return _PAGE_NAME.fullmatch(path.name) is not None


def _trim_cache(force: bool = False) -> None:
    global _last_trim
    now = time.monotonic()
    if not force and now - _last_trim < _TRIM_INTERVAL:
        return
    if not _trim_lock.acquire(blocking=False):
        return
    try:
        _last_trim = now
        files, total = [], 0
        stale = time.time() - _STALE_TEMP_SECONDS
        for path in CACHE_ROOT.rglob("*"):
            try:
                if not path.is_file():
                    continue
                stat = path.stat()
                if not _is_cached_page(path):
                    # 临时文件：写入失败又删不掉（Windows 上被占用）留下的半成品过期即删；
                    # 较新的可能正在写入，既不计入缓存大小也不参与淘汰。
                    if stat.st_mtime < stale:
                        path.unlink()
                    continue
                files.append((stat.st_mtime, stat.st_size, path))
                total += stat.st_size
            except OSError:
                continue
        if total <= CACHE_LIMIT_BYTES:
            return
        # 删到上限的 80%，避免每写一张就触发一次清理
        for _, size, path in sorted(files, key=lambda item: item[0]):
            if total <= CACHE_LIMIT_BYTES * 0.8:
                break
            try:
                path.unlink()
                total -= size
            except OSError:
                pass
        for directory in CACHE_ROOT.iterdir():
            if directory.is_dir() and not any(directory.iterdir()):
                try:
                    directory.rmdir()
                except OSError:
                    pass
    finally:
        _trim_lock.release()
