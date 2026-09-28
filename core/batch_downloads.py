"""批量下载（PR-B）：「下载新章节」「下载未下载的收藏」「下载选中的收藏」的清单与确认，以及收藏单行的「下载」。

只读数据库和本地文件、只写 jobs / wishlist 两张表；不联网，不导入下载代码（job_manager / jm_service / jmcomic），
不启动线程——确认之后由路由调一次调度，任务照常受定时下载时间段和同时下载数的限制。

清单每次都在服务端算：预览时算一次，确认时在同一个 BEGIN IMMEDIATE 事务里重新算，并在这个事务里建任务。
token 是清单（漫画、章节 id、顺序）的摘要：和预览时的对不上就什么都不写（Stale，路由返回 409 和最新的清单）。
客户端只能取消勾选（exclude），从不发送要下载什么。

  new_chapters            检查确认过的新章节：本地可读（core.local_availability）、没有进行中的任务，
                          每部只下载基线之外、已确认的章节 id（从不整部下载）；“章节有变动”的只列出来请用户核对
  undownloaded_favourites 收藏页“未下载”里的（从未下载过的、下载被取消的），每部整部下载；
                          下载记录被清理过（没有任务却有检查记录或本地文件）的不下载，列出原因
  selected_favourites     同一规则用在用户选中的收藏上，其余的列出原因

new_chapters 的漫画都完成过下载（本地可读 ⇒ 有完成的任务），另两种都从没完成过：两边不会重叠，
写入时 insert_job_guarded 在同一条语句里再核对一次。
"""
import hashlib
import json
import os
import threading

from . import database as db
from . import local_availability, path_guard, update_store
from .file_tree import is_link
from .logger import log
from .validation import validate_numeric

KINDS = ("new_chapters", "undownloaded_favourites", "selected_favourites")
MAX_ALBUMS = 50      # 一次确认最多这么多部（与收藏批量下载的上限相同）；其余的下次再列
MAX_PHOTOS = 1000    # 一个任务最多这么多话（与 POST /api/jobs 相同）
MAX_TITLE = 500
MAX_REVIEW = 50
LOCK_WAIT = 30       # 秒：同一时间只处理一次确认

_CONFIRM_LOCK = threading.Lock()


class Stale(Exception):
    """确认时重新算出的清单和预览时的不一样：什么都没写"""


class NothingSelected(Exception):
    """清单里的漫画全被取消勾选了"""


class Busy(Exception):
    """上一次确认还没处理完"""


def _json_list(text) -> list:
    try:
        value = json.loads(text) if text else None
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _skip(album_id, title, reason) -> dict:
    return {"album_id": album_id, "title": title or album_id, "reason": reason}


def _same_dir(a, b) -> bool:
    return os.path.normcase(os.path.realpath(str(a))) == os.path.normcase(os.path.realpath(str(b)))


def _album_dirs_with_pages(candidate_ids) -> set[str]:
    """下载目录第一层或按作者整理后的第二层（<作者>/<名字>_<album_id>）里名字是 <任意>_<album_id>、album_id 在
    candidate_ids 里、有本地能读的页（散图或压缩包）的漫画。第二层只看第一层里不像漫画文件夹（名字不以 _<数字> 结尾）
    的文件夹——漫画文件夹里是章节文件夹（<章节名>__<photo_id>），不进去；再往下不看。
    下载目录不在时为空集合；链接不看。压缩包暂时打不开也算有页：宁可这次不整部下载。"""
    wanted = {str(a) for a in candidate_ids if validate_numeric(str(a))}
    found: set[str] = set()
    if not wanted:
        return found
    matches, parents = [], []
    try:
        with os.scandir(path_guard.DOWNLOAD_ROOT) as entries:
            for entry in entries:
                _, sep, tail = entry.name.rpartition("_")
                if tail in wanted:
                    matches.append((tail, entry.path))
                elif not (sep and validate_numeric(tail)):
                    parents.append(entry.path)      # 可能是按作者整理的文件夹
    except OSError:
        return found
    for parent in parents:
        try:
            if is_link(parent) or not os.path.isdir(parent):
                continue
            with os.scandir(parent) as entries:
                matches += [(entry.name.rpartition("_")[2], entry.path) for entry in entries
                            if entry.name.rpartition("_")[2] in wanted]
        except OSError:
            continue
    root = None
    for album_id, path in matches:
        if album_id in found:
            continue
        try:
            if is_link(path) or not os.path.isdir(path):
                continue
        except OSError:
            continue
        root = root or path_guard.get_download_root()
        if path_guard.is_safe_path(path, root) and local_availability.has_local_pages(path):
            found.add(album_id)
    return found


def _evidence(kind, album_ids, review=False) -> dict:
    """本地文件的依据（在事务之外读：要看文件）：readable 本地可读的 album_id，dirs 下载目录里有页的漫画。
    之后才出现的漫画不在 readable 里（下载新章节时不列），在 dirs 里按没有文件算（清单和预览不一样时会 409）。"""
    if kind == "new_chapters":
        ids = [item["album_id"] for item in update_store.pending_new_chapters()]
        if review:
            ids += [row["album_id"] for row in update_store.changed_albums()]
        return {"readable": local_availability.readable_among(ids), "dirs": set()}
    if kind == "undownloaded_favourites":
        conn = db.get_db()
        try:
            rows = db.undownloaded_favourites(conn)
        finally:
            conn.close()
        candidates = [row["album_id"] for row in rows if row["latest_job"] is None and not row["had_check_row"]]
        return {"readable": set(), "dirs": _album_dirs_with_pages(candidates)}
    return {"readable": local_availability.readable_among(album_ids), "dirs": _album_dirs_with_pages(album_ids)}


# ─── 三种清单 ───


def _plan_new_chapters(conn, readable, review) -> tuple[list, list, list, int]:
    rows = update_store.pending_new_chapters(conn=conn)
    ids = json.dumps([row["album_id"] for row in rows])
    baselines = {row["album_id"]: set(_json_list(row["baseline_ids"])) for row in conn.execute(
        "SELECT album_id, baseline_ids FROM album_update_checks WHERE album_id IN (SELECT value FROM json_each(?))",
        (ids,))}
    active = db.active_album_ids(conn, json.loads(ids))
    outputs = db.newest_completed_outputs(conn, json.loads(ids))
    items, skipped = [], []
    for row in rows:
        album_id = row["album_id"]
        if album_id not in readable:
            continue    # 本地没有能读的内容（文件删了、只在收藏里……）：与 GET /api/updates/pending 一样不列
        title = row["title"] or album_id
        if album_id in active:
            skipped.append(_skip(album_id, title, "active"))
            continue
        known = baselines.get(album_id, set())
        photo_ids = [p for p in dict.fromkeys(str(p) for p in row["photo_ids"]) if validate_numeric(p) and p not in known]
        if not photo_ids:
            continue
        if len(photo_ids) > MAX_PHOTOS:
            skipped.append(_skip(album_id, title, "too_many"))
            continue
        output = outputs.get(album_id)
        if not output:
            continue
        if not _same_dir(os.path.dirname(os.path.abspath(output)), path_guard.DOWNLOAD_ROOT):
            # 按作者整理过：新任务会写到下载目录下另一个文件夹，阅读时只看得到新下载的章节
            skipped.append(_skip(album_id, title, "organized"))
            continue
        wanted, chapters = set(photo_ids), []
        for chapter in row["chapters"]:
            photo_id = str(chapter.get("photo_id"))
            if photo_id in wanted:
                wanted.discard(photo_id)
                chapters.append(chapter)
        items.append({"album_id": album_id, "title": title, "scope": "chapters", "photo_ids": photo_ids,
                      "chapters": chapters, "confirmed_at": row["confirmed_at"], "checked_at": row["checked_at"]})
    changed = []
    if review:
        changed = [dict(row, title=row["title"] or row["album_id"])
                   for row in update_store.changed_albums(conn) if row["album_id"] in readable]
    return items, skipped, changed[:MAX_REVIEW], len(changed)


def _favourite_item(row) -> dict:
    return {"album_id": row["album_id"], "title": row["title"] or row["album_id"], "scope": "all", "photo_ids": [],
            "state": "never" if row["latest_job"] is None else "canceled"}


def _records_cleared(row, dirs) -> bool:
    """没有任何任务记录，却有检查记录（只在下载开始或检查过才会有）或本地文件：下载记录被清理过"""
    return row["latest_job"] is None and bool(row["had_check_row"] or row["album_id"] in dirs)


def _plan_favourites(conn, dirs) -> tuple[list, list]:
    items, skipped = [], []
    for row in db.undownloaded_favourites(conn):
        if not validate_numeric(row["album_id"]):
            continue
        if _records_cleared(row, dirs):
            skipped.append(_skip(row["album_id"], row["title"], "records_cleared"))
        else:
            items.append(_favourite_item(row))
    return items, skipped


def _plan_selected(conn, album_ids, readable, dirs) -> tuple[list, list]:
    rows = db.favourite_rows(conn, album_ids, readable)
    items, skipped = [], []
    for album_id in album_ids:
        row = rows.get(album_id)
        if row is None:
            skipped.append(_skip(album_id, "", "not_favourite"))
        elif row["filter_group"] != "none":
            skipped.append(_skip(album_id, row["title"], row["filter_group"]))  # readable / active / failed / missing
        elif _records_cleared(row, dirs):
            skipped.append(_skip(album_id, row["title"], "records_cleared"))
        else:
            items.append(_favourite_item(row))
    return items, skipped


def _token(kind, album_ids, items) -> str:
    """清单的摘要：哪几部、各自哪些章节、什么顺序（标题、跳过的、说明文字都不算）。一致性校验，不是密钥。"""
    payload = {"v": 1, "kind": kind, "ids": album_ids if kind == "selected_favourites" else None,
               "items": [[item["album_id"], item["photo_ids"]] for item in items]}
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _plan(conn, kind, album_ids, evidence, review=False) -> dict:
    review_rows, changed = [], 0
    if kind == "new_chapters":
        items, skipped, review_rows, changed = _plan_new_chapters(conn, evidence["readable"], review)
    elif kind == "undownloaded_favourites":
        items, skipped = _plan_favourites(conn, evidence["dirs"])
    elif kind == "selected_favourites":
        items, skipped = _plan_selected(conn, album_ids, evidence["readable"], evidence["dirs"])
    else:
        raise ValueError(f"未知的批量下载: {kind}")
    listed = items[:MAX_ALBUMS]
    return {"items": listed, "more": len(items) - len(listed), "skipped": skipped,
            "review": review_rows, "changed": changed, "token": _token(kind, album_ids, listed)}


def _counts(kind, items) -> dict:
    return {"albums": len(items),
            "chapters": sum(len(item["photo_ids"]) for item in items) if kind == "new_chapters" else None}


def _check_args(kind, album_ids):
    if kind not in KINDS:
        raise ValueError(f"未知的批量下载: {kind}")
    if (kind == "selected_favourites") != (album_ids is not None):
        raise ValueError("album_ids 只用于下载选中的收藏")


# ─── 对外 ───


def preview(kind: str, album_ids: list | None = None) -> dict:
    """列出这次会下载什么（只读：不建任务、不写任何表和文件、不联网）。album_ids 只用于 selected_favourites
    （已校验、去重的数字 id）。定时下载时间段、队列等说明由路由补上。"""
    _check_args(kind, album_ids)
    evidence = _evidence(kind, album_ids, review=True)
    out_of_scope = {}
    if kind == "undownloaded_favourites":
        readable = local_availability.readable_among(db.get_completed_album_ids(wishlist_only=True))
        counts = db.get_all_wishlist(page=1, page_size=1, readable_ids=readable)["group_counts"]
        out_of_scope = {group: counts[group] for group in ("readable", "active", "failed", "missing")}
    conn = db.get_db()
    try:
        conn.execute("BEGIN")   # 各个查询看到同一个时刻的数据库；只读，结束时回滚
        plan = _plan(conn, kind, album_ids, evidence, review=True)
    finally:
        conn.close()
    if kind == "new_chapters":
        out_of_scope = {"changed": plan["changed"]}
    return {
        "status": "ok", "kind": kind, "token": plan["token"], "limit": MAX_ALBUMS,
        "items": plan["items"], "counts": _counts(kind, plan["items"]), "skipped": plan["skipped"],
        "more": plan["more"], "review": plan["review"], "out_of_scope": out_of_scope,
    }


def confirm(kind: str, token: str, exclude=(), album_ids: list | None = None) -> dict:
    """按确认时重新算出的清单建任务（一个事务：要么全部建好，要么一条都不建）。
    token 与重新算出的不一致 → Stale；全被取消勾选 → NothingSelected；上一次确认还没处理完 → Busy。
    → {kind, created: [{album_id, job_id, title, photo_ids}], counts}"""
    _check_args(kind, album_ids)
    excluded = {str(a) for a in exclude}
    if not _CONFIRM_LOCK.acquire(timeout=LOCK_WAIT):
        raise Busy()
    try:
        evidence = _evidence(kind, album_ids)
        created = []
        with db.transaction() as conn:
            plan = _plan(conn, kind, album_ids, evidence)
            if plan["token"] != token:
                raise Stale()
            chosen = [item for item in plan["items"] if item["album_id"] not in excluded]
            if not chosen:
                raise NothingSelected()
            completed = "required" if kind == "new_chapters" else "forbidden"
            for item in chosen:
                album_id, photo_ids = item["album_id"], list(item["photo_ids"])
                if kind == "new_chapters" and not photo_ids:
                    raise ValueError(f"下载新章节的任务没有章节 album_id={album_id}")   # [] 会变成整部下载
                job_id = db.new_job_id()
                title = (item["title"] or album_id)[:MAX_TITLE]
                if not db.insert_job_guarded(conn, job_id, album_id, title, photo_ids, completed=completed):
                    raise RuntimeError(f"批量下载没能建任务 album_id={album_id} kind={kind}")
                db.update_wishlist_download_status(album_id, "queued", conn=conn)
                created.append({"album_id": album_id, "job_id": job_id, "title": title, "photo_ids": photo_ids})
        for job in created:
            log.info(f"批量下载 创建任务 job_id={job['job_id']} album_id={job['album_id']} kind={kind} "
                     f"photo_count={len(job['photo_ids'])}")
        log.info(f"批量下载 kind={kind} created={len(created)} excluded={len(plan['items']) - len(chosen)} "
                 f"skipped={len(plan['skipped'])}")
        return {"kind": kind, "created": created, "counts": _counts(kind, created)}
    finally:
        _CONFIRM_LOCK.release()


def enqueue_single(album_ids) -> dict:
    """收藏单行的「下载」（POST /api/wishlist/download）：每部整部下载；已有排队 / 下载中 / 已暂停任务的不重复建。
    album_ids 已校验为数字。→ {job_ids: [{album_id, job_id}], skipped: [{album_id, reason: 'active'}]}"""
    ids = list(dict.fromkeys(str(a) for a in album_ids))
    if not _CONFIRM_LOCK.acquire(timeout=LOCK_WAIT):
        raise Busy()
    try:
        created, skipped = [], []
        with db.transaction() as conn:
            titles = {row["album_id"]: row["title"] for row in conn.execute(
                """SELECT p.value AS album_id,
                          COALESCE(NULLIF(TRIM(w.title), ''), NULLIF(TRIM(m.title), ''), p.value) AS title
                   FROM json_each(?) p
                   LEFT JOIN wishlist w ON w.album_id = p.value
                   LEFT JOIN album_meta m ON m.album_id = p.value""",
                (json.dumps(ids),))}
            for album_id in ids:
                job_id = db.new_job_id()
                title = (titles.get(album_id) or album_id)[:MAX_TITLE]
                if db.insert_job_guarded(conn, job_id, album_id, title, [], completed="any"):
                    db.update_wishlist_download_status(album_id, "queued", conn=conn)
                    created.append({"album_id": album_id, "job_id": job_id})
                else:
                    skipped.append({"album_id": album_id, "reason": "active"})
        for job in created:
            log.info(f"收藏下载 创建任务 album_id={job['album_id']} job_id={job['job_id']}")
        if skipped:
            log.info(f"收藏下载 已在下载队列中，没有重复创建 count={len(skipped)}")
        return {"job_ids": created, "skipped": skipped}
    finally:
        _CONFIRM_LOCK.release()
