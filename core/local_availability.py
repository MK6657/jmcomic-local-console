"""本地可读判定：搜索、详情、下载管理、收藏和资源库共用同一个标准。

一部漫画“本地可读”= 它最近一次已完成的下载任务的输出目录仍然存在、在下载目录内，
而且目录树里至少有一张本地阅读器能打开的图片（has_page_image）。
与本地阅读器（routes/api_preview._find_album_dir + _scan_pages）的判断一致：最近一次完成的任务目录不在了，
就不再回退到更早的任务；目录还在但一页图片都没有（例如被“打开文件夹”重新建出的空目录），也不算可读。
下载总是写到同一个目录（DOWNLOAD_ROOT/<漫画名>_<album_id>）：目录被删除后又被新的下载任务重新建出来时，
更早完成的任务会被标记为 superseded（database.mark_completed_jobs_superseded），不再算可读——
目录里的内容只在写入它的任务完成后才算数，重新下载失败或被取消留下的残缺几页不会让旧任务重新显示成完整。

收藏、资源库的状态筛选和统计每次请求都要判断整个资源库，所以每个目录的结论会被记住：
目录本身的身份与修改时间（一次 lstat）和决定它的任务都没变时直接复用，不再解析真实路径、不再列目录；
记住“可读”时还记下找到的那一页，复用前确认它还在（页被删光会马上发现）。
目录被删除 / 重建 / 增删章节、有新的任务完成，都会马上重新判断；只在已有的章节目录里增删图片
不改变漫画目录的修改时间，由有效期兜底（_READABLE_TTL / _UNREADABLE_TTL）。
"""
import os
import stat
import threading
import time
from pathlib import Path

from . import database as db
from . import path_guard
from .file_tree import _REPARSE_POINT, iter_safe_files
from .path_guard import is_safe_path
from .validation import is_allowed_image, validate_numeric

MAX_IDS = 200  # 一次最多判断这么多个 album_id（一页搜索结果/收藏/资源库足够）

_READABLE_TTL = 600.0   # 记住“可读”多久（每次复用前还会确认记下的那一页仍在）
_UNREADABLE_TTL = 30.0  # 记住“不可读”多久
_CACHE_MAX = 5000
_cache: dict[str, tuple] = {}  # 规范化的目录路径 → (签名, 可读, 找到的那一页, 判断时间)
_cache_lock = threading.Lock()


def is_page_image(path) -> bool:
    """本地阅读器会显示的图片：后缀在允许列表里（core.validation），且真实路径在下载目录内。"""
    path = Path(path)
    return is_allowed_image(path.suffix) and is_safe_path(path)


def first_page_image(folder) -> Path | None:
    """目录树里第一张可读的图片，没有则 None（folder 须已通过 is_safe_path）。

    与阅读器扫描同一套规则（core.file_tree：不跟随符号链接/目录联接，目录本身是链接则不可读），
    但找到第一张就停：一般只列出漫画目录和第一个章节目录，每个条目按列目录时已拿到的信息分类，
    不再逐个 stat，200 部漫画也很便宜。不跟随链接找到的文件都在 folder 之内，
    所以这里不必再对图片逐个做 is_safe_path（它要解析真实路径，是这里最贵的一步）。
    """
    try:
        return next(iter_safe_files(folder, accept=lambda path: is_allowed_image(path.suffix)), None)
    except (OSError, ValueError):  # 目录刚被删除 / 是链接：阅读器也打不开
        return None


def has_page_image(folder) -> bool:
    """目录树里是否至少有一页可读的图片（folder 须已通过 is_safe_path；规则见 first_page_image）。"""
    return first_page_image(folder) is not None


def _lstat(path):
    try:
        return os.lstat(path)
    except (OSError, ValueError):
        return None


def _is_link(info) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)


def _folder_readable(path: str, job_id: str, now: float, root: list) -> bool:
    """path（最近一次完成的任务 job_id 的输出目录）是否可读；目录与任务都没变时复用上次的结论。
    root 是本批共用的下载目录真实路径（第一次需要时才解析）。"""
    info = _lstat(path)
    if info is None or not stat.S_ISDIR(info.st_mode) or _is_link(info):
        return False  # 不在了 / 不是目录 / 目录本身是链接：阅读器也打不开
    key = os.path.normcase(os.path.abspath(path))
    signature = (info.st_dev, info.st_ino, info.st_mtime_ns, job_id, str(path_guard.DOWNLOAD_ROOT))
    with _cache_lock:
        cached = _cache.get(key)
    if cached and cached[0] == signature:
        _, readable, page, checked_at = cached
        if readable and now - checked_at < _READABLE_TTL:
            page_info = _lstat(page)
            if page_info is not None and stat.S_ISREG(page_info.st_mode) and not _is_link(page_info):
                return True
        elif not readable and now - checked_at < _UNREADABLE_TTL:
            return False
    if not root:
        root.append(path_guard.get_download_root())
    page = first_page_image(path) if is_safe_path(path, root[0]) else None
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX and key not in _cache:
            _cache.clear()
        _cache[key] = (signature, page is not None, page, now)
    return page is not None


def readable_album_ids(album_ids) -> set[str]:
    """返回其中本地可读的 album_id。非数字 id 忽略；超过 MAX_IDS 的部分不判断。"""
    ids = [a for a in dict.fromkeys(str(a) for a in album_ids) if validate_numeric(a)][:MAX_IDS]
    if not ids:
        return set()
    conn = db.get_db()
    try:
        rows = conn.execute(
            f"SELECT job_id, album_id, output_path, superseded_at FROM jobs WHERE status='completed' AND album_id IN "
            f"({','.join('?' * len(ids))}) ORDER BY created_at DESC, id DESC",
            ids,
        ).fetchall()
    finally:
        conn.close()
    decided, readable = set(), set()
    now, root = time.monotonic(), []
    for row in rows:
        album_id = row["album_id"]
        if album_id in decided:
            continue  # 只看最近一次完成的任务
        decided.add(album_id)
        if row["superseded_at"]:
            continue  # 它的目录后来被重新下载重新建出：里面的内容不是这次完成的下载
        path = row["output_path"]
        if path and _folder_readable(path, row["job_id"], now, root):
            readable.add(album_id)
    return readable


def is_readable(album_id: str) -> bool:
    return album_id in readable_album_ids([album_id])
