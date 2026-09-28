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

只剩压缩包（自动打包后删了原图）时读压缩包（core.archive_pages）：有散图就用散图（阅读器另用压缩包补齐散图没有的页），
没有散图而有能打开、至少有一页图片的 CBZ / 本程序打包的 ZIP 也算本地可读。每部漫画的本地状态（LocalState.state）：
  loose            散图可读
  archive          没有散图，从压缩包读（LocalState.archive 是 'cbz' / 'zip'）
  archive_corrupt  没有散图，压缩包打不开 / 校验不对 —— 不可读，原因“压缩包损坏”
  archive_empty    没有散图，压缩包里没有能显示的图片 —— 不可读，原因“压缩包无可阅读图片”
  missing          能用的本地文件都不在（目录不在、空目录、目录被后来的下载重新建出）—— 原因“文件已删除”
记住压缩包的结论时同时记下目录里每个候选压缩包的（大小, 修改时间），复用前逐个 lstat 确认没变；
本程序自己往目录里下载、打包时调用 forget()，不必等有效期。
"""
import os
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import archive_pages
from . import database as db
from . import path_guard
from .file_tree import _REPARSE_POINT, iter_safe_files
from .path_guard import is_safe_path
from .validation import is_allowed_image, validate_numeric

MAX_IDS = 200  # 一次最多判断这么多个 album_id（一页搜索结果/收藏/资源库足够）

_READABLE_TTL = 600.0   # 记住“可读”多久（每次复用前还会确认记下的那一页仍在）
_UNREADABLE_TTL = 30.0  # 记住“不可读”多久
_CACHE_MAX = 5000
_cache: dict[str, tuple] = {}  # 规范化的目录路径 → (签名, LocalState, 依据, 判断时间)
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
    空文件不算页（被中断的下载会留下 0 字节的临时文件；压缩包、打包、导出同样不把它当页）。
    """
    try:
        return next(iter_safe_files(folder, accept=lambda path: is_allowed_image(path.suffix), nonempty=True), None)
    except (OSError, ValueError):  # 目录刚被删除 / 是链接：阅读器也打不开
        return None


def has_page_image(folder) -> bool:
    """目录树里是否至少有一页可读的图片（folder 须已通过 is_safe_path；规则见 first_page_image）。"""
    return first_page_image(folder) is not None


@dataclass(frozen=True)
class LocalState:
    """一部漫画本地文件的状态（见模块说明）"""
    state: str                  # 'loose' | 'archive' | 'archive_corrupt' | 'archive_empty' | 'missing'
    archive: str | None = None  # 用到或看过的压缩包格式：'cbz' / 'zip'（散图可读、文件不在时为 None）

    @property
    def readable(self) -> bool:
        return self.state in ("loose", "archive")

    @property
    def problem(self) -> str | None:
        """不可读时的原因：'deleted' 文件已删除 / 'archive_corrupt' 压缩包损坏 / 'archive_empty' 压缩包无可阅读图片"""
        return None if self.readable else {"archive_corrupt": "archive_corrupt",
                                           "archive_empty": "archive_empty"}.get(self.state, "deleted")


MISSING = LocalState("missing")
_ARCHIVE_STATES = {"ok": "archive", "empty": "archive_empty", "corrupt": "archive_corrupt"}


def _lstat(path):
    try:
        return os.lstat(path)
    except (OSError, ValueError):
        return None


def _is_link(info) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)


def _archive_signature(path):
    info = _lstat(path)
    if info is None or not stat.S_ISREG(info.st_mode) or _is_link(info):
        return None
    return (info.st_size, info.st_mtime_ns)


def _archive_evidence(folder):
    """(LocalState, 依据)：没有散图时看压缩包（archive_pages.select_status，没变的压缩包不再打开）；
    依据是各候选压缩包的（路径, 签名）。打开压缩包暂时出错（被占用等）时不留依据：下次重新判断。"""
    try:
        found = archive_pages.select_status(folder)
        if found is None:
            return MISSING, None
        _, status, fmt, transient = found
        if transient and status != "ok":
            # 暂时打不开（被别的程序占用等）：不说成“压缩包损坏”，按还能读算；不留依据，下次重新判断
            return LocalState("archive", fmt), None
        state = LocalState(_ARCHIVE_STATES[status], fmt)
        return state, tuple((path, archive_pages._signature(path)) for path in archive_pages.candidates(folder))
    except Exception:  # 防御：一个压缩包出任何意外都不能让整批判断失败
        return LocalState("archive_corrupt", None), None


def archive_state(folder) -> LocalState:
    """没有散图时看压缩包：能读 / 损坏 / 没有图片；没有压缩包 → missing。folder 须已通过路径安全检查。"""
    return _archive_evidence(folder)[0]


def forget(path) -> None:
    """本程序改了这个目录里的文件（开始下载、打包、删原图）：下次重新判断，不等有效期"""
    key = os.path.normcase(os.path.abspath(str(path)))
    with _cache_lock:
        _cache.pop(key, None)


def has_local_pages(folder) -> bool:
    """目录里有没有本地能读的页：散图，或者能打开、至少有一页图片的压缩包（folder 须已通过路径安全检查）。
    压缩包暂时打不开（被占用、没有权限）时当作还在：据此做的决定（取代旧任务）是不可逆的。"""
    if has_page_image(folder):
        return True
    try:
        found = archive_pages.select_status(folder)
    except Exception:
        return True
    return found is not None and (found[1] == "ok" or found[3])


def _folder_state(path: str, job_id: str, now: float, root: list) -> LocalState:
    """path（最近一次完成的任务 job_id 的输出目录）的本地状态；目录、任务和压缩包都没变时复用上次的结论。
    root 是本批共用的下载目录真实路径（第一次需要时才解析）。"""
    info = _lstat(path)
    if info is None or not stat.S_ISDIR(info.st_mode) or _is_link(info):
        return MISSING  # 不在了 / 不是目录 / 目录本身是链接：阅读器也打不开
    key = os.path.normcase(os.path.abspath(path))
    signature = (info.st_dev, info.st_ino, info.st_mtime_ns, job_id, str(path_guard.DOWNLOAD_ROOT))
    with _cache_lock:
        cached = _cache.get(key)
    if cached and cached[0] == signature:
        _, state, evidence, checked_at = cached
        ttl = _READABLE_TTL if state.readable else _UNREADABLE_TTL
        if now - checked_at < ttl:
            if state.state == "loose":
                page_info = _lstat(evidence)
                if page_info is not None and stat.S_ISREG(page_info.st_mode) and not _is_link(page_info):
                    return state  # 记下的那一页还在
            elif state.state == "missing":
                return state
            elif evidence and all(_archive_signature(path) == signature for path, signature in evidence):
                return state      # 压缩包都没变
    if not root:
        root.append(path_guard.get_download_root())
    state, evidence = MISSING, None
    if is_safe_path(path, root[0]):
        page = first_page_image(path)
        if page is not None:
            state, evidence = LocalState("loose"), page
        else:
            state, evidence = _archive_evidence(path)
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX and key not in _cache:
            _cache.clear()
        _cache[key] = (signature, state, evidence, now)
    return state


def local_states(album_ids) -> dict[str, LocalState]:
    """各 album_id 的本地状态，只含完成过下载的（从未下载的不在结果里）。非数字 id 忽略；超过 MAX_IDS 的部分不判断。
    只看最近一次完成的任务：它的目录后来被重新下载重新建出（superseded）时是 missing。"""
    ids = [a for a in dict.fromkeys(str(a) for a in album_ids) if validate_numeric(a)][:MAX_IDS]
    if not ids:
        return {}
    conn = db.get_db()
    try:
        rows = conn.execute(
            f"SELECT job_id, album_id, output_path, superseded_at FROM jobs WHERE status='completed' AND album_id IN "
            f"({','.join('?' * len(ids))}) ORDER BY created_at DESC, id DESC",
            ids,
        ).fetchall()
    finally:
        conn.close()
    states: dict[str, LocalState] = {}
    now, root = time.monotonic(), []
    for row in rows:
        album_id = row["album_id"]
        if album_id in states:
            continue  # 只看最近一次完成的任务
        if row["superseded_at"] or not row["output_path"]:
            states[album_id] = MISSING  # 它的目录后来被重新下载重新建出：里面的内容不是这次完成的下载
        else:
            states[album_id] = _folder_state(row["output_path"], row["job_id"], now, root)
    return states


def readable_album_ids(album_ids) -> set[str]:
    """返回其中本地可读的 album_id（散图或压缩包）。非数字 id 忽略；超过 MAX_IDS 的部分不判断。"""
    return {album_id for album_id, state in local_states(album_ids).items() if state.readable}


def readable_among(album_ids) -> set[str]:
    """本地可读的 album_id —— 各页面共用上面的同一规则。
    该规则单次最多判断 MAX_IDS 个，这里分批，供“可离线阅读”筛选、统计和批量下载判断整个资源库/收藏。"""
    ids = list(dict.fromkeys(str(a) for a in album_ids))
    readable: set[str] = set()
    for start in range(0, len(ids), MAX_IDS):
        readable |= readable_album_ids(ids[start:start + MAX_IDS])
    return readable


def local_state(album_id: str) -> LocalState | None:
    """一部漫画的本地状态；从未完成过下载时为 None"""
    return local_states([album_id]).get(str(album_id))


def is_readable(album_id: str) -> bool:
    state = local_state(album_id)
    return bool(state and state.readable)


def readable_folder(album_id: str) -> str | None:
    """阅读器现在打开的目录：最近一次完成（没被取代）的任务的输出目录，本地可读时；否则 None。与 local_states 同一规则"""
    album_id = str(album_id)
    if not validate_numeric(album_id):
        return None
    conn = db.get_db()
    try:
        row = conn.execute(
            "SELECT job_id, output_path, superseded_at FROM jobs WHERE status='completed' AND album_id=? "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (album_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None or row["superseded_at"] or not row["output_path"]:
        return None
    state = _folder_state(row["output_path"], row["job_id"], time.monotonic(), [])
    return row["output_path"] if state.readable else None
