"""本地章节清点：详情页的“部分章节已下载 · M/N 话”只在本地文件能证明时显示。

“本地可读”（core.local_availability）只说明至少有一页能读，不说明整部漫画都在：选章节下载、下载失败或取消、
之后又出了新章节、手动删了章节，都会让本地只有一部分。所以各页面的标记只说“已下载内容 · 可离线阅读”，
不说“完整”。本模块在能证明时给出更具体的说法：本地完整地有 M 话，这部漫画共 N 话（M < N）。

证明要求（任何一条不满足都不下结论，返回原因）：
  1. 这部漫画本地可读，清点的正是阅读器打开的那个目录（最近一次完成、没被取代的任务的输出目录）和阅读器看到的页
     （散图 + 压缩包补齐，archive_pages.merge_pages；空文件不算页）。
  2. N 来自刚取到的章节列表：数据库里没过期的专辑详情缓存（db.get_cached_album_detail，默认 1 小时；详情页打开时
     刚写入）。没有、过期、章节编号不对、章节数对不上 → 不知道 N，不下结论。从不联网补取。
  3. 每一页都能归到一个章节：页在“<章节名>__<photo_id>”章节目录的第一层（压缩包里同样的路径），章节目录里的
     .jm-chapter.json 标记写的正是这个 photo_id，页名是连续编号（00001、00002…）。放在漫画目录根上的页
     （整理成“扁平”）、旧版没有标记的目录、别的工具打的压缩包、中断的下载留下的临时文件、同一章节出现在两个目录里：
     都归不了，不下结论。
  4. 本地的每个章节都完整：页号正好是 1..P，P 是章节列表里这一章的页数（0 表示当时没取到，不能证明）。
     章节标记在下载开始时就写好了，只说明这一章开始下过，不能当作下完的证据。
  5. 本地章节都在章节列表里（上游删了或换了章节 → 不下结论），且 0 < M < N。M == N 时不在这里说“全部已下载”。
  6. 这部漫画没有别的下载目录还有内容：按作者整理、上游改了标题时，更早的下载可能在另一个目录里（阅读器只打开最新的
     那个），只看一个目录会把已下载的章节少算 → 不下结论。优先的压缩包暂时打不开（被占用）时也不下结论：
     退而用旧压缩包会少算。只有一话的漫画不可能“部分已下载”，直接返回、不扫描目录（大漫画扫一遍要几秒）。
只读数据库和磁盘：不联网、不建下载任务。
"""
import json
import os
import posixpath
import re
from pathlib import Path

from . import archive_pages
from . import database as db
from .file_tree import iter_safe_files
from .local_availability import has_local_pages, local_state
from .path_guard import is_safe_path
from .validation import is_allowed_image, validate_numeric

MARKER = ".jm-chapter.json"
_CHAPTER_ID = re.compile(r"__(\d+)(?:_\d+)?$")   # 与阅读器相同（routes.api_preview）
_PAGE_NUMBER = re.compile(r"^\d{5,}$")


def _snapshot(album_id: str) -> dict | None:
    """刚取到的章节列表：{photo_id: 页数}；没有 / 过期 / 不完整 → None"""
    cached = db.get_cached_album_detail(album_id)
    detail = cached and cached.get("detail")
    if not isinstance(detail, dict) or str(detail.get("album_id", album_id)) != str(album_id):
        return None
    photos = detail.get("photos")
    if not isinstance(photos, list) or not photos or detail.get("chapter_count") != len(photos):
        return None
    chapters = {}
    for photo in photos:
        if not isinstance(photo, dict):
            return None
        photo_id, pages = str(photo.get("photo_id", "")), photo.get("page_count")
        if not validate_numeric(photo_id) or photo_id in chapters or isinstance(pages, bool) or not isinstance(pages, int):
            return None
        chapters[photo_id] = pages
    return chapters


def _marker_id(chapter_dir: Path) -> str | None:
    try:
        record = json.loads((chapter_dir / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    photo_id = str(record.get("photo_id", "")) if isinstance(record, dict) else ""
    return photo_id if validate_numeric(photo_id) else None


def _archive_hidden(folder: Path, index) -> bool:
    """选中的压缩包之前还有暂时打不开的（被占用）：它可能有更多的页，看不全"""
    for path in archive_pages.candidates(folder):
        if index is not None and os.path.normcase(str(path)) == os.path.normcase(str(index.path)):
            return False
        if archive_pages.read_index(path).transient:
            return True
    return False


def _local_chapters(folder: Path) -> dict | None:
    """阅读器看到的页按章节归类：{photo_id: {页号}}；有一页归不了 → None"""
    # 与阅读器同一组页（散图：图片后缀、非空；folder 已通过路径检查，不跟随链接，所以不必逐个解析真实路径）
    loose = [(img.relative_to(folder).as_posix(), img)
             for img in iter_safe_files(folder, accept=lambda p: is_allowed_image(p.suffix), nonempty=True)]
    index = archive_pages.select(folder)
    if (index is not None and index.transient) or _archive_hidden(folder, index):
        return None  # 压缩包暂时打不开：看不全
    chapters, owner, markers = {}, {}, {}
    for name, _ in archive_pages.merge_pages(loose, index):
        parts = name.split("/")
        if len(parts) != 2:
            return None  # 放在根上（扁平整理）或更深的目录
        chapter, page = parts
        match = _CHAPTER_ID.search(chapter)
        if not match:
            return None  # 旧版目录 / 别的工具的压缩包
        photo_id = match.group(1)
        if chapter not in markers:
            markers[chapter] = _marker_id(folder / chapter)
        if markers[chapter] != photo_id or owner.setdefault(photo_id, chapter) != chapter:
            return None  # 标记对不上，或同一章节在两个目录里
        stem = posixpath.splitext(page)[0]
        if not _PAGE_NUMBER.match(stem):
            return None  # 不是连续编号的页（如中断的下载留下的临时文件）
        chapters.setdefault(photo_id, set()).add(int(stem))
    return chapters


def _other_folders(album_id: str, job_id: str, folder: Path) -> bool:
    """这部漫画别的已完成（没被取代）的下载目录里还有能读的页（按作者整理后的旧目录、上游改标题前的目录）"""
    conn = db.get_db()
    try:
        rows = conn.execute(
            "SELECT output_path FROM jobs WHERE album_id=? AND status='completed' AND superseded_at IS NULL "
            "AND job_id != ? AND output_path IS NOT NULL AND output_path != ''",
            (album_id, job_id),
        ).fetchall()
    finally:
        conn.close()
    seen = {os.path.normcase(os.path.abspath(str(folder)))}
    for row in rows:
        key = os.path.normcase(os.path.abspath(row["output_path"]))
        if key in seen:
            continue
        seen.add(key)
        other = Path(row["output_path"])
        if other.is_dir() and is_safe_path(other) and has_local_pages(other):
            return True
    return False


def partial_chapters(album_id: str) -> tuple[dict | None, str]:
    """({downloaded: M, total: N} 或 None, 原因)。原因：partial 能证明只下载了部分章节 / all_chapters 本地章节
    都在（这里不据此说“全部已下载”）/ not_readable / no_chapter_list / single_chapter / other_folders /
    unattributed / unknown_chapter / incomplete_chapter。"""
    album_id = str(album_id)
    state = local_state(album_id)
    if not state or not state.readable:
        return None, "not_readable"
    job = db.get_completed_job_by_album_id(album_id)
    if not job or not job.get("output_path") or job.get("superseded_at"):
        return None, "not_readable"
    folder = Path(job["output_path"])
    if not folder.is_dir() or not is_safe_path(folder):
        return None, "not_readable"
    upstream = _snapshot(album_id)
    if upstream is None:
        return None, "no_chapter_list"
    if len(upstream) < 2:
        return None, "single_chapter"  # 只有一话：不可能“部分已下载”
    if _other_folders(album_id, job["job_id"], folder):
        return None, "other_folders"
    local = _local_chapters(folder)
    if not local:
        return None, "unattributed"
    for photo_id, pages in local.items():
        if photo_id not in upstream:
            return None, "unknown_chapter"
        total = upstream[photo_id]
        if total <= 0 or pages != set(range(1, total + 1)):
            return None, "incomplete_chapter"
    if len(local) >= len(upstream):
        return None, "all_chapters"
    return {"downloaded": len(local), "total": len(upstream)}, "partial"
