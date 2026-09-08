"""
SQLite 数据库操作模块
"""
import json
import sqlite3
from datetime import datetime
from pathlib import Path

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
                completed_at TEXT
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
    """获取指定 album_id 的已完成任务（取最新的一个），避免全表扫描。"""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM jobs WHERE album_id=? AND status='completed' ORDER BY created_at DESC LIMIT 1",
            (album_id,),
        ).fetchone()
        return dict(row) if row else None
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


def get_all_wishlist(
    page: int = 1, page_size: int = 50,
    keyword: str = "", sort: str = "added_at",
) -> dict:
    """获取收藏列表（分页），支持搜索关键词和排序，返回 {items, total, page, page_size}"""
    conn = get_db()
    try:
        # 排序映射（白名单防注入）
        sort_map = {
            "title": "ORDER BY title",
            "author": "ORDER BY author",
            "added_at": "ORDER BY added_at DESC",
        }
        order_clause = sort_map.get(sort, "ORDER BY added_at DESC")

        if keyword:
            # 转义 LIKE 通配符 _ 和 %，防止匹配过多
            safe_kw = keyword.replace("_", "\\_").replace("%", "\\%")
            like = f"%{safe_kw}%"
            where_clause = "WHERE title LIKE ? ESCAPE '\\' OR author LIKE ? ESCAPE '\\' OR album_id LIKE ? ESCAPE '\\'"
            count_row = conn.execute(
                f"SELECT COUNT(*) FROM wishlist {where_clause}",
                (like, like, like),
            ).fetchone()
            total = count_row[0]
            offset = (page - 1) * page_size
            rows = conn.execute(
                f"SELECT * FROM wishlist {where_clause} {order_clause} LIMIT ? OFFSET ?",
                (like, like, like, page_size, offset),
            ).fetchall()
        else:
            total = conn.execute("SELECT COUNT(*) FROM wishlist").fetchone()[0]
            offset = (page - 1) * page_size
            rows = conn.execute(
                f"SELECT * FROM wishlist {order_clause} LIMIT ? OFFSET ?",
                (page_size, offset),
            ).fetchall()

        items = [dict(r) for r in rows]
        return {"items": items, "total": total, "page": page, "page_size": page_size}
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


def _make_status_sql(status: str | None) -> str:
    """生成状态过滤的 WHERE 子句"""
    if status == "completed" or status == "downloaded":
        return "AND ai.download_status = ?"
    elif status == "queued":
        return "AND ai.download_status = ?"
    elif status == "none" or status == "undownloaded":
        return "AND ai.download_status IN (?, ?)"
    return ""


def get_library(
    page: int = 1,
    page_size: int = 50,
    tag: str | None = None,
    keyword: str = "",
    status: str | None = None,
    sort: str = "updated_at",
    album_id: str | None = None,
) -> dict:
    """获取资源库列表 — SQL 分页下沉版。

    数据源 = wishlist（收藏）UNION completed jobs（已下载但未收藏）。
    SQL 做合并+过滤+排序+分页，Python 只做标签绑定和文件检查。
    album_id 传入时按精确匹配过滤（供 /api/library/<album_id> 单条查询使用，
    避免 keyword LIKE 模糊匹配命中其他条目）。

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
        else:  # updated_at (default)
            order_clause = (
                "ORDER BY COALESCE(NULLIF(_base.updated_at, ''), _base.added_at, '') DESC, _base.album_id ASC"
            )

        # ── 完整查询 SQL ──
        query_sql = f"""
            WITH wishlist_ext AS (
                SELECT
                    w.album_id,
                    w.title,
                    w.author,
                    w.cover_url,
                    CASE WHEN jc.album_id IS NOT NULL THEN 'completed' ELSE w.download_status END AS download_status,
                    w.added_at,
                    '' AS updated_at,
                    1 AS is_wishlisted
                FROM wishlist w
                LEFT JOIN (
                    SELECT DISTINCT album_id FROM jobs WHERE status='completed'
                ) jc ON w.album_id = jc.album_id
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
            _base AS (
                SELECT ai.* FROM all_items ai
                WHERE 1=1
                  {_make_kw_combined_sql(kw)}
                  {_make_status_sql(status)}
                  {tag_having_sql}
                  {"AND ai.album_id = ?" if album_id else ""}
            )
            SELECT _base.* FROM _base
            {order_clause}
            LIMIT ? OFFSET ?
        """

        # ── 参数列表（必须与 SQL 中 ? 顺序一致）──
        params = []

        if kw:
            like_val = f"%{_escape_like(kw)}%"
            esc = chr(92)  # 反斜杠作为 LIKE 转义字符
            # 8 个 ?: title LIKE, ESCAPE, author LIKE, ESCAPE, album_id LIKE, ESCAPE, tag LIKE, ESCAPE
            params.extend([like_val, esc, like_val, esc, like_val, esc, like_val, esc])

        if status == "completed" or status == "downloaded":
            params.append("completed")
        elif status == "queued":
            params.append("queued")
        elif status == "none" or status == "undownloaded":
            params.extend(["none", "none"])

        if wanted_tags:
            params.extend(wanted_tags)
            params.append(tag_count)

        if album_id:
            params.append(album_id)

        params.append(page_size)
        params.append(offset)

        # ── COUNT 查询（复用 WHERE/HAVING, 不 ORDER/LIMIT）──
        count_sql = (
            query_sql
            .replace("SELECT _base.* FROM _base", "SELECT COUNT(*) FROM _base")
            .split("ORDER BY")[0]
        )
        total_row = conn.execute(count_sql, params[:-2]).fetchone()
        total = total_row[0] if total_row else 0

        # ── 分页查询 ──
        page_rows = conn.execute(query_sql, params).fetchall() if total > 0 else []

        if not page_rows:
            return {"items": [], "total": total, "page": page, "page_size": page_size}

        items_list = [dict(r) for r in page_rows]

        # ── Phase 2: 标签绑定 + 文件存在性检查 ──
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

        # One query for the current page instead of one connection/SELECT per album.
        completed_ids = [item["album_id"] for item in items_list if item["download_status"] == "completed"]
        paths = {}
        if completed_ids:
            ph = ",".join("?" for _ in completed_ids)
            rows = conn.execute(
                f"""SELECT j.album_id, j.output_path FROM jobs j
                    WHERE j.album_id IN ({ph}) AND j.status='completed'
                    AND j.id = (SELECT latest.id FROM jobs latest
                        WHERE latest.album_id=j.album_id AND latest.status='completed'
                        ORDER BY latest.created_at DESC, latest.id DESC LIMIT 1)""",
                completed_ids,
            ).fetchall()
            paths = {row["album_id"]: row["output_path"] for row in rows}
        result_items = []
        for it in items_list:
            aid = it["album_id"]
            it["tags"] = tags_by_album.get(aid, [])
            if it["download_status"] == "completed":
                path = paths.get(aid)
                it["file_exists"] = bool(path and Path(path).is_dir())
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
