"""
SQLite 数据库操作模块
"""
import json
import os
import sqlite3
from datetime import datetime

from .path_utils import get_app_root

# 资源库最大条目安全上限（防止全表扫描 OOM）
_LIBRARY_MAX_ITEMS = 10000

DB_DIR = get_app_root() / "runtime" / "data"
DB_PATH = DB_DIR / "app.db"


def get_db() -> sqlite3.Connection:
    """获取数据库连接"""
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db():
    """初始化数据库表"""
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT UNIQUE NOT NULL,
                album_id TEXT NOT NULL,
                title TEXT,
                selected_photo_ids TEXT,
                status TEXT NOT NULL DEFAULT 'queued',
                total_pages INTEGER DEFAULT 0,
                done_pages INTEGER DEFAULT 0,
                current_photo TEXT,
                current_image TEXT,
                output_path TEXT,
                error_message TEXT,
                created_at TEXT,
                updated_at TEXT,
                completed_at TEXT,
                superseded_at TEXT
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS search_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword TEXT NOT NULL,
                created_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_search_history_created ON search_history(created_at);

            CREATE TABLE IF NOT EXISTS wishlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                album_id TEXT UNIQUE NOT NULL,
                title TEXT,
                author TEXT,
                cover_url TEXT,
                added_at TEXT,
                download_status TEXT NOT NULL DEFAULT 'none'
            );

            CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
            CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at);
            CREATE INDEX IF NOT EXISTS idx_jobs_status_album_id ON jobs(status, album_id);
            CREATE INDEX IF NOT EXISTS idx_jobs_album_latest ON jobs(album_id, status, created_at DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_wishlist_download_status ON wishlist(download_status);

            CREATE TABLE IF NOT EXISTS album_tags (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                album_id    TEXT    NOT NULL,
                tag         TEXT    NOT NULL,
                source      TEXT    NOT NULL DEFAULT 'auto',
                created_at  TEXT,
                UNIQUE(album_id, tag)
            );
            CREATE INDEX IF NOT EXISTS idx_album_tags_album_id ON album_tags(album_id);
            CREATE INDEX IF NOT EXISTS idx_album_tags_tag ON album_tags(tag);
            CREATE INDEX IF NOT EXISTS idx_album_tags_source ON album_tags(source);

            CREATE TABLE IF NOT EXISTS album_detail_cache (
                album_id TEXT PRIMARY KEY,
                detail_json TEXT NOT NULL,
                cover_cdn_url TEXT DEFAULT '',
                cached_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS album_meta (
                album_id       TEXT PRIMARY KEY,
                title          TEXT DEFAULT '',
                author         TEXT DEFAULT '',
                cover_url      TEXT DEFAULT '',
                tags_synced_at TEXT,
                updated_at     TEXT
            );
        """)
        conn.commit()

        # ── Schema 版本迁移 ──
        _run_migrations(conn)

        # 存量回填：为已完成任务中缺少 album_meta 的条目补全元数据
        backfill_missing_meta()
    finally:
        conn.close()


def _run_migrations(conn):
    """增量 Schema 迁移"""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < 1:
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
    if version < 2:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS album_detail_cache (
                album_id TEXT PRIMARY KEY,
                detail_json TEXT NOT NULL,
                cover_cdn_url TEXT DEFAULT '',
                cached_at TEXT NOT NULL
            );
        """)
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
    # superseded_at（mark_completed_jobs_superseded）：按列是否存在补，不依赖 user_version
    if "superseded_at" not in {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}:
        conn.execute("ALTER TABLE jobs ADD COLUMN superseded_at TEXT")
        conn.commit()


# ─── Jobs 操作 ───

def insert_job(job_id: str, album_id: str, title: str, photo_ids: list):
    """插入一条新任务记录"""
    now = datetime.now().isoformat()
    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO jobs (job_id, album_id, title, selected_photo_ids, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'queued', ?, ?)""",
            (job_id, album_id, title, _json(photo_ids), now, now),
        )
        conn.commit()
    finally:
        conn.close()


_ALLOWED_JOB_COLUMNS = frozenset({
    "status", "album_id", "title", "selected_photo_ids", "total_pages", "done_pages",
    "current_photo", "current_image", "output_path", "error_message", "completed_at", "updated_at",
})


def update_job(job_id: str, **kwargs):
    """更新任务字段（自动设置 updated_at），列名白名单校验防 SQL 注入"""
    kwargs["updated_at"] = datetime.now().isoformat()
    # 白名单校验
    for k in kwargs:
        if k not in _ALLOWED_JOB_COLUMNS:
            raise ValueError(f"非法任务字段: {k}")
    sets = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [job_id]
    conn = get_db()
    try:
        conn.execute(f"UPDATE jobs SET {sets} WHERE job_id=?", vals)
        conn.commit()
    finally:
        conn.close()


def get_job(job_id: str) -> dict | None:
    """根据 job_id 获取单条任务记录"""
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_all_jobs(limit: int = 5000) -> list[dict]:
    """获取全部任务记录（按创建时间倒序），默认上限 5000 条防 OOM"""
    conn = get_db()
    try:
        if limit > 0:
            rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_completed_job_by_album_id(album_id: str) -> dict | None:
    """获取指定 album_id 的已完成任务（取最新的一个，顺序与 core.local_availability 相同），避免全表扫描。"""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM jobs WHERE album_id=? AND status='completed' ORDER BY created_at DESC, id DESC LIMIT 1",
            (album_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def mark_completed_jobs_superseded(album_id: str, output_path: str, by_job_id: str) -> int:
    """任务 by_job_id 重新建出了 output_path（原目录已被删除，或只剩没有图片的空壳）：同一部漫画更早完成、
    写到这个目录的任务，它们下载的文件已经不在了。记下 superseded_at，之后目录里的内容只在写入它的任务完成后
    才算“本地可读”——重新下载失败或被取消时，残缺的几页不会让旧任务重新显示成完整、可离线阅读。
    返回标记的任务数。"""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT job_id, output_path FROM jobs WHERE album_id=? AND status='completed' "
            "AND superseded_at IS NULL AND job_id != ? AND output_path IS NOT NULL AND output_path != ''",
            (album_id, by_job_id),
        ).fetchall()
        job_ids = [row["job_id"] for row in rows if _same_path(row["output_path"], output_path)]
        if job_ids:
            now = datetime.now().isoformat()
            conn.executemany("UPDATE jobs SET superseded_at=? WHERE job_id=?", [(now, j) for j in job_ids])
            conn.commit()
        return len(job_ids)
    finally:
        conn.close()


def get_jobs_by_status(status: str) -> list[dict]:
    """按状态获取任务记录（按创建时间正序）"""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE status=? ORDER BY created_at ASC", (status,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def claim_next_queued_job(excluded_album_ids=()) -> dict | None:
    """原子地获取并锁定下一个 queued 任务（防双重调度）。

    将第一个 queued 任务的状态更新为 'running'，并返回任务记录。
    如果已被其他调度器抢先，返回 None。
    """
    conn = get_db()
    try:
        excluded_album_ids = tuple(excluded_album_ids)
        exclusion = (" AND album_id NOT IN (" + ",".join("?" for _ in excluded_album_ids) + ")"
                     if excluded_album_ids else "")
        row = conn.execute(
            "SELECT * FROM jobs WHERE status='queued'" + exclusion + " ORDER BY created_at ASC, id ASC LIMIT 1",
            excluded_album_ids,
        ).fetchone()
        if not row:
            return None
        job = dict(row)
        now = datetime.now().isoformat()
        cur = conn.execute(
            "UPDATE jobs SET status='running', updated_at=? WHERE job_id=? AND status='queued'",
            (now, job["job_id"]),
        )
        conn.commit()
        if cur.rowcount == 0:
            return None  # 已被其他调度器抢走
        job["status"] = "running"
        return job
    finally:
        conn.close()


def transition_job_status(job_id: str, from_statuses: list[str], to_status: str, **extra) -> bool:
    """原子状态转换：仅在当前状态在 from_statuses 中时才更新到 to_status。
    返回 True 表示转换成功，False 表示状态不匹配（可能已被其他操作修改）。"""
    # 禁止 extra 覆盖基础字段（status/updated_at 由函数自动管理）
    _BASE_JOB_COLUMNS = frozenset({"status", "updated_at"})
    for k in extra:
        if k in _BASE_JOB_COLUMNS:
            raise ValueError(f"transition_job_status: 不允许手动设置 {k}")
        if k not in _ALLOWED_JOB_COLUMNS:
            raise ValueError(f"transition_job_status: 非法字段 {k}")
    sets = "status=?, updated_at=?"
    vals = [to_status, datetime.now().isoformat()]
    for k, v in extra.items():
        sets += f", {k}=?"
        vals.append(v)
    vals.append(job_id)
    placeholders = ",".join("?" for _ in from_statuses)
    conn = get_db()
    try:
        cur = conn.execute(
            f"UPDATE jobs SET {sets} WHERE job_id=? AND status IN ({placeholders})",
            vals + from_statuses,
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def _refresh_wishlist_after_job_delete(conn, album_ids):
    """Derive bookmark status from remaining jobs using the same deletion transaction."""
    conn.executemany(
        """UPDATE wishlist SET download_status=COALESCE((
            SELECT CASE
                WHEN status='completed' THEN 'completed'
                WHEN status IN ('running', 'paused') THEN 'downloading'
                WHEN status='queued' THEN 'queued'
                WHEN status='failed' THEN 'failed'
                ELSE 'none' END
            FROM jobs WHERE jobs.album_id=wishlist.album_id
            ORDER BY CASE
                WHEN status='completed' THEN 0
                WHEN status IN ('running', 'paused') THEN 1
                WHEN status='queued' THEN 2 ELSE 3 END,
                created_at DESC, id DESC
            LIMIT 1
        ), 'none') WHERE album_id=?""",
        [(album_id,) for album_id in album_ids],
    )


def delete_job(job_id: str):
    """Delete one record and recompute the affected bookmark status atomically."""
    conn = get_db()
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT album_id FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            conn.execute("DELETE FROM jobs WHERE job_id=?", (job_id,))
            if row:
                _refresh_wishlist_after_job_delete(conn, [row["album_id"]])
    finally:
        conn.close()


def clear_jobs_by_status(status: str | list[str]) -> int:
    """Delete matching records, then derive affected bookmark states from remaining jobs."""
    statuses = [status] if isinstance(status, str) else list(status)
    if not statuses:
        return 0
    placeholders = ",".join("?" for _ in statuses)
    conn = get_db()
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            affected = conn.execute(
                f"SELECT DISTINCT album_id FROM jobs WHERE status IN ({placeholders})", statuses,
            ).fetchall()
            deleted = conn.execute(
                f"DELETE FROM jobs WHERE status IN ({placeholders})", statuses,
            ).rowcount
            _refresh_wishlist_after_job_delete(conn, [row["album_id"] for row in affected])
        return deleted
    finally:
        conn.close()


# ─── Settings 操作 ───

def get_setting(key: str, default=None) -> str | None:
    """获取单个设置值"""
    conn = get_db()
    try:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default
    finally:
        conn.close()


def set_setting(key: str, value: str):
    """设置（覆盖）单个设置值"""
    conn = get_db()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value))
        )
        conn.commit()
    finally:
        conn.close()


def get_all_settings() -> dict:
    """获取全部设置项"""
    conn = get_db()
    try:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}
    finally:
        conn.close()


# ─── Wishlist 操作 ───


def add_wishlist(album_id: str, title: str = "", author: str = "", cover_url: str = "") -> bool:
    """添加收藏，返回 True 表示新增，False 表示已存在"""
    now = datetime.now().isoformat()
    conn = get_db()
    try:
        cur = conn.execute(
            """INSERT OR IGNORE INTO wishlist (album_id, title, author, cover_url, added_at, download_status)
               VALUES (?, ?, ?, ?, ?, 'none')""",
            (album_id, title, author, cover_url, now),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def remove_wishlist(album_id: str) -> bool:
    """移除收藏"""
    conn = get_db()
    try:
        cur = conn.execute("DELETE FROM wishlist WHERE album_id=?", (album_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_wishlist(album_id: str) -> dict | None:
    """获取单个收藏条目"""
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM wishlist WHERE album_id=?", (album_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ── 收藏清单：下载状态分组 + 排序白名单（api_wishlist 与测试共用） ──
# 每条收藏恰好落在一个分组里（优先级自上而下）：
#   readable 本地可离线阅读 —— core.local_availability 的共用规则，由调用方算好 readable_ids 传入；
#            能读就算 readable，哪怕之后又排了新任务或新任务失败了
#   active   有排队 / 下载中 / 已暂停的任务
#   failed   最近一次任务失败
#   none     其余：从未下载、已取消，或下载过但本地文件已不在（files_missing）
# 任务表是事实来源；某部漫画一条任务都没有时（任务被清理 / 旧数据），才退回看 wishlist.download_status。
WISHLIST_STATUS_GROUPS = ("readable", "active", "failed", "none")
WISHLIST_SORTS = {
    "added_at": "added_at IS NULL, added_at DESC, id DESC",  # 最新添加（默认）
    "added_asc": "added_at IS NULL, added_at ASC, id ASC",  # 最早添加
    "title": "title = '', LOWER(title), id DESC",  # 没有标题的排最后
    "author": "author = '', LOWER(author), title = '', LOWER(title), id DESC",  # 没有作者的排最后
    "status": ("CASE status_group WHEN 'readable' THEN 0 WHEN 'active' THEN 1 WHEN 'failed' THEN 2 "
               "ELSE 3 END, added_at IS NULL, added_at DESC, id DESC"),
}
_WISHLIST_ACTIVITY = {"running": "downloading", "paused": "paused", "queued": "queued"}
_LEGACY_ACTIVE = ("queued", "downloading", "running", "paused")


def _job_facts_sql(album_id_expr: str) -> str:
    """某部漫画的任务事实（收藏与资源库共用）：进行中的任务、最近一次任务、是否完成过。
    album_id_expr 只接受代码里的列名常量（如 w.album_id），不接受外部输入。"""
    return f"""
               (SELECT j.status FROM jobs j
                 WHERE j.album_id = {album_id_expr} AND j.status IN ('running', 'paused', 'queued')
                 ORDER BY CASE j.status WHEN 'running' THEN 0 WHEN 'paused' THEN 1 ELSE 2 END
                 LIMIT 1) AS active_job,
               (SELECT j.status FROM jobs j WHERE j.album_id = {album_id_expr}
                 ORDER BY j.created_at DESC, j.id DESC LIMIT 1) AS latest_job,
               EXISTS (SELECT 1 FROM jobs j
                        WHERE j.album_id = {album_id_expr} AND j.status = 'completed') AS has_completed"""


def download_state(readable, active_job, latest_job, has_completed, legacy_status) -> dict:
    """收藏与资源库共用的下载状态 → {status_group, activity, files_missing}。

    status_group 的规则与下面 _WISHLIST_ITEMS_CTE 里的 CASE 相同（收藏的筛选/排序/计数在 SQL 里做；
    tests/test_lists.py 保证两边一致，资源库与收藏对同一部漫画显示同一个状态）。
    activity：进行中任务的状态（queued / downloading / paused），可读时也可能有（在更新/补章节）。
    files_missing：下载过（有完成的任务，或旧数据标着已完成）但现在读不到本地文件，且没有更新的任务。
    legacy_status 是 wishlist.download_status（不在收藏里时为空），只在一条任务都没有时才参考。
    """
    legacy = (legacy_status or "").strip().lower() or "none"
    if readable:
        group = "readable"
    elif active_job or (latest_job is None and legacy in _LEGACY_ACTIVE):
        group = "active"
    elif latest_job == "failed" or (latest_job is None and legacy == "failed"):
        group = "failed"
    else:
        group = "none"
    if active_job:
        activity = _WISHLIST_ACTIVITY[active_job]
    elif group == "active":  # 没有任务记录，按旧的状态列
        activity = "downloading" if legacy in ("downloading", "running") else (
            "paused" if legacy == "paused" else "queued")
    else:
        activity = None
    files_missing = group == "none" and bool(
        has_completed or (latest_job is None and legacy in ("completed", "downloaded")))
    return {"status_group": group, "activity": activity, "files_missing": files_missing}


def _status_group_sql(legacy: str) -> str:
    """download_state 的 status_group 规则的 SQL 版（收藏与资源库的筛选/计数共用）。
    需要 readable / active_job / latest_job 三列（_job_facts_sql）；legacy 是已 LOWER(TRIM()) 的旧状态列名，
    只接受代码里的列名常量。"""
    return f"""CASE
            WHEN readable THEN 'readable'
            WHEN active_job IS NOT NULL THEN 'active'
            WHEN latest_job IS NULL AND {legacy} IN ('queued', 'downloading', 'running', 'paused')
                THEN 'active'
            WHEN latest_job = 'failed' THEN 'failed'
            WHEN latest_job IS NULL AND {legacy} = 'failed' THEN 'failed'
            ELSE 'none' END"""


def _files_missing_sql(legacy: str) -> str:
    """download_state 的 files_missing 规则的 SQL 版（仅在 status_group = 'none' 时有意义）。"""
    return f"(has_completed OR (latest_job IS NULL AND {legacy} IN ('completed', 'downloaded')))"


# 标题/作者/封面为空（如批量导入的车号）时用 album_meta 缓存补上，搜索和排序都按补齐后的值
_WISHLIST_ITEMS_CTE = f"""
    WITH readable_ids(album_id) AS (SELECT value FROM json_each(?)),
    facts AS (
        SELECT w.id, w.album_id,
               COALESCE(NULLIF(TRIM(w.title), ''), NULLIF(TRIM(m.title), ''), '') AS title,
               COALESCE(NULLIF(TRIM(w.author), ''), NULLIF(TRIM(m.author), ''), '') AS author,
               COALESCE(NULLIF(w.cover_url, ''), NULLIF(m.cover_url, ''), '') AS cover_url,
               w.added_at,
               COALESCE(NULLIF(LOWER(TRIM(w.download_status)), ''), 'none') AS download_status,
               w.album_id IN (SELECT album_id FROM readable_ids) AS readable,{_job_facts_sql('w.album_id')}
        FROM wishlist w
        LEFT JOIN album_meta m ON m.album_id = w.album_id
    ),
    items AS (
        SELECT facts.*, {_status_group_sql('download_status')} AS status_group
        FROM facts
    )
"""


def _wishlist_item(row) -> dict:
    """SQL 行 → API 条目：readable 转 bool，附上 status_group / activity / files_missing（download_state）。"""
    item = dict(row)
    item["readable"] = bool(item["readable"])
    item.update(download_state(
        item["readable"], item.pop("active_job"), item.pop("latest_job"),
        item.pop("has_completed"), item["download_status"],
    ))
    return item


def get_all_wishlist(
    page: int = 1, page_size: int = 50,
    keyword: str = "", sort: str = "added_at",
    status: str | None = None, readable_ids=None,
) -> dict:
    """获取收藏列表（分页）：关键词搜索 + 下载状态筛选 + 排序，全部在 SQL 里完成。

    status 取 WISHLIST_STATUS_GROUPS 之一（其他值 = 不筛选）；sort 取 WISHLIST_SORTS 的键（其他值 = 最新添加）。
    readable_ids 是本地可读的 album_id 集合（core.local_availability），决定 readable 分组。
    返回 {items, total, page, page_size, group_counts}；total 与 group_counts 都已按关键词过滤，
    total 另按 status 过滤。每项含 status_group / readable / activity / files_missing。
    """
    page = max(1, page)
    page_size = max(1, min(200, page_size))
    order = WISHLIST_SORTS.get(sort, WISHLIST_SORTS["added_at"])
    if status not in WISHLIST_STATUS_GROUPS:
        status = None

    params: list = [json.dumps(sorted({str(a) for a in (readable_ids or ())}))]
    where = ""
    kw = (keyword or "").strip()
    if kw:
        like = f"%{_escape_like(kw)}%"
        where += " AND (title LIKE ? ESCAPE ? OR author LIKE ? ESCAPE ? OR album_id LIKE ? ESCAPE ?)"
        params.extend([like, chr(92)] * 3)

    conn = get_db()
    try:
        counts = {group: 0 for group in WISHLIST_STATUS_GROUPS}
        for row in conn.execute(
            f"{_WISHLIST_ITEMS_CTE} SELECT status_group, COUNT(*) FROM items WHERE 1=1{where} "
            "GROUP BY status_group", params,
        ):
            counts[row[0]] = row[1]
        total = counts[status] if status else sum(counts.values())
        if status:
            where += " AND status_group = ?"
            params.append(status)
        rows = conn.execute(
            f"{_WISHLIST_ITEMS_CTE} SELECT * FROM items WHERE 1=1{where} ORDER BY {order} LIMIT ? OFFSET ?",
            params + [page_size, (page - 1) * page_size],
        ).fetchall() if total else []
    finally:
        conn.close()
    return {
        "items": [_wishlist_item(r) for r in rows],
        "total": total, "page": page, "page_size": page_size,
        "group_counts": counts,
    }


def get_completed_album_ids(wishlist_only: bool = False) -> list[str]:
    """有已完成下载任务的 album_id —— 本地可读判定的候选（是否真能读由 core.local_availability 决定）。"""
    sql = "SELECT DISTINCT album_id FROM jobs WHERE status='completed'"
    if wishlist_only:
        sql += " AND album_id IN (SELECT album_id FROM wishlist)"
    conn = get_db()
    try:
        return [row[0] for row in conn.execute(sql + " ORDER BY album_id")]
    finally:
        conn.close()


def export_all_wishlist() -> list[dict]:
    """获取全部收藏条目（不分页），用于导出"""
    conn = get_db()
    try:
        rows = conn.execute("SELECT * FROM wishlist ORDER BY added_at DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def batch_check_wishlist(album_ids: list[str]) -> dict[str, str]:
    """批量查询 album_id 列表的收藏状态，返回 {album_id: download_status, ...}"""
    if not album_ids:
        return {}
    conn = get_db()
    try:
        placeholders = ",".join("?" for _ in album_ids)
        rows = conn.execute(
            f"SELECT album_id, download_status FROM wishlist WHERE album_id IN ({placeholders})",
            album_ids,
        ).fetchall()
        return {r["album_id"]: r["download_status"] for r in rows}
    finally:
        conn.close()


def update_wishlist_download_status(album_id: str, status: str):
    """更新收藏的下载状态"""
    conn = get_db()
    try:
        conn.execute(
            "UPDATE wishlist SET download_status=? WHERE album_id=?",
            (status, album_id),
        )
        conn.commit()
    finally:
        conn.close()


def update_wishlist_title(album_id: str, title: str = "", author: str = "", cover_url: str = ""):
    """更新收藏条目的标题/作者/封面"""
    conn = get_db()
    try:
        sets = []
        vals = []
        if title:
            sets.append("title=?")
            vals.append(title)
        if author:
            sets.append("author=?")
            vals.append(author)
        if cover_url:
            sets.append("cover_url=?")
            vals.append(cover_url)
        if sets:
            vals.append(album_id)
            conn.execute(f"UPDATE wishlist SET {','.join(sets)} WHERE album_id=?", vals)
            conn.commit()
    finally:
        conn.close()


def get_wishlist_downloading_count() -> int:
    """获取下载中的收藏数量"""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM wishlist WHERE download_status IN ('queued','downloading')"
        ).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


# ─── Search History ───

def add_search_history(keyword: str):
    """添加一条搜索历史记录（最多保留 200 条，自动清理最旧的）"""
    conn = get_db()
    try:
        conn.execute("BEGIN")
        conn.execute(
            "INSERT INTO search_history (keyword, created_at) VALUES (?, ?)",
            (keyword, datetime.now().isoformat()),
        )
        # 原子清理：单事务内完成插入和清理，防止并发写入超过 200 条
        conn.execute("""
            DELETE FROM search_history WHERE id NOT IN (
                SELECT id FROM search_history ORDER BY created_at DESC LIMIT 200
            )
        """)
        conn.commit()
    finally:
        conn.close()


def get_search_history(limit: int = 20) -> list[dict]:
    """获取最近的搜索历史记录（相同关键词合并，只保留最新的）"""
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT keyword, MAX(created_at) as created_at
               FROM search_history
               GROUP BY keyword
               ORDER BY created_at DESC LIMIT ?""", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def delete_search_history(keyword: str) -> bool:
    """删除指定关键词的所有搜索历史"""
    conn = get_db()
    try:
        cur = conn.execute("DELETE FROM search_history WHERE keyword=?", (keyword,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def clear_search_history() -> int:
    """清空全部搜索历史，返回删除条数"""
    conn = get_db()
    try:
        cur = conn.execute("DELETE FROM search_history")
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def _json(obj) -> str:
    """辅助函数：将对象序列化为 JSON 字符串"""
    return json.dumps(obj, ensure_ascii=False)


# ─── Album Tags CRUD ───


def add_album_tag(album_id: str, tag: str, source: str = 'auto') -> bool:
    """添加单个标签。返回 True 表示新增，False 表示已存在或无效。"""
    tag = tag.strip().lower()[:50]
    if not tag:
        return False
    now = datetime.now().isoformat()
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO album_tags (album_id, tag, source, created_at) VALUES (?, ?, ?, ?)",
            (album_id, tag, source, now),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def remove_album_tag(album_id: str, tag: str) -> bool:
    """删除单个标签。返回 True 表示已删除，False 表示不存在。"""
    conn = get_db()
    try:
        cur = conn.execute(
            "DELETE FROM album_tags WHERE album_id=? AND tag=?", (album_id, tag.strip().lower())
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_album_tags(album_id: str) -> list[dict]:
    """获取某个专辑的所有标签，返回 [{tag, source, created_at}, ...]"""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT tag, source, created_at FROM album_tags WHERE album_id=? ORDER BY source, tag",
            (album_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_all_tags(min_count: int = 1, library_only: bool = True) -> list[dict]:
    """获取所有标签及其计数（用于标签云），按 count DESC 排序。
    返回 [{tag, count, auto_count, user_count}, ...] 最多 100 个。

    Args:
        min_count: 最低出现次数过滤。
        library_only: 为 True 时只统计 album_id 仍在资源库（wishlist 或 completed jobs）中的标签。
    """
    conn = get_db()
    try:
        if library_only:
            rows = conn.execute(
                """
                SELECT tag, COUNT(*) as count,
                       SUM(CASE WHEN source='auto' THEN 1 ELSE 0 END) as auto_count,
                       SUM(CASE WHEN source='user' THEN 1 ELSE 0 END) as user_count
                FROM album_tags
                WHERE album_id IN (
                    SELECT album_id FROM wishlist
                    UNION
                    SELECT DISTINCT album_id FROM jobs WHERE status='completed'
                )
                GROUP BY tag
                HAVING count >= ?
                ORDER BY count DESC
                LIMIT 100
                """,
                (min_count,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT tag, COUNT(*) as count,
                       SUM(CASE WHEN source='auto' THEN 1 ELSE 0 END) as auto_count,
                       SUM(CASE WHEN source='user' THEN 1 ELSE 0 END) as user_count
                FROM album_tags
                GROUP BY tag
                HAVING count >= ?
                ORDER BY count DESC
                LIMIT 100
                """,
                (min_count,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def search_library_tags(keyword: str, limit: int = 20) -> list[dict]:
    """Search all library tags before applying the result limit, not just the tag cloud."""
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT tag, COUNT(*) AS count FROM album_tags
               WHERE tag LIKE ? ESCAPE ? AND album_id IN (
                   SELECT album_id FROM wishlist UNION
                   SELECT album_id FROM jobs WHERE status='completed')
               GROUP BY tag ORDER BY count DESC, tag ASC LIMIT ?""",
            ("%" + _escape_like(keyword.strip().lower()) + "%", chr(92), max(1, min(limit, 100))),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_albums_by_tag(tag: str) -> list[str]:
    """获取含有某标签的所有 album_id 列表"""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT album_id FROM album_tags WHERE tag=? ORDER BY album_id", (tag.strip().lower(),)
        ).fetchall()
        return [r["album_id"] for r in rows]
    finally:
        conn.close()


def batch_sync_auto_tags(album_id: str, tags: list) -> int:
    """同步自动标签：删除该 album 下所有 source='auto' 的标签，
    然后插入当前 tags 列表。保留用户自定义标签。
    返回写入的标签数。
    """
    conn = get_db()
    try:
        now = datetime.now().isoformat()
        # 删除所有 auto 标签
        conn.execute(
            "DELETE FROM album_tags WHERE album_id=? AND source='auto'", (album_id,)
        )
        # 插入新标签
        count = 0
        for tag in tags:
            tag = tag.strip().lower()[:50]
            if not tag:
                continue
            try:
                conn.execute(
                    "INSERT INTO album_tags (album_id, tag, source, created_at) VALUES (?, ?, 'auto', ?)",
                    (album_id, tag, now),
                )
                count += 1
            except sqlite3.IntegrityError:
                pass
        conn.commit()
        # 更新 album_meta 的同步时间戳
        conn.execute(
            "UPDATE album_meta SET tags_synced_at=? WHERE album_id=?", (now, album_id)
        )
        conn.commit()
        return count
    finally:
        conn.close()


def clear_album_tags(album_id: str, source: str | None = None) -> int:
    """清除某专辑的标签，可指定只清除特定来源。返回删除数。"""
    conn = get_db()
    try:
        if source:
            cur = conn.execute(
                "DELETE FROM album_tags WHERE album_id=? AND source=?", (album_id, source)
            )
        else:
            cur = conn.execute("DELETE FROM album_tags WHERE album_id=?", (album_id,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()




# ─── Library 查询（SQL 分页版 2026-07） ───


def _escape_like(s: str) -> str:
    """转义 LIKE 通配符 _ 和 %，使用 \\ 作为转义字符"""
    return s.replace("\\", "\\\\").replace("_", "\\_").replace("%", "\\%")



def _make_kw_sql(kw: str) -> str:
    """生成关键词过滤的 WHERE 子句（ESCAPE 用 ? 参数避免字符串字面量歧义）"""
    if not kw:
        return ""
    return """
        AND (
            LOWER(ai.title) LIKE ? ESCAPE ?
            OR LOWER(ai.author) LIKE ? ESCAPE ?
            OR ai.album_id LIKE ? ESCAPE ?
        )
    """


def _make_kw_in_tags_sql(kw: str) -> str:
    """生成关键词在标签中的过滤子句（ESCAPE 用 ? 参数）"""
    if not kw:
        return ""
    return """
        AND (
            EXISTS (
                SELECT 1 FROM album_tags t
                WHERE t.album_id = ai.album_id
                AND LOWER(t.tag) LIKE ? ESCAPE ?
            )
        )
    """


def _make_kw_combined_sql(kw: str) -> str:
    """生成关键词 OR 组合过滤子句（标题/作者/ID 或 标签匹配任一即可）"""
    if not kw:
        return ""
    return """
        AND (
            LOWER(ai.title) LIKE ? ESCAPE ?
            OR LOWER(ai.author) LIKE ? ESCAPE ?
            OR ai.album_id LIKE ? ESCAPE ?
            OR EXISTS (
                SELECT 1 FROM album_tags t
                WHERE t.album_id = ai.album_id
                AND LOWER(t.tag) LIKE ? ESCAPE ?
            )
        )
    """


# 资源库“下载状态”筛选：与收藏清单同一套分组（download_state / _status_group_sql：任务表说了算，
# 一条任务都没有时才看收藏里的旧状态列），收藏的“未下载”在这里按 files_missing 再分成两档。
# 五档互不重叠，合起来正好是全部，每档也正是卡片上显示的徽章：
#   readable     可离线阅读          —— status_group readable
#   active       排队中 / 下载中      —— status_group active（含已暂停；含旧状态列写着排队/下载中、但没有任务记录的收藏）
#   failed       失败                —— status_group failed（含旧状态列写着失败、但没有任务记录的收藏）
#   missing      下载过 · 文件已删除  —— status_group none 且 files_missing
#   undownloaded 未下载              —— status_group none 且不是 files_missing（从未下载 / 已取消）
LIBRARY_STATUS_FILTERS = ("readable", "active", "failed", "missing", "undownloaded")


def _library_status_where(status: str) -> str:
    """资源库状态筛选的 WHERE 子句（作用于 _grouped；status 已按白名单校验，分组值走 ? 参数）。"""
    if status == "missing":
        return "WHERE status_group = ? AND files_missing"
    if status == "undownloaded":
        return "WHERE status_group = ? AND NOT files_missing"
    return "WHERE status_group = ?"


def _library_status_group_param(status: str) -> str:
    return "none" if status in ("missing", "undownloaded") else status


def get_library(
    page: int = 1,
    page_size: int = 50,
    tag: str | None = None,
    keyword: str = "",
    status: str | None = None,
    sort: str = "updated_at",
    album_id: str | None = None,
    author: str | None = None,
    readable_ids=None,
) -> dict:
    """获取资源库列表 — SQL 分页下沉版。

    数据源 = wishlist（收藏）UNION completed jobs（已下载但未收藏）。
    SQL 做合并+过滤+排序+分页，Python 只做标签绑定（本地文件检查在 routes/api_library，与其他页面同一规则）。
    album_id 传入时按精确匹配过滤（供 /api/library/<album_id> 单条查询使用，
    避免 keyword LIKE 模糊匹配命中其他条目）。
    author 传入时只看该作者（去首尾空格、ASCII 不分大小写的精确匹配）；
    status 取 LIBRARY_STATUS_FILTERS 之一时只看这一档（其他值 = 不筛选），分组规则与收藏清单相同；
    readable_ids 是本地可读的 album_id 集合（由调用方按 core.local_availability 算好），决定“可离线阅读”，
    按状态筛选时必须传入整个资源库的可读集合（能读的漫画不会落进 失败 / 排队中 这些档）。
    sort="author" 按作者升序、同作者按标题，没有作者的排最后。

    返回 {items, total, page, page_size}
    每项包含: {album_id, title, author, cover_url, download_status,
               tags: [{tag, source}], is_wishlisted, added_at}
    """
    page = max(1, page)
    page_size = max(1, min(200, page_size))
    offset = (page - 1) * page_size

    kw = keyword.strip().lower() if keyword else ""
    # 标签统一小写存储（add_album_tag 会 lower()），查询侧同样归一化，否则大写输入永远查不到
    wanted_tags = list(dict.fromkeys(t.strip().lower() for t in tag.split(",") if t.strip())) if tag else []
    tag_count = len(wanted_tags)
    author = author.strip() if author else ""
    if status not in LIBRARY_STATUS_FILTERS:
        status = None

    conn = get_db()
    try:
        # ── 构建 tag AND 子查询 ──
        if wanted_tags:
            tag_placeholders = ",".join("?" for _ in wanted_tags)
            tag_having_sql = f"""
                AND (
                    SELECT COUNT(DISTINCT t.tag) FROM album_tags t
                    WHERE t.album_id = ai.album_id
                    AND t.tag IN ({tag_placeholders})
                ) = ?
            """
        else:
            tag_having_sql = ""
            tag_placeholders = ""

        # ── 排序子句 ──
        sort_lower = sort.lower()
        if sort_lower == "title":
            order_clause = "ORDER BY LOWER(_base.title) ASC, _base.album_id ASC"
        elif sort_lower == "added_at":
            order_clause = "ORDER BY _base.added_at DESC, _base.album_id ASC"
        elif sort_lower == "album_id":
            order_clause = "ORDER BY _base.album_id ASC"
        elif sort_lower == "author":
            order_clause = (
                "ORDER BY TRIM(_base.author) = '' ASC, LOWER(TRIM(_base.author)) ASC, "
                "TRIM(_base.title) = '' ASC, LOWER(_base.title) ASC, _base.album_id ASC"
            )
        else:  # updated_at (default)
            order_clause = (
                "ORDER BY COALESCE(NULLIF(_base.updated_at, ''), _base.added_at, '') DESC, _base.album_id ASC"
            )

        # ── 完整查询 SQL（公共 CTE + _base；列表与计数共用） ──
        cte_sql = f"""
            WITH wishlist_ext AS (
                SELECT
                    w.album_id,
                    -- 收藏里没填的标题/作者/封面（如批量导入的车号）用 album_meta 缓存补上，作者排序/筛选才有意义
                    COALESCE(NULLIF(w.title, ''), NULLIF(wm.title, ''), '') AS title,
                    COALESCE(NULLIF(w.author, ''), NULLIF(wm.author, ''), '') AS author,
                    COALESCE(NULLIF(w.cover_url, ''), NULLIF(wm.cover_url, ''), '') AS cover_url,
                    CASE WHEN jc.album_id IS NOT NULL THEN 'completed' ELSE w.download_status END AS download_status,
                    w.added_at,
                    '' AS updated_at,
                    1 AS is_wishlisted
                FROM wishlist w
                LEFT JOIN (
                    SELECT DISTINCT album_id FROM jobs WHERE status='completed'
                ) jc ON w.album_id = jc.album_id
                LEFT JOIN album_meta wm ON wm.album_id = w.album_id
            ),
            completed_ext AS (
                SELECT
                    j.album_id,
                    COALESCE(NULLIF(m.title, ''), '') AS title,
                    COALESCE(NULLIF(m.author, ''), '') AS author,
                    COALESCE(NULLIF(m.cover_url, ''), '') AS cover_url,
                    'completed' AS download_status,
                    '' AS added_at,
                    COALESCE(NULLIF(m.updated_at, ''), '') AS updated_at,
                    0 AS is_wishlisted
                FROM (SELECT DISTINCT album_id FROM jobs WHERE status='completed') j
                LEFT JOIN album_meta m ON j.album_id = m.album_id
                WHERE j.album_id NOT IN (SELECT album_id FROM wishlist)
            ),
            all_items AS (
                SELECT we.* FROM wishlist_ext we
                UNION
                SELECT ce.* FROM completed_ext ce
            ),
            _matched AS (
                SELECT ai.* FROM all_items ai
                WHERE 1=1
                  {_make_kw_combined_sql(kw)}
                  {tag_having_sql}
                  {"AND LOWER(TRIM(ai.author)) = LOWER(?)" if author else ""}
                  {"AND ai.album_id = ?" if album_id else ""}
            ),
        """
        if status:
            # 按状态筛选：先按其他条件缩小范围，再给每条算出与收藏相同的分组（_status_group_sql）
            cte_sql += f"""
            _facts AS (
                SELECT m.*,
                       m.album_id IN (SELECT value FROM json_each(?)) AS readable,
                       COALESCE(NULLIF(LOWER(TRIM(w.download_status)), ''), 'none') AS legacy_status,{_job_facts_sql('m.album_id')}
                FROM _matched m
                LEFT JOIN wishlist w ON w.album_id = m.album_id
            ),
            _grouped AS (
                SELECT f.*, {_status_group_sql('legacy_status')} AS status_group,
                       {_files_missing_sql('legacy_status')} AS files_missing
                FROM _facts f
            ),
            _base AS (
                SELECT album_id, title, author, cover_url, download_status, added_at, updated_at, is_wishlisted
                FROM _grouped
                {_library_status_where(status)}
            )
            """
        else:
            cte_sql += """
            _base AS (SELECT * FROM _matched)
            """
        query_sql = f"{cte_sql} SELECT _base.* FROM _base {order_clause} LIMIT ? OFFSET ?"
        # COUNT 查询复用同一组 CTE（不 ORDER / LIMIT）；任务事实子查询里本身带 ORDER BY，不能再按它切字符串
        count_sql = f"{cte_sql} SELECT COUNT(*) FROM _base"

        # ── 参数列表（必须与 SQL 中 ? 顺序一致）──
        params = []

        if kw:
            like_val = f"%{_escape_like(kw)}%"
            esc = chr(92)  # 反斜杠作为 LIKE 转义字符
            # 8 个 ?: title LIKE, ESCAPE, author LIKE, ESCAPE, album_id LIKE, ESCAPE, tag LIKE, ESCAPE
            params.extend([like_val, esc, like_val, esc, like_val, esc, like_val, esc])

        if wanted_tags:
            params.extend(wanted_tags)
            params.append(tag_count)

        if author:
            params.append(author)

        if album_id:
            params.append(album_id)

        if status:
            params.append(json.dumps(sorted({str(a) for a in (readable_ids or ())})))
            params.append(_library_status_group_param(status))

        total_row = conn.execute(count_sql, params).fetchone()
        total = total_row[0] if total_row else 0

        # ── 分页查询 ──
        page_rows = conn.execute(query_sql, params + [page_size, offset]).fetchall() if total > 0 else []

        if not page_rows:
            return {"items": [], "total": total, "page": page, "page_size": page_size}

        items_list = [dict(r) for r in page_rows]

        # ── Phase 2: 标签绑定 + 任务事实（本地文件由调用方按 core.local_availability 判断） ──
        album_ids = [it["album_id"] for it in items_list]
        tags_by_album: dict[str, list] = {}
        if album_ids:
            ph = ",".join("?" for _ in album_ids)
            tag_rows = conn.execute(
                f"SELECT album_id, tag, source FROM album_tags WHERE album_id IN ({ph})",
                album_ids,
            ).fetchall()
            for tr in tag_rows:
                aid = tr["album_id"]
                if aid not in tags_by_album:
                    tags_by_album[aid] = []
                tags_by_album[aid].append({"tag": tr["tag"], "source": tr["source"]})

        # One query for the current page instead of one connection/SELECT per album:
        # 与收藏同一套任务事实（进行中 / 最近一次 / 是否完成过）+ 收藏里的旧状态列
        fact_rows = conn.execute(
            f"""SELECT p.value AS album_id,
                       w.download_status AS legacy_status,{_job_facts_sql('p.value')}
                FROM json_each(?) p
                LEFT JOIN wishlist w ON w.album_id = p.value""",
            (json.dumps(album_ids),),
        ).fetchall()
        facts = {row["album_id"]: dict(row) for row in fact_rows}
        result_items = []
        for it in items_list:
            aid = it["album_id"]
            it["tags"] = tags_by_album.get(aid, [])
            fact = facts.get(aid, {})
            # 调用方（routes/api_library）判断本地可读后，交给 download_state 算出显示用的状态，
            # 已完成条目的 file_exists 也取同一个结果（原来只看目录在不在：空目录会同时报 file_exists 与 files_missing）
            it["_facts"] = {
                "active_job": fact.get("active_job"),
                "latest_job": fact.get("latest_job"),
                "has_completed": bool(fact.get("has_completed")),
                "legacy_status": fact.get("legacy_status"),
            }
            result_items.append(it)

        return {
            "items": result_items,
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    finally:
        conn.close()


def get_library_stat() -> dict:
    """返回资源库统计：total, wishlist_count, downloaded_count, tag_count"""
    conn = get_db()
    try:
        wishlist_count = conn.execute("SELECT COUNT(*) FROM wishlist").fetchone()[0]
        downloaded_count = conn.execute(
            "SELECT COUNT(DISTINCT album_id) FROM jobs WHERE status='completed'"
        ).fetchone()[0]
        tag_count = conn.execute(
            "SELECT COUNT(DISTINCT tag) FROM album_tags"
        ).fetchone()[0]

        # total = wishlist + completed jobs not in wishlist
        total = conn.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT album_id FROM wishlist
                UNION
                SELECT DISTINCT album_id FROM jobs WHERE status='completed'
            )
            """
        ).fetchone()[0]

        return {
            "total": total,
            "wishlist_count": wishlist_count,
            "downloaded_count": downloaded_count,
            "tag_count": tag_count,
        }
    finally:
        conn.close()


# ─── Album Meta Cache ───


def upsert_album_meta(
    album_id: str, title: str = "", author: str = "", cover_url: str = ""
) -> bool:
    """更新/插入 album 元数据缓存。用于非 wishlist 的已下载漫画也能显示信息。"""
    now = datetime.now().isoformat()
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO album_meta (album_id, title, author, cover_url, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(album_id) DO UPDATE SET "
            "title=COALESCE(NULLIF(?, ''), title), "
            "author=COALESCE(NULLIF(?, ''), author), "
            "cover_url=COALESCE(NULLIF(?, ''), cover_url), "
            "updated_at=?",
            (album_id, title or "", author or "", cover_url or "", now,
             title or "", author or "", cover_url or "", now),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def get_album_meta(album_id: str) -> dict | None:
    """获取缓存的专辑元数据"""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM album_meta WHERE album_id=?", (album_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ─── Album Detail Cache ───

_DETAIL_CACHE_TTL = 3600  # 缓存有效期 1 小时


def get_cached_album_detail(album_id: str, ttl: int = 3600) -> dict | None:
    """从 SQLite 读取持久化的专辑详情缓存，过期返回 None。"""
    cutoff_ts = datetime.now().timestamp() - ttl  # Unix 时间戳（秒）
    cutoff_iso = datetime.fromtimestamp(cutoff_ts).isoformat()  # ISO 字符串，与 cached_at 列类型一致
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT detail_json, cover_cdn_url, cached_at FROM album_detail_cache WHERE album_id=? AND cached_at > ?",
            (album_id, cutoff_iso),
        ).fetchone()
        if not row:
            return None
        try:
            cached_at = datetime.fromisoformat(row["cached_at"])
            detail = json.loads(row["detail_json"])
            if not isinstance(detail, dict):
                return None
        except (ValueError, TypeError):
            return None
        if (datetime.now() - cached_at).total_seconds() > ttl:
            return None
        return {
            "detail": detail,
            "cached_at": cached_at.timestamp(),
            "cover_cdn_url": row["cover_cdn_url"] or "",
        }
    finally:
        conn.close()


def clear_cached_album_details():
    """Clear only derived detail-cache data, never jobs, settings or bookmarks."""
    conn = get_db()
    try:
        with conn:
            conn.execute("DELETE FROM album_detail_cache")
    finally:
        conn.close()


def set_cached_album_detail(album_id: str, detail_json: str, cover_cdn_url: str = ""):
    """写入专辑详情缓存。"""
    conn = get_db()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO album_detail_cache (album_id, detail_json, cover_cdn_url, cached_at) VALUES (?, ?, ?, ?)",
            (album_id, detail_json, cover_cdn_url, datetime.now().isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


# ─── 存量回填 ───


def backfill_missing_meta() -> int:
    """为已完成任务中缺少 album_meta 的条目回填元数据。"""
    conn = get_db()
    try:
        # 先检查是否有缺失，没有则直接跳过
        missing_count = conn.execute("""
            SELECT COUNT(*)
            FROM jobs j
            LEFT JOIN album_meta m ON j.album_id = m.album_id
            WHERE j.status = 'completed' AND m.album_id IS NULL
        """).fetchone()[0]
        if missing_count == 0:
            return 0

        rows = conn.execute("""
            SELECT DISTINCT j.album_id, j.title
            FROM jobs j
            LEFT JOIN album_meta m ON j.album_id = m.album_id
            WHERE j.status = 'completed' AND m.album_id IS NULL
        """).fetchall()
        count = 0
        for r in rows:
            title = r["title"] or r["album_id"]
            conn.execute(
                "INSERT OR IGNORE INTO album_meta (album_id, title, author, cover_url, updated_at) VALUES (?, ?, '', '', ?)",
                (r["album_id"], title, datetime.now().isoformat()),
            )
            count += 1
        conn.commit()
        return count
    finally:
        conn.close()
