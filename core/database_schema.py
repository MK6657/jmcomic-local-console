"""Trusted SQLite schema shared by startup, migrations and restoration.

This module contains SQL and bounded read-only validation; it never opens the
application database, runs migrations, loads settings or contacts the network.
"""
from functools import lru_cache
import re
import sqlite3

CURRENT_SCHEMA_VERSION = 5

SUPERSEDED_COLUMN = "superseded_at"
QUEUED_COLUMN = "queued_at"

BASE_SCHEMA_SQL = r"""
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

            -- 检查新章节（core/update_store.py）：每部漫画一行基线 + 最近一次检查的结论。
            -- 只有检查和下载任务的两个记录点写这两张表；检查从不建下载任务、不碰文件。
            CREATE TABLE IF NOT EXISTS album_update_checks (
                album_id        TEXT PRIMARY KEY,
                baseline_ids    TEXT,                        -- 已知章节 id 的 JSON 列表（上游顺序，下载完成时并入）；NULL = 还没有基线
                baseline_source TEXT,                        -- 'download' | 'first_check'
                baseline_at     TEXT,
                gone_ids        TEXT NOT NULL DEFAULT '[]',  -- 已知章节在上游不见了、且已经确认过的
                removed_ids     TEXT NOT NULL DEFAULT '[]',  -- 发现新章节的那次检查同时不见了的已知章节（'changed'）
                upstream_count  INTEGER,
                new_chapters    TEXT NOT NULL DEFAULT '[]',  -- JSON [{photo_id, index, title, confirmed_at}]，上游顺序
                new_count       INTEGER NOT NULL DEFAULT 0,  -- = len(new_chapters)
                result          TEXT,                        -- 最近一次成功检查的结论：baseline / no_update / new / changed；NULL = 从未成功
                last_success_at TEXT,
                last_attempt_at TEXT,                        -- 发请求之前写（租约）
                last_trigger    TEXT,                        -- 'auto' | 'manual'
                error_kind      TEXT,                        -- 成功后为 NULL；network / timeout / not_found / upstream_error
                error_detail    TEXT,                        -- 只记异常类名，不记网址、代理
                fail_count      INTEGER NOT NULL DEFAULT 0,  -- 连续失败次数
                next_check_at   TEXT,                        -- 最早的下一次自动检查
                created_at      TEXT,
                updated_at      TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_update_checks_next ON album_update_checks(next_check_at);
            CREATE INDEX IF NOT EXISTS idx_update_checks_new ON album_update_checks(new_count);

            CREATE TABLE IF NOT EXISTS update_check_log (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                album_id       TEXT NOT NULL,
                trigger        TEXT NOT NULL,
                started_at     TEXT NOT NULL,
                finished_at    TEXT,
                outcome        TEXT NOT NULL,
                error_kind     TEXT,
                error_type     TEXT,
                requests       INTEGER NOT NULL DEFAULT 0,
                upstream_count INTEGER,
                new_count      INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_update_check_log_started ON update_check_log(started_at);
        """

RESOURCE_TABLE_SQL = r"""CREATE TABLE IF NOT EXISTS resource_catalog (
            job_id TEXT PRIMARY KEY,
            job_order INTEGER NOT NULL,
            album_id TEXT NOT NULL,
            title TEXT,
            selected_photo_ids TEXT,
            output_path TEXT,
            created_at TEXT,
            updated_at TEXT,
            completed_at TEXT,
            superseded_at TEXT
        )"""

RESOURCE_INDEX_SQL = "CREATE INDEX IF NOT EXISTS idx_resource_catalog_album_latest ON resource_catalog(album_id, created_at DESC, job_order DESC)"
QUEUE_INDEX_SQL = "CREATE INDEX IF NOT EXISTS idx_jobs_queue ON jobs(status, queued_at, id)"

PUBLICATION_DDL = {
    'task_page_receipts': """CREATE TABLE task_page_receipts (job_id TEXT NOT NULL, page TEXT NOT NULL,
        digest TEXT NOT NULL, PRIMARY KEY(job_id, page))""",
    'published_tasks': """CREATE TABLE published_tasks (
        job_id TEXT PRIMARY KEY, album_id TEXT NOT NULL, job_order INTEGER NOT NULL,
        snapshot TEXT NOT NULL, manifest TEXT NOT NULL)""",
    'idx_published_album': 'CREATE INDEX idx_published_album ON published_tasks(album_id, job_order)',
    'album_maintenance': """CREATE TABLE album_maintenance (
        album_id TEXT PRIMARY KEY, state TEXT NOT NULL, payload TEXT NOT NULL,
        error TEXT NOT NULL DEFAULT '', generation INTEGER NOT NULL DEFAULT 1)""",
}


def catalog_triggers() -> dict[str, str]:
    columns = ("job_id", "album_id", "title", "selected_photo_ids", "output_path", "created_at",
               "updated_at", "completed_at", "superseded_at")
    names = ", ".join(columns)
    values = ", ".join(f"NEW.{name}" for name in columns)
    assignments = ", ".join(f"{name}=excluded.{name}" for name in columns if name != "job_id")
    result = {}
    for event in ("INSERT", "UPDATE"):
        name = f"catalog_completed_job_{event.lower()}"
        result[name] = f"""CREATE TRIGGER {name}
            AFTER {event} ON jobs WHEN NEW.status = 'completed'
            BEGIN
                INSERT INTO resource_catalog (job_order, {names}) VALUES (NEW.id, {values})
                ON CONFLICT(job_id) DO UPDATE SET job_order=excluded.job_order, {assignments};
            END"""
    result["catalog_revoked_completion"] = """CREATE TRIGGER catalog_revoked_completion
        AFTER UPDATE OF status ON jobs WHEN OLD.status = 'completed' AND NEW.status != 'completed'
        BEGIN
            DELETE FROM resource_catalog WHERE job_id = NEW.job_id;
        END"""
    return result


class SchemaError(RuntimeError):
    """The database is readable but outside the application's known contract."""


# Normalize formatting, unambiguous identifier quoting/ASCII case and comments,
# but keep literal contents: 'completed' and 'COMPLETED' differ in triggers.
_SQL_TOKEN = re.compile(
    r"\s+|--[^\n\r]*(?:\r?\n|$)|/\*[\s\S]*?\*/|"
    r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|`(?:[^`]|``)*`|\[[^\]]*\]|"
    r"[\w$]+|[^\s]", re.UNICODE)
_INTERNAL_TABLES = {
    "sqlite_sequence": "CREATE TABLE sqlite_sequence(name,seq)",
    "sqlite_stat1": "CREATE TABLE sqlite_stat1(tbl,idx,stat)",
    "sqlite_stat4": "CREATE TABLE sqlite_stat4(tbl,idx,neq,nlt,ndlt,sample)",
}
_MAX_SCHEMA_OBJECTS = 128
_MAX_OBJECT_SQL = 64 * 1024
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
# Quoting a grammar/type word can change its role, e.g. TEXT "NOT" NULL is
# a nullable custom type, not TEXT NOT NULL. Only unambiguous identifiers may
# lose quotes. SQLite identifier case-insensitivity is ASCII-only.
_SQL_WORDS = frozenset("""create table if not exists integer primary key autoincrement
    text unique null default index on desc trigger after insert update when begin
    into values conflict do set end delete from where and of""".split())


def _tokens(sql):
    if not isinstance(sql, str) or len(sql) > _MAX_OBJECT_SQL:
        raise SchemaError("数据库结构定义过大或缺失")
    result = []
    for match in _SQL_TOKEN.finditer(sql):
        token = match.group()
        if token.isspace() or token.startswith(("--", "/*")):
            continue
        if token.startswith("'"):
            result.append(token)
        elif token[0] in ('"', '`', '['):
            value = token[1:-1]
            if token[0] != '[':
                value = value.replace(token[0] * 2, token[0])
            normalized = value.translate(_ASCII_LOWER)
            result.append(normalized if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", value)
                          and normalized not in _SQL_WORDS else token)
        else:
            result.append(token.translate(_ASCII_LOWER))
    if result[-1:] == [';']:
        result.pop()
    return tuple(result)


def _table_parts(tokens):
    """Split our known CREATE TABLE syntax at top-level column commas only."""
    try:
        opening = tokens.index('(')
    except ValueError:
        raise SchemaError("数据库表定义不受支持") from None
    fields, current, depth = [], [], 0
    for position in range(opening + 1, len(tokens)):
        token = tokens[position]
        if token == ')' and depth == 0:
            fields.append(tuple(current))
            return tokens[:opening], fields, tokens[position:]
        if token == ',' and depth == 0:
            fields.append(tuple(current))
            current = []
            continue
        depth += int(token == '(') - int(token == ')')
        current.append(token)
    raise SchemaError("数据库表定义不完整")


@lru_cache(maxsize=1)
def _expected_objects():
    """Compile our DDL in memory; never open an instance or run migrations."""
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(BASE_SCHEMA_SQL)
        connection.execute(RESOURCE_TABLE_SQL)
        connection.execute(RESOURCE_INDEX_SQL)
        connection.execute(f"ALTER TABLE jobs ADD COLUMN {QUEUED_COLUMN} TEXT")
        connection.execute(QUEUE_INDEX_SQL)
        for ddl in PUBLICATION_DDL.values():
            connection.execute(ddl)
        for statement in catalog_triggers().values():
            connection.execute(statement)
        return {(kind, name): _tokens(sql) for kind, name, sql in connection.execute(
            "SELECT type,name,sql FROM sqlite_schema WHERE sql IS NOT NULL")
                if name not in _INTERNAL_TABLES}
    finally:
        connection.close()


def validate_schema(connection, version):
    """Read-only contract check before any application or restore writes.

    V4 and later require every table, index and trigger. Historical versions may omit
    tables/indexes that startup added independently of user_version. Each
    existing definition must still be known; a pre-v3 catalog is never guessed.
    """
    if type(version) is not int or not 0 <= version <= CURRENT_SCHEMA_VERSION:
        raise SchemaError("数据库版本不受支持")
    expected = _expected_objects()
    if version < 5:
        expected = {key: value for key, value in expected.items() if key[1] not in PUBLICATION_DDL}
    rows = connection.execute(
        "SELECT type,name,sql FROM sqlite_schema WHERE sql IS NOT NULL LIMIT ?",
        (_MAX_SCHEMA_OBJECTS + 1,),
    ).fetchall()
    if len(rows) > _MAX_SCHEMA_OBJECTS:
        raise SchemaError("数据库结构对象过多")
    actual, internal = {}, set()
    has_queued_at = False
    for kind, name, sql in rows:
        if kind == "table" and name in _INTERNAL_TABLES:
            # ANALYZE is supported, but these are still executable schema.
            # Do not let a forged internal table hide constraints or defaults.
            if _tokens(sql) != _tokens(_INTERNAL_TABLES[name]):
                raise SchemaError(f"数据库内部表定义不匹配：{name}")
            internal.add(name)
            continue
        key = (kind, name)
        if key not in expected:
            raise SchemaError(f"不支持的数据库结构对象：{name}")
        tokens, target = _tokens(sql), expected[key]
        if kind == "table" and name == "jobs" and version < 4:
            header, fields, suffix = _table_parts(tokens)
            target_header, target_fields, target_suffix = _table_parts(target)
            has_queued_at = fields == target_fields
            supported_fields = [target_fields, target_fields[:-1]]
            if version < 3:
                supported_fields.append(target_fields[:-2])
            matches = (header, suffix) == (target_header, target_suffix) and fields in supported_fields
        else:
            matches = tokens == target
        if not matches:
            raise SchemaError(f"数据库结构定义不匹配：{name}")
        actual[key] = sql
    tables = {name for kind, name in actual if kind == "table"}
    required = {"jobs", "settings"}
    if version >= 3:
        required.add("resource_catalog")
    elif "resource_catalog" in tables:
        raise SchemaError("数据库资源目录与版本不一致")
    if not required <= tables:
        raise SchemaError("数据库关键表缺失：" + ", ".join(sorted(required - tables)))
    if "sqlite_sequence" not in internal:
        raise SchemaError("数据库自增序列表缺失")
    wanted_triggers = {("trigger", name) for name in catalog_triggers()} if version >= 3 else set()
    if {key for key in actual if key[0] == "trigger"} != wanted_triggers:
        raise SchemaError("数据库资源目录触发器缺失或版本不一致")
    if version >= 3 and ("index", "idx_resource_catalog_album_latest") not in actual:
        raise SchemaError("数据库资源目录索引缺失")
    if version < 4 and ("index", "idx_jobs_queue") in actual and not has_queued_at:
        raise SchemaError("数据库任务队列索引与列不一致")
    if version >= 4 and actual.keys() != expected.keys():
        missing = sorted(name for _, name in expected.keys() - actual.keys())
        raise SchemaError("数据库结构对象缺失：" + ", ".join(missing))
    return {name: sql for (kind, name), sql in actual.items() if kind == "trigger"}
