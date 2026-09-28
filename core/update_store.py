"""检查新章节：数据库这一侧（只读写 album_update_checks / update_check_log，不联网、不 import jmcomic）。

“新章节”= 基线之后上游才出现的章节 id。下载时没选的章节从不算新章节：
  基线 = 下载任务本来就要取的完整上游章节列表（download_album_job 开始时记下，不多发请求），
        只在这次下载要建一份全新的本地内容时（第一次下载、本地文件删了之后重新下载）重设；
  下载完成（只有 completed）时，这次真正下载的章节并入基线；失败 / 取消的任务什么都不并入，
        已确认的新章节留着等下一次“下载新章节”（PR-B）。
  PR-A 之前下载的漫画（没有基线）：第一次成功检查只记下上游现有的章节，不说有新章节。
“有新章节”只在一次成功的检查确认了具体章节 id 之后才出现。同一次检查里既有新 id、又有已知章节不见了时，
结论是 'changed'（只给用户核对，不进 PR-B 的批量下载）；不见了的章节用“已确认的不见了”集合记住，
以前的一次删除不会让以后的检查也变成 'changed'。

所有“读 → 改 → 写”都在一个 BEGIN IMMEDIATE 事务里（重新读这一行，不会和同时完成的下载任务互相覆盖）；
时间与数据库其他部分一样是本地时间的 ISO 字符串（精确到秒）。
"""
import json
import random
from contextlib import contextmanager
from datetime import datetime, timedelta

from . import database as db
from .validation import validate_numeric

DAILY = (23 * 3600, 25 * 3600)   # 每部漫画大约一天检查一次（成功后、下载记下基线后）
MAX_EPISODES = 5000              # 章节列表最多这么多话（再多当作上游返回的数据不对）
MAX_TITLE = 200
LOG_KEEP = 2000                  # update_check_log 只留最近这么多条
LEGACY_STEP = (180, 1800)        # 旧漫画第一次检查的间隔（秒）：一天内排完，但至少隔 3 分钟、最多隔 30 分钟


def _iso(value: datetime | None) -> str | None:
    return value.replace(microsecond=0).isoformat() if value is not None else None


def _parse(text):
    try:
        return datetime.fromisoformat(text) if text else None
    except (TypeError, ValueError):
        return None


def _loads(text, default=None):
    try:
        value = json.loads(text) if text else None
    except (TypeError, ValueError):
        value = None
    return value if isinstance(value, list) else ([] if default is None else default)


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _daily(now: datetime) -> datetime:
    return now + timedelta(seconds=random.uniform(*DAILY))


@contextmanager
def _tx():
    """一个 BEGIN IMMEDIATE 事务（写锁一开始就拿到，读到的就是提交时的状态）。"""
    conn = db.get_db()
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    finally:
        conn.close()


def _read(sql: str, params=()):
    conn = db.get_db()
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _ids_param(album_ids) -> str:
    return json.dumps([a for a in dict.fromkeys(str(a) for a in album_ids) if validate_numeric(a)])


# ─── 章节列表 ───


def episodes_of(album) -> list[tuple[str, int, str]]:
    """上游章节列表 → [(photo_id, index, title)]，按上游顺序。

    有 album.episode_list（jmcomic：[(photo_id, index, title)]）时用它，否则逐个遍历章节对象。
    列表为空、超过 MAX_EPISODES、有非数字 id、重复 id 或非整数序号时抛 ValueError——看不懂的数据
    只会让检查算失败，绝不会变成“有新章节”。标题截断到 MAX_TITLE 个字符。"""
    raw = getattr(album, "episode_list", None)
    try:
        if raw:
            items = [tuple(entry) for entry in raw]
        else:
            items = []
            for pos, photo in enumerate(album):
                index = getattr(photo, "sort", None)
                if index is None or callable(index):   # 没有序号（像列表一样的章节对象，sort 是方法）：按位置
                    index = pos + 1
                items.append((getattr(photo, "photo_id", None), index, getattr(photo, "name", "")))
    except TypeError:
        raise ValueError("章节列表格式不对") from None
    if not items:
        raise ValueError("章节列表为空")
    if len(items) > MAX_EPISODES:
        raise ValueError(f"章节数超过 {MAX_EPISODES}")
    episodes, seen = [], set()
    for entry in items:
        if len(entry) != 3:
            raise ValueError("章节条目格式不对")
        photo_id, index, title = entry
        photo_id = str(photo_id) if isinstance(photo_id, (str, int)) and not isinstance(photo_id, bool) else ""
        if not validate_numeric(photo_id):
            raise ValueError("章节 id 不是纯数字")
        if photo_id in seen:
            raise ValueError("章节 id 重复")
        seen.add(photo_id)
        if isinstance(index, bool):
            raise ValueError("章节序号不是整数")
        if isinstance(index, str) and index.strip().isdigit():
            index = int(index.strip())
        if not isinstance(index, int):
            raise ValueError("章节序号不是整数")
        episodes.append((photo_id, index, str(title if title is not None else "")[:MAX_TITLE]))
    return episodes


# ─── 下载任务的两个记录点（jm_service.download_album_job 调用，失败只记日志） ───


def note_download_started(album_id: str, episodes, had_local_content: bool,
                          now: datetime | None = None, next_at: datetime | None = None) -> str:
    """下载任务开始（已取到完整章节列表、还没写任何文件）：记下基线。返回 'reset' / 'filled' / 'kept'。

    没有这一行，或者本地原来没有能读的内容（第一次下载、文件删了之后重新下载）：整行重设，
    基线 = 这次取到的完整上游列表，以前的检查结论、待下载的新章节都清掉。
    有这一行但还没有基线（登记过的旧漫画），本地也有内容：补上基线。
    其他情况一概不动——重试或新的任务绝不能吞掉已确认的新章节。下载自己取到的列表从不产生新章节。"""
    album_id = str(album_id)
    ids = [photo_id for photo_id, _, _ in episodes]
    now = now or datetime.now()
    stamp = _iso(now)
    due = _iso(next_at or _daily(now))
    with _tx() as conn:
        row = conn.execute("SELECT baseline_ids FROM album_update_checks WHERE album_id=?", (album_id,)).fetchone()
        if row is None or not had_local_content:
            conn.execute(
                """INSERT INTO album_update_checks
                       (album_id, baseline_ids, baseline_source, baseline_at, gone_ids, removed_ids, upstream_count,
                        new_chapters, new_count, result, last_success_at, error_kind, error_detail, fail_count,
                        next_check_at, created_at, updated_at)
                   VALUES (?, ?, 'download', ?, '[]', '[]', NULL, '[]', 0, NULL, NULL, NULL, NULL, 0, ?, ?, ?)
                   ON CONFLICT(album_id) DO UPDATE SET
                       baseline_ids=excluded.baseline_ids, baseline_source='download',
                       baseline_at=excluded.baseline_at, gone_ids='[]', removed_ids='[]', upstream_count=NULL,
                       new_chapters='[]', new_count=0, result=NULL, last_success_at=NULL,
                       error_kind=NULL, error_detail=NULL, fail_count=0,
                       next_check_at=excluded.next_check_at, updated_at=excluded.updated_at""",
                (album_id, _dumps(ids), stamp, due, stamp, stamp),
            )
            return "reset"
        if row["baseline_ids"] is None:
            conn.execute(
                """UPDATE album_update_checks SET baseline_ids=?, baseline_source='download', baseline_at=?,
                       error_kind=NULL, error_detail=NULL, fail_count=0, next_check_at=?, updated_at=?
                   WHERE album_id=?""",
                (_dumps(ids), stamp, due, stamp, album_id),
            )
            return "filled"
        return "kept"


def absorb_downloaded(album_id: str, photo_ids, now: datetime | None = None) -> int:
    """下载任务完成（只有 completed）：这次真正下载的章节并入基线，从待下载的新章节里去掉。
    没有这一行或还没有基线时什么都不做。返回从新章节里去掉的话数。"""
    album_id = str(album_id)
    done = [str(p) for p in dict.fromkeys(photo_ids) if validate_numeric(str(p))]
    if not done:
        return 0
    stamp = _iso(now or datetime.now())
    with _tx() as conn:
        row = conn.execute(
            "SELECT baseline_ids, new_chapters, removed_ids, gone_ids FROM album_update_checks WHERE album_id=?",
            (album_id,),
        ).fetchone()
        if row is None or row["baseline_ids"] is None:
            return 0
        baseline = _loads(row["baseline_ids"])
        known = set(baseline)
        baseline += [p for p in done if p not in known]
        absorbed = set(done)
        pending = [c for c in _loads(row["new_chapters"]) if isinstance(c, dict)]
        remaining = [c for c in pending if str(c.get("photo_id")) not in absorbed]
        # 下载到的章节显然还在上游：也不再算“不见了”；新章节都下载完了，“章节有变动”的提示也随之结束——
        # 那几话“不见了”算用户已经处理过（记进 gone_ids），以后再出新章节不会因为它们又变成“章节有变动”
        old_removed = _loads(row["removed_ids"])
        gone = set(_loads(row["gone_ids"]))
        if remaining:
            removed = [p for p in old_removed if p not in absorbed]
        else:
            removed = []
            gone |= set(old_removed)
        gone = sorted(gone - absorbed, key=lambda p: (len(p), p))
        conn.execute(
            """UPDATE album_update_checks SET baseline_ids=?, new_chapters=?, new_count=?, removed_ids=?, gone_ids=?,
                   updated_at=? WHERE album_id=?""",
            (_dumps(baseline), _dumps(remaining), len(remaining), _dumps(removed), _dumps(gone), stamp, album_id),
        )
        return len(pending) - len(remaining)


# ─── 自动检查的排队 ───


def download_running() -> bool:
    """有正在下载的任务吗（只读数据库）：自动检查先等下载结束。"""
    return bool(_read("SELECT 1 FROM jobs WHERE status='running' LIMIT 1"))


def register_legacy(now: datetime) -> int:
    """PR-A 之前下载、还没有这一行的漫画（有完成且没被取代的任务）：登记进来（没有基线），
    第一次检查按最近下载的在前、在一天内排开：间隔 = clamp(86400 / n, 180, 1800) 秒。返回登记数。"""
    with _tx() as conn:
        rows = conn.execute(
            """SELECT j.album_id, MAX(COALESCE(j.completed_at, j.updated_at, j.created_at)) AS finished
               FROM jobs j
               WHERE j.status='completed' AND j.superseded_at IS NULL
                 AND NOT EXISTS (SELECT 1 FROM album_update_checks u WHERE u.album_id = j.album_id)
               GROUP BY j.album_id
               ORDER BY finished DESC, j.album_id""",
        ).fetchall()
        ids = [row["album_id"] for row in rows if validate_numeric(row["album_id"])]
        if not ids:
            return 0
        step = min(max(86400 / len(ids), LEGACY_STEP[0]), LEGACY_STEP[1])
        stamp = _iso(now)
        conn.executemany(
            """INSERT OR IGNORE INTO album_update_checks (album_id, next_check_at, created_at, updated_at)
               VALUES (?, ?, ?, ?)""",
            [(album_id, _iso(now + timedelta(seconds=i * step)), stamp, stamp) for i, album_id in enumerate(ids)],
        )
        return len(ids)


def due_candidates(now: datetime, far: datetime, limit: int = 50) -> list[str]:
    """到期该自动检查的漫画（最早到期的在前）：有完成的任务、没有排队 / 下载中 / 已暂停的任务。
    next_check_at 比 far 还晚（系统时间被往回调过）也算到期。只读数据库，不判断本地文件。"""
    rows = _read(
        """SELECT u.album_id FROM album_update_checks u
           WHERE (u.next_check_at IS NULL OR u.next_check_at <= ? OR u.next_check_at > ?)
             AND EXISTS (SELECT 1 FROM jobs j WHERE j.album_id = u.album_id AND j.status = 'completed')
             AND NOT EXISTS (SELECT 1 FROM jobs a WHERE a.album_id = u.album_id
                             AND a.status IN ('queued', 'running', 'paused'))
           ORDER BY u.next_check_at IS NOT NULL, u.next_check_at, u.album_id
           LIMIT ?""",
        (_iso(now), _iso(far), max(1, int(limit))),
    )
    return [row["album_id"] for row in rows]


def next_due_at() -> datetime | None:
    """最早的下一次自动检查时间（只看有完成任务的漫画）；没有时为 None。"""
    rows = _read(
        """SELECT MIN(u.next_check_at) FROM album_update_checks u
           WHERE u.next_check_at IS NOT NULL
             AND EXISTS (SELECT 1 FROM jobs j WHERE j.album_id = u.album_id AND j.status = 'completed')""",
    )
    return _parse(rows[0][0]) if rows else None


def postpone(album_id: str, until: datetime) -> None:
    """本地现在没有能读的内容：先不查，until 之后再看（不发请求）。"""
    with _tx() as conn:
        conn.execute("UPDATE album_update_checks SET next_check_at=?, updated_at=? WHERE album_id=?",
                     (_iso(until), _iso(datetime.now()), str(album_id)))


def mark_due(album_ids, now: datetime) -> int:
    """PR-B：让这些漫画尽快排进后台检查（next_check_at = now），仍按后台的节奏一部一部查，
    绝不循环调用手动检查。没有这一行的也登记进来（没有基线）。返回处理的漫画数。"""
    ids = json.loads(_ids_param(album_ids))
    if not ids:
        return 0
    stamp = _iso(now)
    with _tx() as conn:
        conn.executemany(
            """INSERT INTO album_update_checks (album_id, next_check_at, created_at, updated_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(album_id) DO UPDATE SET next_check_at=excluded.next_check_at,
                   updated_at=excluded.updated_at""",
            [(album_id, stamp, stamp, stamp) for album_id in ids],
        )
    return len(ids)


# ─── 一次检查 ───


def begin_check(album_id: str, trigger: str, now: datetime, lease_seconds: int = 3600) -> None:
    """发请求之前写下租约：last_attempt_at = now，next_check_at = now + 1 小时。
    检查到一半程序被关掉 / 重启（start.bat）也不会马上又查这一部。没有这一行时插入（没有基线）。"""
    stamp = _iso(now)
    with _tx() as conn:
        conn.execute(
            """INSERT INTO album_update_checks (album_id, last_attempt_at, last_trigger, next_check_at,
                                                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(album_id) DO UPDATE SET last_attempt_at=excluded.last_attempt_at,
                   last_trigger=excluded.last_trigger, next_check_at=excluded.next_check_at,
                   updated_at=excluded.updated_at""",
            (str(album_id), stamp, trigger, _iso(now + timedelta(seconds=lease_seconds)), stamp, stamp),
        )


def _log(conn, album_id, trigger, started, finished, outcome, error_kind=None, error_type=None,
         requests=0, upstream_count=None, new_count=0) -> None:
    conn.execute(
        """INSERT INTO update_check_log (album_id, trigger, started_at, finished_at, outcome, error_kind, error_type,
                                         requests, upstream_count, new_count)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (album_id, trigger, _iso(started), _iso(finished), outcome, error_kind, error_type,
         int(requests or 0), upstream_count, int(new_count or 0)),
    )
    conn.execute(f"DELETE FROM update_check_log WHERE id <= (SELECT MAX(id) FROM update_check_log) - {LOG_KEEP}")


def record_success(album_id: str, episodes, trigger: str, started: datetime, now: datetime,
                   requests: int, next_at: datetime) -> dict:
    """一次成功的检查（episodes 是上游完整章节列表，上游顺序）。在同一个事务里重新读这一行再下结论，
    不会和同时完成的下载任务互相覆盖。返回 {result, newly_confirmed, new_count, upstream_count}。

    没有基线 → 记下上游现有的章节（'baseline'），不说有新章节。
    有基线 K：新章节 = 上游里不在 K 的（上游顺序）；已知章节不见了（且不是已经确认过的不见了）时：
      同时有新章节 → 'changed'（不见了的留着不确认，待核对）；没有新章节 → 'no_update'，把它们记为已确认的不见了。
    已经在待下载列表里的新章节保留原来的确认时间；第一次确认的记 now。"""
    album_id = str(album_id)
    upstream = [(str(p), int(i), str(t)) for p, i, t in episodes]
    upstream_ids = [p for p, _, _ in upstream]
    upstream_set = set(upstream_ids)
    stamp = _iso(now)
    with _tx() as conn:
        row = conn.execute("SELECT * FROM album_update_checks WHERE album_id=?", (album_id,)).fetchone()
        newly: list[str] = []
        if row is None or row["baseline_ids"] is None:
            result, baseline, source, baseline_at = "baseline", upstream_ids, "first_check", stamp
            gone, removed, chapters = [], [], []
        else:
            baseline = _loads(row["baseline_ids"])
            source, baseline_at = row["baseline_source"], row["baseline_at"]
            known = set(baseline)
            gone_set = set(_loads(row["gone_ids"])) - upstream_set          # 又出现了的不再算不见了
            fresh_missing = (known - upstream_set) - gone_set
            new = [e for e in upstream if e[0] not in known]
            if new:
                result = "changed" if fresh_missing else "new"
                removed = sorted(fresh_missing, key=lambda p: (len(p), p))
            else:
                result = "no_update"
                gone_set |= fresh_missing                                   # 只是少了章节，不提示
                removed = []
            gone = sorted(gone_set, key=lambda p: (len(p), p))
            previous = {str(c.get("photo_id")): c for c in _loads(row["new_chapters"]) if isinstance(c, dict)}
            chapters = []
            for photo_id, index, title in new:
                confirmed = previous.get(photo_id, {}).get("confirmed_at")
                if not confirmed:
                    confirmed = stamp
                    newly.append(photo_id)
                chapters.append({"photo_id": photo_id, "index": index, "title": title, "confirmed_at": confirmed})
        conn.execute(
            """INSERT INTO album_update_checks
                   (album_id, baseline_ids, baseline_source, baseline_at, gone_ids, removed_ids, upstream_count,
                    new_chapters, new_count, result, last_success_at, last_attempt_at, last_trigger,
                    error_kind, error_detail, fail_count, next_check_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 0, ?, ?, ?)
               ON CONFLICT(album_id) DO UPDATE SET
                   baseline_ids=excluded.baseline_ids, baseline_source=excluded.baseline_source,
                   baseline_at=excluded.baseline_at, gone_ids=excluded.gone_ids, removed_ids=excluded.removed_ids,
                   upstream_count=excluded.upstream_count, new_chapters=excluded.new_chapters,
                   new_count=excluded.new_count, result=excluded.result, last_success_at=excluded.last_success_at,
                   last_attempt_at=excluded.last_attempt_at, last_trigger=excluded.last_trigger,
                   error_kind=NULL, error_detail=NULL, fail_count=0, next_check_at=excluded.next_check_at,
                   updated_at=excluded.updated_at""",
            (album_id, _dumps(baseline), source, baseline_at, _dumps(gone), _dumps(removed), len(upstream),
             _dumps(chapters), len(chapters), result, stamp, _iso(started), trigger, _iso(next_at), stamp, stamp),
        )
        _log(conn, album_id, trigger, started, now, result, requests=requests,
             upstream_count=len(upstream), new_count=len(chapters))
    return {"result": result, "newly_confirmed": newly, "new_count": len(chapters), "upstream_count": len(upstream)}


def record_failure(album_id: str, kind: str, detail: str, trigger: str, started: datetime, now: datetime,
                   requests: int, backoff) -> dict:
    """一次失败的检查：记下原因、连续失败次数 + 1，下一次自动检查 = now + backoff(连续失败次数) 秒。
    基线、已确认的新章节、上次成功的结论都不动：失败从不抹掉已确认的新章节，也从不产生新章节。
    返回 {fail_count, next_check_at}。"""
    album_id = str(album_id)
    stamp = _iso(now)
    detail = str(detail or "")[:120] or None
    with _tx() as conn:
        row = conn.execute("SELECT fail_count FROM album_update_checks WHERE album_id=?", (album_id,)).fetchone()
        fail_count = (row["fail_count"] if row else 0) + 1
        due = now + timedelta(seconds=float(backoff(fail_count)))
        conn.execute(
            """INSERT INTO album_update_checks (album_id, last_attempt_at, last_trigger, error_kind, error_detail,
                                                fail_count, next_check_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(album_id) DO UPDATE SET last_attempt_at=excluded.last_attempt_at,
                   last_trigger=excluded.last_trigger, error_kind=excluded.error_kind,
                   error_detail=excluded.error_detail, fail_count=excluded.fail_count,
                   next_check_at=excluded.next_check_at, updated_at=excluded.updated_at""",
            (album_id, _iso(started), trigger, kind, detail, fail_count, _iso(due), stamp, stamp),
        )
        _log(conn, album_id, trigger, started, now, "failed", error_kind=kind, error_type=detail, requests=requests)
    return {"fail_count": fail_count, "next_check_at": _iso(due)}


# ─── 读取与描述（各页面共用） ───


def get(album_id: str) -> dict | None:
    """这一部漫画的检查记录（JSON 列已解开），没有时为 None。"""
    rows = _read("SELECT * FROM album_update_checks WHERE album_id=?", (str(album_id),))
    if not rows:
        return None
    row = dict(rows[0])
    row["baseline_ids"] = None if row["baseline_ids"] is None else _loads(row["baseline_ids"])
    for key in ("gone_ids", "removed_ids", "new_chapters"):
        row[key] = _loads(row[key])
    return row


def state_of(row: dict | None, checking: bool = False) -> str:
    """一行检查记录的显示状态（优先级从高到低）：checking > failed（仍带着待下载的新章节和上次成功的结论）>
    never（从未成功）> changed > new > baseline > no_update。"""
    if checking:
        return "checking"
    if row and row.get("error_kind"):
        return "failed"
    if not row or row.get("result") is None:
        return "never"
    if row.get("new_count", 0) > 0:
        return "changed" if row.get("removed_ids") else "new"
    return "baseline" if row.get("result") == "baseline" else "no_update"


def _earliest(chapters) -> str | None:
    stamps = [c.get("confirmed_at") for c in chapters if isinstance(c, dict) and c.get("confirmed_at")]
    return min(stamps) if stamps else None


def describe(row: dict | None, auto_enabled: bool, checking: bool, effective_next: str | None,
             paused_until: str | None = None) -> dict:
    """GET /api/updates/<id> 的 update 部分（row 来自 get()）。列表页只看 new_count > 0（summaries）。"""
    row = row or {}
    chapters = [c for c in row.get("new_chapters") or [] if isinstance(c, dict)]
    baseline = row.get("baseline_ids")
    error = None
    if row.get("error_kind"):
        error = {"kind": row["error_kind"], "at": row.get("last_attempt_at"), "fail_count": row.get("fail_count", 0)}
    return {
        "state": state_of(row, checking),
        "last_result": row.get("result"),
        "baseline_count": len(baseline) if baseline is not None else None,
        "baseline_source": row.get("baseline_source") if baseline is not None else None,
        "baseline_at": row.get("baseline_at") if baseline is not None else None,
        "upstream_count": row.get("upstream_count"),
        "removed_count": len(row.get("removed_ids") or []),
        "new_count": len(chapters),
        "new_chapters": chapters,
        "confirmed_at": _earliest(chapters),
        "last_success_at": row.get("last_success_at"),
        "last_attempt_at": row.get("last_attempt_at"),
        "last_trigger": row.get("last_trigger"),
        "error": error,
        "next_check_at": effective_next if auto_enabled else None,
        "paused_until": paused_until if auto_enabled else None,
    }


def summaries(album_ids) -> dict[str, dict]:
    """列表页（资源库、收藏、下载管理）用：只含已确认有新章节（new_count > 0）的漫画，一次查询。
    → {album_id: {state: 'new' | 'changed', new_count, removed_count, confirmed_at, titles: [最多 5 个]}}。
    调用方只传本地可读的 album_id。"""
    params = _ids_param(album_ids)
    if params == "[]":
        return {}
    rows = _read(
        """SELECT album_id, new_chapters, removed_ids FROM album_update_checks
           WHERE album_id IN (SELECT value FROM json_each(?)) AND new_count > 0""",
        (params,),
    )
    result = {}
    for row in rows:
        chapters = [c for c in _loads(row["new_chapters"]) if isinstance(c, dict)]
        if not chapters:
            continue
        removed = _loads(row["removed_ids"])
        result[row["album_id"]] = {
            "state": "changed" if removed else "new",
            "new_count": len(chapters),
            "removed_count": len(removed),
            "confirmed_at": _earliest(chapters),
            "titles": [str(c.get("title") or "") for c in chapters[:5]],
        }
    return result


def _titles(conn, album_ids) -> dict[str, str]:
    """标题：album_meta 里的，没有时用最近一个任务的标题。"""
    rows = conn.execute(
        """SELECT p.value AS album_id,
                  COALESCE(NULLIF(TRIM(m.title), ''),
                           (SELECT j.title FROM jobs j WHERE j.album_id = p.value AND COALESCE(TRIM(j.title), '') != ''
                             ORDER BY j.created_at DESC, j.id DESC LIMIT 1), '') AS title
           FROM json_each(?) p LEFT JOIN album_meta m ON m.album_id = p.value""",
        (_ids_param(album_ids),),
    ).fetchall()
    return {row["album_id"]: row["title"] or "" for row in rows}


def title_of(album_id: str) -> str:
    conn = db.get_db()
    try:
        return _titles(conn, [album_id]).get(str(album_id), "")
    finally:
        conn.close()


def overview(target_ids) -> dict:
    """设置页的统计（只算 target_ids：本地有已下载内容的漫画）：
    targets 部数 / checked 成功检查过 / with_updates 有已确认的新章节（含 changed）/ changed 章节有变动 /
    failing 最近一次没检查成功 / never 还没成功检查过。"""
    targets = json.loads(_ids_param(target_ids))
    counts = {"targets": len(targets), "checked": 0, "with_updates": 0, "changed": 0, "failing": 0, "never": 0}
    if targets:
        rows = _read(
            """SELECT result, new_count, removed_ids, error_kind FROM album_update_checks
               WHERE album_id IN (SELECT value FROM json_each(?))""",
            (json.dumps(targets),),
        )
        for row in rows:
            counts["checked"] += row["result"] is not None
            counts["with_updates"] += row["new_count"] > 0
            counts["changed"] += row["new_count"] > 0 and bool(_loads(row["removed_ids"]))
            counts["failing"] += row["error_kind"] is not None
    counts["never"] = counts["targets"] - counts["checked"]
    return counts


def checks_since(since: datetime) -> int:
    """since 之后开始的检查次数（含失败的）。"""
    return _read("SELECT COUNT(*) FROM update_check_log WHERE started_at >= ?", (_iso(since),))[0][0]


def last_check(target_ids) -> dict | None:
    """target_ids 里最近一次检查（成功或失败）：{album_id, title, at, outcome, new_count, upstream_count, error_kind}。"""
    targets = _ids_param(target_ids)
    conn = db.get_db()
    try:
        row = conn.execute(
            """SELECT album_id, started_at, finished_at, outcome, new_count, upstream_count, error_kind
               FROM update_check_log WHERE album_id IN (SELECT value FROM json_each(?))
               ORDER BY id DESC LIMIT 1""",
            (targets,),
        ).fetchone()
        if row is None:
            return None
        return {
            "album_id": row["album_id"],
            "title": _titles(conn, [row["album_id"]]).get(row["album_id"], ""),
            "at": row["finished_at"] or row["started_at"],
            "outcome": row["outcome"],
            "new_count": row["new_count"],
            "upstream_count": row["upstream_count"],
            "error_kind": row["error_kind"],
        }
    finally:
        conn.close()


def pending_new_chapters(album_ids=None, conn=None) -> list[dict]:
    """PR-B 的输入（不联网、没有副作用）：已确认、可以批量下载的新章节。
    只含 new_count > 0 且没有“章节有变动”（removed_ids 为空）的；调用方再按本地可读过滤。
    conn 传入时在调用方的连接（事务）里读，不关闭它。
    → [{album_id, title, photo_ids: [上游顺序], chapters: [{photo_id, index, title, confirmed_at}],
        confirmed_at: 最早的确认时间, checked_at: 最近一次成功检查}]"""
    sql = ("SELECT album_id, new_chapters, last_success_at FROM album_update_checks "
           "WHERE new_count > 0 AND removed_ids = '[]'")
    params: tuple = ()
    if album_ids is not None:
        sql += " AND album_id IN (SELECT value FROM json_each(?))"
        params = (_ids_param(album_ids),)
    own = conn is None
    conn = conn or db.get_db()
    try:
        rows = conn.execute(sql + " ORDER BY album_id", params).fetchall()
        titles = _titles(conn, [row["album_id"] for row in rows]) if rows else {}
    finally:
        if own:
            conn.close()
    items = []
    for row in rows:
        chapters = [c for c in _loads(row["new_chapters"]) if isinstance(c, dict)]
        if not chapters:
            continue
        items.append({
            "album_id": row["album_id"],
            "title": titles.get(row["album_id"], ""),
            "photo_ids": [str(c.get("photo_id")) for c in chapters],
            "chapters": chapters,
            "confirmed_at": _earliest(chapters),
            "checked_at": row["last_success_at"],
        })
    items.sort(key=lambda item: (item["confirmed_at"] or "", item["album_id"]))
    return items


def changed_albums(conn=None) -> list[dict]:
    """“章节有变动”的漫画（有已确认的新章节，同一次检查里又有已知章节不见了）：不进批量下载，
    只列出来请用户到详情页核对。调用方再按本地可读过滤。conn 传入时复用它（不关闭）。
    → [{album_id, title, new_count, removed_count}]，按 album_id"""
    own = conn is None
    conn = conn or db.get_db()
    try:
        rows = conn.execute(
            "SELECT album_id, new_count, removed_ids FROM album_update_checks "
            "WHERE new_count > 0 AND removed_ids != '[]' ORDER BY album_id").fetchall()
        titles = _titles(conn, [row["album_id"] for row in rows]) if rows else {}
    finally:
        if own:
            conn.close()
    return [{"album_id": row["album_id"], "title": titles.get(row["album_id"], ""),
             "new_count": row["new_count"], "removed_count": len(_loads(row["removed_ids"]))} for row in rows]
